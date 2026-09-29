"""Regression tests for the Codex audit of v2-return-repair (d860fe2): review exclusion in every lane and consumer,
race fence, identity-scoped absences, fairness, NYSE-session eligibility, vendor claim, malformed Tiingo payloads,
CUSIP normalization, CHANGED identity ticker, migration round trip."""
import os
import subprocess
import threading
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text

from tests.test_db import URL
from tests.test_return_queue import _cover, _event, _prices, _queue, _registry, _row

db = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")


# ------------------------------------------------------------------ 1. review never enters a lane + race fence

@db
@pytest.mark.usefixtures("clean")
def test_identity_unsafe_never_enters_the_main_queue(engine):
    from app.backtest.jobs import select_queue
    now = datetime.now(timezone.utc)
    with engine.begin() as c:
        ok = _event(c, "ok", "0001953926", "ZBIO", date(2024, 3, 1))
        rv = [_event(c, f"rv{s}", "0001737450", "OPRA", date(2024, 3, 1), needs_review=True, identity_confidence="LOW",
                     returns_status=s, returns_next_at=now - timedelta(days=1))
              for s in ("pending", "partial", "retry", "no_data")]
    with engine.connect() as c:
        q = [r[0] for r in select_queue(c, now, limit=100)]
    assert q == [ok] and not set(rv) & set(_queue(engine))


@db
@pytest.mark.usefixtures("clean")
def test_identity_unsafe_never_enters_the_vendor_lane(engine):
    from app.backtest.jobs import claim_vendor
    with engine.begin() as c:
        rv = _event(c, "rv", "0001737450", "OPRA", date(2024, 3, 1), needs_review=True, identity_confidence="NONE",
                    returns_status="vendor_wait")
        ok = _event(c, "ok", "0001953926", "ZBIO", date(2024, 3, 1), returns_status="vendor_wait")
        got = [r[0] for r in claim_vendor(c, datetime.now(timezone.utc), 10)]
    assert got == [ok] and _row(engine, rv)["returns_next_at"] is None  # not even leased


@db
@pytest.mark.usefixtures("clean")
def test_event_selected_before_an_identity_repair_cannot_persist_a_return(engine, monkeypatch):
    """Selection happens, then the repair marks the event REVIEW (commits), then the worker gets to it."""
    from app.backtest import jobs
    _prices(monkeypatch)
    with engine.begin() as c:
        eid = _event(c, "race", "0001953926", "ZBIO", date(2024, 3, 1))
    real_spy = jobs._ensure_spy

    def spy_then_repair(conn):  # runs after the batch was selected, before any event is priced
        bars = real_spy(conn)
        with engine.begin() as other:  # what the identity repair writes
            other.execute(text("UPDATE events SET needs_review = true, identity_confidence = 'LOW' WHERE id = :i"), {"i": eid})
        return bars
    monkeypatch.setattr(jobs, "_ensure_spy", spy_then_repair)
    r = jobs.compute_pending(engine)
    assert r["processed"] == 0 and r["fenced"] == 1
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM event_returns WHERE event_id = :i"), {"i": eid}).scalar() == 0
    assert _row(engine, eid)["returns_status"] == "pending"  # untouched by the worker


@db
@pytest.mark.usefixtures("clean")
def test_repair_waits_for_an_inflight_pricing_and_then_wins(engine):
    """The fence holds a row lock while pricing: a concurrent REVIEW update blocks until the worker commits."""
    from app.backtest.jobs import _fence
    with engine.begin() as c:
        eid = _event(c, "lock", "0001953926", "ZBIO", date(2024, 3, 1))
    done = threading.Event()
    with engine.begin() as worker:
        assert _fence(worker, eid, "ZBIO", "0001953926", False)

        def repair():
            with engine.begin() as other:
                other.execute(text("SET lock_timeout = '10s'"))
                other.execute(text("UPDATE events SET needs_review = true, identity_confidence = 'LOW' WHERE id = :i"),
                              {"i": eid})
            done.set()
        t = threading.Thread(target=repair)
        t.start()
        assert not done.wait(1.0)  # blocked by the worker's row lock
    t.join(10)
    assert done.is_set() and _row(engine, eid)["needs_review"] is True


@db
@pytest.mark.usefixtures("clean")
def test_actual_repair_removes_returns_written_by_an_inflight_worker(engine):
    """Worker locks the event (fence) and is pricing; the real identity repair starts, must wait for the worker's
    commit, and then delete what the worker wrote - no stale return survives."""
    from app.backtest.jobs import _fence
    from app.jobs import return_repair
    with engine.begin() as c:
        _registry(c, "0001326706", "ENvue Medical, Inc.", "FEED", "29415F100")
        eid = _event(c, "env", "0001326706", "FEED", date(2024, 5, 15), event_type="INST_13F_EXIT",
                     company_name="ENVISTA HOLDINGS CORP", identity_confidence="MEDIUM")
        c.execute(text("""UPDATE events SET payload = '{"cusip": "29415F104"}' WHERE id = :i"""), {"i": eid})
    finished = threading.Event()
    errors: list = []

    def repair():
        try:
            return_repair.run(engine, items=("identity_13f",), apply=True)
        except Exception as e:  # noqa: BLE001
            errors.append(e)
        finished.set()
    with engine.begin() as worker:
        assert _fence(worker, eid, "FEED", "0001326706", False)
        t = threading.Thread(target=repair)
        t.start()
        assert not finished.wait(1.5)  # the repair is waiting for the worker's row lock
        _ret(worker, eid, 30, 0.2)  # the worker persists its (wrong-issuer) return while the repair waits
    t.join(30)
    assert finished.is_set() and not errors
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM event_returns WHERE event_id = :i"), {"i": eid}).scalar() == 0
    r = _row(engine, eid)
    assert r["needs_review"] is True and r["missing_reason"] == "IDENTITY_REVIEW"


# ------------------------------------------------------------------ 2. analytics consumers

def _ret(c, eid, h=90, ex=0.05):
    c.execute(text("""INSERT INTO event_returns (event_id, horizon_days, entry_date, entry_price, exit_date, exit_price,
                      ret, spy_ret, excess_ret) VALUES (:i, :h, '2023-01-11', 10, '2023-04-11', 11, 0.1, 0.05, :ex)"""),
              {"i": eid, "h": h, "ex": ex})


@db
@pytest.mark.usefixtures("clean")
def test_review_events_are_excluded_from_every_statistics_consumer(engine, monkeypatch):
    from app.backtest import jobs, placebo
    with engine.begin() as c:
        good = _event(c, "g", "0001953926", "ZBIO", date(2023, 1, 10), price_symbol="ZBIO")
        # an identity-SAFE review event: it may carry raw returns, but never counts in a verdict path
        bad = _event(c, "b", "0001737450", "OPRA", date(2023, 1, 10), price_symbol="OPRA", needs_review=True,
                     identity_confidence="HIGH")
        for e, ex in ((good, 0.05), (bad, -0.9)):
            _ret(c, e, 90, ex)
        et = jobs.event_type_stats(c, "INSIDER_OPEN_MARKET_BUY", horizons=(90,))
        ov = [r for r in jobs.stats_overview(c) if r["horizon_days"] == 90]
        monkeypatch.setattr(placebo, "_excess", lambda *a, **k: 0.0)
        pl = placebo.family_vs_placebo(c, "INSIDER_OPEN_MARKET_BUY", horizon=90)
    assert et[90]["n"] == 1 and et[90]["mean_excess"] == pytest.approx(0.05)
    assert len(ov) == 1 and ov[0]["n"] == 1 and ov[0]["mean_excess"] == pytest.approx(0.05)
    assert pl["companies"] == 1 and pl["skipped"] == 0


# ------------------------------------------------------------------ 3. identity-scoped absences

def _counting(calls):
    from app import http
    from app.marketdata import provider as prov

    class P(prov.MarketDataProvider):
        name = "yahoo"

        def get_historical_prices(self, symbol, start, end):
            calls.append((symbol, start))
            raise http.HttpError("x", 404, "No data found")
    return P()


@db
@pytest.mark.usefixtures("clean")
def test_reused_ticker_does_not_share_the_negative_cache_across_issuers_or_markets(engine):
    from app.marketdata.provider import AbsenceScope, ensure_prices
    calls: list = []
    p = _counting(calls)
    s = date(2023, 1, 1)
    with engine.begin() as c:
        ensure_prices(c, "ABC", s, date(2023, 6, 1), provider=p, absence=AbsenceScope("CIK:0000000001"))
        ensure_prices(c, "ABC", s, date(2023, 6, 1), provider=p, absence=AbsenceScope("CIK:0000000001"))
        assert len(calls) == 1  # same issuer, same market, same window: cached
        ensure_prices(c, "ABC", s, date(2023, 6, 1), provider=p, absence=AbsenceScope("CIK:0000000002"))
        assert len(calls) == 2  # another issuer reusing the symbol is asked
        ensure_prices(c, "ABC", s, date(2023, 6, 1), provider=p, absence=AbsenceScope("CIK:0000000001", market="CA"))
        assert len(calls) == 3  # another market is asked
        ensure_prices(c, "ABC", s, date(2023, 6, 1), provider=p, absence=None)
        assert len(calls) == 4  # no scope: never negatively cached


@db
@pytest.mark.usefixtures("clean")
def test_absence_covers_only_the_confirmed_interval(engine):
    from app.marketdata.provider import AbsenceScope, ensure_prices
    calls: list = []
    p = _counting(calls)
    sc = AbsenceScope("CIK:0000000001|SYM:2019-01-01")
    with engine.begin() as c:
        ensure_prices(c, "ABC", date(2023, 1, 1), date(2023, 6, 30), provider=p, absence=sc)  # 404 recorded Jan-Jun
        ensure_prices(c, "ABC", date(2023, 2, 1), date(2023, 5, 1), provider=p, absence=sc)  # inside: cached
        assert len(calls) == 1
        ensure_prices(c, "ABC", date(2023, 7, 1), date(2023, 12, 31), provider=p, absence=sc)  # later, disjoint
        ensure_prices(c, "ABC", date(2021, 1, 1), date(2021, 6, 30), provider=p, absence=sc)  # earlier, disjoint
        ensure_prices(c, "ABC", date(2022, 12, 1), date(2023, 3, 1), provider=p, absence=sc)  # overlapping, not inside
        assert [x[1] for x in calls] == [date(2023, 1, 1), date(2023, 7, 1), date(2021, 1, 1), date(2022, 12, 1)]


@db
@pytest.mark.usefixtures("clean")
def test_predecessor_and_successor_periods_have_distinct_identities(engine):
    from app.securitymaster.provider import SecSecurityMaster
    with engine.begin() as c:
        c.execute(text("""INSERT INTO security_symbols (cik, symbol, first_seen, last_seen, n_obs, source) VALUES
                          ('0000000777', 'ABC', '2015-01-05', '2018-06-01', 3, 'form345_filing'),
                          ('0000000888', 'ABC', '2021-03-01', '2024-01-02', 3, 'form345_filing')"""))
        sm = SecSecurityMaster(c, None, None, skip_known_absent=True)
        pred = sm._absence_scope("0000000777", "ABC", "historical_symbol_verified", date(2017, 1, 3))
        succ = sm._absence_scope("0000000888", "ABC", "historical_symbol_verified", date(2022, 1, 3))
        cur = sm._absence_scope("0000000777", "ABC", "current_ticker_same_cik", date(2025, 1, 3))
        assert len({pred.identity, succ.identity, cur.identity}) == 3
        assert pred.identity == "CIK:0000000777|SYM:2015-01-05"
        # the issuer outside any period it used the symbol in: no negative cache at all
        assert sm._absence_scope("0000000777", "ABC", "historical_symbol_verified", date(2020, 6, 1)) is None
        # a known security id wins over the issuer fallback
        assert sm._absence_scope("0000000777", "ABC", "current_ticker_same_cik", date(2025, 1, 3), "US:XYZ:1").identity == "SEC:US:XYZ:1"
        assert SecSecurityMaster(c, None, None)._absence_scope("0000000777", "ABC", None, date(2017, 1, 3)) is None


@db
@pytest.mark.usefixtures("clean")
def test_absence_records_only_the_interval_actually_requested(engine):
    """Cached Jan + Jun bars: a Jan-Dec request asks the provider only from late May; its 404 must not suppress a
    later request for an earlier, never-checked gap."""
    from app.marketdata.provider import AbsenceScope, Bar, ensure_prices, store_bars
    calls: list = []
    p = _counting(calls)
    sc = AbsenceScope("CIK:0000000001|SYM:2019-01-01")
    with engine.begin() as c:
        store_bars(c, "ABC", [Bar(date(2023, 1, 3), 1, 1, 1, 1, 1, 1), Bar(date(2023, 6, 1), 1, 1, 1, 1, 1, 1)])
        ensure_prices(c, "ABC", date(2023, 1, 1), date(2023, 12, 31), provider=p, absence=sc)
        assert calls == [("ABC", date(2023, 5, 29))]  # fetch_from = last cached bar - 3 days
        ensure_prices(c, "ABC", date(2023, 1, 1), date(2023, 12, 31), provider=p, absence=sc)
        assert len(calls) == 1  # the same provider interval: cached
        ensure_prices(c, "ABC", date(2022, 12, 1), date(2023, 12, 31), provider=p, absence=sc)  # earlier gap
        assert calls[-1] == ("ABC", date(2022, 12, 1)) and len(calls) == 2
        ensure_prices(c, "ABC", date(2022, 6, 1), date(2022, 11, 30), provider=p, absence=sc)  # never checked
        assert len(calls) == 3


# ------------------------------------------------------------------ 4. fairness under continuous arrivals

@db
@pytest.mark.usefixtures("clean")
def test_no_tier_starves_under_sustained_overload_with_batches_of_one(engine):
    """3 new events arrive per hour, only ONE event is priced per batch (the smallest limit / a batch cut short
    after its first event by the time budget). Every lower tier is still served, within a bounded number of batches."""
    from app.backtest.jobs import select_queue
    t0 = datetime.now(timezone.utc) - timedelta(hours=200)
    with engine.begin() as c:
        partial = _event(c, "p", "0000000003", "CCC", date(2024, 3, 1), returns_status="partial",
                         returns_checked_at=t0 - timedelta(days=1), returns_next_at=t0)
        retry = _event(c, "r", "0000000001", "AAA", date(2024, 3, 1), returns_status="retry",
                       returns_checked_at=t0 - timedelta(days=1), returns_next_at=t0)
        nodata = _event(c, "n", "0000000002", "BBB", date(2024, 3, 1), returns_status="no_data",
                        returns_checked_at=t0 - timedelta(days=30), returns_next_at=t0)
    served: dict[int, int] = {}
    for hour in range(120):
        with engine.begin() as c:
            for i in range(3):
                e = _event(c, f"w{hour}-{i}", f"000{hour:03d}{i:04d}", f"W{hour}x{i}", date(2024, 3, 1))
                c.execute(text("UPDATE events SET created_at = :t WHERE id = :i"), {"t": t0 + timedelta(hours=hour), "i": e})
        with engine.connect() as c:
            batch = [r[0] for r in select_queue(c, datetime.now(timezone.utc), limit=1)]
        with engine.begin() as c:
            c.execute(text("""UPDATE events SET returns_status = 'complete', returns_checked_at = now(),
                              returns_next_at = NULL WHERE id = ANY(:i)"""), {"i": batch})
        for e in batch:
            served.setdefault(e, hour)
    # key = due - head start; only arrivals with a smaller key go first: 3 per hour for 12 h / 18 h / 24 h
    assert served[partial] <= 3 * 12 + 1 and served[retry] <= 3 * 18 + 2 and served[nodata] <= 3 * 24 + 3
    assert served[partial] < served[retry] < served[nodata]


@db
@pytest.mark.usefixtures("clean")
def test_a_zero_time_budget_still_makes_progress(engine, monkeypatch):
    from app.backtest import jobs
    _prices(monkeypatch)
    with engine.begin() as c:
        for i in range(3):
            _event(c, f"tb{i}", "0001953926", "ZBIO", date(2024, 3, 1) + timedelta(days=i))
    for expected_left in (2, 1, 0):
        r = jobs.compute_pending(engine, time_budget_s=0)
        assert r["processed"] == 1  # the budget is checked only after progress
        with engine.connect() as c:
            assert c.execute(text("SELECT count(*) FROM events WHERE returns_checked_at IS NULL")).scalar() == expected_left


# ------------------------------------------------------------------ 5. NYSE-session eligibility

def test_friday_entry_one_day_horizon_waits_for_monday_close():
    from app.backtest.jobs import horizon_ready_at, next_eligible_at
    fri = date(2026, 9, 25)
    ready = horizon_ready_at(fri, 1)
    assert ready == datetime(2026, 9, 28, 22, tzinfo=timezone.utc)  # Mon 16:00 EDT = 20:00 UTC, + EOD lag
    sat_noon = datetime(2026, 9, 26, 12, tzinfo=timezone.utc)
    # on the weekend the missing 1d horizon is not a "gap": it is scheduled for Monday, not pushed 7 days out
    assert next_eligible_at(fri, set(), sat_noon.date(), sat_noon) == ready
    # 7d ends Fri 2026-10-02 (a session): that close
    assert horizon_ready_at(fri, 7) == datetime(2026, 10, 2, 22, tzinfo=timezone.utc)
    # 8d ends Saturday: the last bar is Friday's, available from Saturday 00:00 UTC (engine: target <= as_of)
    assert horizon_ready_at(fri, 8) == datetime(2026, 10, 3, tzinfo=timezone.utc)


def test_holiday_and_winter_time_zone():
    from app.backtest.jobs import horizon_ready_at
    # Wed 2026-12-23 entry -> Thu 12-24 is a session; Thu 2026-12-24 entry -> Fri 12-25 holiday -> Mon 12-28
    assert horizon_ready_at(date(2026, 12, 23), 1) == datetime(2026, 12, 24, 23, tzinfo=timezone.utc)  # EST: 21:00 UTC + 2 h
    assert horizon_ready_at(date(2026, 12, 24), 1) == datetime(2026, 12, 28, 23, tzinfo=timezone.utc)
    # a horizon ending on a holiday uses the last session before it
    assert horizon_ready_at(date(2026, 11, 26) - timedelta(days=30), 30) == datetime(2026, 11, 26, tzinfo=timezone.utc)


def test_available_after_the_close_enters_next_session():
    from app.backtest.jobs import horizon_ready_at
    from app.marketdata.calendar import first_tradable_session
    # published Friday 21:30 UTC (17:30 EDT, after the close) -> entry Monday -> 1d exit Tuesday
    entry = first_tradable_session(date(2026, 9, 25), datetime(2026, 9, 25, 21, 30, tzinfo=timezone.utc))
    assert entry == date(2026, 9, 28)
    assert horizon_ready_at(entry, 1) == datetime(2026, 9, 29, 22, tzinfo=timezone.utc)


# ------------------------------------------------------------------ 6. vendor claim

@db
@pytest.mark.usefixtures("clean")
def test_two_vendor_workers_cannot_claim_the_same_event(engine):
    from app.backtest.jobs import claim_vendor
    with engine.begin() as c:
        ids = {_event(c, f"v{i}", f"000000070{i}", f"V{i}", date(2024, 3, 1), returns_status="vendor_wait") for i in range(4)}
    now = datetime.now(timezone.utc)
    with engine.begin() as a:
        got_a = {r[0] for r in claim_vendor(a, now, 2)}  # transaction still open: rows locked
        with engine.begin() as b:
            got_b = {r[0] for r in claim_vendor(b, now, 10)}  # SKIP LOCKED: only the others
    assert len(got_a) == 2 and got_b == ids - got_a
    with engine.begin() as c:  # both leases are live: nothing left to claim
        assert claim_vendor(c, datetime.now(timezone.utc), 10) == []
    with engine.begin() as c:  # a crashed worker: the lease expires and the event is claimed again
        assert len(claim_vendor(c, datetime.now(timezone.utc) + timedelta(minutes=21), 10)) == 4


@db
@pytest.mark.usefixtures("clean")
def test_a_stale_vendor_claim_can_never_reach_the_provider(engine):
    """Worker A claims, stalls past its lease; worker B reclaims; A resumes - A's fence fails, B's holds."""
    from app.backtest.jobs import _fence, claim_vendor
    with engine.begin() as c:
        eid = _event(c, "v", "0000000701", "V1", date(2024, 3, 1), returns_status="vendor_wait")
    t = datetime.now(timezone.utc)
    with engine.begin() as c:
        (row_a,) = claim_vendor(c, t, 1)
    lease_a = row_a[-1]
    with engine.begin() as c:  # A is still inside its lease: its own fence holds
        assert _fence(c, eid, "V1", "0000000701", True, lease_a, t + timedelta(minutes=1))
    with engine.begin() as c:  # A stalls; its lease expired; nobody reclaimed yet -> A is out
        assert not _fence(c, eid, "V1", "0000000701", True, lease_a, t + timedelta(minutes=21))
    with engine.begin() as c:  # B reclaims after expiry
        (row_b,) = claim_vendor(c, t + timedelta(minutes=21), 1)
    lease_b = row_b[-1]
    assert lease_b != lease_a
    with engine.begin() as c:  # A resumes with its old token -> refused; B -> allowed
        assert not _fence(c, eid, "V1", "0000000701", True, lease_a, t + timedelta(minutes=22))
        assert _fence(c, eid, "V1", "0000000701", True, lease_b, t + timedelta(minutes=22))
    with engine.begin() as c:  # B hit a temporary failure: rescheduled; A's stale token still cannot retry at once
        c.execute(text("UPDATE events SET returns_next_at = :n WHERE id = :i"), {"n": t + timedelta(hours=2), "i": eid})
        assert not _fence(c, eid, "V1", "0000000701", True, lease_a, t + timedelta(minutes=23))
        assert _fence(c, eid, "V1", "0000000701", False) is False  # never in the main lane either


# ------------------------------------------------------------------ 7. malformed Tiingo payload

@pytest.mark.parametrize("payload", [
    [{"foo": 1}, "x", {"date": "not-a-date"}],
    [{"date": "2024-01-02"}],  # a valid date but no prices at all
    [{"date": "2024-01-02", "close": "12.5"}],  # a string is not a price
    [{"date": "2024-01-02", "close": float("nan")}, {"date": "2024-01-03", "close": float("inf")}],
    [{"date": "2024-01-02", "close": 0}, {"date": "2024-01-03", "close": -3.0}, {"date": "2024-01-04", "close": True}],
])
def test_malformed_tiingo_list_is_not_no_data(payload):
    from app import http
    from app.securitymaster.vendors import parse_tiingo_prices
    assert parse_tiingo_prices([], "K") == []  # genuine empty answer
    with pytest.raises(http.HttpError) as e:
        parse_tiingo_prices(payload, "K")
    assert e.value.status == 502


def test_tiingo_bar_fields_are_sanitized():
    from app.securitymaster.vendors import parse_tiingo_prices
    bars = parse_tiingo_prices([{"date": "2024-01-02", "close": 10.0, "open": float("nan"), "high": "x", "low": -1,
                                 "adjClose": 9.5, "volume": -5}, {"date": "2024-01-03", "close": None}], "K")
    assert len(bars) == 1
    b = bars[0]
    assert (b.close, b.open, b.high, b.low, b.adj_close, b.volume) == (10.0, None, None, None, 9.5, None)


@db
@pytest.mark.usefixtures("clean")
def test_malformed_vendor_payload_is_temporary_and_never_cached(engine):
    from app.marketdata.provider import AbsenceScope, absence_fresh
    from app.securitymaster.provider import SecSecurityMaster, VendorUnavailable
    from app.securitymaster.vendors import parse_tiingo_prices

    class Vendor:
        name = "tiingo"

        def get_historical_prices(self, symbol, start, end):
            return parse_tiingo_prices([{"close": 1}], symbol)
    sc = AbsenceScope("CIK:0000000555")
    with engine.begin() as c:
        sm = SecSecurityMaster(c, None, Vendor(), skip_known_absent=True)
        with pytest.raises(VendorUnavailable) as e:
            sm._vendor_bars("OLDB", date(2023, 1, 1), date(2023, 6, 1), sc)
        assert e.value.quota is False
        assert not absence_fresh(c, "tiingo", sc, "OLDB", date(2023, 1, 1), date(2023, 6, 1))


# ------------------------------------------------------------------ 8. CUSIP normalization

@db
@pytest.mark.usefixtures("clean")
def test_invalid_check_digit_never_gets_medium_or_high(engine):
    from app.markets.identity import cusip_to_cik, normalize_cusip
    assert normalize_cusip("037833101") is None  # Apple with a wrong check digit
    with engine.begin() as c:
        _cover(c, "0000000000-24-000009", "0000320193", "037833101", "Apple Inc.")  # the 13D row carries the bad value
        _registry(c, "0000320193", "Apple Inc.", "AAPL", "037833101")
        cik, conf, why = cusip_to_cik(c, "037833101", "APPLE INC")
    assert conf not in ("HIGH", "MEDIUM") and (cik is None or why.startswith("REVIEW_REQUIRED"))


@db
@pytest.mark.usefixtures("clean")
@pytest.mark.parametrize("q", ["037833100X", "037833100 0", "0378331000", "037833100-"])
def test_overlength_cusip_lookup_never_gets_medium_or_high(engine, q):
    from app.markets.identity import cusip_to_cik
    with engine.begin() as c:
        _cover(c, "0000000000-24-000011", "0000320193", "037833100", "Apple Inc.")
        _registry(c, "0000320193", "Apple Inc.", "AAPL", "037833100")
        assert cusip_to_cik(c, "037833100", "APPLE INC")[1] == "HIGH"  # the valid value itself
        cik, conf, why = cusip_to_cik(c, q, "APPLE INC")
    assert conf not in ("HIGH", "MEDIUM") and (cik is None or why.startswith("REVIEW_REQUIRED"))


@db
@pytest.mark.usefixtures("clean")
def test_normalized_valid_cusip_is_accepted(engine):
    from app.markets.identity import cusip_to_cik, normalize_cusip
    assert normalize_cusip(" 037833100\n") == "037833100"  # trimmed
    assert normalize_cusip("29415f104") == "29415F104"  # case
    assert normalize_cusip("29415-F10") is None and normalize_cusip("29415F1040") is None  # characters / length
    assert normalize_cusip("000000000") is None  # registry placeholder
    with engine.begin() as c:
        # stored lower-case (the column is VARCHAR(9), so no padding) and queried padded + mixed case
        _registry(c, "0001757073", "Envista Holdings Corp", "NVST", "29415f104")
        assert cusip_to_cik(c, "  29415F104 ", "ENVISTA HOLDINGS CORP")[:2] == ("0001757073", "MEDIUM")
        _cover(c, "0000000000-24-000010", "0001757073", "29415f104", "Envista Holdings Corp")
        assert cusip_to_cik(c, "29415f104", "ENVISTA HOLDINGS CORP")[:2] == ("0001757073", "HIGH")


# ------------------------------------------------------------------ 10. CHANGED identity -> ticker

@db
@pytest.mark.usefixtures("clean")
def test_changed_issuer_does_not_keep_the_old_ticker(engine):
    from app.jobs import return_repair
    with engine.begin() as c:
        _registry(c, "0000320193", "Apple Inc.", "AAPL", "037833100")
        eid = _event(c, "chg", "0001953926", "ZBIO", date(2024, 5, 15), event_type="INST_13F_EXIT",
                     company_name="APPLE INC", identity_confidence="MEDIUM")
        c.execute(text("""UPDATE events SET payload = '{"cusip": "037833100"}' WHERE id = :i"""), {"i": eid})
    dry = return_repair.run(engine, items=("identity_13f",))
    assert dry["identity_13f"]["by_kind"] == {"CHANGED": 1}
    return_repair.run(engine, items=("identity_13f",), apply=True)
    r = _row(engine, eid)
    assert r["issuer_cik"] == "0000320193" and r["ticker"] == "AAPL" and r["returns_status"] == "pending"


# ------------------------------------------------------------------ 9. migration round trip

@db
def test_migration_0013_upgrade_downgrade_reupgrade():
    admin = create_engine(URL, isolation_level="AUTOCOMMIT")
    name = "pe_mig_0013"
    with admin.connect() as c:
        c.execute(text(f"DROP DATABASE IF EXISTS {name}"))
        c.execute(text(f"CREATE DATABASE {name}"))
    url = URL.rsplit("/", 1)[0] + "/" + name
    env = {**os.environ, "DATABASE_URL": url}
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        for args in (["upgrade", "head"], ["downgrade", "0012"], ["upgrade", "head"]):
            p = subprocess.run(["alembic", *args], cwd=root, env=env, capture_output=True, text=True, timeout=300)
            assert p.returncode == 0, p.stderr[-2000:]
        eng = create_engine(url)
        with eng.connect() as c:
            assert c.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0013"
            valid = c.execute(text("""SELECT i.indisvalid FROM pg_index i JOIN pg_class x ON x.oid = i.indexrelid
                                      WHERE x.relname = 'ix_events_returns_queue'""")).scalar()
            cols = c.execute(text("""SELECT count(*) FROM information_schema.columns WHERE table_name = 'price_absences'
                                     AND column_name IN ('identity', 'market', 'window_start', 'window_end')""")).scalar()
        eng.dispose()
        assert valid is True and cols == 4
    finally:
        with admin.connect() as c:
            c.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))


@db
def test_migration_0013_recovers_from_a_failed_concurrent_index_build():
    """Partial state after a failed run: column + table committed, an INVALID index left behind, alembic at 0012.
    Re-running `alembic upgrade head` must complete."""
    admin = create_engine(URL, isolation_level="AUTOCOMMIT")
    name = "pe_mig_0013_fail"
    with admin.connect() as c:
        c.execute(text(f"DROP DATABASE IF EXISTS {name}"))
        c.execute(text(f"CREATE DATABASE {name}"))
    url = URL.rsplit("/", 1)[0] + "/" + name
    env = {**os.environ, "DATABASE_URL": url}
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def alembic(*args):
        p = subprocess.run(["alembic", *args], cwd=root, env=env, capture_output=True, text=True, timeout=300)
        assert p.returncode == 0, p.stderr[-2000:]
    try:
        alembic("upgrade", "0012")
        eng = create_engine(url, isolation_level="AUTOCOMMIT")
        with eng.connect() as c:
            c.execute(text("ALTER TABLE events ADD COLUMN returns_next_at TIMESTAMP WITH TIME ZONE"))
            c.execute(text("""CREATE TABLE price_absences (provider VARCHAR(16) NOT NULL, identity VARCHAR(64) NOT NULL,
                              market VARCHAR(8) NOT NULL, symbol VARCHAR(16) NOT NULL, window_start DATE NOT NULL,
                              window_end DATE NOT NULL, checked_at TIMESTAMPTZ NOT NULL, hits INTEGER DEFAULT 1 NOT NULL,
                              PRIMARY KEY (provider, identity, market, symbol, window_start, window_end))"""))
            for k in ("a", "b"):  # duplicates make a UNIQUE concurrent build fail -> INVALID index remains
                c.execute(text("""INSERT INTO events (event_type, dedupe_key, public_date, title, payload, source_url)
                                  VALUES ('X', :k, '2024-01-02', 't', '{}', 'u')"""), {"k": k})
            with pytest.raises(Exception):
                c.execute(text("CREATE UNIQUE INDEX CONCURRENTLY ix_events_returns_queue ON events (returns_status)"))
            assert c.execute(text("""SELECT i.indisvalid FROM pg_index i JOIN pg_class x ON x.oid = i.indexrelid
                                     WHERE x.relname = 'ix_events_returns_queue'""")).scalar() is False
            assert c.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0012"
        alembic("upgrade", "head")  # the documented recovery: just run it again
        with eng.connect() as c:
            assert c.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0013"
            assert c.execute(text("""SELECT i.indisvalid AND NOT i.indisunique FROM pg_index i JOIN pg_class x
                                     ON x.oid = i.indexrelid WHERE x.relname = 'ix_events_returns_queue'""")).scalar() is True
        alembic("downgrade", "0012")
        alembic("upgrade", "head")
        eng.dispose()
    finally:
        with admin.connect() as c:
            c.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
