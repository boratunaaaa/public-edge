"""V2 unit tests: news intelligence + discovery language/metrics (no database)."""
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.discovery import language as L
from app.discovery import metrics as M
from app.discovery import scoring as S
from app.discovery.thesis import classify_change
from app.news import classify as C
from app.news.cluster import choose_primary, public_time, same_story, tokens
from app.news.extract import contract_value, money_values, tickers_in
from app.news.sources import parse_rss

FX = Path(__file__).parent / "fixtures"
UTC = timezone.utc


def t(h=0, m=0, day=24):
    return datetime(2026, 9, day, h, m, tzinfo=UTC)


# ---------------- clustering / dedup ----------------

def test_syndicated_copies_cluster_into_one_story():
    a = tokens("Acme Robotics Signs $180 Million Supply Agreement With Leading Automaker")
    b = tokens("PRESS RELEASE: Acme Robotics signs $180 million supply agreement with leading automaker")
    c = tokens("Acme Robotics Reports Second Quarter 2026 Results")
    assert same_story(a, t(13), b, t(14))
    assert not same_story(a, t(13), c, t(13))


def test_different_amounts_are_different_stories():
    a = tokens("Acme wins $180 million Army contract")
    b = tokens("Acme wins $18 million Army contract")
    assert not same_story(a, t(13), b, t(13))


def test_same_title_outside_window_is_new_story():
    a = tokens("Acme announces production start")
    assert not same_story(a, t(0, day=20), a, t(0, day=24))


# ---------------- timestamp integrity ----------------

def test_public_time_is_earliest_original_never_updated_or_future():
    items = [
        {"original_published_at": t(14), "first_seen_at": t(14, 2), "source_tier": 4},  # syndicated copy
        {"original_published_at": t(13), "first_seen_at": t(13, 1), "source_tier": 1},  # the wire release
        {"original_published_at": t(23), "first_seen_at": t(13, 30), "source_tier": 2},  # future-dated -> ignored
        {"original_published_at": None, "first_seen_at": t(15), "source_tier": 3},
    ]
    assert public_time(items) == t(13)


def test_public_time_never_before_first_seen_bound_for_future_pubdates():
    items = [{"original_published_at": t(20), "first_seen_at": t(10), "source_tier": 1}]
    assert public_time(items) == t(10)


def test_official_source_precedence():
    items = [{"id": 1, "source_tier": 4, "original_published_at": t(12), "first_seen_at": t(12)},
             {"id": 2, "source_tier": 1, "original_published_at": t(13), "first_seen_at": t(13)},
             {"id": 3, "source_tier": 1, "original_published_at": t(12, 30), "first_seen_at": t(12, 30)}]
    assert choose_primary(items)["id"] == 3  # tier 1 first, then earliest


# ---------------- extraction ----------------

def test_ticker_extraction():
    txt = "SAN JOSE, Calif. -- Acme Corp. (NASDAQ: ACME) and Beta Inc. (NYSE: BETA; TSX: BTA) today announced ..."
    assert tickers_in(txt) == ["ACME", "BETA", "BTA"]
    assert tickers_in("Gamma Holdings (Nasdaq: GMMA) announced") == ["GMMA"]


def test_money_and_contract_value():
    assert money_values("worth $1.2 billion and $350,000") == [1.2e9, 350000.0]
    txt = "Revenue for the quarter was $95 million. The company was awarded a $180 million contract by the U.S. Army."
    assert contract_value(txt) == 180e6


# ---------------- classification / hype filter ----------------

def test_multi_label_classification():
    cls = C.classify("Acme awarded $180 million production contract by the U.S. Army; production to start in Q1")
    assert "GOVERNMENT_CONTRACT" in cls and "NEW_CONTRACT" in cls


def test_financing_agreements_are_not_new_contracts():
    assert "NEW_CONTRACT" not in C.classify("The Company entered into a credit agreement with Bank of America")
    assert "NEW_CONTRACT" not in C.classify("Acme entered into a securities purchase agreement for a registered direct offering")


@pytest.mark.parametrize("text,agr,binding", [
    ("signed a three-year binding supply agreement", "commercial_supply_agreement", True),
    ("entered into a non-binding memorandum of understanding", "mou", False),
    ("signed a letter of intent to explore", "loi", False),
    ("announced a strategic partnership", "collaboration", False),
    ("received a purchase order for 500 units", "purchase_order", True),
    ("announced a pilot program with", "pilot", False),
    ("signed a strategic partnership and non-binding MOU", "mou", False),  # non-binding language wins
])
def test_binding_vs_mou(text, agr, binding):
    a = C.agreement_type(text)
    assert a == agr and ((a in C.BINDING) == binding)


def test_hype_filter_caps_non_binding_materiality():
    kw = dict(contract_value=None, market_cap=300e6, annual_revenue=None, tier=1)
    hype = C.materiality(["PARTNERSHIP", "MAJOR_CUSTOMER"], agreement="mou", **kw)
    real = C.materiality(["NEW_CONTRACT", "MAJOR_CUSTOMER"], agreement="commercial_supply_agreement",
                         contract_value=180e6, market_cap=680e6, annual_revenue=95e6, tier=1)
    assert hype.level in ("LOW", "MEDIUM") and real.level in ("HIGH", "CRITICAL")


@pytest.mark.parametrize("mcap,bucket", [(40e6, "BELOW_UNIVERSE"), (50e6, "MICRO"), (299e6, "MICRO"), (300e6, "EARLY_SMALL"),
                                        (1e9, "SMALL"), (3e9, "GROWTH"), (10e9, "ALREADY_SCALED"), (None, None)])
def test_market_cap_buckets(mcap, bucket):
    assert C.mcap_bucket(mcap) == bucket


def test_same_contract_different_materiality_by_size():
    small = C.materiality(["NEW_CONTRACT"], agreement="binding_contract", contract_value=50e6, market_cap=100e6, annual_revenue=None, tier=1)
    huge = C.materiality(["NEW_CONTRACT"], agreement="binding_contract", contract_value=50e6, market_cap=1e12, annual_revenue=None, tier=1)
    assert C.LEVELS.index(small.level) > C.LEVELS.index(huge.level)


def test_contract_to_revenue_ratio_in_reasons():
    m = C.materiality(["NEW_CONTRACT"], agreement="binding_contract", contract_value=180e6, market_cap=680e6, annual_revenue=95e6, tier=1)
    assert any("contract/annual_revenue=189.5%" in r for r in m.reasons)


# ---------------- RSS parsing on real feeds ----------------

@pytest.mark.parametrize("fn", ["prn_rss.xml", "bw_rss.xml", "yahoo_ionq_rss.xml"])
def test_parse_real_rss(fn):
    items = parse_rss((FX / fn).read_text(encoding="utf-8", errors="replace"))
    assert items and all(i["title"] and i["link"] and i["published_at"] is not None for i in items)
    assert all(i["published_at"].tzinfo is not None for i in items)


# ---------------- discovery language ----------------

def test_customer_relationship_quality():
    potential = L.relations_in("Tesla is a potential customer and may place orders for our actuators.")[0]
    signed = L.relations_in("In June 2026, Tesla signed a five-year supply agreement with us for actuator modules.")[0]
    assert signed.strength == 1.0 and potential.strength < 0.2 and potential.hedged
    comp = L.relations_in("We compete with NVIDIA in the accelerator market.")[0]
    assert comp.relation == "competitor" and comp.strength == 0
    mou = L.relations_in("We entered into a non-binding memorandum of understanding with Samsung.")[0]
    assert mou.agreement == "mou" and mou.strength < 0.5
    gov = L.relations_in("We received a $12.5 million contract from the U.S. Air Force for production of our sensors.")[0]
    assert gov.counterparty == "DoD" and gov.agreement == "paid_contract" and gov.strength == 1.0


def test_ambiguous_aliases_need_exact_case():
    assert L.counterparties_in("John Doe reviewed the meta-analysis; the apple orchard grew.") == []
    assert ("DOE", "US_GOVERNMENT") in L.counterparties_in("The DOE awarded a grant.")


def test_bottleneck_signals():
    s = "We are the sole-source supplier of HALEU deconversion and demand exceeds our capacity."
    assert set(L.signals_in(s)) >= {"SOLE_SUPPLIER", "CAPACITY_CONSTRAINED"}


def test_supplier_dependency_is_not_positive_bottleneck_evidence():
    assert "SOLE_SUPPLIER" not in L.signals_in("We depend on a sole supplier for critical battery components.")
    assert "LIMITED_SUPPLY_BASE" not in L.signals_in("Our raw materials come from a limited supplier base.")


@pytest.mark.parametrize("labels,rev,yoy,stage", [
    (["CLINICAL"], 0, None, "RESEARCH"),
    (["PILOT_PRODUCTION"], 0, None, "PILOT"),
    (["FIRST_COMMERCIAL_PRODUCTION"], 0, None, "PRE-COMMERCIAL"),  # no revenue -> press-release claims capped
    (["QUALIFICATION"], 0, None, "CUSTOMER_QUALIFICATION"),
    (["FIRST_COMMERCIAL_PRODUCTION"], 8e6, 0.3, "EARLY_COMMERCIAL"),
    ([], 40e6, 0.9, "PRODUCTION_RAMP"),
    (["MASS_PRODUCTION"], 120e6, 0.2, "MASS_PRODUCTION"),
    ([], 2e9, 0.1, "SCALED"),
])
def test_stage_classification(labels, rev, yoy, stage):
    assert L.classify_stage(labels, rev, yoy)[0] == stage


def test_emerging_terms_detects_new_vocabulary():
    recent = [f"Our solid oxide electrolysis stack {i} ramps" for i in range(6)]
    baseline = ["We sell lithium batteries to customers."] * 20
    terms = [x[0] for x in L.emerging_terms(recent, baseline)]
    assert "solid oxide electrolysis" in terms or "oxide electrolysis" in terms


# ---------------- metrics ----------------

def q(y, qn):
    return date(y, 3 * qn, 28 if qn != 4 else 31)


def test_revenue_acceleration_needs_two_quarters():
    rev = [(q(2025, 1), 10), (q(2025, 2), 10), (q(2025, 3), 10), (q(2025, 4), 10),
           (q(2026, 1), 11), (q(2026, 2), 13), (q(2026, 3), 16)]
    m = M.compute(rev, [], [], [], [], [], [], [], [], [])
    assert "REVENUE_ACCELERATION" in m.flags
    rev2 = [(q(2025, 1), 10), (q(2025, 2), 10), (q(2025, 3), 10), (q(2025, 4), 10), (q(2026, 1), 10), (q(2026, 2), 10), (q(2026, 3), 20)]
    assert "REVENUE_ACCELERATION" not in M.compute(rev2, [], [], [], [], [], [], [], [], []).flags  # single spike = noise


def test_cash_runway_and_dilution():
    m = M.compute([], [], [(date(2026, 6, 30), 38e6)], [(date(2025, 12, 31), -65e6)], [], [], [], [], [], [],
                  shares=[(date(2025, 6, 30), 100e6), (date(2026, 6, 30), 160e6)])
    assert m.runway_months == pytest.approx(38e6 / (65e6 / 12))
    assert "CASH_RUNWAY_RISK" in m.flags
    assert m.share_growth_yoy == pytest.approx(0.6)


def test_positive_cash_flow_has_no_runway_risk():
    m = M.compute([], [], [(date(2026, 6, 30), 5e6)], [(date(2025, 12, 31), 20e6)], [], [], [], [], [], [])
    assert m.runway_months is None and "CASH_RUNWAY_RISK" not in m.flags


# ---------------- scoring / thesis ----------------

def test_price_is_not_a_discovery_feature():
    import inspect
    assert not any(p in ("price", "momentum", "return") for p in inspect.signature(S.features).parameters)


def test_score_penalises_going_concern_and_requires_progress():
    base = dict(market_cap=400e6, metrics={"yoy_latest": 0.8, "yoy_prev": 0.5, "flags": ["REVENUE_ACCELERATION"]},
                stage="EARLY_COMMERCIAL", stage_prev="PILOT", signal_labels=["SOLE_SUPPLIER"],
                insider={"buy_value": 310_000, "buyers": 1}, activist_13d=False, gov_contract_ratio=None,
                relations=[{"counterparty": "GM", "category": "AUTOMOTIVE_OEM", "strength": 1.0, "hedged": False, "relation": "customer"}])
    good = S.features(risks=[], **base)
    bad = S.features(risks=[{"risk": "GOING_CONCERN", "severity": "HIGH"}, {"risk": "DILUTION", "severity": "HIGH"}], **base)
    sg, lg, why = S.score(good)
    sb, lb, _ = S.score(bad)
    assert lg == "HIGH" and sb < sg - 4 and S.has_real_progress(good)
    empty = S.features(market_cap=400e6, metrics={}, stage="PRE-COMMERCIAL", stage_prev=None, relations=[], signal_labels=[],
                       insider={}, activist_13d=False, gov_contract_ratio=None, risks=[])
    assert not S.has_real_progress(empty)


@pytest.mark.parametrize("classes,agr,mat,change", [
    (["MAJOR_CUSTOMER", "NEW_CONTRACT"], "commercial_supply_agreement", "HIGH", "THESIS_STRENGTHENING"),
    (["MAJOR_CUSTOMER", "PARTNERSHIP"], "mou", "HIGH", "NEUTRAL"),  # hype does not strengthen
    (["GUIDANCE_CUT"], None, "HIGH", "THESIS_WEAKENING"),
    (["BANKRUPTCY_RISK", "NEW_CONTRACT"], None, "CRITICAL", "THESIS_INVALIDATION"),
    (["EARNINGS"], None, "MEDIUM", "NEUTRAL"),
])
def test_thesis_change_classification(classes, agr, mat, change):
    assert classify_change(classes, agr, mat)[0] == change


# ---------------- real 8-K press releases: headline/lead + classification ----------------

def _release(fn):
    from app.news.lead import headline_and_lead
    h, lead = headline_and_lead((FX / fn).read_text())
    return h, f"{h}. {lead}"


@pytest.mark.parametrize("fn,head_start,must,must_not", [
    ("ex99_visionwave.htm", "VisionWave Holdings Receives $20 Million Purchase Order", {"NEW_CONTRACT"}, {"M_AND_A"}),
    ("ex99_flotek.htm", "Flotek Secures $120 Million Senior Secured Term Loan", {"CAPITAL_RAISE"}, {"GOVERNMENT_SUPPORT", "NEW_CONTRACT"}),
    ("ex99_talos.htm", "Talos Energy Appoints Barbara Faulkenberry", {"MANAGEMENT_CHANGE"}, {"M_AND_A", "GOVERNMENT_CONTRACT"}),
    ("ex99_lear.htm", "Lear Increases Share Repurchase Authorization to $1.5 Billion", set(), {"SUPPLY_SHORTAGE", "LAWSUIT"}),
])
def test_real_press_release_classification(fn, head_start, must, must_not):
    h, txt = _release(fn)
    assert h.startswith(head_start)
    cls = set(C.classify(txt))
    assert must <= cls and not (must_not & cls)
    assert "false" not in h and "iso4217" not in txt  # inline-XBRL noise removed


def test_purchase_order_value_and_binding():
    _h, txt = _release("ex99_visionwave.htm")
    assert C.agreement_type(txt) == "purchase_order" and contract_value(txt) == 20e6


def test_financing_amount_is_not_contract_value():
    _h, txt = _release("ex99_flotek.htm")
    assert contract_value(txt) is None


def test_ticker_crl_is_not_fda_rejection():
    assert "FDA_REJECTION" not in C.classify("Charles River (NYSE: CRL) highlights refreshed strategic vision at investor day")
    assert "FDA_REJECTION" in C.classify("Acme receives complete response letter from the FDA")


def test_debt_tender_offer_is_not_m_and_a():
    assert "M_AND_A" not in C.classify("OpenText announces pricing of senior secured notes and tender offer for its 2027 notes")
    assert "M_AND_A" in C.classify("Beta launches tender offer for all outstanding shares of Gamma")


def test_legal_agreement_exhibit_is_not_a_press_release():
    from app.news.lead import headline_and_lead
    raw = ("<p>CONTINGENT VALUE RIGHTS AGREEMENT</p><p>THIS CONTINGENT VALUE RIGHTS AGREEMENT, dated as of September 22, 2026 "
           "(this Agreement), is entered into by and between Acme and the Rights Agent.</p><p>WHEREAS, the parties ... bankruptcy ...</p>")
    assert headline_and_lead(raw) == (None, "")


# ---------------- Turkish plain-language alerts + historical success rate ----------------

def _ev(t, payload, **kw):
    return {"id": 1, "event_type": t, "ticker": "ABC", "company_name": "ABC TECHNOLOGIES INC", "public_date": date.today(),
            "payload": payload, "source_url": "https://www.sec.gov/x", "title": "x", **kw}


def test_shrinkage_never_shows_certainty_from_small_samples():
    from app.backtest.probability import shrink
    assert shrink(3, 3) == pytest.approx(8 / 13)  # 3/3 wins -> ~62%, not 100%
    assert shrink(0, 0) == 0.5
    assert shrink(600, 1000) == pytest.approx(605 / 1010)


def test_turkish_alert_has_no_disclaimer_and_explains_terms():
    from app.backtest.probability import SuccessRate
    from app.notifications.format import format_alert
    ev = _ev("INSIDER_OPEN_MARKET_BUY", {"owner": "Jane Doe", "role": "CEO", "is_ceo": True, "value": 740_000,
                                          "transaction_dates": [str(date.today())], "insider_type": "opportunistic",
                                          "holdings_increase": 0.4})
    rate = SuccessRate(0.62, 0.63, 420, 90, "ceo+fırsatçı", 0.034, 0.05, -0.12, 90)
    msg = format_alert(ev, rate=rate, risks=[{"risk": "LOW_LIQUIDITY", "severity": "HIGH"}], market_cap=420e6, bucket="EARLY_SMALL")
    assert "yatırım tavsiyesi" not in msg.lower() and "investment advice" not in msg.lower()
    for must in ("Ne oldu?", "Neden önemli?", "GEÇMİŞ BAŞARI ORANI", "Historical hit rate: %62", "şirketin en üst yöneticisi",
                 "740 bin $", "%40", "alışılmışın dışında", "geçme oranı: <b>%62</b>", "Hisse az işlem görüyor", "420,00 milyon $", "küçük şirket"):
        assert must in msg, must
    assert "Alım puanı" not in msg and "Sistem değerlendirmesi" not in msg
    assert "tam replikasyonu değildir" in msg


def test_alert_without_enough_history_shows_no_percentage():
    from app.backtest.probability import SuccessRate
    from app.notifications.format import format_alert
    msg = format_alert(_ev("GOV_CONTRACT", {"recipient": "ABC", "agency": "DoD", "amount": 5e7, "value_to_mcap": 0.12}),
                       rate=SuccessRate(None, None, 7, 90, "yetersiz veri", None, None, None, None))
    assert "Historical hit rate:</b> gösterilmiyor" in msg and "ARAŞTIRMA SİNYALİ" in msg and "%12" in msg
    assert "'i kadar" not in msg and "&#x27;" not in msg  # no broken Turkish suffixes / escaped apostrophes


def test_history_is_blocked_when_missing_price_samples_exist():
    from app.backtest.probability import SuccessRate
    from app.notifications.format import history_block
    rate = SuccessRate(None, None, 80, 90, "tüm benzer olaylar", None, None, None, None,
                       company_n=55, affected_samples=4, blocked_reason="missing_price_or_benchmark_data")
    text = "\n".join(history_block(rate))
    assert "Gösterilmiyor" in text and "4 adet" in text and "yapay" in text


def test_wilson_interval_is_wide_at_minimum_company_sample():
    from app.backtest.probability import wilson_interval
    lo, hi = wilson_interval(20, 30)
    assert lo < 0.5 < hi
    assert hi - lo > 0.25


def test_13d_peer_group_excludes_immaterial_amendments():
    from app.backtest.probability import success_rate

    class StopAfterSQL:
        def execute(self, statement, _params):
            sql = str(statement)
            assert "is_amendment" in sql and "change" in sql
            raise RuntimeError("predicate checked")

    with pytest.raises(RuntimeError, match="predicate checked"):
        success_rate(StopAfterSQL(), "ACTIVIST_13D", {"item4_class": "BOARD_CHANGE"})


def test_historical_news_revenue_fails_closed_without_filing_timestamps():
    from datetime import datetime, timedelta, timezone
    from app.news.pipeline import annual_revenue

    class MustNotQuery:
        def execute(self, *_args, **_kwargs):
            raise AssertionError("current XBRL frames must not be read for a historical story")

    assert annual_revenue(MustNotQuery(), "0000000001", datetime.now(timezone.utc) - timedelta(days=30)) is None


def test_multiple_serious_risks_make_verdict_risky():
    from app.notifications.format import verdict
    icon, txt = verdict(None, [{"risk": "GOING_CONCERN", "severity": "HIGH"}, {"risk": "MIN_BID_DEFICIENCY", "severity": "HIGH"}])
    assert icon == "🔴" and txt.startswith("RİSKLİ")


@pytest.mark.parametrize("t,payload", [
    ("CLUSTER_INSIDER_BUY", {"number_of_buyers": 3, "span_days": 5, "aggregate_value": 1.1e6, "buyers": [{"name": "A", "role": "CEO", "is_ceo": True, "value": 7e5}]}),
    ("ACTIVIST_13D", {"investor": "Fund", "percent": 9.1, "item4_class": "BOARD_SEAT"}),
    ("MATERIAL_NEWS", {"headline": "ABC wins order", "classes": ["NEW_CONTRACT"], "agreement": "mou", "contract_value": 2e7}),
    ("EARLY_DISCOVERY", {"why": ["x"], "stage": "EARLY_COMMERCIAL", "themes": ["ROBOTICS"], "market_cap": 3e8, "bucket": "EARLY_SMALL"}),
    ("STAGE_TRANSITION", {"from": "PILOT", "to": "EARLY_COMMERCIAL"}),
    ("FIRST_HYPERSCALER", {"counterparty": "Microsoft", "sentence": "Microsoft signed a supply agreement"}),
    ("CONGRESS_BUY", {"politician": "Rep X", "amount_range": "$1,001 - $15,000", "disclosure_delay_days": 20}),
])
def test_every_alert_type_renders_in_turkish(t, payload):
    from app.notifications.format import format_alert, format_discovery, one_line
    msg = format_discovery(_ev(t, payload), [], ["SEC"]) if t == "EARLY_DISCOVERY" else format_alert(_ev(t, payload))
    assert "Neden" in msg and "Dikkat" in msg and "advice" not in msg.lower()
    assert one_line(_ev(t, payload))
    if t == "MATERIAL_NEWS":
        assert "bağlayıcı değil" in msg  # MOU explained in plain words


def test_routine_vs_opportunistic_insider():
    from app.signals.insider_type import classify, holdings_increase
    hist_routine = {(2023, 3), (2024, 3), (2025, 3)}
    hist_opp = {(2023, 1), (2024, 6), (2025, 11)}
    assert classify(hist_routine, date(2026, 3, 10)) == "routine"
    assert classify(hist_opp, date(2026, 3, 10)) == "opportunistic"
    assert classify({(2025, 3)}, date(2026, 3, 10)) == "unclassified"  # not enough history -> never guessed
    assert holdings_increase(40_000, 140_000) == pytest.approx(0.4)
    assert holdings_increase(10, 5) is None


def test_price_context_line():
    from app.notifications.format import price_context_line
    assert price_context_line({"change_3m": -0.32, "range_pos": 0.05}) == \
        "Hisse son 3 ayda %32 düştü. Fiyat son bir yılın en düşük seviyelerine yakın."
    assert price_context_line({"change_3m": 0.1, "range_pos": 0.5}) == "Hisse son 3 ayda %10 yükseldi."
    assert price_context_line(None) is None and price_context_line({"change_3m": None}) is None


def test_theme_keywords_are_whole_words():
    assert "BIOTECH" not in L.themes_in("International alternative external operations in our international markets.")
    assert "BIOTECH" in L.themes_in("Our mRNA therapy and our RNA platform: therapy candidates.")


def test_established_revenue_is_not_early_stage():
    assert L.classify_stage([], 200e6, 0.05)[0] == "MASS_PRODUCTION"  # $200M, +5% is not "early commercial"
    assert L.classify_stage([], 30e6, 0.9)[0] == "PRODUCTION_RAMP"
    assert L.classify_stage([], 8e6, 0.2)[0] == "EARLY_COMMERCIAL"


def test_growth_without_business_evidence_is_not_high_interest():
    f = S.features(market_cap=900e6, metrics={"yoy_latest": 1.2, "yoy_prev": 0.8, "flags": ["REVENUE_ACCELERATION", "MARGIN_INFLECTION"]},
                   stage="PRODUCTION_RAMP", stage_prev="EARLY_COMMERCIAL", relations=[], signal_labels=[],
                   insider={"buy_value": 2e5, "buyers": 1}, activist_13d=False, gov_contract_ratio=None, risks=[], themes=[])
    assert S.score(f)[1] != "HIGH" and not S.has_real_progress(f)  # e.g. a shipping company with a good year
    f2 = dict(f, themes=["ROBOTICS"])
    assert S.score(f2)[1] == "HIGH" and S.has_real_progress(f2)


def test_already_scaled_is_never_high_interest():
    f = S.features(market_cap=18e9, metrics={"yoy_latest": 1.2, "yoy_prev": 0.8, "flags": ["REVENUE_ACCELERATION"]},
                   stage="PRODUCTION_RAMP", stage_prev=None, relations=[{"counterparty": "NVIDIA", "category": "TOP_SEMICONDUCTOR",
                   "strength": 1.0, "hedged": False, "relation": "customer"}], signal_labels=["SOLE_SUPPLIER"], insider={},
                   activist_13d=False, gov_contract_ratio=None, risks=[], themes=["AI_INFRASTRUCTURE"])
    assert S.score(f)[1] == "MEDIUM"


def test_scaled_stage_and_multiple_high_risks_cap_discovery_interest():
    base = S.features(market_cap=2e9, metrics={"yoy_latest": 2.0, "yoy_prev": 1.0,
                      "flags": ["REVENUE_ACCELERATION", "MARGIN_INFLECTION"]}, stage="SCALED",
                      stage_prev=None, relations=[], signal_labels=["SOLE_SUPPLIER", "GOVERNMENT_BACKED"],
                      insider={"buyers": 3, "buy_value": 2e6}, activist_13d=False, gov_contract_ratio=0.2,
                      risks=[], themes=["BATTERIES"])
    assert S.score(base)[1] == "MEDIUM"
    risky = dict(base, stage="PRODUCTION_RAMP", risks=["GOING_CONCERN:HIGH", "FREQUENT_CAPITAL_RAISES:HIGH"])
    assert S.score(risky)[1] == "MEDIUM"
