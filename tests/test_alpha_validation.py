"""Alpha-validation work: 10-K text changes, insider person profiles, paper ledger, proposals, gate."""
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text

from app.marketdata.provider import Bar
from tests.test_db import URL

# ------------------------------------------------------------------ 10-K sections + diff

FILLER = ("The Company designs and sells industrial sensors to manufacturers across North America and Europe through "
          "direct sales teams and distributors. ")


def _risk(i: int) -> str:
    return f"Risk statement number {'abcdefghijklmnopqrstuvwxyz'[i % 26]}{'xyz'[i % 3]} describes an operational hazard that " \
           f"could adversely affect our business, results of operations and financial condition in material ways. "


def _doc(risks: list[str], mdna: list[str]) -> str:
    toc = "Table of Contents\nItem 1. Business 3\nItem 1A. Risk Factors 9\nItem 1B. Unresolved Staff Comments 20\n" \
          "Item 7. Management's Discussion and Analysis 25\nItem 8. Financial Statements 40\n"
    return (toc + "\nItem 1. Business\n" + FILLER * 8 + "\nItem 1A. Risk Factors\n" + "".join(risks) +
            "\nItem 1B. Unresolved Staff Comments\nNone.\nItem 7. Management's Discussion and Analysis\n" + "".join(mdna) +
            "\nItem 8. Financial Statements\n" + FILLER * 3)


BASE_MDNA = ["As of December 31, 2023, we had cash and cash equivalents of $41.2 million and no outstanding borrowings. ",
             "We believe our existing cash will be sufficient to fund our operations for at least the next 12 months from issuance. ",
             FILLER * 6]


def test_tenk_sections_skip_the_table_of_contents():
    from app.tenk.diff import extract_items
    risks = [_risk(i) for i in range(20)]
    risks.insert(3, "For more detail see Item 7. Management's Discussion and Analysis and Item 1A of this report. ")
    items = extract_items(_doc(risks, BASE_MDNA))
    assert {"BUSINESS", "RISK_FACTORS", "MD&A"} <= set(items)
    assert _risk(19).strip() in items["RISK_FACTORS"]  # an inline cross-reference does not cut the section
    assert "Risk statement" in items["RISK_FACTORS"] and "Table of Contents" not in items["RISK_FACTORS"]


def test_tenk_boilerplate_year_rollforward_is_not_a_change():
    from app.tenk.diff import build_profile, compare, extract_items
    old = build_profile(extract_items(_doc([_risk(i) for i in range(20)], BASE_MDNA)))
    mdna = [BASE_MDNA[0].replace("2023", "2024").replace("41.2", "38.9"), BASE_MDNA[1], BASE_MDNA[2]]
    new = build_profile(extract_items(_doc([_risk(i) for i in range(20)], mdna)))
    assert compare(old, new) == []


def test_tenk_new_going_concern_and_liquidity_deterioration():
    from app.tenk.diff import build_profile, compare, extract_items
    old = build_profile(extract_items(_doc([_risk(i) for i in range(20)], BASE_MDNA)))
    mdna = BASE_MDNA + [
        "There is substantial doubt about our ability to continue as a going concern within one year after issuance. ",
        "Our independent registered public accounting firm has expressed substantial doubt about our ability to continue as a going concern. ",
        "Our cash and cash equivalents will not be sufficient to fund our operations beyond the second quarter of the year. ",
        "We may be unable to obtain additional financing to maintain adequate liquidity on acceptable terms or at all. "]
    items = extract_items(_doc([_risk(i) for i in range(20)], mdna))
    ch = {c.change_type: c for c in compare(old, build_profile(items), items)}
    assert ch["NEW_GOING_CONCERN"].materiality == "HIGH" and ch["NEW_GOING_CONCERN"].examples
    assert "LIQUIDITY_DETERIORATION" in ch
    # and the reverse direction is recognised as its removal
    back = {c.change_type for c in compare(build_profile(items), old)}
    assert "REMOVED_GOING_CONCERN" in back


def test_tenk_going_concern_policy_boilerplate_is_not_a_warning():
    from app.tenk.diff import build_profile, compare, extract_items
    old = build_profile(extract_items(_doc([_risk(i) for i in range(20)], BASE_MDNA)))
    mdna = BASE_MDNA + [
        "Management evaluates whether there are conditions or events that raise substantial doubt about the Company's ability to continue. ",
        "A default under the credit agreement could raise substantial doubt about our ability to continue as a going concern. ",
        "There is substantial doubt about our ability to continue as a going concern within one year after issuance. "]
    items = extract_items(_doc([_risk(i) for i in range(20)], mdna))
    assert not any("GOING_CONCERN" in c.change_type for c in compare(old, build_profile(items), items))


def test_tenk_risk_increase_needs_many_new_statements():
    from app.tenk.diff import build_profile, compare, extract_items
    old = build_profile(extract_items(_doc([_risk(i) for i in range(40)], BASE_MDNA)))
    few = build_profile(extract_items(_doc([_risk(i) for i in range(43)], BASE_MDNA)))
    many = build_profile(extract_items(_doc([_risk(i) for i in range(60)], BASE_MDNA)))
    assert not any(c.change_type == "RISK_INCREASE" for c in compare(old, few))
    assert any(c.change_type == "RISK_INCREASE" for c in compare(old, many))


def test_tenk_entry_uses_the_public_acceptance_timestamp():
    from app.marketdata.calendar import first_tradable_session
    from app.tenk.engine import acceptance_to_utc
    after = acceptance_to_utc("2024-03-15T16:05:12.000Z")  # EDGAR's "Z" is really Eastern time
    assert after == datetime(2024, 3, 15, 20, 5, 12, tzinfo=timezone.utc)
    assert first_tradable_session(date(2024, 3, 15), after) == date(2024, 3, 18)  # after the close -> Monday
    before = acceptance_to_utc("2024-03-15T08:00:00.000Z")
    assert first_tradable_session(date(2024, 3, 15), before) == date(2024, 3, 15)


def test_tenk_annual_filings_from_submissions():
    from app.tenk.engine import annual_filings
    sub = {"filings": {"recent": {"form": ["10-Q", "10-K", "8-K", "10-K/A", "10-K"],
                                  "accessionNumber": ["a", "b", "c", "d", "e"],
                                  "filingDate": ["2024-05-01", "2024-03-01", "2024-02-01", "2023-04-01", "2023-03-01"],
                                  "acceptanceDateTime": ["", "2024-03-01T17:00:00.000Z", "", "", "2023-03-01T09:00:00.000Z"],
                                  "primaryDocument": ["q.htm", "k.htm", "x.htm", "ka.htm", "k23.htm"],
                                  "reportDate": ["", "2023-12-31", "", "", "2022-12-31"]}}}
    fl = annual_filings(sub)
    assert [f["accession"] for f in fl] == ["e", "b"]  # amendments are not a new annual report


# ------------------------------------------------------------------ insider profiles (pure)

def _buy(issuer, d, oc, ex=0.05, sx=None):
    from app.signals.insider_profile import Buy
    return Buy("P1", "Jane Doe", issuer, d, oc, ex, ex, ex, sx)


def test_insider_episode_grouping():
    from app.signals.insider_profile import episodes
    b = [_buy("A", date(2022, 1, 1), "win"), _buy("A", date(2022, 2, 1), "win"), _buy("A", date(2022, 3, 15), "win"),
         _buy("A", date(2022, 6, 1), "loss"), _buy("B", date(2022, 1, 5), "loss")]
    eps = episodes(b)
    assert len(eps) == 3 and sorted((e.issuer, e.public_date) for e in eps)[0] == ("A", date(2022, 1, 1))


def test_insider_labels_need_sample_and_narrow_interval():
    from app.signals.insider_profile import build_profile
    yearly = [date(2015 + i, 1, 1) for i in range(12)]
    strong = build_profile("P1", [_buy(f"C{i}", d, "win") for i, d in enumerate(yearly[:10])])
    assert strong.label == "HISTORICALLY_STRONG" and strong.ci_low > 0.5
    weak = build_profile("P1", [_buy(f"C{i}", d, "win" if i == 0 else "loss", -0.1) for i, d in enumerate(yearly)])
    assert weak.label == "HISTORICALLY_WEAK" and weak.ci_high < 0.5
    mixed = build_profile("P1", [_buy(f"C{i}", d, "win" if i % 2 else "loss") for i, d in enumerate(yearly[:10])])
    assert mixed.label == "MIXED"
    few = build_profile("P1", [_buy(f"C{i}", d, "win") for i, d in enumerate(yearly[:7])])
    assert few.label == "INSUFFICIENT_HISTORY"
    unresolved = build_profile("P1", [_buy(f"C{i}", d, "win" if i < 9 else "unresolved") for i, d in enumerate(yearly)])
    assert unresolved.label == "INSUFFICIENT_HISTORY" and unresolved.data_quality == "PARTIAL"


def test_insider_telegram_line_only_with_a_label():
    from app.signals.insider_profile import Profile, telegram_line
    assert telegram_line(Profile("P", "x")) is None
    p = Profile("P", "x", episode_count=10, unique_company_count=9, spy_beat_rate_90d=0.8, ci_low=0.55, ci_high=0.93,
                label="HISTORICALLY_STRONG")
    assert "geçmişte güçlü" in telegram_line(p) and "%80" in telegram_line(p)


# ------------------------------------------------------------------ sizing / slippage / verdict / Telegram (pure)

def test_position_sizing_caps():
    from app.paper.proposals import size_limits
    assert size_limits(5e9, "ALREADY_SCALED", set()) == 0.05
    assert size_limits(150e6, "MICRO", set()) == 0.02
    assert size_limits(None, None, set()) == 0.02  # unknown size = risky case
    assert size_limits(5e9, "GROWTH", {"HIGH_FINANCING_RISK"}) == 0.01


def test_slippage_is_higher_for_microcaps():
    from app.paper.ledger import slippage_bps
    assert slippage_bps(100e6, "MICRO") > slippage_bps(1e9, "SMALL") > slippage_bps(50e9, "ALREADY_SCALED")


def test_alpha_verdicts():
    from app.paper.alpha import verdict
    assert verdict({"status": "TRUSTED", "company_equal_rate": 0.351, "company_n": 400})[0] == "UNDERPERFORMING"
    assert verdict({"status": "TRUSTED", "company_equal_rate": 0.62, "company_n": 400})[0] == "OUTPERFORMING"
    assert verdict({"status": "TRUSTED", "company_equal_rate": 0.52, "company_n": 60})[0] == "INCONCLUSIVE"
    assert verdict({"status": "PARTIAL", "worst_case": 0.30, "best_case": 0.45})[0] == "UNDERPERFORMING"
    assert verdict({"status": "PARTIAL", "worst_case": 0.37, "best_case": 0.50})[0] == "INCONCLUSIVE"
    assert verdict({"status": "BLOCKED"})[0] == "BLOCKED"


def test_proposal_telegram_text_is_turkish_and_simple():
    from app.paper.proposals import format_proposal
    from app.paper.proposals import thesis_tr
    t = thesis_tr({"event_type": "TENK_CHANGE", "payload": {"change_types": ["REMOVED_GOING_CONCERN", "NEW_LEGAL_RISK"]}})
    assert "sürekliliği" in t and "LEGAL" not in t
    txt = format_proposal({"code": "PEABC123", "ticker": "ZBIO", "reasons": ["yönetici hisse alımı"], "thesis": "CEO aldı",
                           "data_quality": "OK", "suggested_position_pct": 0.02, "max_position_pct": 0.02,
                           "entry_reference_price": 12.5, "reference_date": date(2026, 9, 25), "stop_reference": 10.63,
                           "risks": ["çok küçük şirket: fiyat sert oynayabilir"]})
    assert "PUBLIC EDGE – SANAL İŞLEM ADAYI" in txt and "ONAY BEKLİYOR" in txt and "Bu sanal işlem önerisidir." in txt
    assert "%2" in txt and "yatırım tavsiyesi" not in txt.lower()


def test_tiingo_missing_key_is_skipped(monkeypatch):
    monkeypatch.delenv("TIINGO_API_KEY", raising=False)
    from app.securitymaster import tiingo_backfill
    from app.securitymaster.vendors import delisted_price_provider
    assert delisted_price_provider() is None
    assert tiingo_backfill.run(None) == {"skipped": "TIINGO_NOT_CONFIGURED"}


# ------------------------------------------------------------------ DB

db = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")


class _NoFetch:
    name = "nofetch"

    def get_historical_prices(self, symbol, start, end):
        return []


def _bars(c, symbol, start, n, price=10.0, step=0.05):
    from app.marketdata.provider import store_bars
    out, d, p = [], start, price
    while len(out) < n:
        if d.weekday() < 5:
            out.append(Bar(d, p, p * 1.01, p * 0.99, p, p, 100000))
            p += step
        d += timedelta(days=1)
    store_bars(c, symbol, out)
    return out


@pytest.fixture
def offline_prices(monkeypatch):
    import app.marketdata.provider as mp
    monkeypatch.setattr(mp, "get_provider", lambda: _NoFetch())


def _event(c, et="INSIDER_OPEN_MARKET_BUY", ticker="ZBIO", cik="0001953926", d=date(2024, 2, 28), key="e1", payload=None):
    from app.db.store import create_event
    return create_event(c, event_type=et, dedupe_key=key, public_date=d, ticker=ticker, issuer_cik=cik, title="t",
                        payload=payload or {"owner": "Jane Doe", "owner_cik": "0000000001", "value": 250000},
                        source_url="u", public_at=datetime(d.year, d.month, d.day, 21, 0, tzinfo=timezone.utc))


def _universe(c, cik="0001953926", ticker="ZBIO", adv=5e6, mcap=1e9, bucket="SMALL"):
    from app.db.models import universe_snapshots
    c.execute(universe_snapshots.insert().values(as_of=date(2024, 2, 1), cik=cik, ticker=ticker, market_cap=mcap, bucket=bucket,
                                                 adv_dollar_30d=adv))


@db
@pytest.mark.usefixtures("clean", "offline_prices")
def test_tenk_process_filing_creates_typed_changes_and_event(engine, monkeypatch):
    from app.db.models import events, tenk_changes, tenk_filings
    from app.tenk import engine as te
    docs = {"a1": _doc([_risk(i) for i in range(20)], BASE_MDNA),
            "a2": _doc([_risk(i) for i in range(20)], BASE_MDNA + [
                "There is substantial doubt about our ability to continue as a going concern within one year after issuance. ",
                "Our independent registered public accounting firm has expressed substantial doubt about our ability to continue as a going concern. "])}
    monkeypatch.setattr(te, "fetch_text", lambda cik, f: docs[f["accession"]])
    f1 = {"accession": "a1", "form": "10-K", "filing_date": date(2023, 3, 1), "accepted_at": te.acceptance_to_utc("2023-03-01T16:30:00Z"),
          "primary_doc": "k.htm", "period": "2022-12-31"}
    f2 = {**f1, "accession": "a2", "filing_date": date(2024, 3, 1), "accepted_at": te.acceptance_to_utc("2024-03-01T16:30:00Z")}
    assert te.process_filing(engine, "0001953926", "ZBIO", "Zenas", f1, is_backfill=True)["status"] == "first"
    assert te.process_filing(engine, "0001953926", "ZBIO", "Zenas", f2, is_backfill=True)["status"] == "event"
    assert te.process_filing(engine, "0001953926", "ZBIO", "Zenas", f2, is_backfill=True) == {"skipped": "known"}
    with engine.connect() as c:
        ev = c.execute(select(events).where(events.c.event_type == "TENK_CHANGE")).mappings().one()
        ch = c.execute(select(tenk_changes)).mappings().all()
        assert c.execute(select(tenk_filings.c.prev_accession).where(tenk_filings.c.accession == "a2")).scalar() == "a1"
    assert ev["public_at"] == datetime(2024, 3, 1, 21, 30, tzinfo=timezone.utc) and ev["public_date"] == date(2024, 3, 1)
    assert ev["payload"]["direction"] == "NEGATIVE" and ev["priority"] < 4  # recorded, not alerted
    assert {c["change_type"] for c in ch} == {"NEW_GOING_CONCERN"}
    assert all(c["source_url"].endswith("k.htm") and c["public_timestamp"] for c in ch)


@db
@pytest.mark.usefixtures("clean")
def test_insider_profile_is_point_in_time(engine):
    from app.db.models import event_returns
    from app.signals.insider_profile import profile_at
    with engine.begin() as c:
        for i in range(10):
            d = date(2015 + i, 1, 10)
            eid = _event(c, cik=f"{i + 100:010d}", ticker=None, d=d, key=f"b{i}")
            c.execute(event_returns.insert().values(event_id=eid, horizon_days=90, entry_date=d, entry_price=10, exit_date=d,
                                                    exit_price=11, ret=0.1, spy_ret=0.02, excess_ret=0.08))
        # a top-up 20 days later at the same company is the same episode
        eid = _event(c, cik=f"{100:010d}", ticker=None, d=date(2015, 1, 30), key="topup")
        c.execute(event_returns.insert().values(event_id=eid, horizon_days=90, entry_date=date(2015, 1, 30), entry_price=10,
                                                exit_date=date(2015, 1, 30), exit_price=9, ret=-0.1, spy_ret=0.0, excess_ret=-0.1))
        full = profile_at(c, "0000000001", date(2025, 1, 1))
        early = profile_at(c, "0000000001", date(2020, 1, 1))
    assert full.completed_buy_count == 11 and full.episode_count == 10 and full.label == "HISTORICALLY_STRONG"
    # in 2020 only the 2015-2019 buys had matured: no later outcome leaks in
    assert early.completed_buy_count == 6 and early.label == "INSUFFICIENT_HISTORY"


@db
@pytest.mark.usefixtures("clean", "offline_prices")
def test_gate_rules(engine):
    from app.paper.proposals import evaluate_gate
    with engine.begin() as c:
        eid = _event(c)
        ev = dict(c.execute(text("SELECT * FROM events WHERE id = :i"), {"i": eid}).mappings().one())
        g = evaluate_gate(c, ev, family_ok=(True, "FAMILY_OUTPERFORMING"), price_basis="current_ticker_same_cik")
        assert "LOW_LIQUIDITY" in g.failed and not g.ok  # no liquidity data = no proposal
        _universe(c)
        g = evaluate_gate(c, ev, family_ok=(True, "FAMILY_OUTPERFORMING"), price_basis="current_ticker_same_cik")
        assert g.ok and g.max_pct == 0.05
        g = evaluate_gate(c, ev, family_ok=(False, "FAMILY_UNDERPERFORMING"), price_basis="current_ticker_same_cik")
        assert not g.ok and "FAMILY_UNDERPERFORMING" in g.failed
        g = evaluate_gate(c, ev, family_ok=(True, "FAMILY_BLOCKED"), price_basis="current_ticker_same_cik")
        assert g.ok and "FAMILY_BLOCKED:RESEARCH_MODE_OVERRIDE" in g.passed  # never shown as a plain pass
        g = evaluate_gate(c, {**ev, "payload": {"direction": "NEGATIVE"}}, family_ok=(True, "FAMILY_OUTPERFORMING"),
                          price_basis="current_ticker_same_cik")
        assert "NOT_A_LONG_SIGNAL" in g.failed  # a new 10-K risk never becomes a buy proposal
        g = evaluate_gate(c, ev, family_ok=(True, "x"), price_basis="ticker_only_unverified")
        assert "UNRESOLVED_SECURITY_MAPPING" in g.failed and g.data_quality == "BLOCKED"
        c.execute(text("INSERT INTO discovery_risks (cik, risk, severity) VALUES ('0001953926', 'GOING_CONCERN', 'HIGH'), "
                       "('0001953926', 'HIGH_FINANCING_RISK', 'HIGH')"))
        g = evaluate_gate(c, ev, family_ok=(True, "x"), price_basis="current_ticker_same_cik")
        assert "SEVERE_FINANCING_RISK" in g.failed and g.max_pct == 0.01


@db
@pytest.mark.usefixtures("clean", "offline_prices")
def test_proposal_approval_paper_fill_exit_and_pnl(engine):
    from app.db.models import paper_cash, paper_orders, paper_positions, paper_trades, proposed_trades
    from app.paper import ledger
    from app.paper.proposals import create_proposal, decide
    with engine.begin() as c:
        _universe(c)
        _bars(c, "ZBIO", date(2024, 1, 2), 200, 10.0, 0.05)
        _bars(c, "SPY", date(2024, 1, 2), 200, 400.0, 0.5)
        eid = _event(c)
    t0 = datetime(2024, 3, 1, 15, 0, tzinfo=timezone.utc)
    # INSIDER_BUY is not proven (BLOCKED in an empty DB): automatic mode refuses, research mode may propose
    assert create_proposal(engine, eid, now=t0)["created"] is False
    p = create_proposal(engine, eid, research_only=True, now=t0)
    assert p["created"]
    assert create_proposal(engine, eid, research_only=True, now=t0)["failed"] == ["DUPLICATE"]
    r = decide(engine, p["code"], True, by="test", now=datetime(2024, 3, 1, 16, 0, tzinfo=timezone.utc))
    assert r["ok"] and r["status"] == "APPROVED" and len(r["orders"]) == 5
    assert decide(engine, p["code"], False, by="test")["error"] == "ALREADY_APPROVED"
    with engine.begin() as c:
        assert c.execute(select(paper_orders.c.not_before).limit(1)).scalar() == date(2024, 3, 4)  # next session, never same-day
        assert ledger.fill_orders(c, date(2024, 3, 1))["filled"] == 0
        assert ledger.fill_orders(c, date(2024, 3, 10))["filled"] == 5
        pos = c.execute(select(paper_positions).order_by(paper_positions.c.id)).mappings().all()
        open_px = next(b for b in ledger._bars(c, "ZBIO", date(2024, 3, 4), date(2024, 3, 4))).open
        assert pos[0]["entry_date"] == date(2024, 3, 4)
        assert pos[0]["entry_price"] == pytest.approx(open_px * 1.002)  # SMALL cap: 20 bps slippage
        assert pos[0]["cost"] == pytest.approx(0.03 * 10_000 + 1.0)
        prop = c.execute(select(proposed_trades)).mappings().one()
        assert prop["research_only"] and prop["approval_status"] == "APPROVED"
    ledger.run(engine, today=date(2024, 6, 20))
    with engine.begin() as c:
        rows = {r["strategy"]: r for r in c.execute(text(
            "SELECT a.strategy, p.* FROM paper_positions p JOIN paper_accounts a ON a.id = p.account_id")).mappings()}
        assert rows["A_30D"]["status"] == "CLOSED" and rows["A_30D"]["exit_reason"] == "TIME"
        assert rows["B_90D"]["status"] == "CLOSED" and rows["B_90D"]["exit_date"] == date(2024, 6, 3)
        assert rows["C_180D"]["status"] == "OPEN" and rows["D_THESIS"]["status"] == "OPEN"
        b = rows["B_90D"]
        assert b["realized_pnl"] == pytest.approx(b["shares"] * b["exit_price"] - 1.0 - b["cost"])
        assert b["realized_pnl"] > 0
        # the cash ledger reconciles: starting capital + realized P/L - cost of still-open positions
        cash = ledger.cash_of(c, b["account_id"])
        assert cash == pytest.approx(10_000 + b["realized_pnl"])
        assert c.execute(select(paper_trades).where(paper_trades.c.account_id == b["account_id"])).mappings().all()[-1]["side"] == "SELL"
        assert c.execute(select(paper_cash).where(paper_cash.c.reason == "DEPOSIT")).all()
        m = ledger.account_metrics(c, b["account_id"])
        assert m["win_rate"] == 1.0 and m["avg_holding_days"] == 91 and m["spy_nav"] > 10_000  # SPY benchmark rose too


@db
@pytest.mark.usefixtures("clean", "offline_prices")
def test_stop_loss_exits_at_the_next_session(engine):
    from app.paper import ledger
    from app.paper.proposals import create_proposal, decide
    with engine.begin() as c:
        _universe(c)
        _bars(c, "ZBIO", date(2024, 1, 2), 120, 20.0, -0.12)  # steady decline
        _bars(c, "SPY", date(2024, 1, 2), 120, 400.0, 0.5)
        eid = _event(c)
    p = create_proposal(engine, eid, research_only=True, now=datetime(2024, 3, 1, 15, tzinfo=timezone.utc))
    decide(engine, p["code"], True, by="t", now=datetime(2024, 3, 1, 16, tzinfo=timezone.utc))
    ledger.run(engine, today=date(2024, 5, 20))
    with engine.connect() as c:
        e = c.execute(text("SELECT p.* FROM paper_positions p JOIN paper_accounts a ON a.id = p.account_id "
                           "WHERE a.strategy = 'E_STOP_TIME'")).mappings().one()
    assert e["status"] == "CLOSED" and e["exit_reason"] == "STOP" and e["realized_pnl"] < 0
    assert e["exit_price"] < e["stop_price"] * 1.02


@db
@pytest.mark.usefixtures("clean", "offline_prices")
def test_proposal_expiry_and_rejection(engine):
    from app.paper.proposals import create_proposal, decide, expire
    with engine.begin() as c:
        _universe(c)
        _universe(c, cik="0001737450", ticker="OPRA")
        _bars(c, "ZBIO", date.today() - timedelta(days=90), 80)
        _bars(c, "OPRA", date.today() - timedelta(days=90), 80)
        e1 = _event(c)
        e2 = _event(c, ticker="OPRA", cik="0001737450", key="e2")
    old = datetime.now(timezone.utc) - timedelta(days=5)
    p1 = create_proposal(engine, e1, research_only=True, now=old)
    assert expire(engine) == 1
    assert decide(engine, p1["code"], True, by="t")["error"] == "ALREADY_EXPIRED"
    p2 = create_proposal(engine, e2, research_only=True)
    r = decide(engine, p2["code"], False, by="t")
    assert r["status"] == "REJECTED" and r["orders"] == []
    assert decide(engine, "PE000000", True, by="t")["error"] == "NOT_FOUND"


@db
@pytest.mark.usefixtures("clean")
def test_cached_vendor_series_is_used_before_the_live_provider(engine):
    """Provider priority: a validated series already cached (e.g. Tiingo for a delisted symbol) is
    served without calling Yahoo at all."""
    from app.marketdata.provider import ensure_prices

    class Boom:
        def get_historical_prices(self, *a):
            raise AssertionError("live provider must not be called")
    with engine.begin() as c:
        _bars(c, "OLDCO", date(2019, 1, 2), 60)
        got = ensure_prices(c, "OLDCO", date(2019, 1, 2), date(2019, 3, 15), provider=Boom())
    assert len(got) >= 50


@db
@pytest.mark.usefixtures("clean")
def test_old_classifier_rows_are_superseded_not_deleted(engine):
    from app.db.models import data_cleanup_audit, events, tenk_filings
    from app.tenk.engine import supersede_old_classifier
    with engine.begin() as c:
        c.execute(tenk_filings.insert().values(cik="0001953926", accession="0000000001-24-000001", form="10-K",
                                               filing_date=date(2024, 3, 1), source_url="u", profile={}, status="ok"))
        _event(c, et="TENK_CHANGE", key="tenk:0000000001-24-000001", payload={"direction": "NEGATIVE"})
    assert supersede_old_classifier(engine)["filings"] == 1  # dry run changes nothing
    r = supersede_old_classifier(engine, dry_run=False)
    assert r["filings"] == 1 and r["events"] == 1
    with engine.connect() as c:
        f = c.execute(select(tenk_filings)).mappings().one()
        e = c.execute(select(events)).mappings().one()
        n_audit = len(c.execute(select(data_cleanup_audit)).all())
    assert f["status"] == "superseded" and f["accession"].endswith("#v1")
    assert e["superseded"] and e["dedupe_key"].endswith("#old") and n_audit == 2
