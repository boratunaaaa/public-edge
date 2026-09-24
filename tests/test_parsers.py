"""Parser tests against REAL documents fetched from SEC / House Clerk (tests/fixtures)."""
from pathlib import Path

import pytest

from app.parsers.form4 import (DISPOSITION, GIFT, GRANT_AWARD, OPEN_MARKET_BUY, OPEN_MARKET_SALE, OPTION_EXERCISE,
                               TAX_WITHHOLDING, classify, parse_form4, role_flags)
from app.parsers.form144 import parse_form144
from app.parsers.house_ptr import parse_ptr_text, pdf_text
from app.parsers.schedule13d import classify_item4, is_13d, is_amendment, parse_schedule13
from app.parsers.sec_common import parse_submission, primary_xml, to_date
from app.sources.sec.edgar import parse_feed

FX = Path(__file__).parent / "fixtures"


def read(name, enc="utf-8"):
    return (FX / name).read_text(encoding=enc)


# ---------------- Form 4 ----------------

def test_form4_open_market_purchase_real_filing():
    f = parse_form4(read("form4_purchase.xml"))
    assert f.ticker == "ZBIO"
    o = f.primary_owner
    assert o.is_ceo and not o.is_cfo and o.is_director  # "1" booleans parsed
    buys = [t for t in f.transactions if t.category == OPEN_MARKET_BUY]
    assert len(buys) == 2
    assert buys[0].shares == 20000 and buys[0].price == 18.23 and buys[0].value == 364600.0
    assert str(buys[0].trans_date) == "2026-03-30"
    assert buys[1].shares_after == 1726039
    assert not any(t.is_10b5_1 for t in buys)


def test_form4_cfo_option_exercise_and_sale_with_10b5_1_checkbox():
    f = parse_form4(read("form4_nutanix.xml"))
    assert f.primary_owner.is_cfo and not f.primary_owner.is_ceo
    cats = {t.category for t in f.transactions}
    assert OPEN_MARKET_BUY not in cats
    assert {OPTION_EXERCISE, OPEN_MARKET_SALE} <= cats
    assert all(t.is_10b5_1 for t in f.transactions)  # aff10b5One=true


@pytest.mark.parametrize("code,deriv,cat", [
    ("P", False, OPEN_MARKET_BUY), ("S", False, OPEN_MARKET_SALE), ("M", False, OPTION_EXERCISE),
    ("M", True, OPTION_EXERCISE), ("A", False, GRANT_AWARD), ("F", False, TAX_WITHHOLDING), ("D", False, DISPOSITION),
    ("G", False, GIFT), ("X", True, OPTION_EXERCISE),
])
def test_transaction_code_interpretation(code, deriv, cat):
    assert classify(code, deriv) == cat


def test_derivative_p_is_not_open_market_buy():
    assert classify("P", True) != OPEN_MARKET_BUY


@pytest.mark.parametrize("title,ceo,cfo", [
    ("Chief Executive Officer", True, False), ("President & CEO", True, False), ("CFO", False, True),
    ("EVP, Chief Financial Officer", False, True), ("President", False, False), ("CEO and CFO", True, True),
    ("Chief Operating Officer", False, False),
])
def test_role_flags(title, ceo, cfo):
    assert role_flags(title) == (ceo, cfo)


def test_10b5_1_footnote_detection():
    xml = read("form4_purchase.xml").replace("<aff10b5One>0</aff10b5One>", "<aff10b5One>0</aff10b5One>")
    xml = xml.replace("</ownershipDocument>",
                      '<footnotes><footnote id="F9">Purchased pursuant to a Rule 10b5-1 trading plan adopted May 1.</footnote></footnotes></ownershipDocument>')
    xml = xml.replace("<transactionCode>P</transactionCode>", '<transactionCode>P</transactionCode><footnoteId id="F9"/>', 1)
    f = parse_form4(xml)
    buys = [t for t in f.transactions if t.category == OPEN_MARKET_BUY]
    assert buys[0].is_10b5_1 and not buys[1].is_10b5_1


# ---------------- submission / timezone ----------------

def test_submission_acceptance_timestamp_is_eastern():
    s = parse_submission(read("sc13d_full.txt", "latin-1"))
    assert s.accession == "0001737450-26-000017"
    assert s.accepted_at.isoformat() == "2026-09-24T16:31:09-04:00"  # EDT
    assert s.accepted_at.utcoffset().total_seconds() == -4 * 3600


@pytest.mark.parametrize("v,exp", [("2026-03-30", "2026-03-30"), ("31-MAR-2026", "2026-03-31"), ("07/13/2026", "2026-07-13"),
                                   ("", None), (None, None)])
def test_date_formats(v, exp):
    d = to_date(v)
    assert (str(d) if d else None) == exp


# ---------------- 13D ----------------

def test_13d_structured_xml():
    s = parse_submission(read("sc13d_full.txt", "latin-1"))
    d = parse_schedule13(s)
    assert d.issuer_cik == "0001737450" and d.cusip == "68373M107"
    assert d.max_percent == 67.9
    assert d.lead.name == "Hong Kong Kunlun Tech Holding Limited"
    assert d.item4_text and "Item 4" in d.item4_text
    cls, _ = classify_item4(d.item4_text)
    assert cls != "BUYBACK_REQUEST"  # issuer's own repurchase is not an activist buyback request


def test_13d_vs_13g_not_confused():
    assert is_13d("SCHEDULE 13D") and is_13d("SC 13D/A") and is_13d("SCHEDULE 13D/A")
    assert not is_13d("SCHEDULE 13G") and not is_13d("SC 13G/A")
    assert is_amendment("SC 13D/A") and not is_amendment("SCHEDULE 13D")


@pytest.mark.parametrize("text,cls", [
    ("The Reporting Persons intend to nominate two candidates for election to the Board at the annual meeting.", "BOARD_SEAT"),
    ("The Reporting Persons believe the Issuer should explore strategic alternatives, including a sale.", "STRATEGIC_ALTERNATIVES"),
    ("The Reporting Person submitted a non-binding proposal to acquire all outstanding shares.", "SALE_OF_COMPANY"),
    ("The Reporting Persons urge the Board to authorize a meaningful share repurchase program.", "BUYBACK_REQUEST"),
    ("The Reporting Persons acquired the shares for investment purposes and have no present plans or proposals.", "PASSIVE_OR_UNCLEAR"),
    ("", "PASSIVE_OR_UNCLEAR"),
])
def test_item4_classification(text, cls):
    assert classify_item4(text)[0] == cls


# ---------------- Form 144 ----------------

def test_form144_real():
    s = parse_submission(read("f144_full.txt", "latin-1"))
    f = parse_form144(primary_xml(s))
    assert f.insider_name == "DANA L EVAN" and f.relationship == "Director"
    assert f.shares == 294389 and f.est_value == 715041.44
    assert f.broker.startswith("Morgan Stanley")
    assert f.prior_sales and f.prior_sales[0]["shares"] == 152740
    assert not f.plan_adoption_dates


# ---------------- House PTR ----------------

def test_house_ptr_multi_line_rows():
    p = parse_ptr_text(pdf_text((FX / "house_ptr_20034188.pdf").read_bytes()))
    assert p.name == "Hon. Thomas Suozzi" and p.state_district == "NY03"
    assert [l.ticker for l in p.lines] == ["AMD", "AAPL", "PLTR", "URI", None, None]
    aapl = p.lines[1]
    assert aapl.transaction_type == "SALE" and aapl.amount_min == 15001 and aapl.amount_max == 50000
    assert str(aapl.transaction_date) == "2026-02-18" and str(aapl.notification_date) == "2026-02-19"
    assert p.lines[4].transaction_type == "PURCHASE" and p.lines[4].asset_type == "GS"


def test_house_ptr_single_row():
    p = parse_ptr_text(pdf_text((FX / "house_ptr_20034984.pdf").read_bytes()))
    assert len(p.lines) == 1 and p.lines[0].transaction_type == "PURCHASE" and p.lines[0].ticker is None


def test_house_ptr_owner_codes():
    text = ("Name: Hon. Test Member\nState/District: CA01\nID Owner Asset Transaction Date Notification Amount Cap.\n"
            "Type Date Gains >\n$200?\nSP NVIDIA Corporation - Common P 01/05/2026 01/20/2026 $1,001 - $15,000\nStock (NVDA) [ST]\n"
            "F S : New\nJT Microsoft Corporation (MSFT) [ST] S (partial) 01/06/2026 01/20/2026 $50,001 -\n$100,000\nF S : New\n"
            "* For the complete list of asset type abbreviations")
    p = parse_ptr_text(text)
    assert [(l.owner, l.ticker, l.transaction_type) for l in p.lines] == [("spouse", "NVDA", "PURCHASE"), ("joint", "MSFT", "SALE_PARTIAL")]


# ---------------- EDGAR feed ----------------

def test_feed_parsing_and_duplicate_entries():
    entries = parse_feed(read("feed_form4.xml", "latin-1"))
    assert entries and all(e.form_type in ("4", "4/A") for e in entries)
    accs = [e.accession for e in entries]
    # the feed lists each filing once per party (issuer + reporting owner)
    assert len(set(accs)) < len(accs)
    assert {e.role for e in entries} <= {"Issuer", "Reporting", "Filed by", "Subject"}
