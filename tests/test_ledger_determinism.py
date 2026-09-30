"""Deterministic ledger order: two identical runs must write identical rows. PostgreSQL returns rows without ORDER BY
in physical order, which changes after an UPDATE - exit orders (and every float sum over positions) must not depend
on it (found by the 2023-H1 replay pilot: two runs swapped two same-day exit order ids)."""
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.marketdata.provider import Bar
from tests.test_alpha_validation import _serve, offline_prices  # noqa: F401 - fixture
from tests.test_db import URL

db = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")


def _series(start, n, price):
    out, d, p = [], start, price
    while len(out) < n:
        if d.weekday() < 5:
            out.append(Bar(d, p, p * 1.01, p * 0.99, p, p, 300_000))
            p += 0.01
        d += timedelta(days=1)
    return out


@db
@pytest.mark.usefixtures("clean", "offline_prices")
def test_exit_orders_follow_position_ids_whatever_the_physical_row_order(engine, monkeypatch):
    from app.db.models import paper_orders, paper_positions, paper_trades
    from app.paper.ledger import check_exits, ensure_accounts
    _serve(monkeypatch, AAA=_series(date(2024, 1, 2), 60, 10.0), BBB=_series(date(2024, 1, 2), 60, 20.0))
    with engine.begin() as c:
        aid = ensure_accounts(c, book="RESEARCH")["B_90D"]
        ids = []
        for tk, px in (("AAA", 10.0), ("BBB", 20.0)):
            pid = c.execute(paper_positions.insert().values(
                account_id=aid, ticker=tk, shares=10, entry_date=date(2024, 1, 3), entry_price=px, cost=px * 10,
                adj_factor=1.0, stop_price=px * 0.5, planned_exit_date=date(2024, 2, 1), status="OPEN", last_price=px,
                last_price_date=date(2024, 1, 3)).returning(paper_positions.c.id)).scalar_one()
            oid = c.execute(paper_orders.insert().values(
                account_id=aid, ticker=tk, side="BUY", requested_at=datetime(2024, 1, 2, tzinfo=timezone.utc),
                not_before=date(2024, 1, 3), target_pct=0.03, status="FILLED", fill_date=date(2024, 1, 3), fill_price=px,
                shares=10, position_id=pid).returning(paper_orders.c.id)).scalar_one()
            c.execute(paper_trades.insert().values(account_id=aid, order_id=oid, position_id=pid, ticker=tk, side="BUY",
                                                   trade_date=date(2024, 1, 3), price=px, shares=10, gross=px * 10,
                                                   commission=1, slippage_bps=0))
            ids.append(pid)
        # an UPDATE usually moves the first position's tuple behind the second (not guaranteed - hence the SQL check below)
        c.execute(paper_positions.update().where(paper_positions.c.id == ids[0]).values(last_price=10.5))
        seen: list[str] = []

        def capture(conn, cursor, statement, *args):
            if "FROM paper_positions" in statement and "status" in statement:
                seen.append(statement)
        from sqlalchemy import event
        event.listen(engine, "before_cursor_execute", capture)
        try:
            assert check_exits(c, date(2024, 2, 5))["exit_orders"] == 2
        finally:
            event.remove(engine, "before_cursor_execute", capture)
    # deterministic: the read of open positions itself is ordered by id, whatever PostgreSQL's physical order
    assert seen and all("ORDER BY paper_positions.id" in s for s in seen)
    with engine.connect() as c:
        sells = c.execute(select(paper_orders.c.position_id).where(paper_orders.c.side == "SELL")
                          .order_by(paper_orders.c.id)).scalars().all()
    assert sells == ids  # exit orders created in position id order


def test_every_multi_row_position_read_is_ordered():
    """nav, snapshot, metrics and exits sum floats over positions: each read has an explicit ORDER BY."""
    import inspect

    from app.paper import ledger
    from app.replay import metrics
    for fn in (ledger.check_exits, ledger.nav_of, ledger.snapshot, ledger.account_metrics):
        src = inspect.getsource(fn)
        assert src.count("select(paper_positions") <= src.count("order_by(paper_positions.c.id)"), fn.__name__
    assert "order_by(paper_positions.c.id)" in inspect.getsource(metrics)
    assert "order_by(paper_trades.c.id)" in inspect.getsource(metrics)
