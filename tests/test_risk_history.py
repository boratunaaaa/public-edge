"""Point-in-time risk-label reconstruction (app.replay.risk_history) and its use inside a replay."""
import os
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.replay.risk_history import (FAMILIES, Coverage, RiskHistoryStore, RiskLabel, active_risks, coverage_policy,
                                     detect_dilution, detect_going_concern_10k, detect_text_hits, reconstruct)

URL = os.environ.get("TEST_DATABASE_URL")
db = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")
UTC = timezone.utc
CIK = "0000000101"


def _profile(gc_sentences: int, v=None):
    from app.tenk.diff import CLASSIFIER_VERSION
    tagged = {str(1000 + i): [["GOING_CONCERN"], True, False, []] for i in range(gc_sentences)}
    return {"MDA": {"n": 10, "hashes": [], "tagged": tagged, "text_hash": "x"}, "_meta": {"v": v or CLASSIFIER_VERSION}}


def _tenk(acc, accepted, gc, form="10-K", v=None):
    return {"accession": acc, "cik": CIK, "form": form, "accepted_at": accepted, "filing_date": accepted.date(),
            "source_url": f"https://www.sec.gov/{acc}", "profile": _profile(gc, v)}


GC_2021 = datetime(2021, 3, 15, 20, 5, tzinfo=UTC)


# ------------------------------------------------------------------ 1-2. visibility of a label

def test_a_2020_moment_cannot_see_a_2021_going_concern_label():
    labels = detect_going_concern_10k([_tenk("a-2020", datetime(2020, 3, 10, 20, tzinfo=UTC), 0), _tenk("a-2021", GC_2021, 3)])
    assert [x.source_document_id for x in labels] == ["a-2021"]
    assert active_risks(labels, CIK, datetime(2020, 12, 31, tzinfo=UTC)) == {}
    assert "GOING_CONCERN" in active_risks(labels, CIK, datetime(2021, 6, 1, tzinfo=UTC))


def test_a_label_appears_exactly_when_its_document_becomes_public():
    labels = detect_going_concern_10k([_tenk("a-2021", GC_2021, 3)])
    assert labels[0].available_at == GC_2021 and labels[0].valid_from == GC_2021
    assert active_risks(labels, CIK, GC_2021 - timedelta(microseconds=1)) == {}
    assert active_risks(labels, CIK, GC_2021)["GOING_CONCERN"]["detected_at"] == GC_2021


# ------------------------------------------------------------------ 3. amendments

def test_a_later_amendment_does_not_change_the_earlier_state():
    orig = _tenk("orig", GC_2021, 0)
    amend = _tenk("amend", GC_2021 + timedelta(days=40), 4, form="10-K/A")
    labels = detect_going_concern_10k([amend, orig])  # input order does not matter
    assert active_risks(labels, CIK, GC_2021 + timedelta(days=39)) == {}  # before the amendment was public
    assert "GOING_CONCERN" in active_risks(labels, CIK, GC_2021 + timedelta(days=41))
    # and the reverse: a warning removed by a later report ends when THAT report is public, not before
    w = detect_going_concern_10k([_tenk("w", GC_2021, 3), _tenk("clean", GC_2021 + timedelta(days=365), 0)])
    assert "GOING_CONCERN" in active_risks(w, CIK, GC_2021 + timedelta(days=364))
    assert active_risks(w, CIK, GC_2021 + timedelta(days=366)) == {}


# ------------------------------------------------------------------ 4. current status

def test_current_company_status_cannot_leak_backward():
    """Detectors only take documents: a delisting / today's status is not an input anywhere."""
    import inspect

    from app.replay import risk_history
    for fn in (detect_going_concern_10k, detect_text_hits, detect_dilution, active_risks):
        src = inspect.getsource(fn)
        for forbidden in ("security_master", "delisting", "status", "date.today", "datetime.now", "tickers"):
            assert forbidden not in src, (fn.__name__, forbidden)
    assert "detected_at" not in inspect.getsource(risk_history.detect_text_hits)


# ------------------------------------------------------------------ 5-6. versions and provenance

@db
def test_detector_and_rule_versions_are_recorded():
    st = RiskHistoryStore(URL)
    with st.engine.begin() as c:
        c.execute(text("DROP SCHEMA IF EXISTS risk_hist CASCADE"))
    st.create()
    labels = detect_going_concern_10k([_tenk("a-2021", GC_2021, 3)])
    st.write("t_versions", "GOING_CONCERN_10K", CIK, labels)
    with st.engine.connect() as c:
        row = c.execute(text("SELECT detector, detector_version, rule_version, source_document_id, source_url, available_at "
                             "FROM risk_hist.risk_labels WHERE run_id = 't_versions'")).one()
    assert row[0] == "tenk_gc_present" and row[1] and row[2].startswith("tenk.classifier.v") and row[3] == "a-2021"
    assert row[4] and row[5] == GC_2021


def test_a_risk_label_cannot_exist_without_source_provenance():
    kw = dict(risk_type="GOING_CONCERN", cik=CIK, severity="HIGH", source_kind="x", accepted_at=None,
              available_at=GC_2021, valid_from=GC_2021, valid_to=None, detector="d", detector_version="1",
              rule_version="r", confidence="HIGH")
    with pytest.raises(ValueError):
        RiskLabel(source_document_id="", source_url="u", **kw)
    with pytest.raises(ValueError):
        RiskLabel(source_document_id="a", source_url="", **kw)
    with pytest.raises(ValueError):  # valid before the document was public
        RiskLabel(source_document_id="a", source_url="u", **{**kw, "valid_from": GC_2021 - timedelta(days=1)})


@db
def test_a_staged_risk_row_without_provenance_fails_the_replay():
    from app.replay.errors import ReplayLeakage
    from app.replay.store import ReplayStore
    from app.replay.visibility import Guards, check_state
    st = ReplayStore(URL, "replay_prov")
    st.create()
    with st.engine.begin() as c:
        c.execute(text("INSERT INTO discovery_risks (cik, risk, severity, detected_at) VALUES (:c, 'GOING_CONCERN', 'HIGH', '2021-01-04')"),
                  {"c": CIK})
    with st.engine.connect() as c, pytest.raises(ReplayLeakage) as e:
        check_state(c, Guards(), datetime(2021, 3, 1, tzinfo=UTC), date(2021, 3, 1), date(2021, 3, 1), {})
    assert e.value.kind == "RISK_WITHOUT_PROVENANCE"


# ------------------------------------------------------------------ 7. determinism + job idempotency

def _src():
    shares = {CIK: [["s1", date(2019, 12, 31), date(2020, 2, 20), "10-K", 10_000_000],
                    ["s2", date(2020, 12, 31), date(2021, 2, 22), "10-K", 16_000_000]]}
    hits = [{"risk_key": "ATM_OFFERING", "adsh": f"0000000101-21-00000{i}", "cik": CIK, "form": "8-K",
             "file_date": f"2021-0{i}-10", "filename": "d.htm"} for i in (1, 2, 3)]
    return {"ciks": [CIK], "start": "2021-03-01", "end": "2021-06-30",
            "tenk": {CIK: [_tenk("a-2020", datetime(2020, 3, 10, 20, tzinfo=UTC), 0), _tenk("a-2021", GC_2021, 3)]},
            "tenk_by_year": {"2020": 1900, "2021": 5400, "2022": 6300}, "actions_from": "2004-08-24",
            "thesis_changes_from": "2026-09-25", "shares": shares, "efts_hits": hits,
            "efts_scan": {"done": True, "from": "2020-03-01", "to": "2021-06-30", "truncated": False}}


def test_same_historical_input_gives_the_same_labels():
    a = detect_text_hits(_src()["efts_hits"]) + detect_dilution(CIK, [tuple(f) for f in _src()["shares"][CIK]])
    b = detect_text_hits(list(reversed(_src()["efts_hits"]))) + detect_dilution(CIK, [tuple(f) for f in _src()["shares"][CIK]])
    assert sorted(x.key() for x in a) == sorted(x.key() for x in b) and len(a) == 4
    dil = [x for x in a if x.risk_type == "DILUTION"][0]
    from app.marketdata.calendar import NY
    assert dil.severity == "HIGH" and dil.available_at.astimezone(NY).date() == date(2021, 2, 22)  # filed date, not period end
    at = datetime(2021, 4, 1, tzinfo=UTC)
    act = active_risks(a, CIK, at)
    assert act["ATM_OFFERING"]["severity"] == "HIGH" and act["FREQUENT_CAPITAL_RAISES"]["severity"] == "HIGH"  # 3 raises


@db
def test_reconstruction_job_is_idempotent_and_restartable():
    st = RiskHistoryStore(URL)
    with st.engine.begin() as c:
        c.execute(text("DROP SCHEMA IF EXISTS risk_hist CASCADE"))
    r1 = reconstruct(st, "job1", _src(), [CIK], date(2021, 3, 1), date(2021, 6, 30))
    r2 = reconstruct(st, "job1", _src(), [CIK], date(2021, 3, 1), date(2021, 6, 30))  # restart: all checkpointed
    assert r1["labels_written"] == 5 and r2["labels_written"] == 0 and r2["skipped_done"] == 3
    with st.engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM risk_hist.risk_labels WHERE run_id = 'job1'")).scalar() == 5
    labels, cov = st.load([CIK], datetime(2021, 6, 30, tzinfo=UTC), "job1")
    assert len(labels) == 5 and cov.status("CASH_RUNWAY") == "UNAVAILABLE" and cov.status("FINANCING_TEXT") == "COMPLETE"
    assert r1["policy"] == "RISK_HISTORY_PARTIAL"  # CASH_RUNWAY / THESIS_INVALIDATION are not complete


# ------------------------------------------------------------------ 8. FULL policy

def test_full_is_rejected_when_any_required_family_is_partial_or_starts_late():
    ok = Coverage({f: {"status": "COMPLETE", "from": date(2015, 1, 1), "to": None} for f in FAMILIES})
    assert coverage_policy(ok, date(2021, 3, 1), date(2021, 6, 30))["comparability"] == "FULL"
    part = Coverage({**ok.families, "DILUTION": {"status": "PARTIAL", "from": date(2015, 1, 1), "to": None}})
    assert coverage_policy(part, date(2021, 3, 1), date(2021, 6, 30))["comparability"] == "LIMITED"
    late = Coverage({**ok.families, "GOING_CONCERN": {"status": "COMPLETE", "from": date(2021, 4, 1), "to": None}})
    p = coverage_policy(late, date(2021, 3, 1), date(2021, 6, 30))
    assert p["comparability"] == "LIMITED" and "GOING_CONCERN" in p["not_complete"]
    assert coverage_policy(Coverage(), date(2021, 3, 1), date(2021, 6, 30))["flag"] == "RISK_HISTORY_UNAVAILABLE"


# ------------------------------------------------------------------ inside a replay

@db
def test_replay_stages_the_label_only_from_its_public_time_and_blocks_the_gate():
    from tests.test_replay import _event, cfg, q, run, source
    src = source(events=[_event(1, datetime(2021, 2, 5, 21, 30, tzinfo=UTC)),
                         _event(2, datetime(2021, 3, 19, 21, 30, tzinfo=UTC))])
    src.risk_labels = detect_going_concern_10k([_tenk("a-2021", GC_2021, 3)])
    _, res = run(cfg("t_risk_gc", end=date(2021, 3, 26)), src)
    assert res.status == "COMPLETED", res.leakage
    assert res.rejected.get("SEVERE_FINANCING_RISK") == 1  # the March event, after the 10-K was public
    rows = q("replay_t_risk_gc", "SELECT event_id FROM proposed_trades")
    assert [r[0] for r in rows] == [1]  # the February event was proposed: the warning did not exist yet
    assert q("replay_t_risk_gc", "SELECT risk, detected_at FROM discovery_risks WHERE risk = 'GOING_CONCERN'") == \
        [("GOING_CONCERN", GC_2021)]


def test_full_policy_is_checked_against_the_sessions_actually_replayed():
    from app.replay.config import ReplayConfig
    from app.replay.engine import risk_history
    from app.replay.source import SourceData
    cov = Coverage({f: {"status": "COMPLETE", "from": date(2021, 3, 1), "to": date(2021, 6, 30)} for f in FAMILIES})
    cfg = ReplayConfig(replay_id="x", start=datetime(2021, 3, 1, tzinfo=UTC), end=datetime(2021, 6, 30, 23, tzinfo=UTC))
    assert risk_history(cfg, SourceData(risk_cov=cov))["comparability"] == "FULL"  # UTC midnight = Feb 28 in New York
    cfg2 = ReplayConfig(replay_id="x", start=datetime(2021, 2, 26, tzinfo=UTC), end=datetime(2021, 6, 30, 23, tzinfo=UTC))
    assert risk_history(cfg2, SourceData(risk_cov=cov))["comparability"] == "LIMITED"  # Friday Feb 26 is not covered
