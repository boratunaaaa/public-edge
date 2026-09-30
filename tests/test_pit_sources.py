"""Point-in-time sources for a FULL replay: XBRL facts with their filings, the complete submissions history, runway."""
from datetime import datetime, timezone

import pytest

from app.marketdata.calendar import NY

CIK = "0000000101"


def _cf():
    def f(val, end, filed, accn, form="10-Q", start=None):
        d = {"val": val, "end": end, "filed": filed, "accn": accn, "form": form}
        if start:
            d["start"] = start
        return d
    return {"facts": {
        "us-gaap": {
            "CashAndCashEquivalentsAtCarryingValue": {"units": {"USD": [
                f(120e6, "2022-12-31", "2023-02-28", "a-10k22", "10-K"), f(90e6, "2023-03-31", "2023-05-10", "a-10q1")]}},
            "NetCashProvidedByUsedInOperatingActivities": {"units": {"USD": [
                f(-60e6, "2022-12-31", "2023-02-28", "a-10k22", "10-K", "2022-01-01"),
                f(-20e6, "2023-03-31", "2023-05-10", "a-10q1", "10-Q", "2023-01-01")]}},  # a quarter: not annual
            "LongTermDebt": {"units": {"USD": [f(50e6, "2022-12-31", "2023-02-28", "a-10k22", "10-K"),
                                               {"val": 1, "end": "2022-12-31"}]}},  # no filing: dropped
        },
        "dei": {"EntityCommonStockSharesOutstanding": {"units": {"shares": [f(1e7, "2023-02-20", "2023-02-28", "a-10k22", "10-K")]}}},
    }}


def test_companyfacts_keep_the_filing_that_disclosed_each_fact():
    from app.replay.pit_sources import parse_companyfacts
    facts = parse_companyfacts("101", _cf())
    fams = {(f.family, f.accession) for f in facts}
    assert ("CASH", "a-10k22") in fams and ("OCF", "a-10q1") in fams and ("SHARES_DEI", "a-10k22") in fams
    assert all(f.cik == CIK and f.filed and f.accession for f in facts)
    assert [f.val for f in facts if f.family == "DEBT"] == [50e6]  # the fact without filing / accession is dropped


def test_the_complete_submissions_history_merges_older_pages_and_maps_acceptance():
    from app.replay.pit_sources import acceptance_map, merge_submission_pages
    recent = {"accessionNumber": ["n2", "n1"], "filingDate": ["2024-05-01", "2024-02-01"], "form": ["8-K", "10-K"],
              "acceptanceDateTime": ["2024-05-01T20:05:00.000Z", "2024-02-01T21:00:00.000Z"]}
    old = {"accessionNumber": ["o2", "n1", "o1"], "filingDate": ["2019-03-01", "2024-02-01", "2015-06-01"],
           "form": ["S-3", "10-K", "424B5"], "acceptanceDateTime": ["2019-03-01T12:00:00.000Z", "2024-02-01T21:00:00.000Z", ""]}
    m = merge_submission_pages(recent, [old])
    assert m["accessionNumber"] == ["n2", "n1", "o2", "o1"]  # newest first, duplicate accession once
    assert m["form"] == ["8-K", "10-K", "S-3", "424B5"]
    acc = acceptance_map(m)
    assert acc["o2"] == datetime(2019, 3, 1, 12, tzinfo=timezone.utc) and "o1" not in acc  # no time: not guessed


@pytest.mark.parametrize("at, visible", [
    (datetime(2023, 5, 10, 9, 0, tzinfo=NY), False),  # date-only filing, same day: not yet
    (datetime(2023, 5, 11, 9, 0, tzinfo=NY), True),
])
def test_a_fact_without_acceptance_time_is_visible_from_the_next_day(at, visible):
    from app.replay.pit_sources import parse_companyfacts, visible as vis
    f = next(x for x in parse_companyfacts(CIK, _cf()) if x.accession == "a-10q1")
    assert vis(f, {}, at) is visible
    assert vis(f, {"a-10q1": datetime(2023, 5, 10, 11, 0, tzinfo=timezone.utc)}, datetime(2023, 5, 10, 9, 0, tzinfo=NY))


def test_runway_uses_only_facts_public_at_the_moment_and_the_production_formula():
    from app.replay.pit_sources import parse_companyfacts, runway_months
    facts = parse_companyfacts(CIK, _cf())
    acc = {"a-10k22": datetime(2023, 2, 28, 21, 30, tzinfo=timezone.utc), "a-10q1": datetime(2023, 5, 10, 20, 0, tzinfo=timezone.utc)}
    assert runway_months(facts, acc, datetime(2023, 2, 1, tzinfo=timezone.utc)) is None  # nothing public yet
    assert runway_months(facts, acc, datetime(2023, 3, 1, tzinfo=timezone.utc)) == pytest.approx(120e6 / (60e6 / 12))
    # the Q1 cash (90M) is public after 2023-05-10 20:00 UTC; the quarterly OCF never replaces the annual one
    assert runway_months(facts, acc, datetime(2023, 5, 11, tzinfo=timezone.utc)) == pytest.approx(90e6 / (60e6 / 12))
    positive = [f for f in facts if f.family != "OCF"]
    assert runway_months(positive, acc, datetime(2023, 6, 1, tzinfo=timezone.utc)) is None  # no burn -> no runway


def test_duplicate_accessions_fill_missing_fields_from_other_pages():
    from app.replay.pit_sources import acceptance_map, merge_submission_pages
    recent = {"accessionNumber": ["x1"], "filingDate": ["2020-01-02"], "acceptanceDateTime": [""]}
    old = {"accessionNumber": ["x1"], "filingDate": ["2020-01-02"], "acceptanceDateTime": ["2020-01-02T15:00:00.000Z"]}
    assert acceptance_map(merge_submission_pages(recent, [old]))["x1"] == datetime(2020, 1, 2, 15, tzinfo=timezone.utc)


def test_offset_less_acceptance_is_utc_and_naive_moments_are_rejected():
    from app.replay.pit_sources import acceptance_map, parse_companyfacts, visible
    assert acceptance_map({"accessionNumber": ["a"], "acceptanceDateTime": ["2021-03-01T14:00:00"]})["a"] == \
        datetime(2021, 3, 1, 14, tzinfo=timezone.utc)
    f = parse_companyfacts(CIK, _cf())[0]
    with pytest.raises(ValueError):
        visible(f, {}, datetime(2023, 6, 1))


def test_within_one_filing_the_preferred_concept_wins_whatever_the_input_order():
    from app.replay.pit_sources import parse_companyfacts, runway_months
    cf = _cf()
    # the same 10-K also tags a secondary cash concept (restricted cash included): the primary concept must be used
    cf["facts"]["us-gaap"]["CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"] = {"units": {"USD": [
        {"val": 999e6, "end": "2022-12-31", "filed": "2023-02-28", "accn": "a-10k22", "form": "10-K"}]}}
    acc = {"a-10k22": datetime(2023, 2, 28, 21, 30, tzinfo=timezone.utc)}
    at = datetime(2023, 3, 1, tzinfo=timezone.utc)
    facts = parse_companyfacts(CIK, cf)
    assert runway_months(facts, acc, at) == pytest.approx(120e6 / (60e6 / 12))
    assert runway_months(list(reversed(facts)), acc, at) == runway_months(facts, acc, at)


def test_a_same_day_correction_accepted_later_wins_even_with_a_smaller_accession():
    from app.replay.pit_sources import PitFact, runway_months
    d = datetime(2023, 3, 1).date()
    facts = [PitFact(CIK, "CASH", "CashAndCashEquivalentsAtCarryingValue", None, d, 120.0, d, "b-original", "10-K"),
             PitFact(CIK, "CASH", "CashAndCashEquivalentsAtCarryingValue", None, d, 30.0, d, "a-correction", "10-K/A"),
             PitFact(CIK, "OCF", "NetCashProvidedByUsedInOperatingActivities", datetime(2022, 3, 2).date(), d, -60.0, d,
                     "b-original", "10-K")]
    acc = {"b-original": datetime(2023, 3, 1, 14, tzinfo=timezone.utc), "a-correction": datetime(2023, 3, 1, 20, tzinfo=timezone.utc)}
    assert runway_months(facts, acc, datetime(2023, 3, 2, tzinfo=timezone.utc)) == pytest.approx(30.0 / (60.0 / 12))  # 6 months
    assert runway_months(facts, acc, datetime(2023, 3, 1, 15, tzinfo=timezone.utc)) == pytest.approx(120.0 / (60.0 / 12))
