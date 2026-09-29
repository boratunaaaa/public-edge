"""Return queue (0013): deterministic next-eligible time, priority tiers, vendor lane, confirmed absences, delisted
vendor parser; 13F identity (9-character CUSIP, name check) and ticker population; ca_grants failed-run health."""
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text

from app.backtest.jobs import HORIZONS, next_eligible_at
from tests.test_db import URL

db = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")
NOW = datetime(2026, 9, 29, 12, tzinfo=timezone.utc)


# ------------------------------------------------------------------ pure pieces

def test_next_eligible_follows_nyse_sessions():
    from app.backtest.jobs import horizon_ready_at
    entry = date(2026, 8, 3)  # Monday
    # 60d ends Fri 2026-10-02 -> that session's close (16:00 ET = 20:00 UTC) + EOD lag
    assert horizon_ready_at(entry, 60) == datetime(2026, 10, 2, 22, tzinfo=timezone.utc)
    now = datetime(2026, 9, 29, 12, tzinfo=timezone.utc)
    assert next_eligible_at(entry, {1, 7, 30}, now.date(), now) == datetime(2026, 10, 2, 22, tzinfo=timezone.utc)
    assert next_eligible_at(entry, set(HORIZONS), now.date(), now) is None


def test_elapsed_but_missing_horizon_is_a_gap_recheck_not_every_round():
    now = datetime(2026, 9, 29, 12, tzinfo=timezone.utc)
    entry = date(2026, 6, 1)  # 7d / 30d / 90d should exist but are missing (data gap); 180d is months away
    assert next_eligible_at(entry, {1, 60}, now.date(), now) == now + timedelta(days=7)
    # a future horizon that becomes ready before the gap recheck wins
    assert next_eligible_at(date(2026, 8, 3), {1, 30}, now.date(), now) == datetime(2026, 10, 2, 22, tzinfo=timezone.utc)


def test_tiingo_error_object_is_classified_not_iterated():
    """Regression: {"detail": ...} used to be iterated -> "string indices must be integers" -> retry forever."""
    from app import http
    from app.securitymaster.vendors import parse_tiingo_prices
    with pytest.raises(http.HttpError) as e:
        parse_tiingo_prices({"detail": "Error: Ticker 'COOP' not found"}, "COOP")
    assert e.value.status == 404
    with pytest.raises(http.HttpError) as e:
        parse_tiingo_prices({"detail": "Error: You have run over your hourly request allocation."}, "K")
    assert e.value.status == 429
    with pytest.raises(http.HttpError) as e:
        parse_tiingo_prices({"unexpected": 1}, "K")
    assert e.value.status == 502
    bars = parse_tiingo_prices([{"date": "2024-01-02T00:00:00.000Z", "open": 1, "high": 2, "low": 1, "close": 2,
                                 "adjClose": 2, "volume": 5}, "garbage"], "K")
    assert len(bars) == 1 and bars[0].date == date(2024, 1, 2)


def test_tiingo_provider_raises_http_error_for_an_object_answer(monkeypatch):
    from app import http
    from app.securitymaster import vendors

    class R:
        def json(self):
            return {"detail": "Error: Ticker 'ENV' not found"}
    monkeypatch.setattr(vendors.http, "get", lambda *a, **k: R())
    with pytest.raises(http.HttpError) as e:
        vendors.TiingoPriceProvider("k").get_historical_prices("ENV", date(2020, 1, 1), date(2020, 2, 1))
    assert e.value.status == 404 and "string indices" not in str(e.value)


def test_names_agree_accepts_renames_and_abbreviations_but_not_other_issuers():
    from app.markets.identity import names_agree
    assert names_agree("WAL-MART STORES INC", ["Walmart Inc.", "WAL MART STORES INC"])
    assert names_agree("NEW YORK CMNTY BANCORP INC", ["FLAGSTAR BANK, NATIONAL ASSOCIATION", "NEW YORK COMMUNITY BANCORP INC"])
    assert names_agree("GLATFELTER CORP", ["Magnera Corp", "GLATFELTER CORP"])
    assert names_agree("New Mtn Fin Corp", ["New Mountain Finance Corp"])
    assert names_agree("PAR PAC HOLDINGS INC", ["PAR PACIFIC HOLDINGS, INC."])
    assert names_agree("CTRI", ["Centuri Holdings, Inc.", "CTRI"])  # the filer typed the ticker
    assert names_agree("MSM 04/17/2025 PUT 75", ["MSC INDUSTRIAL DIRECT CO INC", "MSM"])
    assert not names_agree("SINCLAIR INC", ["VIASAT INC", "VSAT"])  # filer mislabel: kept for review
    assert not names_agree("Deerfield Management Co LP", ["Some Biotech Inc", "SBIO"])
    assert not names_agree("COURSERA INC", ["HEALTHSTREAM INC"])
    assert not names_agree("ENVISTA HOLDINGS CORP", ["ENvue Medical, Inc."])


# ------------------------------------------------------------------ DB helpers

def _prices(monkeypatch, *, missing=(), fail=(), calls=None, end=None):
    """Yahoo stand-in: 404 for `missing`, 503 for `fail`, a steady series otherwise (counts requests)."""
    from app import http
    from app.marketdata import provider as prov
    from tests.test_backtest import series

    class P(prov.MarketDataProvider):
        name = "yahoo"

        def get_historical_prices(self, symbol, start, end_):
            if calls is not None:
                calls.append(symbol)
            if symbol in missing:
                raise http.HttpError("x", 404, "No data found, symbol may be delisted")
            if symbol in fail:
                raise http.HttpError("x", 503, "unavailable")
            return [b for b in series(date(2024, 1, 2), 900, base=50) if start <= b.date <= end_]
    monkeypatch.setattr(prov, "get_provider", lambda: P())


def _event(c, key, cik, ticker, d, **cols):
    from app.db.store import create_event
    eid = create_event(c, event_type=cols.pop("event_type", "INSIDER_OPEN_MARKET_BUY"), dedupe_key=key, public_date=d,
                       ticker=ticker, issuer_cik=cik, title="t", payload={}, source_url="u",
                       public_at=datetime(d.year, d.month, d.day, 21, tzinfo=timezone.utc), is_backfill=True)
    if cols:
        sets = ", ".join(f"{k} = :{k}" for k in cols)
        c.execute(text(f"UPDATE events SET {sets} WHERE id = :i"), {**cols, "i": eid})
    return eid


def _row(engine, eid):
    from app.db.models import events
    with engine.connect() as c:
        return c.execute(select(events).where(events.c.id == eid)).mappings().one()


def _queue(engine, now=None, **kw):
    from app.backtest.jobs import queue_select
    with engine.connect() as c:
        return [r[0] for r in c.execute(queue_select(now or datetime.now(timezone.utc), **kw))]


# ------------------------------------------------------------------ queue selection / priority

@db
@pytest.mark.usefixtures("clean")
def test_partial_is_not_requeued_before_its_next_horizon_and_is_when_due(engine):
    now = datetime.now(timezone.utc)
    with engine.begin() as c:
        eid = _event(c, "p1", "0001953926", "ZBIO", date(2024, 3, 1), returns_status="partial",
                     returns_checked_at=now - timedelta(days=5), returns_next_at=now + timedelta(days=9))
    assert eid not in _queue(engine)  # the pre-0013 rule (checked 5 days ago > 20 h) would have re-selected it
    with engine.begin() as c:
        c.execute(text("UPDATE events SET returns_next_at = :n WHERE id = :i"), {"n": now - timedelta(minutes=1), "i": eid})
    assert eid in _queue(engine)


@db
@pytest.mark.usefixtures("clean")
def test_tiers_are_interleaved_and_no_cik_order(engine):
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=3)
    with engine.begin() as c:
        retry = _event(c, "r", "0000000001", "AAA", date(2024, 3, 1), returns_status="retry", returns_checked_at=old)
        nodata = _event(c, "n", "0000000002", "BBB", date(2024, 3, 1), returns_status="no_data",
                        returns_checked_at=now - timedelta(days=40))
        partial = _event(c, "p", "0000000003", "CCC", date(2024, 3, 1), returns_status="partial", returns_checked_at=old,
                         returns_next_at=now - timedelta(hours=1))
        fresh = [_event(c, f"f{i}", f"00000001{i:02d}", f"F{i}", date(2024, 3, 1)) for i in range(12)]
    from app.backtest.jobs import select_queue
    with engine.connect() as c:
        q = [r[0] for r in select_queue(c, datetime.now(timezone.utc), limit=100)]
    # weighted round robin 6 : 2 : 1 : 1 - due partial / retry / re-verification are served in the FIRST round
    assert set(q[:6]) <= set(fresh) and q[6:9] == [partial, retry, nodata] and set(q[9:]) <= set(fresh)
    # inside a tier the order is an issuer hash, not the CIK: low CIKs are not systematically first
    ciks = {e: f"00000001{i:02d}" for i, e in enumerate(fresh)}
    fq = [e for e in q if e in ciks]
    assert [ciks[e] for e in fq] != sorted(ciks.values())


@db
@pytest.mark.usefixtures("clean")
def test_fresh_partial_gets_deterministic_next_at_and_is_not_repriced_until_then(engine, monkeypatch):
    from app.backtest import jobs
    _prices(monkeypatch)
    d = date.today() - timedelta(days=45)
    with engine.begin() as c:
        eid = _event(c, "z", "0001953926", "ZBIO", d)
    assert jobs.compute_pending(engine)["processed"] == 1
    r = _row(engine, eid)
    assert r["returns_status"] == "partial"
    with engine.connect() as c:
        entry, done = c.execute(text("SELECT min(entry_date), array_agg(horizon_days) FROM event_returns WHERE event_id = :i"),
                                {"i": eid}).one()
    assert set(done) == {1, 7, 30}
    assert r["returns_next_at"] == jobs.horizon_ready_at(entry, 60)  # NYSE session of the 60d exit, not a calendar day
    assert jobs.compute_pending(engine).get("candidates", 0) == 0  # nothing new until the 60d horizon has ended
    with engine.begin() as c:  # the 60d horizon ends
        c.execute(text("UPDATE events SET returns_next_at = now() - interval '1 minute' WHERE id = :i"), {"i": eid})
    assert jobs.compute_pending(engine)["processed"] == 1


# ------------------------------------------------------------------ failures

@db
@pytest.mark.usefixtures("clean")
def test_temporary_failure_is_retried_later(engine, monkeypatch):
    from app.backtest import jobs
    _prices(monkeypatch, fail={"ZBIO"})
    with engine.begin() as c:
        eid = _event(c, "t", "0001953926", "ZBIO", date(2024, 3, 1))
    assert jobs.compute_pending(engine)["retry"] == 1
    r = _row(engine, eid)
    assert r["returns_status"] == "retry" and "503" in r["returns_error"]
    assert timedelta(hours=19) < r["returns_next_at"] - r["returns_checked_at"] <= timedelta(hours=jobs.RECHECK_HOURS)
    assert eid not in _queue(engine)
    _prices(monkeypatch)  # provider recovers, backoff elapses
    with engine.begin() as c:
        c.execute(text("UPDATE events SET returns_next_at = now() - interval '1 minute' WHERE id = :i"), {"i": eid})
    assert jobs.compute_pending(engine)["processed"] == 1


@db
@pytest.mark.usefixtures("clean")
def test_permanent_404_is_not_requested_again(engine, monkeypatch):
    from app.backtest import jobs
    from tests.test_free_prices import OLD, _company
    calls: list[str] = []
    _prices(monkeypatch, missing={"OLDB"}, calls=calls)
    with engine.begin() as c:
        _company(c)
        e1 = _event(c, "a", OLD, "OLDB", date(2023, 1, 10))
        e2 = _event(c, "b", OLD, "OLDB", date(2023, 1, 12))
    jobs.compute_pending(engine)
    assert calls.count("OLDB") == 1  # second event of the same issuer/symbol: the per-run cache
    assert {_row(engine, e)["returns_status"] for e in (e1, e2)} == {"no_data"}
    with engine.begin() as c:  # monthly re-verification comes round
        c.execute(text("UPDATE events SET returns_next_at = now() - interval '1 minute'"))
    jobs.compute_pending(engine)
    assert calls.count("OLDB") == 1  # a later run: the recorded absence (TTL) - no new provider request


# ------------------------------------------------------------------ vendor lane

@db
@pytest.mark.usefixtures("clean")
def test_vendor_work_never_blocks_the_main_lane(engine, monkeypatch):
    from app.backtest import jobs
    from app.securitymaster import vendors
    from tests.test_free_prices import OLD, _company, series
    _prices(monkeypatch, missing={"OLDB"})
    vendor_calls: list[str] = []

    class Vendor:
        name = "tiingo"

        def get_historical_prices(self, symbol, start, end):
            vendor_calls.append(symbol)
            return [b for b in series(date(2022, 6, 1), 200, 20.0, 0.05) if start <= b.date <= end]
    monkeypatch.setattr(vendors, "delisted_price_provider", lambda: Vendor())
    with engine.begin() as c:
        _company(c)
        delisted = _event(c, "d", OLD, "OLDB", date(2023, 1, 10))
        normal = _event(c, "n", "0001953926", "ZBIO", date(2024, 3, 1))
    r = jobs.compute_pending(engine)
    assert vendor_calls == []  # the main lane never calls (or waits for) the vendor
    assert r["processed"] == 1 and r["vendor_queued"] == 1
    assert _row(engine, normal)["returns_status"] in ("partial", "complete")
    assert _row(engine, delisted)["returns_status"] == "vendor_wait"
    assert delisted not in _queue(engine)  # not in the main lane any more
    v = jobs.compute_vendor_lane(engine)
    assert vendor_calls == ["OLDB"] and v["processed"] == 1
    assert _row(engine, delisted)["returns_status"] in ("partial", "complete")


@db
@pytest.mark.usefixtures("clean")
def test_vendor_404_is_recorded_and_quota_stops_the_lane(engine, monkeypatch):
    from app import http
    from app.backtest import jobs
    from app.securitymaster import vendors
    from tests.test_free_prices import OLD, _company
    _prices(monkeypatch, missing={"OLDB", "OLDC"})
    mode = {"m": "404"}
    calls: list[str] = []

    class Vendor:
        name = "tiingo"

        def get_historical_prices(self, symbol, start, end):
            calls.append(symbol)
            raise http.HttpError("x", 404 if mode["m"] == "404" else 429, "tiingo")
    monkeypatch.setattr(vendors, "delisted_price_provider", lambda: Vendor())
    with engine.begin() as c:
        _company(c)
        _company(c, cik="0000000556", name="Other Old Inc", symbol="OLDC")
        a = _event(c, "a", OLD, "OLDB", date(2023, 1, 10))
    jobs.compute_pending(engine)
    jobs.compute_vendor_lane(engine)
    assert calls == ["OLDB"] and _row(engine, a)["returns_status"] == "no_data"
    with engine.begin() as c:
        c.execute(text("UPDATE events SET returns_status = 'vendor_wait', returns_next_at = NULL WHERE id = :i"), {"i": a})
    jobs.compute_vendor_lane(engine)
    assert calls == ["OLDB"]  # confirmed vendor absence: no second request
    mode["m"] = "429"
    with engine.begin() as c:
        b = _event(c, "b", "0000000556", "OLDC", date(2023, 1, 10), returns_status="vendor_wait")
        _event(c, "b2", "0000000556", "OLDC", date(2023, 1, 11), returns_status="vendor_wait")
    r = jobs.compute_vendor_lane(engine)
    assert r.get("stopped") == "vendor quota" and calls.count("OLDC") == 1
    rb = _row(engine, b)
    assert rb["returns_status"] == "vendor_wait" and rb["returns_next_at"] > datetime.now(timezone.utc)


# ------------------------------------------------------------------ 13F identity / ticker

def _registry(c, cik, name, symbol, cusip):
    cid = c.execute(text("""INSERT INTO companies (cik, name, name_norm) VALUES (:c, :n, lower(:n))
                            ON CONFLICT (cik) DO UPDATE SET name = EXCLUDED.name RETURNING id"""), {"c": cik, "n": name}).scalar()
    c.execute(text("INSERT INTO tickers (symbol, company_id, cusip) VALUES (:s, :i, :cu)"), {"s": symbol, "i": cid, "cu": cusip})


def _cover(c, acc, issuer_cik, cusip, name):
    c.execute(text("""INSERT INTO activist_filings (accession, form_type, is_13d, is_amendment, filed_date, source_url,
                      issuer_cik, issuer_name, cusip) VALUES (:a, 'SCHEDULE 13D', true, false, '2024-01-02', 'u', :c, :n, :cu)"""),
              {"a": acc, "c": issuer_cik, "n": name, "cu": cusip})


@db
@pytest.mark.usefixtures("clean")
def test_cusip_prefix_collision_is_review_not_a_silent_match(engine):
    """Envista 29415F104 vs a registry row of ENvue Medical with the corrupt CUSIP 29415F100 (wrong check digit)."""
    from app.markets.identity import cusip_check_digit_ok, cusip_to_cik
    assert cusip_check_digit_ok("29415F104") and not cusip_check_digit_ok("29415F100")
    with engine.begin() as c:
        _registry(c, "0001326706", "ENvue Medical, Inc.", "FEED", "29415F100")
        cik, conf, why = cusip_to_cik(c, "29415F104", "ENVISTA HOLDINGS CORP")
        assert (cik, conf) == ("0001326706", "LOW") and why.startswith("REVIEW_REQUIRED:CUSIP_PREFIX_ONLY")
        _registry(c, "0000320193", "Apple Inc.", "AAPL", "037833100")
        assert cusip_to_cik(c, "037833100", "APPLE INC")[:2] == ("0000320193", "MEDIUM")  # exact 9 characters
        _registry(c, "0000000001", "Placeholder Co", "PLC", "000000000")
        assert cusip_to_cik(c, "000000000", "ANY")[:2] == (None, "NONE")  # a placeholder never identifies anyone


@db
@pytest.mark.usefixtures("clean")
def test_two_sources_with_the_same_wrong_cusip_are_caught_by_the_name(engine):
    """Coursera 22266M104 attributed to HealthStream by both the 13D cover data and the registry."""
    from app.markets.identity import cusip_to_cik
    with engine.begin() as c:
        _registry(c, "0001095565", "HealthStream, Inc.", "HSTM", "22266M104")
        _cover(c, "0000000000-24-000001", "0001095565", "22266M104", "HealthStream, Inc.")
        assert cusip_to_cik(c, "22266M104")[:2] == ("0001095565", "HIGH")  # no name given: unchanged behaviour
        cik, conf, why = cusip_to_cik(c, "22266M104", "COURSERA INC")
        assert conf == "LOW" and why == "REVIEW_REQUIRED:NAME_MISMATCH:13D_COVER+SEC_REGISTRY"
        # a rename is not a contradiction: the security master keeps the former name
        c.execute(text("""INSERT INTO security_master (cik, name, former_names) VALUES
                          ('0000041719', 'Magnera Corp', '[{"name": "GLATFELTER CORP"}]')"""))
        _registry(c, "0000041719", "Magnera Corp", "MAGN", "377320106")
        assert cusip_to_cik(c, "377320106", "GLATFELTER CORPORATION")[1] == "MEDIUM"


@db
@pytest.mark.usefixtures("clean")
def test_13f_ticker_is_filled_in_a_fresh_process(engine):
    from app.entities.registry import resolver
    from app.signals.v2_families import _ticker
    resolver.cik_to_ticker, resolver._loaded_at = {}, 0.0  # a fresh v2 historical runner: nothing loaded yet
    with engine.connect() as c:
        assert _ticker(c, "0001953926") == "ZBIO"
        assert _ticker(c, None) is None


@db
@pytest.mark.usefixtures("clean")
def test_low_identity_13f_event_is_marked_for_review(engine):
    from app.signals.v2_families import accumulation_events
    with engine.begin() as c:
        _registry(c, "0001326706", "ENvue Medical, Inc.", "FEED", "29415F100")
        q, p = date(2024, 3, 31), date(2023, 12, 31)
        for i in range(3):
            m = f"000000990{i}"
            for per, acc in ((p, f"{m}-24-00000{i}"), (q, f"{m}-24-10000{i}")):
                c.execute(text("""INSERT INTO inst_filings_13f (accession, manager_cik, manager_name, period, filed_date,
                                  available_at, entries, submission_type, source) VALUES (:a, :m, 'M', :p, :f, :av, 20, '13F-HR', 's')"""),
                          {"a": acc, "m": m, "p": per, "f": per + timedelta(days=40),
                           "av": datetime.combine(per + timedelta(days=40), datetime.min.time(), timezone.utc) + timedelta(hours=i)})
                cu = "29415F104" if per == q else "11111111"
                c.execute(text("""INSERT INTO inst_holdings_13f (accession, line, cusip, issuer_name, shares, value)
                                  VALUES (:a, 1, :cu, 'ENVISTA HOLDINGS CORP', 100, 1000)"""), {"a": acc, "cu": cu})
        ids = accumulation_events(c, q, is_backfill=True)
        ev = c.execute(text("SELECT needs_review, identity_confidence, payload FROM events WHERE id = ANY(:i)"), {"i": ids}).mappings().all()
    assert len(ev) == 1 and ev[0]["needs_review"] is True and ev[0]["identity_confidence"] == "LOW"
    assert ev[0]["payload"]["identity_review"].startswith("REVIEW_REQUIRED")


# ------------------------------------------------------------------ source health / repair

@db
@pytest.mark.usefixtures("clean")
def test_failed_ca_grants_run_is_not_reported_healthy(engine, monkeypatch):
    from app.jobs import v2_steps
    from app.sources.ca.grants_source import CanadaGrantsSource

    def boom(self, eng):
        raise RuntimeError("peer closed connection without sending complete message body")
    monkeypatch.setattr(CanadaGrantsSource, "run_live", boom)
    with pytest.raises(RuntimeError):
        v2_steps.ca_grants_weekly(engine)
    with engine.connect() as c:
        h = c.execute(text("SELECT * FROM source_health WHERE source = 'ca_grants'")).mappings().one()
        d = c.execute(text("SELECT status FROM source_daily_status WHERE source = 'ca_grants'")).scalar()
    assert h["health_status"] == "DEGRADED" and h["consecutive_failures"] == 1 and h["last_error"]
    assert h["watermark"] == {"initial_load_complete": False} and d == "SOURCE_UNAVAILABLE"


@db
@pytest.mark.usefixtures("clean")
def test_repair_dry_run_writes_nothing_and_apply_fixes_the_envista_case(engine):
    from app.jobs import return_repair
    with engine.begin() as c:
        _registry(c, "0001326706", "ENvue Medical, Inc.", "FEED", "29415F100")
        eid = _event(c, "13fx", "0001326706", None, date(2024, 5, 15), event_type="INST_13F_EXIT",
                     returns_status="complete", company_name="ENVISTA HOLDINGS CORP", identity_confidence="MEDIUM")
        c.execute(text("""UPDATE events SET payload = '{"cusip": "29415F104"}' WHERE id = :i"""), {"i": eid})
        c.execute(text("""INSERT INTO event_returns (event_id, horizon_days, entry_date, entry_price, exit_date, exit_price, ret)
                          VALUES (:i, 30, '2024-05-16', 10, '2024-06-14', 11, 0.1)"""), {"i": eid})
        p = _event(c, "part", "0001953926", "ZBIO", date(2026, 8, 1), returns_status="partial")
        c.execute(text("""INSERT INTO event_returns (event_id, horizon_days, entry_date, entry_price, exit_date, exit_price, ret)
                          VALUES (:i, 1, '2026-08-03', 10, '2026-08-04', 11, 0.1)"""), {"i": p})
    dry = return_repair.run(engine, apply=False)
    assert dry["mode"] == "DRY_RUN" and dry["identity_13f"]["by_kind"] == {"REVIEW": 1}
    assert dry["identity_13f"]["stale_or_wrong_returns"] == 1 and dry["ticker_13f"]["would_fill"] == 1
    assert dry["partial_next_at"]["rows"] == 1
    r = _row(engine, eid)
    assert r["needs_review"] is False and r["returns_status"] == "complete" and r["ticker"] is None  # nothing written
    assert _row(engine, p)["returns_next_at"] is None
    return_repair.run(engine, apply=True)
    r = _row(engine, eid)
    assert r["needs_review"] is True and r["missing_reason"] == "IDENTITY_REVIEW" and r["ticker"] == "FEED"
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM event_returns WHERE event_id = :i"), {"i": eid}).scalar() == 0
    assert _row(engine, p)["returns_next_at"] is not None
