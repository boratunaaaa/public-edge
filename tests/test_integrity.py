"""Data-integrity unit tests (no database): survivorship, identity, independence, news, OGE, discovery."""
from datetime import date, datetime, timedelta, timezone

import pytest

from app.backtest.engine import compute_returns
from app.backtest import quality as Q
from app.marketdata.provider import Bar
from app.securitymaster.delisting import classify_delisting


def bars_between(start: date, n: int, price=10.0, step=0.0, **kw) -> list[Bar]:
    out, d, p = [], start, price
    while len(out) < n:
        if d.weekday() < 5:
            out.append(Bar(d, p, p * 1.01, p * 0.99, p, p, 1000))
            p += step
        d += timedelta(days=1)
    return out


# ------------------------------------------------------------------ delisting / survivorship

def a(action, d, acc=""):
    return {"action": action, "action_date": d, "accession": acc}


def test_bankruptcy_delisting_is_classified_from_sec_actions():
    dl = classify_delisting([a("BANKRUPTCY", date(2023, 3, 10)), a("FORM25_NSE", date(2023, 4, 20), "x")], currently_listed=False)
    assert dl.reason == "BANKRUPTCY" and dl.date == date(2023, 4, 20) and dl.confidence == "HIGH" and dl.is_performance


def test_merger_delisting_is_not_a_performance_delisting():
    dl = classify_delisting([a("MERGER_PROXY", date(2023, 1, 5)), a("CHANGE_IN_CONTROL", date(2023, 3, 1)),
                             a("FORM25_NSE", date(2023, 3, 2))], currently_listed=False)
    assert dl.reason == "MERGER" and not dl.is_performance


def test_exchange_compliance_delisting():
    dl = classify_delisting([a("LISTING_DEFICIENCY", date(2023, 1, 5)), a("FORM25_NSE", date(2023, 9, 1))], currently_listed=False)
    assert dl.reason == "EXCHANGE_COMPLIANCE" and dl.is_performance


def test_currently_listed_company_is_never_delisted_by_old_notices():
    dl = classify_delisting([a("FORM25_NSE", date(2020, 1, 1))], currently_listed=True)
    assert dl.reason == "NOT_DELISTED" and dl.date is None


def test_unknown_delisting_without_sec_record_is_low_confidence():
    dl = classify_delisting([], currently_listed=False, last_symbol_seen=date(2022, 5, 1))
    assert dl.reason == "UNKNOWN" and dl.confidence == "LOW"


def test_bankruptcy_inside_horizon_counts_as_loss_not_as_missing():
    oc, why = Q.outcome_for(None, False, "DELISTED_BANKRUPTCY", "BANKRUPTCY", date(2023, 4, 20), date(2023, 2, 1), 90)
    assert oc == "loss" and why == "DELISTED_PERFORMANCE_RULE"


def test_merger_without_prices_stays_unresolved():
    oc, why = Q.outcome_for(None, False, "DELISTED_MERGER", "MERGER", date(2023, 3, 2), date(2023, 2, 1), 90)
    assert oc == "unresolved" and why == "DELISTED_MERGER"


def test_delisting_after_horizon_does_not_resolve_the_sample():
    oc, _ = Q.outcome_for(None, False, "NO_PRICE_DATA", "BANKRUPTCY", date(2024, 1, 1), date(2023, 2, 1), 90)
    assert oc == "unresolved"


def test_missing_benchmark_is_unresolved_and_blocks_trust():
    oc, why = Q.outcome_for(None, True, None, None, None, date(2023, 2, 1), 90)
    assert oc == "unresolved" and why == "BENCHMARK_MISSING"


def test_merged_stock_exits_at_last_traded_price_delisting_return():
    """Delisted after 40 days at a cash-out price: the 90-day return uses the final quote, SPY runs the full horizon."""
    start = date(2023, 1, 3)
    stock = bars_between(start, 28, price=10.0, step=0.1)  # trades ~40 calendar days, then disappears
    spy = bars_between(date(2022, 12, 20), 200, price=400.0, step=0.5)
    delisted = stock[-1].date
    res = {r.horizon_days: r for r in compute_returns(stock, spy, date(2023, 1, 2), None, as_of=date(2024, 1, 1),
                                                      horizons=(30, 90), terminal_date=delisted)}
    assert res[30].exit_basis == "horizon"
    r90 = res[90]
    assert r90.exit_basis == "delisting_terminal" and r90.exit_date == delisted
    assert r90.ret == pytest.approx(stock[-1].close / stock[0].open - 1)
    assert r90.spy_ret is not None and r90.excess_ret == pytest.approx(r90.ret - r90.spy_ret)


def test_without_terminal_date_a_vanished_stock_has_no_long_horizon_return():
    stock = bars_between(date(2023, 1, 3), 28)
    spy = bars_between(date(2022, 12, 20), 200, price=400.0)
    res = compute_returns(stock, spy, date(2023, 1, 2), None, as_of=date(2024, 1, 1), horizons=(90,))
    assert res == []  # no silent neutral fill: the security master must explain it


def test_reverse_split_does_not_create_a_fake_return():
    """1:10 reverse split mid-window: raw prices jump 10x, adjusted prices are flat."""
    raw = []
    d = date(2023, 1, 2)
    while len(raw) < 80:
        if d.weekday() < 5:
            after = len(raw) >= 30
            px = 20.0 if after else 2.0  # raw price x10 after the reverse split
            adj = 20.0  # vendor-adjusted series (split factor applied to history)
            raw.append(Bar(d, px, px, px, px, adj, 100))
        d += timedelta(days=1)
    spy = bars_between(date(2022, 12, 20), 200, price=400.0)
    r = compute_returns(raw, spy, date(2023, 1, 2), None, as_of=date(2024, 1, 1), horizons=(60,))[0]
    assert abs(r.ret) < 1e-9


def test_forward_split_with_adjusted_close_is_flat():
    raw = []
    d = date(2023, 1, 2)
    while len(raw) < 80:
        if d.weekday() < 5:
            px = 100.0 if len(raw) < 30 else 50.0  # 2:1 split
            raw.append(Bar(d, px, px, px, px, 50.0, 100))
        d += timedelta(days=1)
    spy = bars_between(date(2022, 12, 20), 200, price=400.0)
    r = compute_returns(raw, spy, date(2023, 1, 2), None, as_of=date(2024, 1, 1), horizons=(60,))[0]
    assert abs(r.ret) < 1e-9


# ------------------------------------------------------------------ price identity (ticker reuse)

def test_historical_series_continuing_after_delisting_is_rejected_as_reused():
    from app.securitymaster.delisting import Delisting
    from app.securitymaster.provider import _validate_historical
    dl = Delisting(date(2022, 10, 28), "MERGER", "HIGH", "FORM25")
    reused = bars_between(date(2022, 6, 1), 400)  # same symbol keeps trading into 2024 -> another company
    bars, why = _validate_historical(reused, date(2022, 7, 1), dl)
    assert bars == [] and why == "TICKER_REUSED"
    ok = [b for b in reused if b.date <= dl.date]
    bars, why = _validate_historical(ok, date(2022, 7, 1), dl)
    assert bars and why is None


def test_unverifiable_live_symbol_without_delisting_record_is_rejected():
    from app.securitymaster.provider import _validate_historical
    live = bars_between(date.today() - timedelta(days=300), 200)
    bars, why = _validate_historical(live, live[10].date, None)
    assert bars == [] and why == "SYMBOL_UNVERIFIED"


# ------------------------------------------------------------------ episodes / weighting / grade

def row(issuer, d, oc, sector="TECHNOLOGY", verified=True, reason=None, i=[0]):
    i[0] += 1
    return Q.Row(i[0], issuer, sector, d, oc, reason, verified, 0.05 if oc == "win" else -0.05)


def test_overlapping_events_inside_window_are_one_episode():
    rows = [row("A", date(2024, 1, 1), "win"), row("A", date(2024, 1, 10), "win"), row("A", date(2024, 1, 20), "win"),
            row("A", date(2024, 5, 1), "loss")]
    eps = Q.episodes(rows, 90)
    assert len(eps) == 2 and eps[0].n_events == 3 and eps[0].first.outcome == "win" and eps[1].first.outcome == "loss"


def test_episode_window_is_configurable(monkeypatch):
    monkeypatch.setenv("EPISODE_WINDOWS", '{"INSIDER_BUY": 5}')
    assert Q.episode_windows()["INSIDER_BUY"] == 5
    monkeypatch.setenv("EPISODE_WINDOWS", "not json")
    assert Q.episode_windows()["INSIDER_BUY"] == 90  # malformed override never disables the control


def test_company_equal_weighting_stops_one_prolific_issuer_dominating():
    rows = [row("BIG", date(2020, 1, 1) + timedelta(days=100 * k), "win") for k in range(20)]
    rows += [row(f"S{k}", date(2021, 1, 1), "loss") for k in range(4)]
    st = Q.summarize(rows, 90, 90)
    assert st.event_level_rate == pytest.approx(20 / 24)
    assert st.company_equal_rate == pytest.approx(1 / 5)
    assert st.unique_companies == 5 and st.unique_events == 24


def test_sector_equal_weighting():
    rows = [row(f"T{k}", date(2021, 1, 1), "win", sector="TECHNOLOGY") for k in range(9)]
    rows += [row("E0", date(2021, 1, 1), "loss", sector="ENERGY_MINING")]
    st = Q.summarize(rows, 90, 90)
    assert st.company_equal_rate == pytest.approx(0.9) and st.sector_equal_rate == pytest.approx(0.5) and st.sectors_n == 2


def _many(n_companies, unresolved=0, verified=True):
    rows = [row(f"C{k}", date(2021, 1, 1), "win" if k % 2 else "loss", verified=verified) for k in range(n_companies)]
    rows += [row(f"U{k}", date(2021, 1, 1), "unresolved", reason="NO_PRICE_DATA") for k in range(unresolved)]
    return rows


def test_grade_trusted_partial_blocked():
    assert Q.summarize(_many(100), 90, 90).status == "TRUSTED"
    assert Q.summarize(_many(100, unresolved=5), 90, 90).status == "PARTIAL"
    st = Q.summarize(_many(100, unresolved=30), 90, 90)
    assert st.status == "BLOCKED" and "fiyat verisi eksik" in Q.reason_text_tr(st)
    assert Q.summarize(_many(20), 90, 90).status == "BLOCKED"  # too few independent companies
    assert Q.summarize(_many(100, verified=False), 90, 90).status == "PARTIAL"  # identity not verified


def test_partial_reports_worst_and_best_case_bounds():
    st = Q.summarize(_many(100, unresolved=5), 90, 90)
    assert st.worst_case < st.company_equal_rate < st.best_case


def test_pending_computation_is_reported_as_not_yet_computed():
    rows = _many(100) + [row(f"P{k}", date(2021, 1, 1), "unresolved", reason="NO_RETURN_COMPUTED") for k in range(40)]
    st = Q.summarize(rows, 90, 90)
    assert st.status == "BLOCKED" and "henüz hesaplanmadı" in Q.reason_text_tr(st)


# ------------------------------------------------------------------ walk-forward

def test_walk_forward_folds_never_overlap_and_move_forward():
    from app.backtest.walkforward import folds
    fs = folds(date(2021, 1, 1), date(2026, 9, 25))
    assert [(f.train_start.year, f.train_end.year, f.validation_end.year) for f in fs] == [(2021, 2024, 2025), (2022, 2025, 2026)]
    for f in fs:
        assert f.train_start < f.train_end < f.validation_end <= f.test_end or f.validation_end == f.test_end
        assert f.test_end <= date(2026, 9, 25)


def test_walk_forward_scores_only_realized_outcomes(monkeypatch):
    """A train window ending 2024-01-01 must not see an event from 2023-12-01 (its 90-day outcome was not known yet)."""
    from app.backtest import walkforward as W
    seen = {}

    def fake_load(conn, et, cond, params, horizon, as_of=None):
        seen["as_of"] = as_of
        mature = as_of - timedelta(days=horizon + Q.ENTRY_LAG_DAYS)
        rows = [row(f"C{k}", date(2023, 1, 1) + timedelta(days=3 * k), "win") for k in range(120)]
        return [r for r in rows if r.public_date <= mature]

    monkeypatch.setattr(Q, "load_rows", fake_load)
    s = W.score_window(None, "INSIDER_OPEN_MARKET_BUY", W.Candidate("all", "TRUE", {}), date(2023, 1, 1), date(2024, 1, 1), 90)
    assert seen["as_of"] == date(2024, 1, 1)
    assert s["episodes"] < 120  # late-2023 events were excluded as not yet realized


def test_walk_forward_window_failing_the_data_gate_reports_no_rate(monkeypatch):
    from app.backtest import walkforward as W
    rows = [row(f"C{k}", date(2021, 1, 1), "win") for k in range(40)] + \
        [row(f"U{k}", date(2021, 1, 1), "unresolved", reason="NO_PRICE_DATA") for k in range(20)]
    monkeypatch.setattr(Q, "load_rows", lambda *a, **k: rows)
    s = W.score_window(None, "INSIDER_OPEN_MARKET_BUY", W.Candidate("all", "TRUE", {}), date(2021, 1, 1), date(2022, 1, 1), 90)
    assert s["rate"] is None and s["rate_withheld"] and s["unresolved_share"] > 0.1


# ------------------------------------------------------------------ Form 4 multi-owner + trusts

def test_row_is_attributed_only_when_its_text_names_one_owner():
    from app.signals.owners import OwnerRef, attribute
    ault = OwnerRef("0001", "AULT MILTON C III", True, True, True)
    co = OwnerRef("0002", "Ault & Company, Inc.", False, False, True)
    who, how = attribute([ault, co], "By Ault & Company, Inc.")
    assert who is co and how == "footnote"
    who, how = attribute([ault, co], "See Footnote")
    assert who is None and how == "joint"  # never silently the first-listed owner
    who, how = attribute([ault], "")
    assert who is ault and how == "single"


def test_joint_roles_and_links_group_a_person_with_his_vehicles():
    from app.signals.owners import OwnerRef, filing_links, joint_roles
    p = OwnerRef("0001", "Sprott Eric", False, False, True)
    e = OwnerRef("0002", "2176423 Ontario Ltd.", False, False, True)
    assert joint_roles([p, e])["is_ten_pct"] is True
    links = filing_links([p, e], "acc", "0000009999")
    assert links == [{"entity_key": "0002", "entity_name": "2176423 Ontario Ltd.", "beneficial_owner_key": "0001",
                      "beneficial_owner_name": "Sprott Eric", "relationship_type": "JOINT_FILER", "confidence": "HIGH",
                      "source": "form4 joint filing acc", "issuer_cik": "0000009999"}]


def test_trust_named_after_insider_is_the_same_beneficial_owner():
    from app.signals.owners import OwnerRef, name_links
    links = name_links([OwnerRef("0001", "Smith John A"), OwnerRef("0002", "John A. Smith Revocable Trust"),
                        OwnerRef("0003", "Doe Jane"), OwnerRef("0004", "Doe Family Holdings LLC")], "0000000001")
    got = {(lk["entity_key"], lk["beneficial_owner_key"], lk["relationship_type"], lk["confidence"]) for lk in links}
    assert ("0002", "0001", "NAMED_TRUST", "HIGH") in got
    assert ("0004", "0003", "FAMILY_VEHICLE", "MEDIUM") in got


def test_family_vehicle_is_not_linked_when_surname_is_ambiguous():
    from app.signals.owners import OwnerRef, name_links
    links = name_links([OwnerRef("0001", "Lee Anna"), OwnerRef("0005", "Lee Brian"), OwnerRef("0002", "Lee Family Trust")], "x")
    assert links == []


def test_form4_parser_keeps_every_owner_and_ownership_text():
    from app.parsers.form4 import parse_form4
    xml = """<ownershipDocument><documentType>4</documentType>
    <issuer><issuerCik>0000009999</issuerCik><issuerName>X Corp</issuerName><issuerTradingSymbol>XC</issuerTradingSymbol></issuer>
    <reportingOwner><reportingOwnerId><rptOwnerCik>0000000001</rptOwnerCik><rptOwnerName>AULT MILTON C III</rptOwnerName></reportingOwnerId>
      <reportingOwnerRelationship><isDirector>1</isDirector><isOfficer>1</isOfficer><officerTitle>Executive Chairman</officerTitle></reportingOwnerRelationship></reportingOwner>
    <reportingOwner><reportingOwnerId><rptOwnerCik>0000000002</rptOwnerCik><rptOwnerName>Ault &amp; Company, Inc.</rptOwnerName></reportingOwnerId>
      <reportingOwnerRelationship><isTenPercentOwner>1</isTenPercentOwner></reportingOwnerRelationship></reportingOwner>
    <nonDerivativeTable>
      <nonDerivativeTransaction><securityTitle><value>Common</value></securityTitle><transactionDate><value>2026-03-30</value></transactionDate>
        <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
        <transactionAmounts><transactionShares><value>100</value></transactionShares><transactionPricePerShare><value>5</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode></transactionAmounts>
        <postTransactionAmounts><sharesOwnedFollowingTransaction><value>1100</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
        <ownershipNature><directOrIndirectOwnership><value>I</value></directOrIndirectOwnership>
          <natureOfOwnership><value>By Ault &amp; Company, Inc.</value></natureOfOwnership></ownershipNature></nonDerivativeTransaction>
      <nonDerivativeTransaction><securityTitle><value>Common</value></securityTitle><transactionDate><value>2026-03-30</value></transactionDate>
        <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
        <transactionAmounts><transactionShares><value>50</value></transactionShares><transactionPricePerShare><value>5</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode></transactionAmounts>
        <postTransactionAmounts><sharesOwnedFollowingTransaction><value>50</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
        <ownershipNature><directOrIndirectOwnership><value>D</value></directOrIndirectOwnership></ownershipNature></nonDerivativeTransaction>
    </nonDerivativeTable></ownershipDocument>"""
    doc = parse_form4(xml)
    assert len(doc.owners) == 2
    t1, t2 = doc.transactions
    assert t1.direct_indirect == "I" and t1.nature_of_ownership == "By Ault & Company, Inc."
    assert t2.direct_indirect == "D" and t2.nature_of_ownership is None


# ------------------------------------------------------------------ 13D + multi-signal cleanup rules

def test_13d_amendment_materiality():
    from app.integrity.cleanup import is_material_amendment
    assert not is_material_amendment(0.3, "BOARD_SEAT", "BOARD_SEAT", True)  # boilerplate update
    assert is_material_amendment(1.4, "BOARD_SEAT", "BOARD_SEAT", True)  # >= 1pp ownership move
    assert is_material_amendment(-2.0, None, None, True)
    assert is_material_amendment(0.0, "PROXY_FIGHT", "PASSIVE_OR_UNCLEAR", True)  # new activist purpose
    assert not is_material_amendment(0.0, "PASSIVE_OR_UNCLEAR", "MERGER", True)  # "no change" text is not a new episode
    assert is_material_amendment(None, None, None, False)  # nothing before it in our data: opens the episode


def test_multi_signal_one_canonical_per_group_combination_and_episode():
    from app.integrity.cleanup import canonical_multi
    mk = lambda i, d, groups: {"id": i, "public_date": d, "payload": {"source_groups": groups}}
    evs = [mk(1, date(2025, 1, 1), ["ACTIVIST", "INSIDER"]), mk(2, date(2025, 1, 9), ["ACTIVIST", "INSIDER"]),
           mk(3, date(2025, 1, 20), ["ACTIVIST", "INSIDER"]), mk(4, date(2025, 1, 10), ["CONTRACT", "INSIDER"]),
           mk(5, date(2025, 4, 1), ["ACTIVIST", "INSIDER"])]
    res = {e["id"]: c for e, c in canonical_multi(evs)}
    assert res == {1: None, 2: 1, 3: 1, 4: None, 5: None}


def test_multi_signal_rolling_episode_chains_continuous_activity():
    from app.integrity.cleanup import canonical_multi
    mk = lambda i, d: {"id": i, "public_date": d, "payload": {"source_groups": ["ACTIVIST", "INSIDER"]}}
    evs = [mk(1, date(2021, 6, 25)), mk(2, date(2021, 7, 13)), mk(3, date(2021, 7, 30)), mk(4, date(2021, 8, 23)),
           mk(5, date(2021, 11, 12))]  # 18, 17, 24 days apart, then an 81-day gap
    assert {e["id"]: c for e, c in canonical_multi(evs)} == {1: None, 2: 1, 3: 1, 4: 1, 5: None}


def test_multi_signal_legacy_rows_without_groups_use_families():
    from app.integrity.cleanup import groups_of
    assert groups_of({"families": ["CEO_BUY", "INSIDER_CLUSTER", "ACTIVIST_13D"]}) == ("ACTIVIST", "INSIDER")


# ------------------------------------------------------------------ news dedupe + timestamps

def test_syndicated_copy_with_rewritten_headline_is_the_same_story():
    from app.news.cluster import is_same_story, lead_signature, tokens
    lead = ("Acme Robotics Inc. today announced a five-year production agreement with General Motors to supply "
            "actuator modules for its 2027 platform, with deliveries starting in the second quarter.")
    t0 = datetime(2026, 9, 1, 13, tzinfo=timezone.utc)
    a = {"tokens": tokens("Acme Robotics Signs Production Agreement With General Motors"), "time": t0,
         "canonical_url": "https://prnewswire.com/a", "signature": lead_signature("x", lead), "lineage": "PRNEWSWIRE"}
    b = {"tokens": tokens("GM taps small robotics maker in five-year deal"), "time": t0 + timedelta(hours=3),
         "canonical_url": "https://finance.yahoo.com/b", "signature": lead_signature("y", lead + " (Reuters)"), "lineage": "REUTERS"}
    assert is_same_story(a, b)
    other = dict(b, signature=lead_signature("z", "Acme Robotics reported second quarter revenue of $12 million and "
                                                     "raised its full-year outlook citing strong demand."))
    assert not is_same_story(a, other)


def test_canonical_url_strips_tracking_and_amp():
    from app.news.cluster import canonical_url
    assert canonical_url("https://www.example.com/news/story/amp?utm_source=x&id=7#top") == canonical_url("http://example.com/news/story?id=7")


def test_different_figures_are_different_stories_even_with_same_lead_shape():
    from app.news.cluster import is_same_story, tokens
    t0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
    a = {"tokens": tokens("Acme wins $180M order"), "time": t0, "canonical_url": None, "signature": None, "lineage": None}
    b = {"tokens": tokens("Acme wins $18M order"), "time": t0, "canonical_url": None, "signature": None, "lineage": None}
    assert not is_same_story(a, b)


def test_timestamp_confidence_levels():
    from app.news.cluster import timestamp_confidence
    seen = datetime(2026, 9, 1, 14, tzinfo=timezone.utc)
    assert timestamp_confidence("sec_8k", seen - timedelta(minutes=1), seen, exact=True) == "HIGH"
    assert timestamp_confidence("news_prnewswire", seen - timedelta(minutes=10), seen, tier=1) == "HIGH"
    assert timestamp_confidence("news_yahoo_watchlist", seen - timedelta(minutes=10), seen, tier=3) == "MEDIUM"
    assert timestamp_confidence("news_yahoo_watchlist", None, seen) == "LOW"
    assert timestamp_confidence("news_yahoo_watchlist", seen + timedelta(hours=2), seen) == "LOW"  # future-dated
    assert timestamp_confidence("news_yahoo_watchlist", seen - timedelta(days=9), seen, tier=3) == "LOW"


# ------------------------------------------------------------------ point-in-time fundamentals

def test_ttm_revenue_ignores_facts_filed_after_the_event():
    from app.discovery.pit_facts import ttm_from_facts
    f = lambda s, e, v, filed: {"period_start": s, "period_end": e, "val": v, "filed": filed}
    facts = [f(date(2023, 1, 1), date(2023, 3, 31), 10, date(2023, 5, 1)), f(date(2023, 4, 1), date(2023, 6, 30), 10, date(2023, 8, 1)),
             f(date(2023, 7, 1), date(2023, 9, 30), 10, date(2023, 11, 1)), f(date(2022, 10, 1), date(2022, 12, 31), 10, date(2023, 3, 1)),
             f(date(2023, 10, 1), date(2023, 12, 31), 500, date(2024, 3, 1))]  # filed later - must not leak
    assert ttm_from_facts(facts, date(2023, 12, 15)) == 40.0
    assert ttm_from_facts(facts, date(2024, 3, 2)) == 530.0


def test_restated_quarter_replaces_not_adds():
    from app.discovery.pit_facts import ttm_from_facts
    f = lambda s, e, v, filed: {"period_start": s, "period_end": e, "val": v, "filed": filed}
    facts = [f(date(2023, 1, 1), date(2023, 12, 31), 100, date(2024, 2, 1)),
             f(date(2023, 1, 1), date(2023, 12, 31), 90, date(2024, 6, 1))]  # 10-K/A restatement
    assert ttm_from_facts(facts, date(2024, 3, 1)) == 100.0
    assert ttm_from_facts(facts, date(2024, 7, 1)) == 90.0


# ------------------------------------------------------------------ government parent mapping

def test_government_recipient_mapping_confidence(monkeypatch):
    from app.entities.registry import resolver
    from app.sources.usaspending import awards as A
    names = {"ACME DEFENSE INC": "ACME", "ACME HOLDINGS CORP": "ACME", "SUBCO LLC": None, "PARENTCO INC": "PRNT"}
    monkeypatch.setattr(resolver, "ticker_for_name", lambda n: names.get((n or "").upper()))
    assert A.map_recipient("Acme Defense Inc", "Acme Holdings Corp")[1] == "VERIFIED"
    assert A.map_recipient("Acme Defense Inc", None)[1] == "HIGH_CONFIDENCE"
    t, conf, src = A.map_recipient("Subco LLC", "Parentco Inc")
    assert (t, conf) == ("PRNT", "MEDIUM") and "parent" in src
    assert A.map_recipient("Subco LLC", "Unknown Group")[1] == "LOW"
    assert A.map_recipient("Subco LLC", None, manual={"SUBCO LLC": "PRNT"})[:2] == ("PRNT", "VERIFIED")


def test_unverified_contract_mapping_never_joins_a_multi_signal():
    from app.signals.detect import signal_family
    assert signal_family("GOV_CONTRACT", {"value_to_mcap": 0.2, "mapping_confidence": "HIGH_CONFIDENCE"}) == "GOV_CONTRACT"
    assert signal_family("GOV_CONTRACT", {"value_to_mcap": 0.2, "mapping_confidence": "MEDIUM"}) is None
    assert signal_family("GOV_CONTRACT", {"value_to_mcap": 0.2, "mapping_confidence": "LOW"}) is None


# ------------------------------------------------------------------ discovery precision rules

def _feats(**kw):
    base = {"market_cap": 500e6, "in_priority_band": True, "revenue_yoy": 0.8, "revenue_yoy_prev": 0.5,
            "inflection_flags": ["REVENUE_ACCELERATION", "BACKLOG_BREAKOUT"], "stage": "EARLY_COMMERCIAL", "stage_prev": "PILOT",
            "stage_advanced": True, "major_customer_count": 0, "major_customers": [], "bottleneck_signals": ["SOLE_SUPPLIER"],
            "government_backed": True, "gov_contract_to_mcap": None, "insider_buyers_90d": 0, "insider_buy_value_90d": 0,
            "activist_13d": False, "positive_ocf": True, "risks": [], "themes": ["ROBOTICS"], "ttm_revenue": 0, "hype_sentences": 0}
    base.update(kw)
    return base


def test_language_only_company_cannot_be_high():
    from app.discovery.scoring import score, strong_evidence
    f = _feats()
    f["strong_evidence"] = strong_evidence(f, [], ["SOLE_SUPPLIER", "GOVERNMENT_BACKED"])
    assert f["strong_evidence"] == []
    _s, level, _why = score(f)
    assert level != "HIGH"


def test_real_world_evidence_allows_high():
    from app.discovery.scoring import score, strong_evidence
    f = _feats(ttm_revenue=40e6)
    rels = [{"counterparty": "GM", "category": "AUTOMOTIVE_OEM", "relation": "customer", "agreement": "production_agreement",
             "hedged": False, "strength": 1.0}]
    f["strong_evidence"] = strong_evidence(f, rels, ["SOLE_SUPPLIER"])
    assert {"COMMERCIAL_REVENUE", "BINDING_CONTRACT"} <= set(f["strong_evidence"])
    assert score(f)[1] == "HIGH"


def test_mou_is_not_binding_evidence():
    from app.discovery.scoring import strong_evidence
    rels = [{"agreement": "mou", "hedged": False, "relation": "partner"}, {"agreement": "loi", "hedged": False, "relation": "customer"}]
    assert "BINDING_CONTRACT" not in strong_evidence(_feats(), rels, [])


def test_promotional_tam_language_is_counted_not_rewarded():
    from app.discovery.scoring import hype_count
    assert hype_count(["We address a multi-billion dollar market opportunity", "Our revolutionary platform",
                       "Revenue was $4.1 million"]) == 2


@pytest.mark.parametrize("label,sentence", [
    ("SOLE_SUPPLIER", "Because we do not intend to pay any cash dividends, capital appreciation, if any, would be your sole source of gain."),
    ("QUALIFICATION", "All forward-looking statements attributable to us are expressly qualified by these cautionary statements."),
    ("GOVERNMENT_BACKED", "Purchaser is not a foreign person as such term is defined in Section 721 of the Defense Production Act of 1950."),
    ("GOVERNMENT_BACKED", "The Company's critical mineral recycling factory is in McCarran, Nevada."),
    ("LONG_TERM_SUPPLY", "Section 7.17 Take-or-Pay or other Prepayments."),
    ("FIRST_COMMERCIAL_PRODUCTION", "The license terminates fifteen years after the first commercial sale of a product, with royalties on net sales."),
    ("RESEARCH_STAGE", "Research and development, or R&D, expense includes personnel costs, mask costs and prototype costs."),
])
def test_boilerplate_never_counts_as_discovery_evidence(label, sentence):
    from app.discovery import language as L
    assert not L.signal_ok(label, sentence)
    assert label not in L.signals_in(sentence)


def test_real_signals_survive_the_boilerplate_filter():
    from app.discovery import language as L
    assert "LONG_TERM_SUPPLY" in L.signals_in("On July 26, 2026, we entered into a six-year capacity reservation agreement with a customer.")
    assert "GOVERNMENT_BACKED" in L.signals_in("The DOE Loan Programs Office issued a conditional commitment for a $1.6 billion loan guarantee.")
    assert "FIRST_COMMERCIAL_PRODUCTION" in L.signals_in("We commenced commercial production of enriched isotopes in the first half of 2025.")


@pytest.mark.parametrize("sentence", [
    "JJDC, the investment arm of Johnson & Johnson and owner of Janssen, owns more than 5% of our outstanding shares.",
    "The Termination Agreement also terminated that certain Supply Agreement with Janssen.",
    "New users are drawn to our products by rankings circulated by Amazon, Apple and Google app stores.",
])
def test_investor_terminated_and_channel_mentions_are_not_customers(sentence):
    from app.discovery import language as L
    assert all(r.strength == 0.0 for r in L.relations_in(sentence))


def test_scope_and_scale_rules():
    from app.discovery.scoring import score
    f = _feats(ttm_revenue=40e6, strong_evidence=["COMMERCIAL_REVENUE", "BINDING_CONTRACT"])
    assert score(f)[1] == "HIGH"
    assert score(dict(f, ttm_revenue=400e6))[1] == "MEDIUM"  # established revenue base is not an early discovery
    assert score(dict(f, ttm_revenue=2e9))[1] == "LOW"
    assert score(dict(f, market_cap=40e9))[1] == "LOW"
    lvl, why = score(dict(f, scope_excluded="sector FINANCIALS (SIC 6022)"))[1:]
    assert lvl == "LOW" and "kapsam dışı" in why[0]


def test_financing_risk_flags_and_high_label():
    from app.discovery.financing import financing_score, is_high
    flags = ["ATM_PROGRAM", "PIPE_3_02", "SHARE_GROWTH_50PCT", "WARRANTS"]
    sc = financing_score(flags)
    assert sc == pytest.approx(6.0) and is_high(flags, sc)
    assert is_high(["GOING_CONCERN"], financing_score(["GOING_CONCERN"]))
    assert not is_high(["SHELF_S3"], financing_score(["SHELF_S3"]))


# ------------------------------------------------------------------ OGE parsing (OCR text layer)

def test_oge_rows_parse_with_ocr_noise_and_confidence():
    from app.sources.trump.oge import parse_transactions
    text = "\n".join([
        "1 MIAMI DADE CNTY FL AVIATION REV RFDG SER A B/E PTC 5.00 % Due Oct 1, 2031 lourchaso 7/7/2026 Yes $1 000 001 -$5 000 000",
        "4 MAIN STR NAT GAS INC GA GAS SPLY REV SER A B/E 4.00 % Due Jul 1, 2052 lourchaso 7/812026 Vos $1 000 001 -$5 000 000",
        "34 PITTSBURGH & ALLEGHENY CNTY PA SPORTS & EXHIB AUTH REV AGM B/E 5.00 % Due Feb 1, 2027 I ourchaso 7/17/2026 No S50 001-$100000",
        "Page 02 of37",
    ])
    rows = {r["line_no"]: r for r in parse_transactions(text)}
    assert rows[1]["tx_type"] == "PURCHASE" and rows[1]["tx_date"] == date(2026, 7, 7) and rows[1]["amount_min"] == 1_000_001
    assert rows[1]["parse_confidence"] == "HIGH"
    assert rows[4]["tx_date"] == date(2026, 7, 8) and rows[4]["parse_confidence"] == "MEDIUM"  # repaired OCR date
    assert rows[34]["amount_min"] == 50_001 and rows[34]["amount_max"] == 100_000
    assert all(r["ticker"] is None for r in rows.values())  # no name -> ticker inference


def test_oge_index_filter_keeps_only_trump_filers():
    from app.sources.trump.oge import parse_index
    rows = [{"type": "<a href='https://x/a.pdf'>278 Transaction</a>", "name": "Trump, Donald J", "docDate": "2026-09-22T04:21:09"},
            {"type": "<a href='https://x/b.pdf'>Ethics Agreement</a>", "name": "Stebbins, Robert", "docDate": "2026-09-24T04:21:38"}]
    out = parse_index(rows)
    assert len(out) == 1 and out[0]["doc_type"] == "278 Transaction" and out[0]["doc_date"].tzinfo is not None


# ------------------------------------------------------------------ SEC index parsing

def test_full_index_parser_extracts_delisting_notices():
    from app.securitymaster.build import parse_full_index
    txt = ("25-NSE           1Life Healthcare Inc                                          1404123     2023-02-22  "
           "edgar/data/1404123/0001354457-23-000108.txt\n"
           "SC 14D9          Target Co                                                     1234567     2023-02-01  "
           "edgar/data/1234567/0001234567-23-000001.txt\n"
           "10-K             Other                                                         7654321     2023-02-01  edgar/data/7654321/x.txt\n")
    rows = parse_full_index(txt)
    assert [(r["cik"], r["action"], r["action_date"]) for r in rows] == [
        ("0001404123", "FORM25_NSE", date(2023, 2, 22)), ("0001234567", "TENDER_TARGET", date(2023, 2, 1))]


def test_submissions_items_become_corporate_actions():
    from app.securitymaster.build import parse_submissions
    d = {"name": "X", "sic": "3674", "sicDescription": "Semiconductors", "tickers": ["XX"], "exchanges": ["Nasdaq"],
         "filings": {"recent": {"form": ["8-K", "S-3", "424B5", "10-Q"], "filingDate": ["2024-01-02", "2024-02-01", "2024-02-10", "2024-05-01"],
                                "items": ["1.03,9.01", "", "", ""], "accessionNumber": ["a1", "a2", "a3", "a4"]}}}
    master, acts = parse_submissions("123", d)
    assert master["sector"] == "TECHNOLOGY" and master["cik"] == "0000000123"
    assert {x["action"] for x in acts} == {"BANKRUPTCY", "SHELF_REGISTRATION", "PROSPECTUS_424B"}


def test_form345_symbols_aggregate_point_in_time_intervals():
    from app.securitymaster.build import aggregate_symbols
    rows = [{"ISSUERCIK": "1", "ISSUERTRADINGSYMBOL": "FB", "FILING_DATE": "02-JAN-2021"},
            {"ISSUERCIK": "1", "ISSUERTRADINGSYMBOL": "FB", "FILING_DATE": "15-MAY-2022"},
            {"ISSUERCIK": "1", "ISSUERTRADINGSYMBOL": "META", "FILING_DATE": "20-JUN-2022"},
            {"ISSUERCIK": "2", "ISSUERTRADINGSYMBOL": "NONE", "FILING_DATE": "20-JUN-2022"}]
    agg = aggregate_symbols(rows)
    assert agg[("0000000001", "FB")] == [date(2021, 1, 2), date(2022, 5, 15), 2]
    assert agg[("0000000001", "META")][0] == date(2022, 6, 20)
    assert ("0000000002", "NONE") not in agg


def test_listing_periods_are_merged_per_symbol_and_exchange():
    from app.securitymaster.vendors import merge_periods
    rows = [{"symbol": "ABC", "exchange": "NYSE", "start_date": date(2015, 1, 1), "end_date": date(2019, 1, 1)},
            {"symbol": "ABC", "exchange": "NYSE", "start_date": date(2020, 1, 1), "end_date": date(2023, 1, 1)},
            {"symbol": "ABC", "exchange": "NASDAQ", "start_date": date(2024, 1, 1), "end_date": None}]
    got = {(r["symbol"], r["exchange"]): (r["start_date"], r["end_date"]) for r in merge_periods(rows)}
    assert got == {("ABC", "NYSE"): (date(2015, 1, 1), date(2023, 1, 1)), ("ABC", "NASDAQ"): (date(2024, 1, 1), None)}


def test_sector_mapping():
    from app.securitymaster.sic import sector_for_sic
    assert sector_for_sic("2834") == "PHARMA_BIOTECH" and sector_for_sic("3674") == "TECHNOLOGY"
    assert sector_for_sic("6770") == "SHELL_SPAC" and sector_for_sic(None) is None and sector_for_sic("1311") == "ENERGY_MINING"


def test_oge_index_retries_the_transient_domino_rendering_error(monkeypatch):
    from app import http
    from app.sources.trump import oge
    calls = []

    class R:
        def json(self):
            return {"data": [{"name": "Trump, Donald J"}]}

    def fake_get(url, **kw):
        calls.append(url)
        if len(calls) == 1:
            raise http.HttpError(url, 400, '{"message":"Error while rendering service"}')
        return R()

    monkeypatch.setattr(oge.http, "get", fake_get)
    assert oge.fetch_index(pause_s=0) == [{"name": "Trump, Donald J"}] and len(calls) == 2
