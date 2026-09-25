"""KAP adapter on 27 real public disclosure pages (kap.org.tr/tr/Bildirim/<id>, Jun-Sep 2026)."""
import gzip
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.sources.tr import kap

FX = Path(__file__).parent / "fixtures" / "global" / "tr"


def page(i):
    return gzip.decompress((FX / f"{i}.html.gz").read_bytes()).decode("utf-8", "ignore")


def dsc(i):
    return kap.parse_page(page(i), str(i))


def fams(i):
    return [f for f, _ in kap.classify(dsc(i))]


def test_every_fixture_parses():
    ids = [p.name.split(".")[0] for p in FX.glob("*.html.gz")]
    assert len(ids) >= 15
    for i in ids:
        d = dsc(i)
        assert d.published_at.tzinfo is not None and d.type_code and d.summary


def test_header_fields_and_istanbul_timestamp():
    d = dsc(1668274)
    assert d.tickers == ["MOGAN"] and d.company_names[0].startswith("MOGAN ENERJİ")
    assert d.type_code == "ÖDA" and d.form_title == "Özel Durum Açıklaması (Genel)"
    assert d.published_at.astimezone(timezone.utc) == datetime(2026, 9, 25, 15, 6, 26, tzinfo=timezone.utc)  # 18:06:26 İstanbul


def test_multi_ticker_company():
    assert dsc(1668336).tickers == ["KRDMA", "KRDMB", "KRDMD"]


def test_update_and_correction_flags():
    assert dsc(1668285).is_update is True and dsc(1668285).is_correction is False
    assert dsc(1668293).is_correction is True


def test_buyback_table_rows_and_turkish_numbers():
    d = dsc(1668285)
    assert len(d.buyback_rows) == 6
    last = d.buyback_rows[-1]
    assert last == {"ticker": "ALFAS", "isin": "TREALFS00023", "date": "2026-09-25", "nominal": 100000.0, "pct_capital": 0.02717, "price": 33.38}
    fam, f = kap.classify(d)[0]
    assert fam == "SHARE_BUYBACK" and f["last_price_try"] == 33.38


def test_major_holder_purchase_fields():
    fam, f = kap.classify(dsc(1668274))[0]
    assert fam == "MAJOR_HOLDER_BUY"
    assert f["avg_price_try"] == 9.09 and f["nominal_try"] == 164992 and f["amount_try"] == 1499998.82
    assert f["ownership_pct_after"] == 0.1875 and "Güriş" in f["buyer_or_seller"]


def test_official_share_trade_form_without_server_side_values():
    fam, f = kap.classify(dsc(1618436))[0]
    assert fam == "MAJOR_HOLDER_BUY" and "amount_try" not in f  # values are in the attachment: nothing invented


def test_vbts_measure_ticker_and_dates():
    d = dsc(1668314)
    ev = kap.to_canonical(d)[0]
    assert ev.family == "TRADING_RESTRICTION" and ev.ticker == "DMRGD"  # from "İlgili Şirketler", the page is Borsa İstanbul's
    assert ev.fields["short_sale_ban"] and ev.fields["margin_ban"]
    assert (ev.fields["from"], ev.fields["to"]) == ("2026-09-28", "2026-10-27") and ev.confidence == "HIGH"


def test_circuit_breaker():
    assert kap.classify(dsc(1613016))[0] == ("TRADING_RESTRICTION", {"measure": "CIRCUIT_BREAKER"})


def test_rights_issue_is_also_dilution():
    assert fams(1668293) == ["RIGHTS_ISSUE", "DILUTION"]
    assert kap.classify(dsc(1668293))[0][1]["rate_pct"] == 250


@pytest.mark.parametrize("i,expected", [
    (1611390, "NEW_CONTRACT"), (1612203, "MANAGEMENT_CHANGE"), (1668296, "MANAGEMENT_CHANGE"), (1612474, "DIVIDEND"),
    (1616810, "ACQUISITION"), (1668319, "ACQUISITION"), (1668354, "EARNINGS"), (1668355, "PERIODIC_REPORT"),
])
def test_real_disclosures_classified(i, expected):
    assert fams(i)[0] == expected


@pytest.mark.parametrize("i,subtype", [
    (1611932, "BONUS_ISSUE"), (1612745, "NO_DIVIDEND"), (1668205, "RELATED_PARTY_TRANSACTION"), (1668302, "CREDIT_RATING"),
    (1668305, "INTRAGROUP_MERGER"), (1668318, "MERGER_TALKS_ENDED"), (1668328, "DIVESTMENT"), (1668336, "BUYBACK_PROGRAM_ENDED"),
])
def test_negated_or_non_directional_disclosures_are_not_mislabelled(i, subtype):
    fam, f = kap.classify(dsc(i))[0]
    assert fam == "OTHER_MATERIAL_DISCLOSURE" and f["subtype"] == subtype


@pytest.mark.parametrize("i", [1613287, 1668261, 1668265, 1668289, 1668356])
def test_administrative_debt_fund_and_articles_are_skipped(i):
    assert kap.classify(dsc(i)) == []  # articles of association, fund form, commercial paper, market making, responsibility statement


def test_waf_block_page_is_detected_not_parsed():
    with pytest.raises(kap.KapBlocked):
        kap.parse_page('<html><p>Yasadiginiz bu erisim problemini "150207-PPE13" hata numarasi ile</p></html>', "1")


def test_unexpected_layout_raises_instead_of_guessing():
    with pytest.raises(ValueError):
        kap.parse_page("<html><body>maintenance</body></html>", "1")


def test_duplicate_disclosure_is_one_event():
    d = dsc(1668274)
    e1 = kap.to_canonical(d)
    d.disclosure_id = "999999"  # the same text re-published under another id
    e2 = kap.to_canonical(d)
    assert len(kap.dedupe(e1 + e2)) == 1


def test_skeleton_fingerprint_is_stable_across_disclosure_types():
    a = kap.skeleton_keys(page(1668285))
    assert a == kap.skeleton_keys(page(1668314)) == kap.skeleton_keys(page(1612474)) == list(kap.SKELETON)


def test_canonical_events_carry_raw_and_source_url():
    for e in kap.to_canonical(dsc(1668285)):
        assert e.country == "TR" and e.exchange == "XIST" and e.source_url.endswith("/1668285")
        assert e.raw["type_code"] == "ÖDA" and e.raw["summary"] and e.security_id == "XIST:ALFAS"
