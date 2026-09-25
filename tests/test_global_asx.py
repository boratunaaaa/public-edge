"""ASX adapter on real public data (announcement list of 25 Sep 2026, markitdigital per-company JSON, ASX CSV)."""
import gzip
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.sources.au import asx

FX = Path(__file__).parent / "fixtures" / "global" / "au"


def _page():
    return gzip.decompress((FX / "prevBusDayAnns.html.gz").read_bytes()).decode()


def test_list_page_headers_and_rows():
    page = _page()
    assert asx.list_headers(page)[:4] == asx.LIST_HEADERS
    rows = asx.parse_list_page(page)
    assert len(rows) == 851 and asx.list_date(page).isoformat() == "2026-09-25"
    first = rows[0]
    assert first.code == "AGG" and first.ids_id == "03144322" and first.price_sensitive and first.pages == 4


def test_list_times_are_sydney_local_converted_to_utc():
    agg = asx.parse_list_page(_page())[0]
    assert agg.published_at == datetime(2026, 9, 25, 9, 29, tzinfo=timezone.utc)  # 7:29 pm AEST


def test_price_sensitive_flag_is_carried_to_the_canonical_event():
    rows = asx.parse_list_page(_page())
    ps = [r for r in rows if r.price_sensitive]
    assert 0 < len(ps) < len(rows)
    ev = next(e for e in (asx.to_canonical(r) for r in ps) if e)
    assert ev.price_sensitive is True and ev.to_json()["price_sensitive"] is True


def test_markit_json_parse_and_document_key():
    p = json.loads(gzip.decompress((FX / "markit_BHP.json.gz").read_bytes()))
    anns = asx.parse_markit("bhp", p)
    assert anns and anns[0].code == "BHP" and anns[0].published_at.tzinfo is not None
    assert anns[0].ids_id.isdigit() and len(anns[0].ids_id) == 8


@pytest.mark.parametrize("headline,family", [
    ("Change of Director's Interest Notice - T Streitberg", "DIRECTOR_INTEREST_CHANGE"),
    ("ASX Appendix 3X - Sundich", "DIRECTOR_INTEREST_CHANGE"),
    ("Becoming a substantial holder from CBA", "THRESHOLD_CROSSING"),
    ("Change in substantial holding", "THRESHOLD_CROSSING"),
    ("Trading Halt", "TRADING_RESTRICTION"),
    ("Dataworks Completes $3.0 million Placement", "PRIVATE_PLACEMENT"),
    ("Rights Issue Prospectus", "RIGHTS_ISSUE"),
    ("Update - Notification of buy-back - S32", "SHARE_BUYBACK"),
    ("Mineral Resource Increases to 59.0Mlbs U3O8 at Lance Project", "RESOURCE_UPDATE"),
    ("Diamond Drilling Extends High Grade Gold Mineralisation", "MINING_DISCOVERY"),
    ("Elizabeth Creek PFS Copper & Silver Results - Amended", "FEASIBILITY_STUDY"),
    ("Response to Iris Bidder's Statement", "MERGER"),
    ("Appointment of Interim Chief Executive Officer", "MANAGEMENT_CHANGE"),
    ("Annual Report 2026", "EARNINGS"),
])
def test_real_headlines_classified(headline, family):
    assert asx.classify(headline)[0] == family


def test_director_interest_never_invents_a_direction():
    fam, f = asx.classify("Change of Director's Interest Notice")
    assert fam == "DIRECTOR_INTEREST_CHANGE" and f["direction"] == "UNKNOWN"


def test_substantial_holder_subject_and_holder_are_not_swapped():
    fam, f = asx.classify("Change in substantial holding for TGN")
    assert f["subject_code"] == "TGN" and f["holder_is_announcer"]
    a = asx.Announcement("GWR", datetime(2026, 9, 25, 1, tzinfo=timezone.utc), False, "Change in substantial holding for TGN", "03144000")
    ev = asx.to_canonical(a)
    assert ev.ticker == "TGN" and ev.fields["holder"] == "GWR"
    fam, f = asx.classify("Becoming a substantial holder from CBA")
    assert f["holder"] == "CBA" and "subject_code" not in f


def test_other_company_prefix_is_the_subject():
    a = asx.Announcement("EVN", datetime(2026, 9, 25, 1, tzinfo=timezone.utc), False, "CNB: Dispatch of Scheme Booklet", "03144001")
    assert asx.to_canonical(a).ticker == "CNB"


def test_follow_up_and_admin_notices_are_not_events():
    for h in ("Cleansing notice - Placement", "Appendix 4G and Corporate Governance Statement", "Notice of Annual General Meeting/Proxy Form"):
        assert asx.classify(h) is None


def test_negated_agreement_is_not_a_partnership():
    assert asx.classify("Completion of Termination of Joint Venture") is None
    assert asx.classify("Termination of Offtake Agreement", price_sensitive=True)[1]["negated"] is True


def test_appendix_3b_is_low_confidence_capital_raise():
    a = asx.Announcement("UM1", datetime(2026, 9, 25, 1, tzinfo=timezone.utc), False, "Proposed issue of securities - UM1", "03144002")
    ev = asx.to_canonical(a)
    assert ev.family == "CAPITAL_RAISE" and ev.confidence == "LOW" and ev.fields["subtype"] == "APPENDIX_3B"


def test_amount_is_parsed_in_aud():
    assert asx.classify("Dataworks Completes $3.0 million Placement")[1]["amount_aud"] == 3_000_000


def test_day_of_real_announcements_yields_canonical_events():
    raw = [e for e in (asx.to_canonical(r) for r in asx.parse_list_page(_page())) if e]
    evs = asx.dedupe(raw)
    assert len(evs) >= 300
    assert all(e.country == "AU" and e.exchange == "XASX" and e.source_url.startswith("https://") for e in evs)
    assert len({e.dedupe_key for e in evs}) == len(evs)
    # a multi-ETF document stays one event PER security; a notice lodged under two codes collapses to one
    assert len([e for e in evs if e.source_id == "03144322"]) > 5
    jdo = [e for e in evs if e.ticker == "JDO" and e.family == "THRESHOLD_CROSSING"]
    assert len(jdo) == 1 and jdo[0].fields.get("holder") == "MQG"


def test_companies_csv_security_master():
    rows = asx.parse_companies_csv(gzip.decompress((FX / "ASXListedCompanies.csv.gz").read_bytes()).decode())
    assert len(rows) > 1500 and {"company_name", "ticker", "sector"} <= set(rows[0])
    assert any(r["ticker"] == "BHP" for r in rows)
