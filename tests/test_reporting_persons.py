"""Schedule 13D reporting persons: issuers often file their insiders' 13D under their own CIK (SEC header FILED BY =
issuer); the investor must come from the cover pages and is never the issuer. Group filings keep every member."""
import gzip
from pathlib import Path

import pytest
from sqlalchemy import select

from app.parsers.schedule13d import parse_filer_names, parse_schedule13
from app.parsers.sec_common import parse_submission
from app.sources.sec.processing import lead_investor
from tests.test_db import URL

FX = Path(__file__).parent / "fixtures"
db = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")


def load(name):
    t = gzip.decompress((FX / name).read_bytes()).decode("latin-1")
    return t, parse_submission(t), parse_filer_names(t.split("</SEC-HEADER>")[0])


def test_self_filed_13d_reporting_person_is_read_from_the_cover_page():
    t, sub, names = load("sc13d_self_filed_single.txt.gz")
    d = parse_schedule13(sub)
    assert [(p.name, p.percent, p.type_codes) for p in d.persons] == [("Stephen C. Taylor", 5.0, ["IN"])]
    issuer_name, issuer_cik = names["SUBJECT COMPANY"][0]
    assert names["FILED BY"][0][1] == issuer_cik  # the header names the issuer as the filer
    assert lead_investor(d, names, issuer_cik, issuer_name) == ("Stephen C. Taylor", None)  # never the issuer


def test_group_filing_keeps_every_reporting_person():
    t, sub, names = load("sc13d_group_21_persons.txt.gz")
    d = parse_schedule13(sub)
    assert len(d.persons) == 21 and len({p.name for p in d.persons}) == 21
    assert d.persons[0].name == "Sterling Fund Management, LLC" and all(p.type_codes for p in d.persons)
    issuer_name, issuer_cik = names["SUBJECT COMPANY"][0]
    assert lead_investor(d, names, issuer_cik, issuer_name)[0] == "Sterling Fund Management, LLC"


def test_a_real_filer_in_the_header_is_kept_as_before():
    from app.parsers.schedule13d import ReportingPerson, Schedule13
    d = Schedule13("SC 13D", None, None, None, None, persons=[ReportingPerson("Fund LP", None, 7.0, None)])
    names = {"SUBJECT COMPANY": [("Target Inc", "0000000001")], "FILED BY": [("Adviser LLC", "0000000002")]}
    assert lead_investor(d, names, "0000000001", "Target Inc") == ("Adviser LLC", "0000000002")


@db
@pytest.mark.usefixtures("clean")
def test_processing_stores_the_real_investor_and_all_persons(engine):
    from app.db.models import activist_filings, activist_reporting_persons
    from app.sources.sec.processing import process_text
    t, _, _ = load("sc13d_group_21_persons.txt.gz")
    with engine.begin() as c:
        process_text(c, t, "912766", is_backfill=True)
    with engine.connect() as c:
        af = c.execute(select(activist_filings)).mappings().one()
        rp = c.execute(select(activist_reporting_persons).order_by(activist_reporting_persons.c.person_index)).mappings().all()
    assert af["investor_cik"] != af["issuer_cik"] and af["investor_name"] == "Sterling Fund Management, LLC"
    assert len(rp) == 21 and rp[0]["is_lead"] and len({r["group_id"] for r in rp}) == 1 and rp[0]["group_id"].startswith("GRP:")
    assert all(r["identity_key"] for r in rp) and {r["kind"] for r in rp} <= {"ENTITY", "INDIVIDUAL"}


@pytest.mark.parametrize("raw,expected", [
    ("John D. Baker II 1. Check the Appropriate Box if a Member of a Group", "John D. Baker II"),
    ("Jeffrey Postal – an individual", "Jeffrey Postal"), ("FL Investment Holdings LLC (fka Globalstar Holdings, LLC) 41-2116509",
    "FL Investment Holdings LLC (fka Globalstar Holdings, LLC)"), ("Medici Holdings V, Inc. (1)", "Medici Holdings V, Inc"),
    ("Combined Reporting Persons", None)])
def test_cover_name_cleanup(raw, expected):
    from app.parsers.schedule13d import cover_reporting_persons
    txt = f"1 NAMES OF REPORTING PERSONS {raw} 2 CHECK THE APPROPRIATE BOX (a) o 14 TYPE OF REPORTING PERSON IN"
    got = cover_reporting_persons(txt)
    assert (got[0].name if got else None) == expected
