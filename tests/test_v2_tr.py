"""TR: SPK bulletins (redacted real text) - share issue approvals, enforcement codes, no personal data."""
from datetime import date
from pathlib import Path

from sqlalchemy import text

from app.core.provenance import end_of_local_day

FX = Path(__file__).parent / "fixtures" / "v2" / "tr"


def test_share_issue_table_with_wrapped_company_name():
    from app.sources.tr.spk import events_of, parse_text
    b = parse_text((FX / "spk_2026-64_redacted.txt").read_text())
    assert b.number == "2026/64" and b.date == date(2026, 9, 23)
    by = {x["company"]: x for x in b.issues}
    assert by["Kiler Holding AŞ"]["bonus_internal"] == 1_300_000_000 and by["Kiler Holding AŞ"]["rights"] is None
    assert by["Ayes Çelik Hasır ve Çit Sanayi AŞ"]["new_capital"] == 500_000_000
    assert [k for k, _ in events_of(b)] == ["TR_BONUS_ISSUE_APPROVAL", "TR_BONUS_ISSUE_APPROVAL"]


def test_enforcement_section_yields_codes_only():
    from app.sources.tr.spk import events_of, parse_text
    b = parse_text((FX / "spk_2026-65_redacted.txt").read_text())
    ev = events_of(b)
    assert ev == [("TR_SPK_ENFORCEMENT", {"code": "OZATD"})]


def test_fixtures_hold_no_personal_data():
    import re
    for f in FX.glob("*.txt"):
        t = f.read_text()
        assert not re.search(r"\d{2}\*{3,}\d{2}", t) and "IBAN" not in t


def test_store_resolves_code_via_tr_master_and_names_need_review(engine, clean):
    from app.markets import master_db
    from app.sources.tr.spk import parse_text, store
    with engine.begin() as c:
        master_db.upsert_security(c, security_id="TR:XIST:OZATD", country="TR", exchange="XIST", issuer_id="OZATD",
                                  issuer_name="Özata Denizcilik Sanayi ve Ticaret AŞ")
        master_db.add_symbol_period(c, "TR:XIST:OZATD", "OZATD", date(2025, 1, 1), None, "kap_members", exchange="XIST")
        ids = store(c, parse_text((FX / "spk_2026-65_redacted.txt").read_text()), "https://spk.gov.tr/x/2026-65.pdf", is_backfill=True)
        ev = c.execute(text("SELECT country, exchange, security_id, available_at, event_type FROM events")).mappings().one()
        ids2 = store(c, parse_text((FX / "spk_2026-64_redacted.txt").read_text()), "https://spk.gov.tr/x/2026-64.pdf", is_backfill=True)
        nr = c.execute(text("SELECT count(*) FROM dataset_records WHERE needs_review AND country = 'TR'")).scalar()
    assert len(ids) == 1 and ev["security_id"] == "TR:XIST:OZATD" and ev["available_at"] == end_of_local_day(date(2026, 9, 25), "TR")
    assert ids2 == [] and nr == 2  # Kiler / Ayes are not in this test's TR master: stored for review, no event


def test_kap_member_list_builds_tr_master(engine, clean):
    from app.markets import master_db
    from app.sources.tr.members import parse, sync
    rows = parse((FX / "kap_bist_sirketler_excerpt.txt").read_text())
    assert len(rows) == 28 and rows[0]["codes"] == ["ACSEL"] and any(len(r["codes"]) > 1 for r in rows)
    with engine.begin() as c:
        n = sync(c, rows, date(2026, 9, 28))
        assert master_db.resolve(c, "ACSEL", date(2026, 9, 28), country="TR") == "TR:XIST:ACSEL"
        assert master_db.resolve(c, "ACSEL", date(2026, 1, 1), country="TR") is None  # before first sight: unknown
    assert n >= 28


def test_turkish_legal_suffix_normalises_like_the_kap_title():
    from app.entities.names import normalize_company
    assert normalize_company("Kiler Holding AŞ") == normalize_company("KİLER HOLDİNG A.Ş.")
    assert normalize_company("Ayes Çelik Hasır ve Çit Sanayi AŞ") == normalize_company("AYES ÇELİK HASIR VE ÇİT SANAYİ A.Ş.")
