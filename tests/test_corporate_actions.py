"""Corporate-action EXIT resolution: verified merger consideration values a position still open at the close;
it never fabricates the predecessor's pre-merger prices."""
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text

from app.backtest.engine import compute_returns
from app.corpactions import CorporateExit, exit_value
from app.marketdata.provider import Bar
from tests.test_db import URL
from tests.test_free_prices import OLD, _company, _unresolved_event, series

db = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")
EFF = date(2023, 3, 17)


def test_stock_cash_and_mixed_consideration_values():
    succ = [Bar(EFF - timedelta(days=1), 40, 40, 40, 40, 40, 1), Bar(EFF + timedelta(days=3), 50, 51, 49, 50, 50, 1)]
    base = {"id": 1, "effective_date": EFF}
    stock = exit_value(base | {"share_ratio": 0.5, "cash_component": None}, succ)
    cash = exit_value(base | {"share_ratio": None, "cash_component": 30}, None)
    mixed = exit_value(base | {"share_ratio": 0.5, "cash_component": 10}, succ)
    assert (stock.value, stock.basis, stock.exit_date) == (25.0, "merger_stock", EFF + timedelta(days=3))  # first session AFTER close
    assert (cash.value, cash.basis) == (30.0, "merger_cash")
    assert mixed.value == 35.0 and mixed.basis == "merger_mixed"
    assert exit_value(base | {"share_ratio": 0.5, "cash_component": None}, []) is None  # stock leg unpriced: never guessed


def test_horizon_across_the_close_exits_at_the_consideration():
    bars = series(date(2022, 6, 1), 200, 20.0, 0.0)  # predecessor trades until ~2023-03-07, then the merger closes
    ce = CorporateExit(EFF, EFF + timedelta(days=3), 30.0, "merger_cash", 1)
    pub = datetime(2023, 1, 10, 21, tzinfo=timezone.utc)
    res = {r.horizon_days: r for r in compute_returns(bars, None, date(2023, 1, 10), pub, as_of=date(2024, 6, 1), corporate_exit=ce)}
    assert res[30].exit_basis == "horizon"  # ends before the close: ordinary price exit
    assert res[180].exit_basis == "merger_cash" and res[180].exit_price == pytest.approx(30.0)
    assert res[180].ret == pytest.approx(0.5)
    late = compute_returns(bars, None, date(2023, 4, 1), None, as_of=date(2024, 6, 1), corporate_exit=ce)
    assert all(r.exit_basis != "merger_cash" for r in late)  # no entry after the security ceased to exist


def _resolution(c, **kw):
    vals = dict(security_id=f"CIK:{OLD}", action_type="STOCK_MERGER", effective_date=EFF, predecessor_cik=OLD,
                successor_cik="0000000888", predecessor_ticker="OLDB", successor_ticker="NEWB", share_ratio=0.5,
                cash_component=None, validation_status="CONFIRMED", complexity="SIMPLE")
    vals.update(kw)
    c.execute(text("""INSERT INTO corporate_action_resolution (security_id, action_type, effective_date, predecessor_cik,
                      successor_cik, predecessor_ticker, successor_ticker, share_ratio, cash_component, validation_status, complexity)
                      VALUES (:security_id, :action_type, :effective_date, :predecessor_cik, :successor_cik, :predecessor_ticker,
                      :successor_ticker, :share_ratio, :cash_component, :validation_status, :complexity)"""), vals)


@pytest.fixture
def offline(monkeypatch):
    import app.marketdata.provider as mp

    class NoFetch:
        def get_historical_prices(self, *a):
            return []
    monkeypatch.setattr(mp, "get_provider", lambda: NoFetch())


@db
@pytest.mark.usefixtures("clean", "offline")
@pytest.mark.parametrize("status,complexity,expected", [("CONFIRMED", "SIMPLE", "merger_stock"),
                                                        ("CONFIRMED", "COMPLEX_CORPORATE_ACTION", None),
                                                        ("INSUFFICIENT", "SIMPLE", None), ("CONFLICT", "SIMPLE", None)])
def test_only_confirmed_simple_resolutions_value_the_exit(engine, status, complexity, expected):
    from app.backtest.jobs import compute_pending
    from app.db.models import event_returns
    from app.marketdata.provider import store_bars
    from app.pricesources.store import save_source
    with engine.begin() as c:
        _company(c, reason_marker="MERGER_PROXY")
        eid = _unresolved_event(c)
        store_bars(c, "SPY", series(date(2022, 12, 1), 600, 380.0, 0.2))
        store_bars(c, "NEWB", series(EFF - timedelta(days=30), 60, 50.0, 0.0))
        save_source(c, cik=OLD, symbol="OLDB", provider="finsaber", bars=series(date(2022, 6, 1), 205, 20.0, 0.0),
                    identity="cik", source_ref="t")
        _resolution(c, validation_status=status, complexity=complexity)
    compute_pending(engine, backfill=None)
    with engine.connect() as c:
        r = c.execute(select(event_returns).where(event_returns.c.event_id == eid, event_returns.c.horizon_days == 180)).mappings().first()
    if expected:
        assert r["exit_basis"] == expected and r["exit_price"] == pytest.approx(25.0) and r["ret"] == pytest.approx(0.25)
    else:
        assert r is None or r["exit_basis"] != "merger_stock"  # last-traded-price terminal or nothing, never the ratio


@db
@pytest.mark.usefixtures("clean", "offline")
def test_missing_predecessor_price_is_never_filled_from_the_successor(engine):
    from app.backtest.jobs import compute_pending
    from app.db.models import event_returns, events
    from app.marketdata.provider import store_bars
    with engine.begin() as c:
        _company(c, reason_marker="MERGER_PROXY")
        eid = _unresolved_event(c)
        store_bars(c, "SPY", series(date(2022, 12, 1), 600, 380.0, 0.2))
        store_bars(c, "NEWB", series(date(2021, 1, 4), 800, 50.0, 0.0))
        _resolution(c)
        c.execute(text("UPDATE events SET returns_status = 'retry', returns_checked_at = NULL WHERE id = :i"), {"i": eid})
    compute_pending(engine, backfill=None)
    with engine.connect() as c:
        ev = c.execute(select(events).where(events.c.id == eid)).mappings().one()
        n = c.execute(select(event_returns).where(event_returns.c.event_id == eid)).first()
    assert n is None and ev["missing_reason"] == "HISTORICAL_PRICE_STILL_MISSING"
