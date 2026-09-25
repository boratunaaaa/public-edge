"""Canada adapter on real public data (Canada NewsWire news sitemap 25-26 Sep 2026, TSX/TSXV directories, GNW RSS)."""
import gzip
import json
from pathlib import Path

import pytest

from app.sources.ca import newswire as nw
from app.sources.ca import sedi

FX = Path(__file__).parent / "fixtures" / "global" / "ca"


def _releases():
    out = []
    for p in (1, 2, 3):
        out += nw.parse_sitemap(gzip.decompress((FX / f"newswire_ca_sitemap_p{p}.xml.gz").read_bytes()).decode())
    return out


def test_sitemap_parse_real_pages():
    rel = _releases()
    assert len(rel) == 266
    assert all(r.published_at.tzinfo is not None and r.url.startswith(("https://www.newswire.ca/", "https://www.prnewswire.com/")) for r in rel)
    assert sum(1 for r in rel if r.tickers) >= 60


def test_exchange_mapping_and_foreign_tickers_kept_apart():
    rel = {r.title[:30]: r for r in _releases()}
    dpf = next(r for r in _releases() if r.title.startswith("DR. PHONE FIX"))
    assert dpf.tickers == [("XTSX", "DPF")]
    hpl = next(r for r in _releases() if r.title.startswith("Horizon Petroleum"))
    assert ("XTSX", "HPL") in hpl.tickers and any(t.startswith("Frankfurt") for t in hpl.other_tickers)
    assert rel


def test_html_entities_in_titles_are_decoded():
    hr = next(r for r in _releases() if r.title.startswith("H&R REIT"))
    assert "&amp;" not in hr.title


@pytest.mark.parametrize("title,family", [
    ("DEFENSE METALS ANNOUNCES NON-BROKERED PRIVATE PLACEMENT FOR GROSS PROCEEDS OF UP TO $6,000,000", "PRIVATE_PLACEMENT"),
    ("National Bank of Canada announces its intention to launch a normal course issuer bid", "SHARE_BUYBACK"),
    ("Lundin Gold Reports 204.26 g/t Au over 3.05 Metres at Bonza Sur; Quebrada Dorada Discovery Extended", "MINING_DISCOVERY"),
    ("1911 Gold Files NI 43-101 Technical Report for the Ogama-Rockland Gold Project Updated Mineral Resource", "RESOURCE_UPDATE"),
    ("Neo Performance Materials Signs MOU with Rainbow Rare Earths for Magnet Rare Earth Offtake", "OFFTAKE_AGREEMENT"),
    ("Silver Mountain Commences Production at Reliquias Mine", "PRODUCTION_START"),
    ("LARGO ANNOUNCES PRICING OF US$5.7 MILLION REGISTERED DIRECT OFFERING", "CAPITAL_RAISE"),
    ("Doseology Announces Proposed Shares-for-Debt Settlement and Resignation of Director", "DILUTION"),
    ("PIZZA PIZZA ROYALTY CORP. ANNOUNCES SEPTEMBER DIVIDEND", "DIVIDEND"),
    ("ORBIT GARANT REPORTS FISCAL 2026 FOURTH QUARTER AND YEAR-END FINANCIAL RESULTS", "EARNINGS"),
])
def test_real_titles_classified(title, family):
    assert nw.classify(title)[0] == family


@pytest.mark.parametrize("title", [
    "Quarterhill Introduces TOPAS-Registered Intersection Solution in United Kingdom",  # not a drill intersection
    "Decisive Dividend Corporation Announces Chief Operating Officer Transition and Provides Tariff Update",  # company name
    "RATIFICATION OF A NEW COLLECTIVE AGREEMENT: END OF THE LABOUR DISPUTE AT THE PRODUCE DISTRIBUTION CENTRE",
    "H&R REIT ANNOUNCES RECORD DATE AND MEETING DATE FOR SPECIAL MEETING",
    "i-80 Gold Publishes its 2025 Sustainability Report",
])
def test_real_false_positives_rejected(title):
    assert nw.classify(title) is None


def test_critical_minerals_and_flow_through_fields():
    fam, f = nw.classify("Collective Mining Discovers High-Grade Tungsten and Gold Mineralization in Schist at Depth")
    assert fam == "MINING_DISCOVERY" and f["minerals"] == ["gold", "tungsten"]
    fam, f = nw.classify("Atlas Closes Flow-Through Private Placement")
    assert fam == "PRIVATE_PLACEMENT" and f["flow_through"] and f["stage"] == "CLOSED"


def test_only_canadian_listings_become_events():
    evs = [e for r in _releases() for e in nw.to_canonical(r)]
    assert evs and all(e.country == "CA" and e.exchange in ("XTSE", "XTSX", "XCNQ") for e in evs)
    assert all(e.published_at.tzinfo is not None for e in evs)


def test_tsx_and_tsxv_directories():
    tsx = nw.parse_directory(json.loads(gzip.decompress((FX / "tsx_directory.json.gz").read_bytes())), "tsx")
    tsxv = nw.parse_directory(json.loads(gzip.decompress((FX / "tsxv_directory.json.gz").read_bytes())), "tsxv")
    assert len(tsx) > 2000 and len(tsxv) > 1000
    assert tsx[0]["exchange"] == "XTSE" and tsxv[0]["exchange"] == "XTSX"


def test_sedi_is_blocked_and_only_open_market_code_maps_to_insider_families():
    assert sedi.STATUS == "BLOCKED_SOURCE"
    assert sedi.family_for("10", 5000) == "INSIDER_BUY" and sedi.family_for("10", -100) == "INSIDER_SELL"
    assert sedi.family_for("50", 10000) is None  # option grant is not a signal
