"""V2 source adapters on real public records (tests/fixtures/v2/*/README.md: source URL + retrieval date)."""
import json
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import text

from app.core.provenance import end_of_local_day

FX = Path(__file__).parent / "fixtures" / "v2"
UTC = timezone.utc


# ------------------------------------------------------------------ FINRA / SEC FTD

def test_finra_short_volume_parse_real_file():
    from app.sources.us.short_data import parse_finra
    rows = parse_finra((FX / "us" / "finra_CNMSshvol20260925_head.txt").read_text())
    assert len(rows) == 300 and rows[0]["symbol"] == "A" and rows[0]["trade_date"] == date(2026, 9, 25)
    assert all(0 <= r["short_volume"] <= r["total_volume"] for r in rows)


def test_finra_header_change_is_detected_not_silently_parsed():
    from app.sources.us.short_data import FormatChanged, parse_finra
    with pytest.raises(FormatChanged):
        parse_finra("Date|Symbol|Short|Total\n20260925|A|1|2\n")


def test_ftd_parse_real_file():
    from app.sources.us.short_data import parse_ftd
    rows = parse_ftd((FX / "us" / "sec_cnsfails202608b_head.txt").read_text())
    r = next(x for x in rows if x["symbol"] == "MDXH")
    assert r["cusip"] == "B5950S113" and r["quantity"] == 1206826 and r["price"] == pytest.approx(0.81)
    assert all(x["settlement_date"].month == 8 for x in rows)


def test_ftd_available_at_is_publication_never_settlement(engine, clean, monkeypatch):
    from app.sources.us import short_data as S

    class R:
        headers = {"last-modified": "Tue, 15 Sep 2026 13:09:21 GMT"}
        content = b""
    txt = (FX / "us" / "sec_cnsfails202608b_head.txt").read_text()
    import io
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("cnsfails202608b", txt)
    R.content = buf.getvalue()
    monkeypatch.setattr(S.http, "get", lambda url, **k: R)
    monkeypatch.setattr(S.rawlake, "store", lambda *a, **k: 1)
    out = S.ingest_ftd_file(engine, "cnsfails202608b")
    assert out["rows"] == 300
    with engine.connect() as c:
        mn, mx = c.execute(text("SELECT min(available_at), max(settlement_date) FROM fails_to_deliver")).one()
    assert mn == datetime(2026, 9, 15, 13, 9, 21, tzinfo=UTC) and mx < mn.date()


# ------------------------------------------------------------------ 13F

def test_13f_parse_and_position_changes_across_two_real_quarters(engine, clean):
    from app.sources.us import form13f as F
    for q in ("13f_2026q1_mini.zip", "13f_2026q2_mini.zip"):
        p = F.parse_dataset(str(FX / "us" / q))
        F.store(engine, p, source=q)
    with engine.connect() as c:
        fil = c.execute(text("SELECT manager_cik, period, filed_date, available_at FROM inst_filings_13f ORDER BY period")).mappings().all()
        assert {f["period"] for f in fil} >= {date(2026, 3, 31), date(2026, 6, 30)}
        for f in fil:
            assert f["available_at"] == end_of_local_day(f["filed_date"])  # filing date, never the period end
        ch = F.position_changes(c, "0002019841", date(2026, 6, 30))
    kinds = {x["change"] for x in ch}
    assert ch and kinds <= {"NEW", "EXIT", "INCREASE", "DECREASE", "HOLD"}
    assert abs(sum(x["weight"] for x in ch if x["change"] != "EXIT") - 1.0) < 1e-6


def test_13f_value_is_normalised_to_dollars():
    from app.sources.us import form13f as F
    p = F.parse_dataset(str(FX / "us" / "13f_2026q2_mini.zip"))
    tot = {f["accession"]: f["total_value"] for f in p["filings"]}
    by = {}
    for h in p["holdings"]:
        by[h["accession"]] = by.get(h["accession"], 0) + h["value"]
    for acc, v in by.items():
        assert v == pytest.approx(tot[acc], rel=0.02)


# ------------------------------------------------------------------ ClinicalTrials.gov

def test_trial_parse_real_and_first_snapshot_only_claims_provable_history():
    from app.sources.us.clinical import diff_events, parse_study
    d = json.loads((FX / "us" / "ctgov_pfizer_results_2026-08.json").read_text())
    s = parse_study(d["studies"][0])
    assert s["nct_id"].startswith("NCT") and s["sponsor_class"] == "INDUSTRY" and s["results_first_post_date"] >= date(2026, 8, 1)
    kinds = [k for k, _ in diff_events(None, s)]
    assert kinds == ["TRIAL_REGISTERED", "RESULTS_POSTED"]  # never a status change without its posting date


def test_trial_forward_diff_detects_termination_and_phase_change():
    from app.sources.us.clinical import diff_events, parse_study
    d = json.loads((FX / "us" / "ctgov_pfizer_terminated.json").read_text())
    cur = parse_study(d["studies"][0])
    prev = {**cur, "overall_status": "RECRUITING", "phases": ["PHASE1"], "results_first_post_date": cur["results_first_post_date"]}
    kinds = {k for k, _ in diff_events(prev, cur)}
    assert "TRIAL_TERMINATED" in kinds
    if cur["phases"] and cur["phases"] != ["PHASE1"]:
        assert "TRIAL_PHASE_CHANGE" in kinds


def test_trial_unmapped_sponsor_is_not_an_event(engine, clean):
    from app.sources.us.clinical import process_study
    d = json.loads((FX / "us" / "ctgov_pfizer_results_2026-08.json").read_text())
    with engine.begin() as c:
        assert process_study(c, d["studies"][0])["status"] == "UNMAPPED"  # Pfizer not in the test security master
        assert c.execute(text("SELECT count(*) FROM events")).scalar() == 0


def test_trial_mapped_sponsor_creates_results_event_at_posting_date(engine, clean):
    from app.markets import identity
    from app.sources.us.clinical import process_study
    with engine.begin() as c:
        c.execute(text("INSERT INTO security_master (cik, name) VALUES ('0000078003', 'PFIZER INC')"))
    identity.reset_cache()
    d = json.loads((FX / "us" / "ctgov_pfizer_results_2026-08.json").read_text())
    with engine.begin() as c:
        out = process_study(c, d["studies"][0], is_backfill=True)
        ev = c.execute(text("SELECT event_type, available_at, public_date, issuer_cik FROM events WHERE event_type='RESULTS_POSTED'")).mappings().one()
    identity.reset_cache()
    assert out["status"] == "STORED" and ev["issuer_cik"] == "0000078003"
    assert ev["available_at"] == end_of_local_day(ev["public_date"])


def _pfizer(c):
    c.execute(text("INSERT INTO security_master (cik, name) VALUES ('0000078003', 'PFIZER INC') ON CONFLICT DO NOTHING"))


def test_delayed_live_status_change_stays_in_the_live_lane(engine, clean):
    """A live run that sees a status change days after it was posted (outage, delayed poll) keeps it LIVE: the lane is
    decided by provenance (historical load or not), never by event age."""
    import copy
    from datetime import timedelta

    from app.markets import identity
    from app.sources.us.clinical import process_study
    with engine.begin() as c:
        _pfizer(c)
    identity.reset_cache()
    cur = json.loads((FX / "us" / "ctgov_pfizer_terminated.json").read_text())["studies"][0]
    prev = copy.deepcopy(cur)
    prev["protocolSection"]["statusModule"]["overallStatus"] = "RECRUITING"
    prev["protocolSection"]["statusModule"]["lastUpdatePostDateStruct"] = {"date": "2009-01-05", "type": "ACTUAL"}
    try:
        with engine.begin() as c:
            process_study(c, prev, first_seen=end_of_local_day(date(2009, 1, 5)), is_backfill=False)
            posted = date.fromisoformat(cur["protocolSection"]["statusModule"]["lastUpdatePostDateStruct"]["date"])
            out = process_study(c, cur, first_seen=end_of_local_day(posted) + timedelta(days=10), is_backfill=False)
            row = c.execute(text("SELECT is_backfill FROM events WHERE event_type = 'TRIAL_TERMINATED'")).scalar()
    finally:
        identity.reset_cache()
    assert out["status"] == "STORED" and row is False


@pytest.mark.parametrize("historical", [True, False])
def test_only_the_explicit_historical_load_goes_to_the_backfill_lane(engine, clean, monkeypatch, historical):
    from app.markets import identity
    from app.sources.us import clinical
    page = json.loads((FX / "us" / "ctgov_pfizer_results_2026-08.json").read_text())
    page.pop("nextPageToken", None)
    monkeypatch.setattr(clinical, "fetch_page", lambda since=None, token=None, page_size=500: page)
    with engine.begin() as c:
        _pfizer(c)
    identity.reset_cache()
    try:
        clinical.ClinicalTrialsSource().run_live(engine, since=date(2000, 1, 1), historical=historical)
    finally:
        identity.reset_cache()
    with engine.connect() as c:
        flags = {r[0] for r in c.execute(text("SELECT is_backfill FROM events"))}
    assert flags == {historical}  # a live run with a watermark stays live even for an old registration


def test_trial_lane_repair_moves_only_the_verified_historical_cohort(engine, clean):
    from datetime import datetime, timedelta, timezone

    from app.backtest.jobs import select_queue
    from app.db.store import create_event
    from app.jobs.trial_lane_repair import run
    w0, w1 = datetime(2026, 9, 28, 12, 5, 48, tzinfo=timezone.utc), datetime(2026, 9, 28, 12, 18, 41, tzinfo=timezone.utc)
    ids = {}
    with engine.begin() as c:
        for key, created in (("hist", w0 + timedelta(minutes=3)), ("live_old", datetime(2026, 9, 28, 5, 26, tzinfo=timezone.utc)),
                             ("live_delayed", datetime(2026, 9, 29, 17, 5, tzinfo=timezone.utc))):
            ids[key] = create_event(c, event_type="TRIAL_REGISTERED", dedupe_key=key, public_date=date(2024, 1, 10),
                                    ticker="ZBIO", issuer_cik="0001953926", title="t", payload={}, source_url="u",
                                    is_backfill=False, event_family="US_TRIAL_CATALYST")
            c.execute(text("UPDATE events SET created_at = :t WHERE id = :i"), {"t": created, "i": ids[key]})
        # an earlier age-based apply also moved the 05:26 live-run event
        c.execute(text("UPDATE events SET is_backfill = true WHERE id = :i"), {"i": ids["live_old"]})
        c.execute(text("""INSERT INTO data_cleanup_audit (run_id, action, table_name, row_id, reason, dry_run)
                          VALUES ('apply-old', 'RECLASSIFY', 'events', :i, 'age', false)"""), {"i": ids["live_old"]})
    with engine.begin() as c:
        dry = run(c, apply=False, undo_run="apply-old", window=(w0, w1))
        assert (dry["to_backfill"], dry["undo_to_live"]) == (1, 1)
    with engine.begin() as c:
        out = run(c, apply=True, undo_run="apply-old", window=(w0, w1))
    assert (out["moved"], out["reverted"], out["audited"]) == (1, 1, 2)
    with engine.connect() as c:
        flags = dict(c.execute(text("SELECT dedupe_key, is_backfill FROM events")).all())
        now = datetime.now(timezone.utc)
        backfill_lane = {r[0] for r in select_queue(c, now, backfill=True, limit=100)}
        live_lane = {r[0] for r in select_queue(c, now, backfill=False, limit=100)}
    assert flags == {"hist": True, "live_old": False, "live_delayed": False}
    assert backfill_lane == {ids["hist"]} and live_lane == {ids["live_old"], ids["live_delayed"]}
    with engine.begin() as c:  # idempotent
        assert run(c, apply=True, undo_run="apply-old", window=(w0, w1))["audited"] == 0


# ------------------------------------------------------------------ openFDA / CFTC

def test_openfda_only_material_approvals():
    from app.sources.us.fda import approvals
    d = json.loads((FX / "us" / "openfda_drugsfda_orig_2026-08.json").read_text())
    out = [a for r in d["results"] for a in approvals(r, date(2026, 8, 1), date(2026, 9, 25))]
    assert out and all(a["kind"] in ("FDA_APPROVAL", "FDA_EFFICACY_SUPPLEMENT") for a in out)
    assert all(date(2026, 8, 1) <= a["date"] <= date(2026, 9, 25) for a in out)


def test_cftc_parse_and_friday_release():
    from app.sources.us.cftc import available_at, parse_row
    rows = json.loads((FX / "us" / "cftc_legacy_crude_2026-09-15.json").read_text())
    p = parse_row(rows[0])
    assert p["commodity"] == "CRUDE OIL" and p["net_noncommercial"] == 59781 - 23041
    assert available_at(date(2026, 9, 15)) == end_of_local_day(date(2026, 9, 18))  # Tuesday data, Friday release


# ------------------------------------------------------------------ Canada

def test_canadabuys_parse_real_and_no_personal_data():
    from app.sources.ca.official import parse_contracts
    txt = (FX / "ca" / "canadabuys_contracthistory_2026-2027_head.csv").read_text(encoding="utf-8")
    rows = parse_contracts(txt)
    nmg = next(r for r in rows if r["supplier"] == "NMG Matawinie Inc.")
    assert nmg["total_value"] == pytest.approx(469896667.16) and nmg["publication_date"] == date(2026, 5, 15)
    assert not any("contact" in k.lower() or "@" in str(v) for r in rows for k, v in r.items())


def test_canadabuys_subsidiary_needs_a_reviewed_edge(engine, clean):
    from app.markets import master_db
    from app.sources.ca.official import parse_contracts, store_contract
    rows = parse_contracts((FX / "ca" / "canadabuys_contracthistory_2026-2027_head.csv").read_text(encoding="utf-8"))
    nmg = next(r for r in rows if r["supplier"] == "NMG Matawinie Inc.")
    with engine.begin() as c:
        master_db.build_ca_directory(c, [{"ticker": "NOU", "company_name": "Nouveau Monde Graphite Inc.", "exchange": "XTSX"}], date(2026, 9, 1))
        assert store_contract(c, nmg, is_backfill=True) is None  # name differs: no event, record needs review
        assert c.execute(text("SELECT needs_review FROM dataset_records")).scalar() is True
        c.execute(text("""INSERT INTO entity_edges (src_type, src_id, rel, dst_type, dst_id, source, confidence, evidence)
                          VALUES ('ENTITY_NAME', 'NMG MATAWINIE', 'SUBSIDIARY_OF', 'SECURITY', 'CA:XTSX:NOU', 'manual_review', 'HIGH',
                                  '{"doc": "issuer MD&A names NMG Matawinie Inc. as wholly-owned subsidiary"}')"""))
        c.execute(text("DELETE FROM dataset_records"))
        eid = store_contract(c, nmg, is_backfill=True)
        ev = c.execute(text("SELECT country, exchange, security_id, available_at FROM events WHERE id = :i"), {"i": eid}).mappings().one()
    assert ev["country"] == "CA" and ev["security_id"] == "CA:XTSX:NOU"
    assert ev["available_at"] == end_of_local_day(date(2026, 5, 15), "CA")


def test_grants_only_businesses_and_historical_rows_are_not_point_in_time():
    import csv
    from app.sources.ca.official import parse_grant_row
    rows = list(csv.DictReader(open(FX / "ca" / "grants_head.csv", encoding="utf-8")))
    parsed = [parse_grant_row(r) for r in rows]
    assert sum(p is not None for p in parsed) == sum(r["recipient_type"] == "F" for r in rows) > 0


def test_valet_parse_real():
    from app.sources.ca.official import parse_valet
    obs = parse_valet(json.loads((FX / "ca" / "boc_valet_fxusdcad_v39079_2026-09.json").read_text()))
    assert {o["series"] for o in obs} == {"FXUSDCAD", "V39079"} and all(o["value"] > 0 for o in obs)


def test_ticker_reuse_resolution_is_period_bound(engine, clean):
    from app.markets import master_db
    with engine.begin() as c:
        for sid, f, t in (("US:0000000001:COM", date(2010, 1, 1), date(2015, 6, 1)), ("US:0000000002:COM", date(2018, 1, 1), None)):
            master_db.upsert_security(c, security_id=sid, country="US", issuer_id=sid[3:13], issuer_name=sid)
            master_db.add_symbol_period(c, sid, "ABC", f, t, "test")
        assert master_db.resolve(c, "ABC", date(2012, 1, 1)) == "US:0000000001:COM"
        assert master_db.resolve(c, "ABC", date(2020, 1, 1)) == "US:0000000002:COM"
        assert master_db.resolve(c, "ABC", date(2016, 1, 1)) is None  # between listings: never guessed
        assert master_db.collisions(c) == []
        master_db.add_symbol_period(c, "US:0000000002:COM", "ABC", date(2014, 1, 1), date(2014, 12, 31), "vendor")
        assert master_db.resolve(c, "ABC", date(2014, 6, 1)) is None and master_db.collisions(c)


def test_cusip_check_digit():
    from app.markets.identity import cusip_check_digit_ok
    assert cusip_check_digit_ok("037833100") and cusip_check_digit_ok("B5950S113") is not None
    assert not cusip_check_digit_ok("037833101")


def test_usaspending_resumes_from_page_checkpoint_after_disconnect(engine, clean, monkeypatch):
    from app.sources.usaspending import awards as A
    calls, failed = [], []

    def search(start, end, page, date_type="new_awards_only"):
        calls.append(page)
        if page == 3 and not failed:
            failed.append(1)
            raise RuntimeError("Server disconnected without sending a response.")
        return {"results": [], "page_metadata": {"hasNext": page < 4}}
    monkeypatch.setattr(A, "search_new_awards", search)
    monkeypatch.setattr(A, "_search_resilient", lambda s, e, p, dt, **k: search(s, e, p, dt))
    import datetime as _dt
    today = _dt.date.today()
    with pytest.raises(RuntimeError):
        A.ingest_range(engine, today, today, is_backfill=True, checkpoint=lambda p: calls.append(("cp", p)))
    assert ("cp", 2) in calls and ("cp", 3) not in calls
    calls.clear()
    A.ingest_range(engine, today, today, is_backfill=True, start_page=3)
    assert calls == [3, 4]


def test_build_us_security_master_from_sec_master(engine, clean):
    from app.markets import master_db
    with engine.begin() as c:
        c.execute(text("INSERT INTO security_master (cik, name, status) VALUES ('0000000901', 'OLD CO', 'DELISTED'), ('0000000902', 'NEW CO', 'ACTIVE')"))
        c.execute(text("""INSERT INTO security_symbols (cik, symbol, first_seen, last_seen, n_obs, source) VALUES
                          ('0000000901', 'ABCD', '2012-01-01', '2015-06-01', 5, 'form345_filing'),
                          ('0000000902', 'ABCD', '2019-01-01', '2026-09-01', 5, 'form345_filing')"""))
        out = master_db.build_us(c)
        assert out["securities"] == 2
        assert master_db.resolve(c, "ABCD", date(2014, 1, 1)) == "US:0000000901:COM"
        assert master_db.resolve(c, "ABCD", date(2020, 1, 1)) == "US:0000000902:COM"
        assert master_db.resolve(c, "ABCD", date(2017, 1, 1)) is None


def test_live_ftd_source_takes_only_the_newest_files(engine, clean, monkeypatch):
    from app.sources.us import short_data as S
    seen = []
    monkeypatch.setattr(S, "ftd_files", lambda: ["cnsfails202608b", "cnsfails202608a", "cnsfails202607b", "cnsfails202401a"])
    monkeypatch.setattr(S, "ingest_ftd_file", lambda e, n, **k: seen.append(n) or {"rows": 0})
    S.SecFtdSource().run_live(engine)
    assert seen == ["cnsfails202608b", "cnsfails202608a"]
    seen.clear()
    S.SecFtdSource().run_live(engine, backfill_since="cnsfails202401a")
    assert seen == ["cnsfails202608b", "cnsfails202608a", "cnsfails202607b", "cnsfails202401a"]


def test_13f_member_lookup_tolerates_folder_prefix_and_case(tmp_path):
    import zipfile as zf
    from app.sources.us import form13f as F
    src = zf.ZipFile(str(FX / "us" / "13f_2026q2_mini.zip"))
    out = tmp_path / "x.zip"
    with zf.ZipFile(out, "w") as z:
        for n in src.namelist():
            z.writestr("FORM13F_2026/" + n.lower(), src.read(n))
    p = F.parse_dataset(str(out))
    assert len(p["filings"]) == 3 and p["holdings"]
