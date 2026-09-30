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
        c.execute(text("DROP SCHEMA IF EXISTS risk_hist_v3 CASCADE"))
    st.create()
    labels = detect_going_concern_10k([_tenk("a-2021", GC_2021, 3)])
    st.write("t_versions", "GOING_CONCERN_10K", CIK, labels)
    with st.engine.connect() as c:
        row = c.execute(text("SELECT detector, detector_version, rule_version, source_document_id, source_url, available_at "
                             "FROM risk_hist_v3.risk_labels WHERE run_id = 't_versions'")).one()
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

def _meta(**kw):
    m = {"ciks": [CIK], "start": "2021-03-01", "end": "2021-06-30", "tenk_by_year": {"2020": 1900, "2021": 5400, "2022": 6300},
         "thesis_changes_from": "2026-09-25",
         "efts_scan": {"done": True, "from": "2020-03-01", "to": "2021-06-30", "truncated": False}}
    m.update(kw)
    return m


def _issuer(cik=CIK, tenk=None, shares=None, hits=None, actions=None):
    shares = shares if shares is not None else [["s0", "2019-06-30", "2019-08-10", "10-Q", 9_000_000],
                                                ["s1", "2019-12-31", "2020-02-20", "10-K", 10_000_000],
                                                ["s1b", "2020-06-30", "2020-08-10", "10-Q", 12_000_000],
                                                ["s2", "2020-12-31", "2021-02-22", "10-K", 16_000_000]]
    hits = hits if hits is not None else [{"risk_key": "ATM_OFFERING", "adsh": f"0000000101-21-00000{i}", "cik": cik,
                                           "form": "8-K", "file_date": f"2021-0{i}-10", "filename": "d.htm"} for i in (1, 2, 3)]
    return {"cik": cik, "tenk": tenk if tenk is not None else [_tenk("a-2020", datetime(2020, 3, 10, 20, tzinfo=UTC), 0),
                                                               _tenk("a-2021", GC_2021, 3)],
            "shares": shares, "efts_hits": hits,
            "actions": actions if actions is not None else {"earliest": "2005-01-10", "fetched": "2026-09-20"}}


def _write(path, meta, issuers, trailer=True):
    """An export file as scripts/risk_source_export.py writes it: metadata, issuers, completion trailer."""
    import json

    from app.replay.risk_history import TRAILER, scope_key
    with open(path, "w") as fh:
        fh.write(json.dumps(meta, default=str) + "\n")
        for r in issuers:
            fh.write(json.dumps(r, default=str) + "\n")
        if trailer:
            ciks = sorted(meta["ciks"])
            fh.write(json.dumps({TRAILER: {"issuers": len(ciks), "ciks_digest": scope_key(ciks), "batches": 1,
                                           "efts_batches": {p: 1 for p in (meta.get("efts_scan") or {}).get("phrases", [])}}})
                     + "\n")
    return path


def _fresh_store():
    st = RiskHistoryStore(URL)
    with st.engine.begin() as c:
        c.execute(text("DROP SCHEMA IF EXISTS risk_hist_v3 CASCADE"))
    return st


def test_same_historical_input_gives_the_same_labels():
    from app.replay.risk_history import _issuer as parse
    rec = parse(_issuer())
    a = detect_text_hits(rec["efts_hits"]) + detect_dilution(CIK, rec["shares"])
    b = detect_text_hits(list(reversed(rec["efts_hits"]))) + detect_dilution(CIK, list(reversed(rec["shares"])))
    assert sorted(x.key() for x in a) == sorted(x.key() for x in b)
    dil = [x for x in a if x.risk_type == "DILUTION" and x.source_document_id == "s2"][0]
    from app.marketdata.calendar import NY
    assert dil.severity == "HIGH" and dil.available_at.astimezone(NY).date() == date(2021, 2, 22)  # filed date, not period end
    act = active_risks(a, CIK, datetime(2021, 4, 1, tzinfo=UTC))
    assert act["ATM_OFFERING"]["severity"] == "HIGH" and act["FREQUENT_CAPITAL_RAISES"]["severity"] == "HIGH"  # 3 raises


@db
def test_reconstruction_job_is_idempotent_and_restartable(tmp_path):
    st = _fresh_store()
    src = _write(tmp_path / "s.jsonl", _meta(), [_issuer()])
    r1 = reconstruct(st, "job1", src)
    r2 = reconstruct(st, "job1", src)  # restart: every (issuer, family) is checkpointed
    assert r1["labels_written"] > 0 and r2["labels_written"] == 0 and r2["skipped_done"] == 3
    labels, cov = st.load([CIK], datetime(2021, 6, 30, tzinfo=UTC), "job1")
    assert len(labels) == r1["labels_written"] and cov.status("CASH_RUNWAY") == "UNAVAILABLE"
    assert cov.status("FINANCING_TEXT") == "COMPLETE" and r1["policy"] == "RISK_HISTORY_PARTIAL"


@db
def test_overlapping_runs_keep_complete_reproducible_snapshots(tmp_path):
    """Run B reconstructs the same issuer with a longer history (a 2022 report ends the 2021 warning): both runs load
    their own complete label set; A's is unchanged by B."""
    st = _fresh_store()
    short = _issuer(tenk=[_tenk("a-2021", GC_2021, 3)])
    longer = _issuer(tenk=[_tenk("a-2021", GC_2021, 3), _tenk("a-2022", GC_2021 + timedelta(days=365), 0)])
    reconstruct(st, "run_a", _write(tmp_path / "a.jsonl", _meta(), [short]))
    reconstruct(st, "run_b", _write(tmp_path / "b.jsonl", _meta(end="2022-06-30"), [longer]))
    la, _ = st.load([CIK], datetime(2023, 1, 1, tzinfo=UTC), "run_a")
    lb, _ = st.load([CIK], datetime(2023, 1, 1, tzinfo=UTC), "run_b")
    ga = [x for x in la if x.risk_type == "GOING_CONCERN"]
    gb = [x for x in lb if x.risk_type == "GOING_CONCERN"]
    assert len(ga) == 1 and ga[0].valid_to is None
    assert len(gb) == 1 and gb[0].valid_to == GC_2021 + timedelta(days=365)
    assert len(la) == len(lb)  # same documents, each run complete
    reconstruct(st, "run_a", _write(tmp_path / "a.jsonl", _meta(), [short]))  # re-running A reproduces A
    assert [x.key() for x in st.load([CIK], datetime(2023, 1, 1, tzinfo=UTC), "run_a")[0]] == [x.key() for x in la]


def test_coverage_is_issuer_specific_and_bounded():
    from app.replay.risk_history import _issuer as parse
    from app.replay.risk_history import compute_coverage, issuer_inventory
    s, e = date(2021, 3, 1), date(2021, 6, 30)

    def cov(**issuer_kw):
        rec = parse(_issuer(**issuer_kw))
        return compute_coverage(_meta(), {CIK: issuer_inventory(rec, s, e)}, [CIK])
    assert cov().status("DILUTION") == "COMPLETE"
    sparse = cov(shares=[["s2", "2020-12-31", "2021-02-22", "10-K", 16_000_000]])  # one fact: no comparison pair
    assert sparse.status("DILUTION") == "UNAVAILABLE"
    gap = cov(shares=[["s1", "2016-12-31", "2017-02-20", "10-K", 1], ["s0", "2015-12-31", "2016-02-20", "10-K", 1],
                      ["s2", "2020-12-31", "2021-02-22", "10-K", 2]])  # a four-year silence
    assert gap.status("DILUTION") == "PARTIAL"
    assert cov(actions={}).status("FINANCING_ACTIONS") == "UNAVAILABLE"  # no submissions inventory
    old = cov(tenk=[_tenk("a-2021", GC_2021, 3, v=1)])  # an unprocessed (older-classifier) report in range
    assert old.status("GOING_CONCERN") == "PARTIAL"
    full = cov()
    for fam in ("GOING_CONCERN", "FINANCING_TEXT", "FINANCING_ACTIONS", "DILUTION"):
        assert full.families[fam]["to"] is not None and full.families[fam]["to"] <= e  # never past the export horizon
    assert coverage_policy(full, date(2021, 3, 1), date(2021, 8, 31))["not_complete"].keys() >= \
        {"GOING_CONCERN", "FINANCING_TEXT", "FINANCING_ACTIONS", "DILUTION"}


def test_the_export_full_text_scan_cannot_reach_the_database_limiter():
    import subprocess
    import sys
    code = """
import os, importlib.util
os.environ["PE_SHARED_RATELIMIT"] = "1"   # inherited from a production container
spec = importlib.util.spec_from_file_location("exp", "scripts/risk_source_export.py")
exp = importlib.util.module_from_spec(spec); spec.loader.exec_module(exp)
from app import http
from app.core import ratelimit
assert not http._SHARED
def boom(*a, **k): raise SystemExit("database limiter called")
ratelimit.acquire = boom; ratelimit.record_429 = boom
class R:
    status_code = 200
    text = ""
    def json(self): return {"hits": {"hits": [], "total": {"value": 0}}}
http._client.request = lambda *a, **k: R()
from datetime import date
from app.discovery.sec_data import efts_search
assert efts_search("x", ["8-K"], date(2021, 1, 1), date(2021, 2, 1), ciks=["1"]) == []
http._SHARED = True
try:
    exp.ensure_db_free_http()
    raise SystemExit("guard did not refuse")
except SystemExit as e:
    assert "refusing" in str(e), e
print("OK")
"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    p = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True, timeout=120)
    assert p.returncode == 0 and "OK" in p.stdout, p.stderr[-2000:] + p.stdout[-500:]


@db
def test_reconstruction_memory_is_bounded_by_one_issuer(tmp_path):
    import tracemalloc
    st = _fresh_store()

    def issuers(n):
        big = [_tenk(f"t{i}", GC_2021 + timedelta(days=i), 3) for i in range(20)]
        for f in big:
            f["profile"]["MDA"]["hashes"] = list(range(3000))  # ~ a real profile's size
        return (_issuer(cik=f"{9000000000 + k:010d}", tenk=big) for k in range(n))

    peaks = {}
    for n in (5, 60):
        path = tmp_path / f"m{n}.jsonl"
        _write(path, _meta(ciks=[f"{9000000000 + k:010d}" for k in range(n)]), issuers(n))
        tracemalloc.start()
        reconstruct(st, f"mem{n}", path)
        peaks[n] = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
    assert peaks[60] < 2 * peaks[5], peaks  # 12x the issuers, not 12x the memory


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


# ------------------------------------------------------------------ financing assessment inside a replay

def _actions_source(shares: int, extra_labels=()):
    from app.marketdata.provider import ShareFact
    from tests.test_replay import _event, source
    src = source(events=[_event(1, datetime(2021, 2, 5, 21, 30, tzinfo=UTC)), _event(2, datetime(2021, 5, 10, 21, 30, tzinfo=UTC))])
    src.share_facts[CIK] = [ShareFact("a1", date(2020, 9, 30), date(2020, 11, 2), "10-Q", shares)]
    src.corporate_actions = [
        {"cik": CIK, "action": "SHELF_REGISTRATION", "action_date": date(2020, 6, 1), "accession": "s3-1", "source": "t"},
        {"cik": CIK, "action": "UNREGISTERED_SALE", "action_date": date(2020, 11, 2), "accession": "pipe-1", "source": "t"},
        {"cik": CIK, "action": "LISTING_DEFICIENCY", "action_date": date(2020, 12, 1), "accession": "def-1", "source": "t"},
    ] + [{"cik": CIK, "action": "PROSPECTUS_424B", "action_date": date(2020, 9 + i, 5), "accession": f"424b-{i}", "source": "t"}
         for i in range(3)]
    src.risk_labels = list(extra_labels)
    return src


@db
def test_corporate_actions_alone_make_high_financing_risk_with_their_filings_as_evidence():
    from tests.test_replay import cfg, q, run
    r, res = run(cfg("t_fin_actions", end=date(2021, 2, 12)), _actions_source(10_000_000))  # ~$230M: sub-billion
    assert res.status == "COMPLETED", res.leakage
    rows = q("replay_t_fin_actions", "SELECT risk, severity FROM discovery_risks")
    assert rows == [("HIGH_FINANCING_RISK", "HIGH")]
    ev = r.stager.risk_provenance[(CIK, "HIGH_FINANCING_RISK")]
    assert {"SHELF_REGISTRATION:s3-1", "UNREGISTERED_SALE:pipe-1", "LISTING_DEFICIENCY:def-1",
            "PROSPECTUS_424B:424b-0"} <= set(ev)
    assert q("replay_t_fin_actions", "SELECT max_position_pct FROM proposed_trades") == [(0.01,)]  # production cap


@db
def test_a_label_published_later_cannot_change_an_earlier_step():
    from tests.test_replay import cfg, q, run
    later = detect_going_concern_10k([_tenk("late", datetime(2021, 9, 1, 20, tzinfo=UTC), 3)])  # after the window
    run(cfg("t_inv_a", end=date(2021, 5, 14)), _actions_source(10_000_000))
    run(cfg("t_inv_b", end=date(2021, 5, 14)), _actions_source(10_000_000, later))
    for sql in ("SELECT event_id, max_position_pct, proposal_created_at FROM proposed_trades ORDER BY 1",
                "SELECT cik, risk, severity, value FROM discovery_risks ORDER BY 1, 2"):
        assert q("replay_t_inv_a", sql) == q("replay_t_inv_b", sql)


@db
def test_first_event_above_one_billion_is_not_capped_and_crossing_the_threshold_lifts_the_cap():
    from tests.test_replay import cfg, q, run
    run(cfg("t_fin_big", end=date(2021, 2, 12)), _actions_source(100_000_000))  # ~$2.3B at the first event
    assert q("replay_t_fin_big", "SELECT count(*) FROM discovery_risks WHERE risk = 'HIGH_FINANCING_RISK'") == [(0,)]
    assert q("replay_t_fin_big", "SELECT max_position_pct FROM proposed_trades")[0][0] != 0.01
    # 40M shares: ~$0.94B in February (capped), ~$1.05B by May (above the production $1B line: not assessed)
    run(cfg("t_fin_cross", end=date(2021, 5, 14)), _actions_source(40_000_000))
    caps = q("replay_t_fin_cross", "SELECT event_id, max_position_pct FROM proposed_trades ORDER BY 1")
    assert caps == [(1, 0.01)]  # the May event is a DUPLICATE (still held), so the cap is read off the risk row:
    # every action flag is still inside its window in May, so only the capitalisation removed the row
    assert q("replay_t_fin_cross", "SELECT count(*) FROM discovery_risks WHERE risk = 'HIGH_FINANCING_RISK'") == [(0,)]
    assert q("replay_t_fin_cross", "SELECT market_cap > 1e9 FROM universe_snapshots ORDER BY as_of DESC LIMIT 1") == [(True,)]
    assert q("replay_t_fin_cross", "SELECT market_cap < 1e9 FROM universe_snapshots ORDER BY as_of LIMIT 1") == [(True,)]


@db
def test_a_same_day_filing_cannot_move_that_mornings_sizing():
    """The corporate actions carry only a filing DATE: public at the end of that day. An event decided at 09:00 on the
    day the 8-K / 424B / S-3 are filed is sized without them; the next morning they count."""
    from tests.test_replay import _event, cfg, q, run

    def src(action_day):
        s = _actions_source(10_000_000)
        s.events = [_event(1, datetime(2021, 2, 8, 12, 30, tzinfo=UTC))]  # 07:30 ET: decided at PRE_OPEN Feb 8
        s.corporate_actions = [{**a, "action_date": action_day} for a in s.corporate_actions]
        return s
    r, res = run(cfg("t_same_day", end=date(2021, 2, 12)), src(date(2021, 2, 8)))
    assert res.status == "COMPLETED", res.leakage
    assert q("replay_t_same_day", "SELECT max_position_pct FROM proposed_trades")[0][0] != 0.01  # not visible yet
    assert q("replay_t_same_day", "SELECT risk FROM discovery_risks") == [("HIGH_FINANCING_RISK",)]  # from Feb 9 on
    run(cfg("t_prev_day", end=date(2021, 2, 12)), src(date(2021, 2, 5)))  # control: filed the session before
    assert q("replay_t_prev_day", "SELECT max_position_pct FROM proposed_trades") == [(0.01,)]


DIL_FACTS = [("p", date(2020, 3, 31), date(2020, 5, 1), "10-Q", 100),
             ("c", date(2021, 3, 31), date(2021, 5, 3), "10-Q", 160),       # +60 % -> HIGH
             ("amd", date(2020, 3, 31), date(2021, 6, 1), "10-Q/A", 110)]  # base restated: +45 % -> MEDIUM


def test_an_amendment_opens_its_own_dilution_interval_with_both_compared_facts():
    labels = detect_dilution(CIK, DIL_FACTS)
    assert [(x.severity, x.source_document_id, x.related_documents) for x in labels] == \
        [("HIGH", "c", ("p",)), ("MEDIUM", "amd", ("c",))]
    assert labels[0].valid_to == labels[1].valid_from  # the amendment ends the earlier state when it is public
    assert len({x.key() for x in labels}) == 2
    assert set(active_risks(labels, CIK, datetime(2021, 5, 20, tzinfo=UTC))["DILUTION"]["evidence"]) == {"c", "p"}


@db
def test_dilution_intervals_survive_the_store_and_drive_financing_across_the_amendment():
    from app.marketdata.provider import ShareFact
    from tests.test_replay import _event, cfg, q, run, source
    st = _fresh_store()
    st.create()
    labels = detect_dilution(CIK, DIL_FACTS)
    assert st.write("t_dil", "DILUTION", CIK, labels) == 2
    back, _ = st.load([CIK], datetime(2021, 12, 31, tzinfo=UTC), "t_dil")
    assert [(x.key(), x.severity, x.related_documents, x.valid_to) for x in back] == \
        [(x.key(), x.severity, x.related_documents, x.valid_to) for x in labels]

    def src():
        s = source(events=[_event(1, datetime(2021, 4, 5, 21, 30, tzinfo=UTC))])
        s.share_facts[CIK] = [ShareFact("a1", date(2020, 9, 30), date(2020, 11, 2), "10-Q", 10_000_000)]
        # 3.5 points of actions: + SHARE_GROWTH_50PCT (2.5) is HIGH, + SHARE_GROWTH_20PCT (1.0) is not
        s.corporate_actions = [
            {"cik": CIK, "action": "UNREGISTERED_SALE", "action_date": date(2020, 12, 1), "accession": "pipe-1", "source": "t"},
            {"cik": CIK, "action": "LISTING_DEFICIENCY", "action_date": date(2020, 12, 2), "accession": "def-1", "source": "t"}]
        s.risk_labels = list(back)
        return s
    r1, res1 = run(cfg("t_dil_high", end=date(2021, 5, 20)), src())
    assert res1.status == "COMPLETED", res1.leakage
    assert q("replay_t_dil_high", "SELECT risk, severity FROM discovery_risks ORDER BY 1") == \
        [("DILUTION", "HIGH"), ("HIGH_FINANCING_RISK", "HIGH")]
    assert {"c", "p", "UNREGISTERED_SALE:pipe-1"} <= set(r1.stager.risk_provenance[(CIK, "HIGH_FINANCING_RISK")])
    _, res2 = run(cfg("t_dil_med", end=date(2021, 6, 10)), src())
    assert res2.status == "COMPLETED", res2.leakage
    assert q("replay_t_dil_med", "SELECT risk, severity FROM discovery_risks ORDER BY 1") == [("DILUTION", "MEDIUM")]


# ------------------------------------------------------------------ export completion + run fingerprint

@db
def test_a_truncated_export_publishes_no_coverage(tmp_path):
    from app.replay.risk_history import SourceIncomplete
    st = _fresh_store()
    two = _meta(ciks=[CIK, "0000000202"])
    cut = _write(tmp_path / "cut.jsonl", two, [_issuer()], trailer=False)  # stopped at a valid line boundary
    with pytest.raises(SourceIncomplete, match="trailer"):
        reconstruct(st, "cut", cut)
    labels, cov = st.load([CIK, "0000000202"], datetime(2021, 6, 30, tzinfo=UTC), "cut")
    assert labels and cov.families == {}  # correct labels of the issuer read, but no coverage claimed
    miss = _write(tmp_path / "miss.jsonl", two, [_issuer()])  # trailer present, an issuer missing
    with pytest.raises(SourceIncomplete, match="missing issuers"):
        reconstruct(st, "miss", miss)
    dup = _write(tmp_path / "dup.jsonl", _meta(), [_issuer(), _issuer()])
    with pytest.raises(SourceIncomplete, match="duplicate"):
        reconstruct(st, "dup", dup)
    phr = _meta(efts_scan={**_meta()["efts_scan"], "phrases": ["ATM_OFFERING", "GOING_CONCERN"]})
    short = _write(tmp_path / "short.jsonl", phr, [_issuer()])
    lines = short.read_text().splitlines()
    lines[-1] = lines[-1].replace('"GOING_CONCERN": 1', '"GOING_CONCERN": 0')  # one phrase never finished
    short.write_text("\n".join(lines) + "\n")
    with pytest.raises(SourceIncomplete, match="phrase scans"):
        reconstruct(st, "short", short)
    for run_id, ciks in (("miss", [CIK, "0000000202"]), ("dup", [CIK]), ("short", [CIK])):
        assert st.load(ciks, datetime(2021, 6, 30, tzinfo=UTC), run_id)[1].families == {}
    assert reconstruct(st, "ok", _write(tmp_path / "ok.jsonl", phr, [_issuer()]))["coverage"]


@db
def test_a_run_id_cannot_resume_with_changed_inputs(tmp_path, monkeypatch):
    from app.replay.risk_history import RunInputChanged
    st = _fresh_store()
    ciks = [CIK, "0000000202"]
    a = _write(tmp_path / "a.jsonl", _meta(ciks=ciks), [_issuer(), _issuer(cik="0000000202")])
    real, calls = RiskHistoryStore.write, []

    def crash(self, *args):
        calls.append(1)
        if len(calls) > 3:
            raise RuntimeError("killed")
        return real(self, *args)
    monkeypatch.setattr(RiskHistoryStore, "write", crash)
    with pytest.raises(RuntimeError):
        reconstruct(st, "fp", a)  # first issuer done, then the job dies
    monkeypatch.setattr(RiskHistoryStore, "write", real)
    b = _write(tmp_path / "b.jsonl", _meta(ciks=ciks), [_issuer(hits=[]), _issuer(cik="0000000202", hits=[])])
    with pytest.raises(RunInputChanged):
        reconstruct(st, "fp", b)  # a different export under the same run id
    assert st.load(ciks, datetime(2021, 6, 30, tzinfo=UTC), "fp")[1].families == {}  # coverage did not advance
    with pytest.raises(RunInputChanged):
        reconstruct(st, "fp", _write(tmp_path / "w.jsonl", _meta(ciks=ciks, end="2021-09-30"),
                                     [_issuer(), _issuer(cik="0000000202")]))  # another window
    res = reconstruct(st, "fp", a)  # the original input resumes
    assert res["skipped_done"] == 3 and res["coverage"]
