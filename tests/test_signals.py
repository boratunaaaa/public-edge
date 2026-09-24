from datetime import date

from app.entities.names import normalize_company, normalize_person
from app.entities.registry import Resolver
from app.signals import priority as P
from app.signals.detect import Buy, find_clusters, signal_family
from app.sources.usaspending.awards import conservative_public_date


def b(d, owner, v=100_000, ceo=False):
    return Buy(d, owner, owner, v, ceo, False, False, "Director", "u")


def test_cluster_7_14_30_reports_growth_only():
    buys = [b(date(2026, 9, 1), "A"), b(date(2026, 9, 20), "B"), b(date(2026, 9, 24), "C")]
    cl = find_clusters(buys, date(2026, 9, 24))
    assert [(c.window, c.n_buyers) for c in cl] == [(7, 2), (30, 3)]


def test_single_insider_repeated_buys_is_not_cluster():
    buys = [b(date(2026, 9, 20), "A"), b(date(2026, 9, 22), "A"), b(date(2026, 9, 24), "A")]
    assert find_clusters(buys, date(2026, 9, 24)) == []


def test_future_buys_excluded_from_cluster():
    buys = [b(date(2026, 9, 24), "A"), b(date(2026, 9, 25), "B")]
    assert find_clusters(buys, date(2026, 9, 24)) == []


def test_company_name_normalization():
    assert normalize_company("TUTOR PERINI CORPORATION") == normalize_company("Tutor Perini Corp")
    assert normalize_company("Lockheed Martin Corp /DE/") == "LOCKHEED MARTIN"
    assert normalize_company("The Boeing Company") == "BOEING"
    assert normalize_company("AT&T INC.") == "AT AND T"
    assert normalize_company("WHITING-TURNER CONTRACTING COMPANY, THE") == "WHITING TURNER CONTRACTING"
    assert normalize_person("Suozzi Thomas") == normalize_person("Thomas SUOZZI")


def _resolver():
    r = Resolver()
    r.cik_to_ticker = {"0000936468": "LMT", "0000077543": "TPC"}
    r.ticker_to_cik = {"LMT": "0000936468", "TPC": "0000077543"}
    r.name_to_ciks = {"LOCKHEED MARTIN": {"0000936468"}, "TUTOR PERINI": {"0000077543"}, "ACME": {"1", "2"}}
    r.cusip_to_ticker = {"539830AB": "LMT"}
    return r


def test_ticker_mapping_strict():
    r = _resolver()
    assert r.ticker_for_name("TUTOR PERINI CORPORATION") == "TPC"
    assert r.ticker_for_name("Lockheed Martin Corporation") == "LMT"
    assert r.ticker_for_name("ACME INC") is None  # ambiguous -> no guess
    assert r.ticker_for_name("Lockheed Martin Aeronautics Services Co") is None  # no fuzzy matching
    assert r.ticker_for_cik("936468") == "LMT"


def test_cusip_mapping():
    r = _resolver()
    assert r.ticker_for_cusip("539830AB9") == "LMT"  # check digit ignored
    assert r.ticker_for_cusip("539830ab") == "LMT"
    assert r.ticker_for_cusip("123") is None and r.ticker_for_cusip(None) is None


def test_contract_levels_and_megacap_noise():
    assert P.contract_level(0.6) == ">50%" and P.contract_level(0.12) == ">10%" and P.contract_level(0.021) == ">2%"
    assert P.contract_level(0.001) is None and P.contract_priority(0.001) == 0  # $50M vs $50B mega-cap -> nothing


def test_insider_priority():
    assert P.insider_buy_priority(is_ceo=True, is_cfo=False, is_director=False, value=740_000, is_10b5_1=False) == 6
    assert P.insider_buy_priority(is_ceo=False, is_cfo=False, is_director=True, value=50_000, is_10b5_1=False) == 2
    planned = P.insider_buy_priority(is_ceo=True, is_cfo=False, is_director=False, value=740_000, is_10b5_1=True)
    assert planned < 6


def test_priority_functions_have_no_party_input():
    import inspect
    for fn in (P.congress_buy_priority, P.congress_cluster_priority):
        assert not any("party" in p for p in inspect.signature(fn).parameters)


def test_form144_major():
    assert P.form144_is_major(6_000_000, None, None)
    assert P.form144_is_major(100_000, 2_000_000, 100_000_000)  # 2% of outstanding
    assert not P.form144_is_major(715_041, 294_389, 259_381_559)


def test_multi_signal_families():
    assert signal_family("INSIDER_OPEN_MARKET_BUY", {"is_ceo": True}) == "CEO_BUY"
    assert signal_family("INSIDER_OPEN_MARKET_BUY", {"is_ceo": True, "is_10b5_1": True}) is None
    assert signal_family("GOV_CONTRACT", {"value_to_mcap": 0.01}) is None
    assert signal_family("GOV_CONTRACT", {"value_to_mcap": 0.05}) == "GOV_CONTRACT"
    assert signal_family("FORM144_MAJOR_SALE", {}) is None


def test_money_format_boundaries():
    from app.notifications.format import money
    from app.signals.detect import _money
    for f in (money, _money):
        assert f(999_999.6) == "$1.00M"
        assert f(1_240_000) == "$1.24M"
        assert f(4_610_000_000) == "$4.61B"
    assert money(210_000) == "$210k" and _money(210_000) == "$210k"


def test_contract_public_date_is_conservative_for_backfill():
    assert conservative_public_date(date(2025, 1, 10), "Department of Defense") == date(2025, 4, 10)
    assert conservative_public_date(date(2025, 1, 10), "Department of Energy") == date(2025, 1, 15)


# ---------------- point-in-time shares outstanding ----------------

def _f(acc, end, filed, val, form="10-Q"):
    from app.marketdata.provider import ShareFact
    return ShareFact(acc, date.fromisoformat(end), date.fromisoformat(filed), form, val)


def test_shares_same_end_rereported_is_not_summed():
    from app.marketdata.provider import shares_as_of
    facts = [_f("A-10Q", "2025-06-30", "2025-08-01", 100), _f("B-10QA", "2025-06-30", "2025-09-15", 105, "10-Q/A")]
    assert shares_as_of(facts, date(2025, 8, 10))[0] == 100  # amendment not yet filed -> invisible
    assert shares_as_of(facts, date(2025, 9, 20))[0] == 105  # amendment replaces, never 205


def test_shares_distinct_classes_in_one_filing_are_summed():
    from app.marketdata.provider import shares_as_of
    facts = [_f("A", "2025-06-30", "2025-08-01", 700), _f("A", "2025-06-30", "2025-08-01", 300)]
    assert shares_as_of(facts, date(2025, 9, 1))[0] == 1000


def test_shares_no_look_ahead_and_staleness():
    from app.marketdata.provider import shares_as_of
    facts = [_f("A", "2024-03-31", "2024-05-01", 100), _f("B", "2025-03-31", "2025-05-01", 400)]  # 4x issuance later
    assert shares_as_of(facts, date(2024, 12, 1))[0] == 100  # future dilution not visible
    assert shares_as_of(facts, date(2024, 4, 1)) is None  # nothing filed yet -> unknown
    assert shares_as_of(facts, date(2026, 9, 1)) is None  # >400 days old -> unknown
