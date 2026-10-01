"""FULL replay engine on the point-in-time tables: CASH_RUNWAY from pit_facts, FINANCING_ACTIONS / DILUTION from the
complete SEC filing history (acceptance times, insider forms never read), the ADR market-cap rule shared by live and
replay, and the SEC-only THESIS proxy as its own coverage class (PROXY_VALIDATED / FULL_WITH_PROXY, never FULL)."""
import json
from datetime import date, datetime, time, timedelta, timezone

import pytest
from sqlalchemy import text

from app.marketdata.calendar import NY
from app.pit.sources import PitFact
from tests.test_db import URL

db = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")
UTC = timezone.utc
CIK = "0000000101"
CASH, OCF = "CashAndCashEquivalentsAtCarryingValue", "NetCashProvidedByUsedInOperatingActivities"


def _cash(val, end, filed, acc):
    return PitFact(CIK, "CASH", CASH, None, end, val, filed, acc, "10-Q")


def _ocf(val, start, end, filed, acc, form="10-K"):
    return PitFact(CIK, "OCF", OCF, start, end, val, filed, acc, form)


D = date


# ------------------------------------------------------------------ CASH_RUNWAY (pure)

def _runway_facts():
    return [_ocf(-120.0, D(2022, 1, 1), D(2022, 12, 31), D(2023, 3, 1), "k22"),   # annual burn 120 -> 10 / month
            _cash(150.0, D(2022, 12, 31), D(2023, 3, 1), "k22"),                  # 15 months: MEDIUM
            _cash(60.0, D(2023, 3, 31), D(2023, 5, 10), "q1"),                    # 6 months: HIGH
            _cash(300.0, D(2023, 6, 30), D(2023, 8, 9), "q2")]                    # 30 months: no label


def test_runway_labels_use_only_what_is_public_and_the_production_severity():
    from app.replay.risk_history import active_risks, detect_cash_runway
    acc = {"k22": datetime(2023, 3, 1, 21, 5, tzinfo=UTC), "q1": datetime(2023, 5, 10, 20, 30, tzinfo=UTC)}  # q2: date only
    labels = detect_cash_runway(CIK, _runway_facts(), acc)
    assert [(x.source_document_id, x.severity, x.available_at) for x in labels] == [
        ("k22", "MEDIUM", acc["k22"]), ("q1", "HIGH", acc["q1"])]
    # each assessment lasts until the next public runway fact; q2 (date only) counts from the next New York day
    assert labels[0].valid_to == acc["q1"]
    assert labels[1].valid_to == datetime.combine(D(2023, 8, 10), time(0), NY).astimezone(UTC)
    assert labels[1].related_documents == ("k22",)  # the annual OCF it used
    assert active_risks(labels, CIK, acc["q1"] - timedelta(seconds=1))["CASH_RUNWAY"]["severity"] == "MEDIUM"
    assert active_risks(labels, CIK, acc["q1"])["CASH_RUNWAY"]["severity"] == "HIGH"
    assert "CASH_RUNWAY" not in active_risks(labels, CIK, datetime(2023, 8, 10, 14, tzinfo=UTC))  # 30 months: none
    assert active_risks(labels, CIK, acc["k22"] - timedelta(seconds=1)) == {}  # nothing before the 10-K


def test_a_later_restatement_never_changes_an_earlier_runway_assessment():
    from app.replay.risk_history import detect_cash_runway
    acc = {"k22": datetime(2023, 3, 1, 21, tzinfo=UTC), "q1": datetime(2023, 5, 10, 20, tzinfo=UTC),
           "amend": datetime(2023, 6, 1, 20, tzinfo=UTC)}
    base = detect_cash_runway(CIK, _runway_facts()[:3], acc)
    restated = _runway_facts()[:3] + [_cash(5.0, D(2023, 3, 31), D(2023, 6, 1), "amend")]  # Q1 cash restated later
    after = detect_cash_runway(CIK, restated, acc)
    early = [x for x in after if x.available_at < acc["amend"]]
    assert [(x.source_document_id, x.severity, x.value) for x in early] == \
        [(x.source_document_id, x.severity, x.value) for x in base]
    assert early[-1].valid_to == acc["amend"]  # the restatement only opens a NEW interval from its own public time
    assert after[-1].source_document_id == "amend" and after[-1].available_at == acc["amend"]


def test_a_fiscal_year_change_report_is_an_annual_ocf_like_the_production_frames():
    """0001482541 (production CASH_RUNWAY_RISK 3.19 months): its annual OCF covers 2025-06-07..2026-04-30 (327 days)."""
    from app.pit.sources import runway_months
    facts = [_ocf(-26_622_000, D(2025, 6, 7), D(2026, 4, 30), D(2026, 6, 23), "kt"),
             _ocf(-3_055_425, D(2024, 1, 1), D(2024, 12, 31), D(2025, 3, 27), "k24"),
             _cash(7_084_000, D(2026, 7, 31), D(2026, 9, 11), "q")]
    at = datetime(2026, 9, 26, 3, 59, tzinfo=UTC)
    assert runway_months(facts, {}, at) == pytest.approx(7_084_000 / (26_622_000 / 12))  # = 3.19, not 27.8


# ------------------------------------------------------------------ DILUTION / THESIS proxy / actions (pure)

def test_dilution_is_public_from_the_acceptance_time_of_its_filings():
    from app.replay.risk_history import DILUTION_DETECTOR_VERSION_ACCEPTED, end_of_public_day
    from app.replay.risk_history import detect_dilution as det
    facts = [("s1", D(2020, 1, 31), D(2020, 3, 2), "10-K", 10_000_000), ("s2", D(2021, 1, 31), D(2021, 3, 1), "10-K", 16_000_000)]
    acc = {"s2": datetime(2021, 3, 1, 14, 2, tzinfo=UTC)}
    with_t, without = det(CIK, facts, acc)[0], det(CIK, facts)[0]
    assert with_t.available_at == acc["s2"] and with_t.accepted_at == acc["s2"]
    assert with_t.detector_version == DILUTION_DETECTOR_VERSION_ACCEPTED
    assert without.available_at == end_of_public_day(D(2021, 3, 1))  # the filing-date rule when no time is known


def test_the_thesis_proxy_reads_only_8k_items_1_03_and_3_01_from_their_acceptance_time():
    from app.replay.risk_history import THESIS_PROXY, detect_thesis_proxy
    t = datetime(2022, 4, 5, 21, 1, tzinfo=UTC)
    rows = [{"accession": "a", "form": "8-K", "filing_date": D(2022, 4, 5), "acceptance_at": t, "items": "3.01,9.01"},
            {"accession": "b", "form": "8-K", "filing_date": D(2022, 4, 6), "acceptance_at": None, "items": "1.031,2.02"},
            {"accession": "c", "form": "8-K/A", "filing_date": D(2022, 4, 7), "acceptance_at": None, "items": "1.03"},
            {"accession": "d", "form": "10-Q", "filing_date": D(2022, 4, 8), "acceptance_at": None, "items": "1.03"}]
    out = detect_thesis_proxy(CIK, rows)
    assert [(x.source_document_id, x.risk_type) for x in out] == [("a", THESIS_PROXY), ("c", THESIS_PROXY)]
    assert out[0].available_at == t


def test_proxy_labels_are_never_discovery_risks():
    from app.replay.risk_history import active_risks, detect_thesis_proxy
    rows = [{"accession": "a", "form": "8-K", "filing_date": D(2022, 4, 5), "acceptance_at": None, "items": "1.03"}]
    assert active_risks(detect_thesis_proxy(CIK, rows), CIK, datetime(2023, 1, 1, tzinfo=UTC)) == {}


def test_actions_come_from_the_production_mapping_with_their_acceptance_time():
    from app.replay.actions import actions_from_filings
    t = datetime(2021, 6, 1, 20, 15, tzinfo=UTC)
    rows = [{"accession": "p", "form": "8-K", "filing_date": D(2021, 6, 1), "acceptance_at": t, "items": "3.02,9.01"},
            {"accession": "s", "form": "S-3", "filing_date": D(2021, 6, 2), "acceptance_at": None, "items": None},
            {"accession": "n", "form": "25/A", "filing_date": D(2021, 6, 3), "acceptance_at": None, "items": None},
            {"accession": "x", "form": "10-Q", "filing_date": D(2021, 6, 4), "acceptance_at": None, "items": None}]
    got = {(a["action"], a["accession"], a["accepted_at"]) for a in actions_from_filings(CIK, rows)}
    assert got == {("UNREGISTERED_SALE", "p", t), ("SHELF_REGISTRATION", "s", None), ("FORM25", "n", None)}


# ------------------------------------------------------------------ ADR market cap (shared rule)

def test_the_market_cap_rule_is_unknown_for_a_depositary_programme_and_unchanged_otherwise():
    from app.marketdata.market_cap import DEPOSITARY_RATIO_UNKNOWN, market_cap
    at = datetime(2026, 9, 30, 20, tzinfo=UTC)
    assert market_cap(446.57, 25_932_524_521, datetime(2003, 6, 25, tzinfo=UTC), at) == (None, DEPOSITARY_RATIO_UNKNOWN)
    assert market_cap(225.51, 24_300_000_000, None, at) == (225.51 * 24_300_000_000, None)  # domestic: as before
    later = datetime(2027, 1, 1, tzinfo=UTC)  # a programme registered after the moment does not reach back
    assert market_cap(10.0, 1_000, later, at) == (10_000.0, None)
    with pytest.raises(ValueError):
        market_cap(1.0, 1, None, datetime(2026, 1, 1))


@db
@pytest.mark.usefixtures("clean")
def test_live_universe_writes_an_adr_as_unknown_so_no_older_inflated_row_is_read(engine, monkeypatch):
    from app.db.models import sec_filings, universe_snapshots
    from app.discovery import sec_data
    from app.paper.proposals import size_limits
    opera, tutor = "0001737450", "0000077543"  # OPRA (an ADR) and TPC (domestic) from the clean registry
    monkeypatch.setattr(sec_data, "shares_frames", lambda: {opera: (180_000_000, D(2026, 6, 30)),
                                                             tutor: (52_000_000, D(2026, 6, 30))})
    monkeypatch.setattr(sec_data, "spark_closes", lambda syms: {s: (20.0, 1) for s in syms})
    with engine.begin() as c:
        c.execute(sec_filings.insert().values(cik=opera, accession="f6-1", form="F-6", filing_date=D(2018, 3, 1),
                                              acceptance_at=datetime(2018, 3, 1, 21, tzinfo=UTC), source_block="recent",
                                              run_id="t"))
        c.execute(universe_snapshots.insert().values(as_of=D(2026, 9, 1), cik=opera, ticker="OPRA", market_cap=7.2e12,
                                                     bucket="ALREADY_SCALED"))
    sec_data.build_universe(engine, D(2026, 10, 1), with_adv=False)
    with engine.connect() as c:
        rows = dict(c.execute(text("""SELECT DISTINCT ON (cik) cik, market_cap FROM universe_snapshots
                                      ORDER BY cik, as_of DESC""")).all())
    assert rows[opera] is None and rows[tutor] == pytest.approx(20.0 * 52_000_000)
    assert size_limits(None, None, set()) == size_limits(1e6, "MICRO", set())  # unknown = the risky size class


@db
@pytest.mark.usefixtures("clean")
def test_point_in_time_market_cap_paths_use_the_same_rule(engine, monkeypatch):
    from app.db.models import corporate_actions
    from app.marketdata import market_cap as mc
    with engine.begin() as c:  # a programme the live submissions refresh recorded after the backfill
        c.execute(corporate_actions.insert().values(cik="0001737450", action=mc.DEPOSITARY_ACTION, action_date=D(2026, 9, 2),
                                                    accession="f6-2", source="sec_submissions"))
        since = mc.depositary_since(c, ["0001737450", "0000077543"])
    assert set(since) == {"0001737450"}
    assert mc.market_cap(5.0, 10, since["0001737450"], mc.live_moment(D(2026, 9, 1)))[0] == 50.0  # not yet
    assert mc.market_cap(5.0, 10, since["0001737450"], mc.live_moment(D(2026, 9, 2)))[0] is None


# ------------------------------------------------------------------ readers + export + coverage (database)

def _seed_pit(engine, cik=CIK, *, sub="done", xbrl="done"):
    from app.db.models import pit_backfill_progress, pit_facts, sec_filings
    t = lambda y, m, d, h=21: datetime(y, m, d, h, tzinfo=UTC)  # noqa: E731
    with engine.begin() as c:
        c.execute(sec_filings.insert(), [
            {"cik": cik, "accession": acc, "form": form, "filing_date": fd, "acceptance_at": at, "items": items,
             "source_block": "recent", "run_id": "t"}
            for acc, form, fd, at, items in [
                ("k21", "10-K", D(2021, 3, 1), t(2021, 3, 1), None), ("q21", "10-Q", D(2021, 5, 10), t(2021, 5, 10), None),
                ("ins4", "4", D(2021, 5, 11), t(2021, 5, 11), None),            # insider: stored by the pre-filter run
                ("e301", "8-K", D(2021, 6, 2), t(2021, 6, 2, 14), "3.01,9.01"),
                ("s3", "S-3", D(2021, 6, 3), None, None)]])
        c.execute(pit_facts.insert(), [
            {"cik": cik, "family": fam, "concept": con, "period_start": ps, "period_end": pe, "val": v, "filed": f,
             "accession": a, "form": fo, "run_id": "t"}
            for fam, con, ps, pe, v, f, a, fo in [
                ("OCF", OCF, D(2020, 1, 1), D(2020, 12, 31), -120.0, D(2021, 3, 1), "k21", "10-K"),
                ("CASH", CASH, None, D(2020, 12, 31), 60.0, D(2021, 3, 1), "k21", "10-K"),
                ("SHARES_DEI", "EntityCommonStockSharesOutstanding", None, D(2021, 2, 20), 1e7, D(2021, 3, 1), "k21", "10-K"),
                ("SHARES_DEI", "EntityCommonStockSharesOutstanding", None, D(2021, 5, 1), 1.1e7, D(2021, 5, 10), "q21", "10-Q"),
                ("SHARES_DEI", "EntityCommonStockSharesOutstanding", None, D(2021, 5, 1), 9e9, D(2021, 5, 11), "ins4",
                 "10-Q")]])  # its own form looks harmless: only the filing it cites (a Form 4) betrays it
        c.execute(pit_backfill_progress.insert(), [{"job": "submissions", "cik": cik, "status": sub, "rows": 5, "attempts": 1,
                                                     "run_id": "t"},
                                                    {"job": "xbrl", "cik": cik, "status": xbrl, "rows": 5, "attempts": 1,
                                                     "run_id": "t"}])


@db
@pytest.mark.usefixtures("clean")
def test_readers_never_return_insider_forms_even_from_the_pre_filter_rows(engine):
    from app.pit import readers
    _seed_pit(engine)
    with engine.connect() as c:
        fs = readers.filings(c, [CIK])[CIK]
        assert [f["accession"] for f in fs] == ["k21", "q21", "e301", "s3"]
        assert readers.filings(c, [CIK], ["4", "8-K"])[CIK][0]["accession"] == "e301"  # asking for a Form 4 is not enough
        assert "ins4" not in readers.acceptance(c, [CIK])[CIK]
        assert readers.progress(c, [CIK])[CIK]["submissions"]["status"] == "done"
        # facts: neither one citing an insider filing (ins4) nor one whose own form is insider is ever returned
        c.execute(text("""INSERT INTO pit_facts (cik, family, concept, period_end, val, filed, accession, form, run_id)
                          VALUES (:c, 'CASH', :k, '2021-04-30', 1, '2021-05-12', 'own4', '4', 't')"""), {"c": CIK, "k": CASH})
        got = readers.facts(c, [CIK], ["EntityCommonStockSharesOutstanding", CASH])[CIK]
        assert {f.accession for f in got} == {"k21", "q21"}


@db
@pytest.mark.usefixtures("clean")
def test_pit_export_feeds_runway_dilution_proxy_and_complete_coverage(engine, tmp_path, monkeypatch):
    """scripts/risk_source_export.py --source pit -> reconstruct: the labels and a coverage that is COMPLETE for
    FINANCING_ACTIONS / DILUTION / CASH_RUNWAY and PROXY_VALIDATED (never COMPLETE) for THESIS."""
    import importlib.util

    from app.replay.risk_history import PROXY_VALIDATED, RiskHistoryStore, reconstruct
    from tests.test_risk_history import _meta, _write
    _seed_pit(engine)
    spec = importlib.util.spec_from_file_location("rse", "scripts/risk_source_export.py")
    rse = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rse)
    with engine.connect() as c:
        rec = rse._batch_rows(c, [CIK], pit=True)[CIK]
    # a fact citing the (pre-filter) insider filing never reaches DILUTION: it would have replaced q21's same-period
    # count (9e9 vs 1.1e7) in detect_dilution's series
    assert [s[0] for s in rec["shares"]] == ["k21", "q21"] and [s[4] for s in rec["shares"]] == [10_000_000, 11_000_000]
    assert "ins4" not in rec["acceptance"]
    assert [f["accession"] for f in rec["proxy_8k"]] == ["e301"] and rec["pit"]["xbrl"]["status"] == "done"
    meta = _meta(start="2021-03-01", end="2021-06-30", pit=True, actions_source="sec_filings")
    rec.update(tenk=[], tenk_other=[], efts_hits=[], actions={})
    st = RiskHistoryStore(URL)
    with st.engine.begin() as c:
        c.execute(text("DROP SCHEMA IF EXISTS risk_hist_v3 CASCADE"))
    out = reconstruct(st, "pit1", _write(tmp_path / "p.jsonl", meta, [json.loads(json.dumps(rec, default=str))]))
    assert out["coverage"]["FINANCING_ACTIONS"] == "COMPLETE" and out["coverage"]["DILUTION"] == "COMPLETE"
    assert out["coverage"]["CASH_RUNWAY"] == "COMPLETE" and out["coverage"]["THESIS_INVALIDATION"] == PROXY_VALIDATED
    labels, cov = st.load([CIK], datetime(2021, 6, 30, 23, tzinfo=UTC), "pit1")
    rw = [x for x in labels if x.risk_type == "CASH_RUNWAY"]
    assert [(x.source_document_id, x.severity, x.available_at) for x in rw] == [
        ("k21", "HIGH", datetime(2021, 3, 1, 21, tzinfo=UTC))]  # 60 / (120 / 12) = 6 months, from its acceptance
    assert [x.source_document_id for x in labels if x.risk_type == "THESIS_INVALIDATION_PROXY"] == ["e301"]


def test_pit_coverage_is_partial_while_any_issuer_backfill_is_missing():
    from app.replay.risk_history import Coverage, _pit_coverage
    fam = {}
    inv = {"A": {"pit": {"submissions": {"status": "done", "date": "2026-10-01"}, "xbrl": {"status": "not_found"}}},
           "B": {"pit": {"submissions": {"status": "failed"}, "xbrl": {"status": "done"}}}}
    _pit_coverage(fam, {"actions_source": "sec_filings"}, inv, ["A", "B"], D(2026, 9, 30))
    c = Coverage(fam)
    assert {f: c.status(f) for f in ("FINANCING_ACTIONS", "DILUTION", "CASH_RUNWAY", "THESIS_INVALIDATION")} == \
        {"FINANCING_ACTIONS": "PARTIAL", "DILUTION": "PARTIAL", "CASH_RUNWAY": "PARTIAL", "THESIS_INVALIDATION": "PARTIAL"}
    fam2 = {}  # every issuer complete but the actions would not come from the SEC history: not COMPLETE
    _pit_coverage(fam2, {}, {"A": inv["A"]}, ["A"], D(2026, 9, 30))
    assert fam2["FINANCING_ACTIONS"]["status"] == "PARTIAL" and fam2["CASH_RUNWAY"]["status"] == "COMPLETE"
    assert fam2["CASH_RUNWAY"]["to"] == D(2026, 9, 30)  # never past the export horizon


def test_proxy_validated_is_never_full_and_only_full_with_proxy_when_the_run_uses_it():
    from app.replay.risk_history import FAMILIES, PROXY_VALIDATED, Coverage, coverage_policy
    rng = {"from": D(2015, 1, 1), "to": D(2026, 9, 30)}
    cov = Coverage({f: {"status": "COMPLETE", **rng} for f in FAMILIES})
    cov.families["THESIS_INVALIDATION"] = {"status": PROXY_VALIDATED, **rng, "validation": {"recall": 0.888}}
    s, e = D(2021, 3, 1), D(2021, 6, 30)
    off, on = coverage_policy(cov, s, e), coverage_policy(cov, s, e, allow_proxy=True)
    assert off["comparability"] == "LIMITED" and off["not_complete"] == {"THESIS_INVALIDATION": PROXY_VALIDATED}
    assert on["comparability"] == "FULL_WITH_PROXY" and on["flag"] == "RISK_HISTORY_COMPLETE_WITH_PROXY"
    assert on["proxy"]["THESIS_INVALIDATION"]["validation"] == {"recall": 0.888}
    cov.families["THESIS_INVALIDATION"]["status"] = "COMPLETE"
    assert coverage_policy(cov, s, e, allow_proxy=True)["comparability"] == "FULL"  # a real history stays FULL


def test_runs_with_and_without_the_proxy_are_a_comparable_pair():
    from tests.test_replay import cfg
    a, b = cfg("pa"), cfg("pb", thesis_proxy=True)
    assert a.comparison_key() == b.comparison_key() and a.config_hash() != b.config_hash()
    # a run configured before the option (no thesis_proxy) keeps its hash
    import hashlib
    legacy = {k: v for k, v in a.as_dict().items() if k not in ("replay_id", "notes", "thesis_proxy")}
    assert a.config_hash() == hashlib.sha256(json.dumps(legacy, sort_keys=True).encode()).hexdigest()[:16]


# ------------------------------------------------------------------ inside a replay (database)

@db
def test_an_action_is_staged_at_its_acceptance_time_and_the_guard_catches_an_early_one():
    from app.replay.engine import ReplayRun
    from app.replay.errors import ReplayLeakage
    from app.replay.visibility import Guards, check_state
    from tests.test_replay import cfg, source
    src = source()
    t = datetime(2021, 2, 8, 20, 0, tzinfo=UTC)  # 15:00 New York, during the session
    src.corporate_actions = [{"cik": CIK, "action": "UNREGISTERED_SALE", "action_date": D(2021, 2, 8), "accession": "p1",
                              "source": "pit:sec_submissions", "accepted_at": t}]
    r = ReplayRun(cfg("t_act_time"), src, URL)
    r.store.create()
    pre, close = datetime.combine(D(2021, 2, 8), time(9), NY), datetime.combine(D(2021, 2, 8), time(18), NY)
    with r.store.engine.begin() as c:
        assert r.stager.stage_actions(c, pre) == 0  # not yet accepted at the morning step
        assert r.stager.stage_actions(c, close) == 1  # known at that day's close step (no day-end wait)
        check_state(c, Guards(), close, D(2021, 2, 8), D(2021, 2, 8), {})
        with pytest.raises(ReplayLeakage) as e:
            check_state(c, Guards(), pre, D(2021, 2, 8), None, {})
    assert e.value.kind == "FUTURE_CORPORATE_ACTION"


@db
def test_a_runway_label_blocks_the_gate_only_from_its_public_time():
    from app.replay.risk_history import detect_cash_runway
    from tests.test_replay import _event, cfg, q, run, source
    src = source(events=[_event(1, datetime(2021, 2, 5, 21, 30, tzinfo=UTC)), _event(2, datetime(2021, 3, 19, 21, 30, tzinfo=UTC))])
    k = datetime(2021, 3, 15, 20, 5, tzinfo=UTC)
    facts = [_ocf(-120.0, D(2020, 1, 1), D(2020, 12, 31), D(2021, 3, 15), "k"), _cash(60.0, D(2020, 12, 31), D(2021, 3, 15), "k")]
    src.risk_labels = detect_cash_runway(CIK, facts, {"k": k})
    _, res = run(cfg("t_runway_gate", end=D(2021, 3, 26)), src)
    assert res.status == "COMPLETED", res.leakage
    assert res.rejected.get("SEVERE_FINANCING_RISK") == 1  # the March event: 6 months of runway, production SEVERE
    assert [r[0] for r in q("replay_t_runway_gate", "SELECT event_id FROM proposed_trades")] == [1]
    assert q("replay_t_runway_gate", "SELECT severity, detected_at FROM discovery_risks WHERE risk = 'CASH_RUNWAY'") == \
        [("HIGH", k)]


@db
def test_an_adr_has_no_market_cap_in_the_replay_either():
    from tests.test_replay import cfg, q, run, source
    src = source()
    src.corporate_actions = [{"cik": CIK, "action": "DEPOSITARY_PROGRAM", "action_date": D(2015, 3, 2), "accession": "f6",
                              "source": "pit:sec_submissions", "accepted_at": datetime(2015, 3, 2, 21, tzinfo=UTC)}]
    run(cfg("t_adr", end=D(2021, 2, 12)), src)
    n, known = q("replay_t_adr", "SELECT count(*), count(market_cap) FROM universe_snapshots")[0]
    assert n > 0 and known == 0  # rows exist (price, shares) but the capitalisation is unknown
    run(cfg("t_dom", end=D(2021, 2, 12)), source())
    assert q("replay_t_dom", "SELECT count(*) = count(market_cap) FROM universe_snapshots") == [(True,)]


@db
def test_the_thesis_proxy_is_staged_only_when_the_run_asks_and_is_reported_separately():
    from app.replay.engine import risk_history
    from app.replay.risk_history import FAMILIES, PROXY_VALIDATED, Coverage, detect_thesis_proxy
    from tests.test_replay import cfg, q, run, source
    t = datetime(2021, 2, 10, 21, 2, tzinfo=UTC)
    labels = detect_thesis_proxy(CIK, [{"accession": "d1", "form": "8-K", "filing_date": D(2021, 2, 10), "acceptance_at": t,
                                        "items": "3.01"}])
    rng = {"from": D(2015, 1, 1), "to": D(2026, 9, 30)}
    cov = Coverage({f: {"status": "COMPLETE", **rng} for f in FAMILIES})
    cov.families["THESIS_INVALIDATION"] = {"status": PROXY_VALIDATED, **rng}
    for rid, on in (("t_proxy_on", True), ("t_proxy_off", False)):
        src = source()
        src.risk_labels, src.risk_cov, src.actions_source = list(labels), cov, "sec_filings"
        _, res = run(cfg(rid, end=D(2021, 2, 19), thesis_proxy=on), src)
        assert res.status == "COMPLETED", res.leakage
    assert q("replay_t_proxy_on", "SELECT cik, change, created_at FROM thesis_changes") == [(CIK, "THESIS_INVALIDATION", t)]
    assert q("replay_t_proxy_off", "SELECT count(*) FROM thesis_changes") == [(0,)]
    on = risk_history(cfg("x", thesis_proxy=True), src)
    off = risk_history(cfg("x"), src)
    assert on["comparability"] == "FULL_WITH_PROXY" and on["gates_on_proxy"] == ["exit:D_THESIS thesis invalidation"]
    assert off["comparability"] == "LIMITED" and "exit:D_THESIS thesis invalidation" in off["gates_not_applied"]
