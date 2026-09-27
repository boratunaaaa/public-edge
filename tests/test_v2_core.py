"""V2 core: available_at contract, shared rate limits, source health (failure vs true zero), raw lake, manifests."""
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.core import manifest, ratelimit, rawlake
from app.core.provenance import available_at, check_entry, end_of_local_day
from app.core.source_state import RunOutcome, classify_exception, daily_status, health_status, record_run
from app.marketdata.calendar import first_tradable_session

UTC = timezone.utc


# ---------------------------------------------------------------- available_at (pure)

def test_exact_timestamp_is_available_at():
    t = datetime(2026, 3, 2, 21, 5, tzinfo=UTC)
    assert available_at(public_date=date(2026, 3, 2), public_at=t, date_quality="exact") == t


def test_date_only_is_end_of_new_york_day_and_enters_next_session():
    a = available_at(public_date=date(2026, 3, 6), date_quality="date")  # Friday
    assert a == datetime(2026, 3, 7, 4, 59, 59, tzinfo=UTC)  # 23:59:59 EST
    assert first_tradable_session(a.date(), a) == date(2026, 3, 9)  # Monday open, never Friday


def test_date_only_istanbul_uses_local_day():
    a = available_at(public_date=date(2026, 3, 6), date_quality="date", country="TR")
    assert a == datetime(2026, 3, 6, 20, 59, 59, tzinfo=UTC)  # 23:59:59 TRT (UTC+3)


def test_first_seen_live_uses_observation_but_backfill_never_does():
    seen = datetime(2026, 3, 2, 13, 0, tzinfo=UTC)
    assert available_at(public_date=date(2026, 3, 2), date_quality="first_seen", first_seen_at=seen) == seen
    b = available_at(public_date=date(2026, 3, 2), date_quality="first_seen", first_seen_at=seen, is_backfill=True)
    assert b == end_of_local_day(date(2026, 3, 2))


def test_estimated_timestamp_is_not_trusted_as_exact():
    t = datetime(2026, 3, 2, 12, 0, tzinfo=UTC)
    assert available_at(public_date=date(2026, 3, 2), public_at=t, date_quality="estimated") == end_of_local_day(date(2026, 3, 2))


def test_look_ahead_guard():
    avail = datetime(2026, 3, 2, 21, 0, tzinfo=UTC)
    check_entry(datetime(2026, 3, 3, 14, 30, tzinfo=UTC), avail)
    with pytest.raises(ValueError):
        check_entry(datetime(2026, 3, 2, 14, 30, tzinfo=UTC), avail)


# ---------------------------------------------------------------- health (pure)

def test_health_status_vocabulary():
    now = datetime(2026, 9, 28, tzinfo=UTC)
    kw = dict(expected_fp=None, fp=None, last_success=now, stale_after=timedelta(hours=2), now=now)
    assert health_status(failure_kind=None, consecutive_failures=0, **kw) == "HEALTHY"
    assert health_status(failure_kind="ERROR", consecutive_failures=1, **kw) == "DEGRADED"
    assert health_status(failure_kind="AUTH_REQUIRED", consecutive_failures=5, **kw) == "AUTH_REQUIRED"
    assert health_status(failure_kind="BLOCKED", consecutive_failures=5, **kw) == "BLOCKED"
    assert health_status(failure_kind=None, consecutive_failures=0, **{**kw, "expected_fp": "a", "fp": "b"}) == "SCHEMA_CHANGED"
    assert health_status(failure_kind=None, consecutive_failures=0, **{**kw, "last_success": now - timedelta(hours=3)}) == "STALE"


def test_failure_is_never_a_true_zero():
    assert daily_status(runs=3, failures=3, events=0, last_kind="ERROR") == "SOURCE_UNAVAILABLE"
    assert daily_status(runs=3, failures=3, events=0, last_kind="API_QUOTA") == "API_QUOTA"
    assert daily_status(runs=3, failures=0, events=0, last_kind=None) == "TRUE_NO_EVENT"
    assert daily_status(runs=3, failures=1, events=0, last_kind="ERROR") == "PARTIAL"
    assert daily_status(runs=3, failures=0, events=2, last_kind=None) == "OK_EVENTS"


def test_classify_exception():
    from app.http import HttpError
    assert classify_exception(HttpError("u", 401, "x"))[0] == "AUTH_REQUIRED"
    assert classify_exception(HttpError("u", 403, "x"))[0] == "BLOCKED"
    assert classify_exception(ratelimit.QuotaExhausted("tiingo"))[0] == "API_QUOTA"
    assert classify_exception(RuntimeError("boom"))[0] == "ERROR"


# ---------------------------------------------------------------- DB-backed

def test_create_event_stores_available_at_never_transaction_date(engine, clean):
    from app.db.store import create_event
    with engine.begin() as c:
        eid = create_event(c, event_type="INSIDER_OPEN_MARKET_BUY", dedupe_key="T:1", public_date=date(2026, 3, 6),
                           transaction_date=date(2026, 3, 2), title="t", payload={}, source_url="https://www.sec.gov/x",
                           is_backfill=True)
        r = c.execute(text("SELECT available_at, first_seen_at, country FROM events WHERE id = :i"), {"i": eid}).one()
    assert r[0] == end_of_local_day(date(2026, 3, 6)) and r[1] is None and r[2] == "US"


def test_shared_rate_limit_serialises_slots_and_enforces_quota(engine, clean):
    with engine.begin() as c:
        c.execute(text("INSERT INTO rate_limits (key, min_interval_s, daily_limit) VALUES ('t', 0.2, 3)"))
    with engine.begin() as c:
        w = [ratelimit.acquire("t", conn=c, block=False) for _ in range(3)]
    assert w[0] <= 0.01 and w[1] == pytest.approx(0.2, abs=0.05) and w[2] == pytest.approx(0.4, abs=0.05)
    with engine.begin() as c, pytest.raises(ratelimit.QuotaExhausted):
        ratelimit.acquire("t", conn=c, block=False)
    with engine.begin() as c:
        ratelimit.record_429("t", conn=c, backoff_s=30)
        st = {s["key"]: s for s in ratelimit.status(c)}["t"]
    assert st["n_429"] == 1 and st["remaining_today"] == 0


def test_unknown_key_is_created_from_defaults(engine, clean):
    with engine.begin() as c:
        ratelimit.acquire("finra", conn=c, block=False)
        assert c.execute(text("SELECT min_interval_s FROM rate_limits WHERE key = 'finra'")).scalar() == 0.5


def test_record_run_failure_then_success_and_daily_status(engine, clean):
    now = datetime(2026, 9, 28, 12, tzinfo=UTC)
    with engine.begin() as c:
        c.execute(text("INSERT INTO source_health (source, consecutive_failures) VALUES ('s', 1)"))
        hs = record_run(c, "s", RunOutcome(ok=False, failure_kind="ERROR", error="x"), now=now)
        assert hs == "DEGRADED"
        assert c.execute(text("SELECT status FROM source_daily_status WHERE source='s'")).scalar() == "SOURCE_UNAVAILABLE"
        c.execute(text("UPDATE source_health SET consecutive_failures = 0"))
        hs = record_run(c, "s", RunOutcome(ok=True, items=5, events=0, fingerprint="f1"), now=now)
        assert hs == "HEALTHY"
        assert c.execute(text("SELECT status FROM source_daily_status WHERE source='s'")).scalar() == "PARTIAL"
        hs = record_run(c, "s", RunOutcome(ok=True, items=5, events=1, fingerprint="f2"), now=now)
        assert hs == "SCHEMA_CHANGED"  # baseline f1 kept until an operator accepts f2


def test_raw_lake_dedupe_verify_and_retention(engine, clean, tmp_path):
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    with engine.begin() as c:
        a = rawlake.store(c, country="US", source="sec_ftd", doc_key="cnsfails202601a", content=b"x" * 1000, root=tmp_path, now=t0)
        b = rawlake.store(c, country="US", source="sec_ftd", doc_key="cnsfails202601a", content=b"x" * 1000, root=tmp_path, now=t0)
        other = rawlake.store(c, country="US", source="mirror", doc_key="k", content=b"x" * 1000, root=tmp_path, now=t0)
        ev = rawlake.store(c, country="US", source="sec_13f", doc_key="acc", content=b"evidence", root=tmp_path, now=t0)
        rawlake.mark_evidence(c, ev)
        assert a == b and other != a
        assert rawlake.load(c, a) == b"x" * 1000
        assert len(list(tmp_path.rglob("*.gz"))) == 2  # identical bytes stored once
        dry = rawlake.apply_retention(c, hot_days=30, now=t0 + timedelta(days=60))
        assert dry["expired"] == 2 and dry["dry_run"]
        rawlake.apply_retention(c, hot_days=30, dry_run=False, now=t0 + timedelta(days=60))
        assert rawlake.load(c, ev) == b"evidence"
        assert c.execute(text("SELECT count(*) FROM raw_documents WHERE storage_path = ''")).scalar() == 2


def test_manifest_is_reproducible_and_exports_parquet(engine, clean, tmp_path):
    from app.db.store import create_event
    with engine.begin() as c:
        for i in range(3):
            create_event(c, event_type="X", dedupe_key=f"M:{i}", public_date=date(2026, 1, 1 + i), title="t", payload={},
                         source_url="u", is_backfill=True)
        m1 = manifest.build(c, "events", export=True, export_dir=tmp_path)
        m2 = manifest.build(c, "events")
    assert m1["row_count"] == 3 and m1["content_hash"] == m2["content_hash"]
    import pyarrow.parquet as pq
    assert pq.read_table(m1["export_path"]).num_rows == 3
