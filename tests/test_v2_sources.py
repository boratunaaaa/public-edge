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
