"""Production liquidity gate + paper fill model (ported from the verified historical-replay-v1 fixes, fills.v3):
the gate measures 30-session $ volume itself when build_universe left it NULL (every non-MICRO / EARLY_SMALL class),
a fill is sized only on prior-session volume, a BUY retries inside its validity and then expires, exits never expire."""
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text

from app.marketdata.provider import Bar
from tests.test_alpha_validation import _event, _serve, _universe, offline_prices  # noqa: F401 - fixture
from tests.test_db import URL

db = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")


def _series(start, n, price=10.0, volume=200_000, overrides=None):
    """Weekday bars; overrides {date: (open, volume)} replace single sessions."""
    out, d, p = [], start, price
    while len(out) < n:
        if d.weekday() < 5:
            o, v = (overrides or {}).get(d, (p, volume))
            out.append(Bar(d, o, (o or p) * 1.01, (o or p) * 0.99, p, p, v))
            p += 0.01
        d += timedelta(days=1)
    return out


def _store(c, symbol, bars):
    from app.marketdata.provider import store_bars
    store_bars(c, symbol, bars)


# ------------------------------------------------------------------ gate

@db
@pytest.mark.usefixtures("clean", "offline_prices")
@pytest.mark.parametrize("bucket", ["SMALL", "GROWTH", "ALREADY_SCALED"])
def test_gate_measures_liquidity_when_the_universe_left_it_null(engine, monkeypatch, bucket):
    from app.paper.proposals import evaluate_gate
    now = datetime(2024, 3, 1, 15, tzinfo=timezone.utc)
    bars = _series(date(2024, 1, 2), 45, volume=500_000)  # ~$5M / day
    # the decision day's own session is enormous: it must never count (point in time)
    bars = [Bar(b.date, b.open, b.high, b.low, b.close, b.adj_close, 10**9) if b.date == now.date() else b for b in bars]
    _serve(monkeypatch, ZBIO=bars)
    with engine.begin() as c:
        _universe(c, adv=None, mcap=5e9, bucket=bucket)
        ev = dict(c.execute(text("SELECT * FROM events WHERE id = :i"), {"i": _event(c)}).mappings().one())
        g = evaluate_gate(c, ev, family_ok=(True, "x"), price_basis="current_ticker_same_cik", now=now, price_symbol="ZBIO")
    assert "LIQUIDITY" in g.passed and g.info["adv_source"] == "prior_sessions_30d"
    assert 4e6 < g.info["adv_dollar_30d"] < 6e6  # the 10^9-share decision day is excluded


@db
@pytest.mark.usefixtures("clean", "offline_prices")
def test_gate_keeps_rejecting_unknown_or_thin_liquidity(engine, monkeypatch):
    from app.paper.proposals import evaluate_gate
    now = datetime(2024, 3, 1, 15, tzinfo=timezone.utc)
    with engine.begin() as c:
        _universe(c, adv=None, bucket="SMALL")
        ev = dict(c.execute(text("SELECT * FROM events WHERE id = :i"), {"i": _event(c)}).mappings().one())
        _serve(monkeypatch, ZBIO=[])
        g = evaluate_gate(c, ev, family_ok=(True, "x"), price_basis="current_ticker_same_cik", now=now, price_symbol="ZBIO")
        assert "LOW_LIQUIDITY" in g.failed and g.info["adv_source"] == "unknown"  # never guessed
    _serve(monkeypatch, ZBIO=_series(date(2024, 1, 2), 45, volume=5_000))  # ~$50k / day: measured, but thin
    with engine.begin() as c:
        g = evaluate_gate(c, ev, family_ok=(True, "x"), price_basis="current_ticker_same_cik", now=now, price_symbol="ZBIO")
    assert "LOW_LIQUIDITY" in g.failed and g.info["adv_source"] == "prior_sessions_30d"
    assert g.info["adv_dollar_30d"] is not None and g.info["adv_dollar_30d"] < 1e6


@db
@pytest.mark.usefixtures("clean", "offline_prices")
def test_a_stored_universe_value_still_wins(engine, monkeypatch):
    from app.paper.proposals import evaluate_gate
    _serve(monkeypatch, ZBIO=_series(date(2024, 1, 2), 45, volume=10))
    with engine.begin() as c:
        _universe(c, adv=5e6, bucket="MICRO")
        ev = dict(c.execute(text("SELECT * FROM events WHERE id = :i"), {"i": _event(c)}).mappings().one())
        g = evaluate_gate(c, ev, family_ok=(True, "x"), price_basis="current_ticker_same_cik",
                          now=datetime(2024, 3, 1, 15, tzinfo=timezone.utc), price_symbol="ZBIO")
    assert "LIQUIDITY" in g.passed and g.info["adv_source"] == "universe_snapshot"


# ------------------------------------------------------------------ fills

def _order(c, not_before, side="BUY", target=0.03, ticker="ZBIO", book="B_90D"):
    from app.db.models import paper_orders
    from app.paper.ledger import ensure_accounts
    aid = ensure_accounts(c, book="RESEARCH")[book]
    return c.execute(paper_orders.insert().values(account_id=aid, proposal_id=None, ticker=ticker, side=side,
                                                  requested_at=datetime(2024, 3, 1, 16, tzinfo=timezone.utc),
                                                  not_before=not_before, target_pct=target, status="PENDING")
                     .returning(paper_orders.c.id)).scalar_one()


def _row(c, oid):
    from app.db.models import paper_orders
    return c.execute(select(paper_orders).where(paper_orders.c.id == oid)).mappings().one()


@db
@pytest.mark.usefixtures("clean", "offline_prices")
def test_fill_is_sized_on_prior_sessions_only(engine):
    """The fill session's own total volume is known only at its close: price, size and cost must not depend on it."""
    from app.paper.ledger import fill_orders
    d0 = date(2024, 3, 4)
    with engine.begin() as c:
        _store(c, "LOWV", _series(date(2024, 1, 2), 60, volume=200_000, overrides={d0: (11.0, 37)}))
        _store(c, "HIGHV", _series(date(2024, 1, 2), 60, volume=200_000, overrides={d0: (11.0, 5_000_000)}))
        a = _order(c, d0, ticker="LOWV", book="A_30D")
        b = _order(c, d0, ticker="HIGHV", book="B_90D")
        assert fill_orders(c, date(2024, 3, 8))["filled"] == 2
        ra, rb = _row(c, a), _row(c, b)
    assert ra["fill_date"] == rb["fill_date"] == d0
    assert (ra["fill_price"], ra["slippage_bps"], ra["fill_model"]) == (rb["fill_price"], rb["slippage_bps"], rb["fill_model"])


@db
@pytest.mark.usefixtures("clean", "offline_prices")
def test_a_session_without_an_opening_trade_is_skipped_not_fatal(engine):
    from app.paper.ledger import fill_orders
    d0, d1 = date(2024, 3, 4), date(2024, 3, 5)
    with engine.begin() as c:
        _store(c, "ZBIO", _series(date(2024, 1, 2), 60, overrides={d0: (11.0, 0)}))
        oid = _order(c, d0)
        assert fill_orders(c, date(2024, 3, 8))["filled"] == 1
        assert _row(c, oid)["fill_date"] == d1  # v2 left it PENDING forever on the zero-volume first bar


@db
@pytest.mark.usefixtures("clean", "offline_prices")
def test_a_buy_expires_after_its_validity(engine):
    from app.paper.ledger import ORDER_VALID_SESSIONS, fill_orders
    d0 = date(2024, 3, 4)
    dead = {d0 + timedelta(days=i): (None, 0) for i in range(14)}  # no opening trade for two weeks
    with engine.begin() as c:
        _store(c, "ZBIO", _series(date(2024, 1, 2), 60, overrides=dead))
        oid = _order(c, d0)
        assert fill_orders(c, date(2024, 3, 6))["waiting"] == 1  # still inside its validity
        out = fill_orders(c, date(2024, 3, 15))
        o = _row(c, oid)
    assert out["cancelled"] == 1 and o["status"] == "CANCELLED" and "expired" in o["note"]
    assert ORDER_VALID_SESSIONS == 5


@db
@pytest.mark.usefixtures("clean", "offline_prices")
def test_no_prior_volume_estimate_means_no_blind_buy(engine):
    from app.paper.ledger import fill_orders
    d0 = date(2024, 3, 4)
    with engine.begin() as c:
        _store(c, "ZBIO", _series(d0, 3))  # listed on d0: nothing before it
        oid = _order(c, d0)
        assert fill_orders(c, d0)["filled"] == 0 and _row(c, oid)["status"] == "PENDING"  # listing day: no estimate
        assert fill_orders(c, date(2024, 3, 6))["filled"] == 1
        o = _row(c, oid)
    # the next session has one prior session: sized as ILLIQUID (2 % of that volume), never on the listing day
    assert o["fill_date"] == date(2024, 3, 5) and o["fill_model"] == "ILLIQUID"


def test_quote_uses_prior_adv_and_needs_an_opening_trade():
    from app.paper.fills import MODEL_VERSION, quote
    prior = _series(date(2024, 1, 2), 25, volume=100_000)
    fill = Bar(date(2024, 3, 4), 10.0, 10.1, 9.9, 10.0, 10.0, 7)
    q = quote(prior, fill, 1_000_000, 10.0)
    assert q is not None and q.shares == pytest.approx(0.05 * 100_000) and q.capped  # cap on prior ADV, not 7 shares
    assert quote(prior, Bar(fill.date, None, 1, 1, 1, 1, 100), 10, 10.0) is None  # no opening trade
    assert quote([], fill, 10, 10.0) is None
    assert MODEL_VERSION == "fills.v3-prior-session-adv"


@db
@pytest.mark.usefixtures("clean", "offline_prices")
def test_an_exit_never_expires_and_waits_for_an_opening_trade(engine, monkeypatch):
    from app.db.models import paper_orders, paper_positions
    from app.paper.ledger import fill_orders
    d0 = date(2024, 3, 4)
    dead = {date(2024, 4, 1) + timedelta(days=i): (None, 0) for i in range(14)}  # two weeks without an opening trade
    bars = _series(date(2024, 1, 2), 90, overrides=dead)
    _serve(monkeypatch, ZBIO=bars)  # exits price from a freshly fetched series
    with engine.begin() as c:
        _store(c, "ZBIO", bars)
        buy = _order(c, d0)
        assert fill_orders(c, date(2024, 3, 8))["filled"] == 1
        pos = c.execute(select(paper_positions)).mappings().one()
        sell = c.execute(paper_orders.insert().values(account_id=pos["account_id"], proposal_id=None, position_id=pos["id"],
                                                      ticker="ZBIO", side="SELL", requested_at=datetime(2024, 3, 29, tzinfo=timezone.utc),
                                                      not_before=date(2024, 4, 1), status="PENDING", note="TIME")
                         .returning(paper_orders.c.id)).scalar_one()
        c.execute(paper_positions.update().values(status="CLOSING", exit_reason="TIME"))
        out = fill_orders(c, date(2024, 4, 12))  # 10 sessions later, still no opening trade
        assert out["cancelled"] == 0 and _row(c, sell)["status"] == "PENDING"
        fill_orders(c, date(2024, 4, 20))
        s = _row(c, sell)
    assert s["status"] == "FILLED" and s["fill_date"] == date(2024, 4, 15) and buy


@db
@pytest.mark.usefixtures("clean", "offline_prices")
def test_a_concurrent_run_never_overwrites_a_fill(engine):
    """Run A fills (not yet committed); run B, whose `today` is past the validity, would expire the same order. B must
    wait on the order row and, once A committed, leave the fill alone."""
    from sqlalchemy.exc import OperationalError

    from app.paper.ledger import fill_orders
    d0 = date(2024, 3, 4)
    with engine.begin() as c:
        _store(c, "ZBIO", _series(date(2024, 1, 2), 60))
        oid = _order(c, d0)
    conn_a = engine.connect()
    tx = conn_a.begin()
    try:
        assert fill_orders(conn_a, date(2024, 3, 8))["filled"] == 1
        with engine.connect() as conn_b:
            conn_b.execute(text("SET lock_timeout = '300ms'"))
            with pytest.raises(OperationalError):
                fill_orders(conn_b, date(2024, 4, 30))
            conn_b.rollback()
    finally:
        tx.commit()
        conn_a.close()
    with engine.begin() as c:
        out = fill_orders(c, date(2024, 4, 30))
        o = _row(c, oid)
    assert out["cancelled"] == 0 and o["status"] == "FILLED" and o["fill_date"] == d0


@db
@pytest.mark.usefixtures("clean", "offline_prices")
def test_a_catch_up_run_fills_in_session_order_like_daily_runs(engine):
    """Order 1 (lower id) is halted until Friday, order 2 is fillable on Tuesday. A single catch-up run must size both
    exactly like daily runs did: Tuesday first, on the portfolio as it stood then."""
    from app.paper.ledger import fill_orders
    mon, tue, fri = date(2024, 3, 4), date(2024, 3, 5), date(2024, 3, 8)
    halted = {mon + timedelta(days=i): (None, 0) for i in range(4)}

    def world(c):
        _store(c, "HALT", _series(date(2024, 1, 2), 60, overrides=halted))
        _store(c, "OKAY", _series(date(2024, 1, 2), 60, price=20.0))
        return _order(c, mon, ticker="HALT", target=0.5), _order(c, tue, ticker="OKAY", target=0.5)

    def result(c, ids):
        return [(r["fill_date"], round(r["fill_price"], 6), round(r["shares"], 6)) for r in (_row(c, i) for i in ids)]

    conn = engine.connect()
    tx = conn.begin()
    ids = world(conn)
    for d in (mon, tue, date(2024, 3, 6), date(2024, 3, 7), fri):
        fill_orders(conn, d)
    daily = result(conn, ids)
    tx.rollback()
    tx = conn.begin()
    ids = world(conn)
    fill_orders(conn, fri)
    catch_up = result(conn, ids)
    tx.rollback()
    conn.close()
    assert daily[0][0] == fri and daily[1][0] == tue
    assert catch_up == daily


@db
@pytest.mark.usefixtures("clean", "offline_prices")
def test_ordering_uses_the_fill_rule_after_a_long_volume_gap(engine):
    """After > 20 volume-less sessions the fill model has no ADV estimate (its 20-bar window): the ordering key must
    agree with the fill itself, not with older volume."""
    from app.paper.ledger import _first_fillable, fill_orders
    mon = date(2024, 3, 4)
    gap = {date(2024, 1, 29) + timedelta(days=i): (10.0, 0) for i in range(35)}  # ~25 stored sessions, zero volume
    with engine.begin() as c:
        _store(c, "GAPD", _series(date(2023, 11, 1), 110, overrides={**gap, mon: (10.0, 50_000)}))
        oid = _order(c, mon, ticker="GAPD")
        o = _row(c, oid)
        key = _first_fillable(c, o, date(2024, 3, 8), {})
        fill_orders(c, date(2024, 3, 8))
        filled = _row(c, oid)["fill_date"]
    assert key == filled == date(2024, 3, 5)  # Monday prints, but has no prior estimate; Tuesday does (Monday)


@db
@pytest.mark.usefixtures("clean", "offline_prices")
def test_concurrent_runs_with_different_dates_lock_in_the_same_order(engine):
    """Two BUYs: a Monday run (both unfillable, id order) and a Friday catch-up (id 2 first chronologically) must not
    deadlock: every run locks the pending set in id order first, so the second one only waits."""
    from sqlalchemy.exc import OperationalError

    from app.paper.ledger import fill_orders
    mon = date(2024, 3, 4)
    with engine.begin() as c:
        _store(c, "HALTA", _series(date(2024, 1, 2), 60, overrides={mon + timedelta(days=i): (None, 0) for i in range(4)}))
        _store(c, "HALTB", _series(date(2024, 1, 2), 60, overrides={mon: (None, 0)}))
        a = _order(c, mon, ticker="HALTA", book="A_30D")
        b = _order(c, mon, ticker="HALTB", book="B_90D")
    conn_a = engine.connect()
    tx = conn_a.begin()
    try:
        fill_orders(conn_a, mon)  # holds both order locks, fills nothing
        with engine.connect() as conn_b:
            conn_b.execute(text("SET lock_timeout = '300ms'"))
            with pytest.raises(OperationalError) as err:
                fill_orders(conn_b, date(2024, 3, 8))
            assert "deadlock" not in str(err.value).lower()  # a plain lock wait, never a deadlock
            conn_b.rollback()
    finally:
        tx.commit()
        conn_a.close()
    with engine.begin() as c:
        fill_orders(c, date(2024, 3, 8))
        ra, rb = _row(c, a), _row(c, b)
    assert (ra["fill_date"], rb["fill_date"]) == (date(2024, 3, 8), date(2024, 3, 5))
