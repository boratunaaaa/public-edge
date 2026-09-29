"""Return eligibility (identity safety) vs validation eligibility (any review flag) - app.core.event_policy."""
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from tests.test_db import URL
from tests.test_return_queue import _event, _prices, _registry, _row

db = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")


def test_python_and_sql_predicates_agree_on_the_rules():
    from app.core.event_policy import is_identity_unsafe, validation_excluded
    assert is_identity_unsafe({"identity_confidence": "LOW"}) and is_identity_unsafe({"identity_confidence": "NONE"})
    assert is_identity_unsafe({"identity_confidence": "HIGH", "payload": {"identity_review": "REVIEW_REQUIRED:X"}})
    assert is_identity_unsafe({"identity_confidence": None, "missing_reason": "IDENTITY_REVIEW"})
    # a review flag alone is NOT an identity problem
    assert not is_identity_unsafe({"identity_confidence": "HIGH", "needs_review": True, "payload": {}})
    assert not is_identity_unsafe({"identity_confidence": None, "needs_review": True})
    # ... but it always excludes the event from verdict paths
    assert validation_excluded({"needs_review": True, "identity_confidence": "HIGH"})
    assert not validation_excluded({"needs_review": False})


def _q(engine):
    from app.backtest.jobs import select_queue
    with engine.connect() as c:
        return [r[0] for r in select_queue(c, datetime.now(timezone.utc), limit=100)]


@db
@pytest.mark.usefixtures("clean")
@pytest.mark.parametrize("ic", ["HIGH", "MEDIUM", None])
def test_review_with_safe_identity_enters_the_queue_and_is_priced(engine, monkeypatch, ic):
    from app.backtest import jobs
    _prices(monkeypatch)
    with engine.begin() as c:
        eid = _event(c, "r", "0001953926", "ZBIO", date(2024, 3, 1), needs_review=True, identity_confidence=ic)
    assert eid in _q(engine)
    assert jobs.compute_pending(engine)["processed"] == 1
    assert _row(engine, eid)["returns_status"] in ("partial", "complete")
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM event_returns WHERE event_id = :i"), {"i": eid}).scalar() > 0


@db
@pytest.mark.usefixtures("clean")
def test_review_with_low_identity_is_never_queued(engine):
    with engine.begin() as c:
        eid = _event(c, "l", "0001953926", "ZBIO", date(2024, 3, 1), needs_review=True, identity_confidence="LOW")
    assert eid not in _q(engine)


@db
@pytest.mark.usefixtures("clean")
@pytest.mark.parametrize("cols", [dict(identity_confidence="NONE"), dict(missing_reason="IDENTITY_REVIEW"),
                                  dict(payload='{"identity_review": "REVIEW_REQUIRED:NAME_MISMATCH"}')])
def test_unresolved_identity_enters_no_return_path(engine, cols):
    from app.backtest.jobs import _fence, claim_vendor
    now = datetime.now(timezone.utc)
    with engine.begin() as c:
        main = _event(c, "m", "0001953926", "ZBIO", date(2024, 3, 1), returns_status="partial",
                      returns_checked_at=now - timedelta(days=2), returns_next_at=now - timedelta(hours=1))
        vend = _event(c, "v", "0001737450", "OPRA", date(2024, 3, 1), returns_status="vendor_wait")
        retry = _event(c, "t", "0001846069", "KIND", date(2024, 3, 1), returns_status="retry",
                       returns_checked_at=now - timedelta(days=2), returns_next_at=now - timedelta(hours=1))
        for e in (main, vend, retry):
            sets = ", ".join(f"{k} = CAST(:{k} AS jsonb)" if k == "payload" else f"{k} = :{k}" for k in cols)
            c.execute(text(f"UPDATE events SET {sets} WHERE id = :i"), {**cols, "i": e})
    assert not set(_q(engine)) & {main, vend, retry}  # main lane: partial + retry paths
    with engine.begin() as c:
        assert claim_vendor(c, now, 10) == []  # vendor lane
        assert not _fence(c, main, "ZBIO", "0001953926", False)  # and the last-moment fence


@db
@pytest.mark.usefixtures("clean")
def test_identity_safe_review_event_keeps_returns_but_stays_out_of_verdict_paths(engine, monkeypatch):
    from app.backtest import jobs, placebo
    from app.paper.proposals import evaluate_gate
    _prices(monkeypatch)
    with engine.begin() as c:
        eid = _event(c, "s", "0001953926", "ZBIO", date(2023, 1, 10), needs_review=True, identity_confidence="HIGH")
    assert jobs.compute_pending(engine)["processed"] == 1  # raw returns are computed
    with engine.begin() as c:
        c.execute(text("UPDATE events SET price_symbol = 'ZBIO' WHERE id = :i"), {"i": eid})
        stats = jobs.event_type_stats(c, "INSIDER_OPEN_MARKET_BUY", horizons=(90,))
        overview = jobs.stats_overview(c)
        monkeypatch.setattr(placebo, "_excess", lambda *a, **k: 0.0)
        pl = placebo.family_vs_placebo(c, "INSIDER_OPEN_MARKET_BUY", horizon=90)
        ev = dict(c.execute(text("SELECT * FROM events WHERE id = :i"), {"i": eid}).mappings().one())
        gate = evaluate_gate(c, ev, family_ok=(True, "test"), price_basis="current_ticker_same_cik")
    assert stats[90]["n"] == 0 and overview == [] and pl["companies"] == 0
    assert not gate.ok and "UNRESOLVED_SECURITY_MAPPING" in gate.failed


@db
@pytest.mark.usefixtures("clean")
def test_envista_and_coursera_review_cases_stay_blocked(engine):
    """The real generation / repair paths mark them LOW -> never priced."""
    from app.jobs import return_repair
    from tests.test_return_queue import _cover
    with engine.begin() as c:
        _registry(c, "0001326706", "ENvue Medical, Inc.", "FEED", "29415F100")
        _registry(c, "0001095565", "HealthStream, Inc.", "HSTM", "22266M104")
        _cover(c, "0000000000-24-000001", "0001095565", "22266M104", "HealthStream, Inc.")
        ids = []
        for k, cik, tk, cu, name in (("env", "0001326706", "FEED", "29415F104", "ENVISTA HOLDINGS CORP"),
                                     ("crs", "0001095565", "HSTM", "22266M104", "COURSERA INC")):
            e = _event(c, k, cik, tk, date(2024, 5, 15), event_type="INST_13F_EXIT", company_name=name,
                       identity_confidence="MEDIUM")
            c.execute(text("UPDATE events SET payload = CAST(:p AS jsonb) WHERE id = :i"), {"p": f'{{"cusip": "{cu}"}}', "i": e})
            ids.append(e)
    assert set(ids) <= set(_q(engine))  # before the repair (the production state today) they would be priced
    return_repair.run(engine, items=("identity_13f",), apply=True)
    assert not set(ids) & set(_q(engine))
    for e in ids:
        assert _row(engine, e)["identity_confidence"] == "LOW"


@db
@pytest.mark.usefixtures("clean")
@pytest.mark.parametrize("et", ["INSIDER_OPEN_MARKET_BUY", "CLUSTER_INSIDER_BUY", "CONGRESS_BUY"])
def test_existing_review_types_are_not_frozen_when_identity_is_safe(engine, et):
    now = datetime.now(timezone.utc)
    with engine.begin() as c:
        safe = _event(c, "safe", "0001953926", "ZBIO", date(2024, 3, 1), event_type=et, needs_review=True,
                      identity_confidence="MEDIUM", returns_status="partial", returns_checked_at=now - timedelta(days=2),
                      returns_next_at=now - timedelta(hours=1))
        # the 2026-09-28 V2 identity audit flag (TICKER_OWNED_BY_OTHER_ISSUER) is an identity flag -> LOW -> blocked
        audit = _event(c, "audit", "0001737450", "OPRA", date(2024, 3, 1), event_type=et, needs_review=True,
                       identity_confidence="LOW", returns_status="partial", returns_checked_at=now - timedelta(days=2),
                       returns_next_at=now - timedelta(hours=1))
    q = _q(engine)
    assert safe in q and audit not in q


@db
@pytest.mark.usefixtures("clean")
def test_repair_reports_both_eligibilities(engine):
    from app.jobs import return_repair
    with engine.begin() as c:
        _event(c, "a", "0001953926", "ZBIO", date(2024, 3, 1), needs_review=True, identity_confidence="HIGH")
        _event(c, "b", "0001737450", "OPRA", date(2024, 3, 1), needs_review=True, identity_confidence="LOW")
        _event(c, "c", "0001846069", "KIND", date(2024, 3, 1))
    r = return_repair.run(engine, items=("return_eligibility",))["return_eligibility"]
    assert r["return_excluded_identity_unsafe"] == 1 and r["identity_safe_needs_review"] == 1
    assert r["validation_excluded_needs_review"] == 2
