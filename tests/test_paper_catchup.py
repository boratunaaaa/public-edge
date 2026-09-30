"""A catch-up ledger run (after an outage) must produce exactly the trades the daily runs would have: an earlier
day's exit releases cash before a later day's BUY is sized. Runs never overlap."""
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text

from app.marketdata.provider import Bar
from tests.test_alpha_validation import _event, _serve, offline_prices  # noqa: F401 - fixture
from tests.test_db import URL

db = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")
FRI, TUE, WED, THU = date(2024, 3, 1), date(2024, 3, 5), date(2024, 3, 6), date(2024, 3, 7)


def _series(start, n, price=10.0, step=0.01, volume=300_000):
    out, d, p = [], start, price
    while len(out) < n:
        if d.weekday() < 5:
            out.append(Bar(d, p, p * 1.01, p * 0.99, p, p, volume))
            p += step
        d += timedelta(days=1)
    return out


def _setup(engine, monkeypatch):
    """Friday: a 90 % position in AAA; it is due to exit on Tuesday; a 50 % BUY of BBB is due on Wednesday."""
    from app.db.models import paper_orders, paper_positions, proposed_trades
    from app.paper.ledger import ensure_accounts, run
    _serve(monkeypatch, AAA=_series(date(2024, 1, 2), 60, 10.0, 0.05), BBB=_series(date(2024, 1, 2), 60, 20.0, 0.02),
           SPY=_series(date(2024, 1, 2), 60, 400.0, 0.5))
    with engine.begin() as c:
        aid = ensure_accounts(c, book="RESEARCH")["B_90D"]
        c.execute(text("UPDATE paper_accounts SET active = (id = :a)"), {"a": aid})  # one book keeps the test readable
        pids = {}
        for tk, cik in (("AAA", "0000000101"), ("BBB", "0000000102")):
            eid = _event(c, ticker=tk, cik=cik, key=f"e{tk}", d=date(2024, 2, 27))
            pids[tk] = c.execute(proposed_trades.insert().values(
                code=f"PECU{tk}", ticker=tk, cik=cik, event_id=eid, signal_family="INSIDER_BUY",
                event_type="INSIDER_OPEN_MARKET_BUY", proposal_created_at=datetime(2024, 2, 28, tzinfo=timezone.utc),
                reference_date=date(2024, 2, 28), entry_reference_price=10.0, suggested_position_pct=0.03,
                max_position_pct=0.05, stop_reference=8.5, thesis="t", risks=[], reasons=[], data_quality="OK", gate={},
                approval_status="APPROVED", expires_at=datetime(2024, 3, 2, tzinfo=timezone.utc), research_only=True,
            ).returning(proposed_trades.c.id)).scalar_one()
        c.execute(paper_orders.insert().values(account_id=aid, proposal_id=pids["AAA"], ticker="AAA", side="BUY",
                                               requested_at=datetime(2024, 2, 28, tzinfo=timezone.utc), not_before=FRI,
                                               target_pct=0.9, status="PENDING"))
    assert run(engine, today=FRI)["fills"]["filled"] == 1
    with engine.begin() as c:
        c.execute(paper_positions.update().values(planned_exit_date=TUE))
        c.execute(paper_orders.insert().values(account_id=aid, proposal_id=pids["BBB"], ticker="BBB", side="BUY",
                                               requested_at=datetime(2024, 3, 1, tzinfo=timezone.utc), not_before=WED,
                                               target_pct=0.5, status="PENDING"))
    return aid


def _result(engine, aid):
    from app.db.models import paper_orders, paper_performance, paper_trades
    from app.paper.ledger import cash_of
    with engine.connect() as c:
        trades = [tuple(r) for r in c.execute(select(
            paper_trades.c.ticker, paper_trades.c.side, paper_trades.c.trade_date, paper_trades.c.price,
            paper_trades.c.shares, paper_trades.c.gross).order_by(paper_trades.c.trade_date, paper_trades.c.side))]
        orders = [tuple(r) for r in c.execute(select(paper_orders.c.ticker, paper_orders.c.side, paper_orders.c.status,
                                                     paper_orders.c.fill_date).order_by(paper_orders.c.id))]
        perf = [tuple(r) for r in c.execute(select(paper_performance.c.on_date, paper_performance.c.nav,
                                                   paper_performance.c.cash).where(paper_performance.c.account_id == aid)
                                            .order_by(paper_performance.c.on_date))]
        return {"trades": trades, "orders": orders, "cash": round(cash_of(c, aid), 6), "perf": perf}


def _truncate(engine):
    from app.db.models import metadata
    with engine.begin() as c:
        c.execute(text("TRUNCATE " + ", ".join(t.name for t in metadata.sorted_tables) + " RESTART IDENTITY CASCADE"))


@db
@pytest.mark.usefixtures("clean", "offline_prices")
def test_a_catch_up_run_equals_the_daily_runs_through_exits_and_sizing(engine, monkeypatch):
    from app.paper.ledger import run
    aid = _setup(engine, monkeypatch)
    for d in (date(2024, 3, 2), date(2024, 3, 3), date(2024, 3, 4), TUE, WED, THU):  # the scheduler runs every day
        run(engine, today=d)
    daily = _result(engine, aid)
    _truncate(engine)
    aid = _setup(engine, monkeypatch)
    out = run(engine, today=THU)  # the outage: nothing ran Saturday..Wednesday
    catch_up = _result(engine, aid)
    assert out["days"] == [str(date(2024, 3, 2) + timedelta(days=i)) for i in range(6)]
    # Tuesday's exit released the cash before Wednesday's BUY was sized - as in the daily runs
    sell = next(t for t in daily["trades"] if t[1] == "SELL")
    buy = next(t for t in daily["trades"] if t[0] == "BBB")
    assert sell[2] == TUE and buy[2] == WED and buy[5] > 4000  # ~50 % of NAV, not the ~10 % left before the exit
    assert catch_up == daily


@db
@pytest.mark.usefixtures("clean", "offline_prices")
def test_the_daily_run_is_unchanged_when_nothing_was_missed(engine, monkeypatch):
    from app.paper.ledger import run
    _setup(engine, monkeypatch)
    out = run(engine, today=date(2024, 3, 2))
    assert out["days"] == ["2024-03-02"] and "catch_up" not in out
    assert set(out) >= {"expired", "fills", "exits", "fills_after_exit", "snapshots"}


def test_ledger_days():
    from app.paper.ledger import MAX_CATCHUP_DAYS, ledger_days
    assert ledger_days(None, THU) == ([THU], 0)
    assert ledger_days(THU, THU) == ([THU], 0)  # a re-run the same day
    assert ledger_days(WED, THU) == ([THU], 0)  # the normal daily case
    assert ledger_days(FRI, THU) == ([FRI + timedelta(days=i) for i in range(1, 7)], 0)
    days, left = ledger_days(THU - timedelta(days=MAX_CATCHUP_DAYS + 5), THU)
    assert len(days) == MAX_CATCHUP_DAYS and left == 5 and days[0] == THU - timedelta(days=MAX_CATCHUP_DAYS + 4)


@db
@pytest.mark.usefixtures("clean", "offline_prices")
def test_ledger_runs_never_overlap(engine, monkeypatch):
    from app.paper.ledger import RUN_LOCK, run
    _setup(engine, monkeypatch)
    with engine.connect() as holder:
        assert holder.execute(text("SELECT pg_try_advisory_lock(hashtextextended(:k, 0))"), {"k": RUN_LOCK}).scalar()
        holder.commit()
        try:
            assert run(engine, today=date(2024, 3, 2)) == {"skipped": "another paper ledger run is in progress"}
        finally:
            holder.execute(text("SELECT pg_advisory_unlock(hashtextextended(:k, 0))"), {"k": RUN_LOCK})
            holder.commit()
    assert run(engine, today=date(2024, 3, 2))["days"] == ["2024-03-02"]  # released: the next run proceeds
