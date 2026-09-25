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


class _Serve:
    """A provider answering from fixed series (one adjustment basis per symbol)."""

    def __init__(self, series: dict):
        self.series = series

    def get_historical_prices(self, symbol, start, end):
        return [b for b in self.series.get(symbol, []) if start <= b.date <= end]


def _serve(monkeypatch, **series):
    import app.marketdata.provider as mp
    monkeypatch.setattr(mp, "get_provider", lambda: _Serve(series))


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
def test_proposal_approval_paper_fill_exit_and_pnl(engine, monkeypatch):
    from app.db.models import paper_cash, paper_orders, paper_positions, paper_trades, proposed_trades
    from app.paper import ledger
    from app.paper.proposals import create_proposal, decide
    with engine.begin() as c:
        _universe(c)
        zb = _bars(c, "ZBIO", date(2024, 1, 2), 200, 10.0, 0.05)
        spy = _bars(c, "SPY", date(2024, 1, 2), 200, 400.0, 0.5)
        eid = _event(c)
    _serve(monkeypatch, ZBIO=zb, SPY=spy)
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
def test_stop_loss_exits_at_the_next_session(engine, monkeypatch):
    from app.paper import ledger
    from app.paper.proposals import create_proposal, decide
    with engine.begin() as c:
        _universe(c)
        zb = _bars(c, "ZBIO", date(2024, 1, 2), 120, 20.0, -0.12)  # steady decline
        spy = _bars(c, "SPY", date(2024, 1, 2), 120, 400.0, 0.5)
        eid = _event(c)
    _serve(monkeypatch, ZBIO=zb, SPY=spy)
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
    assert f["status"] == "superseded" and "#" in f["accession"]
    assert e["superseded"] and "#old" in e["dedupe_key"] and n_audit == 2


@db
@pytest.mark.usefixtures("clean")
def test_award_amount_basis_fits_the_backfill_label(engine):
    """Regression: 'current_total_at_backfill' (25 chars) overflowed varchar(24) and every historical
    USAspending insert failed silently in the backfill."""
    from app.db.models import government_awards
    cols = {c.name: c for c in government_awards.columns}
    assert cols["amount_basis"].type.length >= len("current_total_at_backfill")


def test_oge_pdf_extraction_runs_isolated_with_a_memory_cap():
    from pathlib import Path

    from app.sources.trump import oge
    pdf = (Path(__file__).parent / "fixtures" / "house_ptr_20034188.pdf").read_bytes()
    txt, n = oge.pdf_text(pdf)
    assert n >= 1 and len(txt) > 50
    with pytest.raises(RuntimeError):
        oge.pdf_text(b"not a pdf at all")  # a failure is an exception for this document, never a crash of the caller


# ------------------------------------------------------------------ review fixes (Codex, 2026-09-25)

class _SplitAfterEntry:
    """A fresh provider series AFTER a 2:1 split (2024-04-15) and a 2% dividend: every price is on the
    post-split basis (the entry day's 100 now reads 50) and pre-dividend adj_close is 2% lower."""

    def get_historical_prices(self, symbol, start, end):
        out, d = [], start
        while d <= end:
            if d.weekday() < 5:
                adj = 49.0 if d < date(2024, 4, 15) else 50.0
                out.append(Bar(d, 50.0, 50.5, 49.5, 50.0, adj, 100000))
            d += timedelta(days=1)
        return out


@db
@pytest.mark.usefixtures("clean", "offline_prices")
def test_paper_valuation_stop_and_sale_survive_a_split_after_entry(engine, monkeypatch):
    import app.marketdata.provider as mp
    from app.paper import ledger
    from app.paper.proposals import create_proposal, decide
    with engine.begin() as c:
        _universe(c)
        _bars(c, "ZBIO", date(2024, 1, 2), 200, 100.0, 0.0)  # what the cache held at entry time
        _bars(c, "SPY", date(2024, 1, 2), 200, 400.0, 0.5)
        eid = _event(c)
    p = create_proposal(engine, eid, research_only=True, now=datetime(2024, 3, 1, 15, tzinfo=timezone.utc))
    decide(engine, p["code"], True, by="t", now=datetime(2024, 3, 1, 16, tzinfo=timezone.utc))
    with engine.begin() as c:
        assert ledger.fill_orders(c, date(2024, 3, 10))["filled"] == 5  # bought at 100 (pre-split basis)
    monkeypatch.setattr(mp, "get_provider", lambda: _SplitAfterEntry())
    ledger.run(engine, today=date(2024, 5, 10))
    with engine.begin() as c:
        rows = {r["strategy"]: dict(r) for r in c.execute(text(
            "SELECT a.strategy, p.* FROM paper_positions p JOIN paper_accounts a ON a.id = p.account_id")).mappings()}
        e = rows["E_STOP_TIME"]
        assert e["status"] == "OPEN"  # a naive fixed factor would read 50 vs entry 100 and trigger the 15% stop
        value, px, _ = ledger.position_value(c, e, date(2024, 5, 10))
        assert px == pytest.approx(100 * 50 / 49, rel=1e-6)  # dividend counted as reinvested, split neutral
        a = rows["A_30D"]  # sold at the first open on/after 2024-04-03, still before the split
        assert a["status"] == "CLOSED" and a["exit_price"] == pytest.approx(100 * (1 - 0.002), rel=1e-6)
        assert a["realized_pnl"] == pytest.approx(a["shares"] * a["exit_price"] - 1.0 - a["cost"])
        nav = c.execute(text("SELECT nav FROM paper_performance p JOIN paper_accounts a ON a.id = p.account_id "
                             "WHERE a.strategy = 'B_90D'")).scalar()
        assert nav > 10_000 - 5  # no phantom 50% loss


@db
@pytest.mark.usefixtures("clean", "offline_prices")
def test_tenk_transient_fetch_failure_is_retried_and_the_successor_recompared(engine, monkeypatch):
    from app.db.models import events, tenk_filings
    from app.tenk import engine as te
    docs = {"b1": _doc([_risk(i) for i in range(20)], BASE_MDNA),
            "b2": _doc([_risk(i) for i in range(20)], BASE_MDNA + [
                "There is substantial doubt about our ability to continue as a going concern within one year after issuance. ",
                "Our independent registered public accounting firm has expressed substantial doubt about our ability to "
                "continue as a going concern. "])}
    flaky = {"b1": 1}

    def fetch(cik, f):
        if flaky.get(f["accession"]):
            flaky[f["accession"]] -= 1
            raise te.FetchError("HTTP 503")
        return docs[f["accession"]]
    monkeypatch.setattr(te, "fetch_text", fetch)
    f1 = {"accession": "b1", "form": "10-K", "filing_date": date(2023, 3, 1), "accepted_at": te.acceptance_to_utc("2023-03-01T16:30:00Z"),
          "primary_doc": "k.htm", "period": None}
    f2 = {**f1, "accession": "b2", "filing_date": date(2024, 3, 1), "accepted_at": te.acceptance_to_utc("2024-03-01T16:30:00Z")}
    assert te.process_filing(engine, "0001953926", "ZBIO", "Z", f1, is_backfill=True)["retryable"] is True
    assert te.process_filing(engine, "0001953926", "ZBIO", "Z", f2, is_backfill=True)["status"] == "first"
    r = te.process_filing(engine, "0001953926", "ZBIO", "Z", f1, is_backfill=True)  # retry succeeds
    assert r["status"] == "first" and r["successor"] == "event"
    with engine.connect() as c:
        st = dict(c.execute(select(tenk_filings.c.accession, tenk_filings.c.status)).all())
        b2_prev = c.execute(select(tenk_filings.c.prev_accession).where(tenk_filings.c.accession == "b2")).scalar()
        ev = c.execute(select(events.c.payload).where(events.c.event_type == "TENK_CHANGE")).scalar_one()
    assert st == {"b1": "ok", "b2": "ok"} and b2_prev == "b1" and ev["prev_accession"] == "b1"
    assert te.process_filing(engine, "0001953926", "ZBIO", "Z", f1, is_backfill=True) == {"skipped": "known"}


@db
@pytest.mark.usefixtures("clean")
def test_tenk_backfill_keeps_a_quarter_open_until_a_failed_company_succeeds(engine, monkeypatch):
    import app.securitymaster.provider as smp
    from app.db.store import job_state
    from app.tenk import engine as te

    class SM:
        def resolve_ticker_at_date(self, cik, d):
            return "ZBIO"
    monkeypatch.setattr(smp, "get_security_master", lambda conn: SM())
    monkeypatch.setattr(te, "_quarters", lambda years: ["2024Q1"])
    monkeypatch.setattr(te, "quarter_10k_ciks", lambda y, q: [("0001953926", date(2024, 3, 1))])
    calls = []

    def proc(engine_, cik, *a, **k):
        calls.append(cik)
        return {"error": "HTTP 503"} if len(calls) == 1 else {"docs": 1, "events": 0, "failed": 0}
    monkeypatch.setattr(te, "process_cik", proc)
    r1 = te.run_backfill(engine)
    with engine.connect() as c:
        st = job_state(c, "tenk_backfill")
    assert r1["failed"] == 1 and "2024Q1" not in st["done_quarters"] and "0001953926" not in st["done_ciks"]
    te.run_backfill(engine)  # e.g. after a restart
    with engine.connect() as c:
        st = job_state(c, "tenk_backfill")
    assert calls == ["0001953926", "0001953926"] and "2024Q1" in st["done_quarters"] and st["fail_counts"] == {}


def test_tenk_history_reads_archived_submissions_files():
    from app.tenk.engine import all_annual_filings
    sub = {"filings": {"recent": {"form": ["10-K", "8-K"], "accessionNumber": ["r25", "x"],
                                  "filingDate": ["2025-03-01", "2025-02-01"], "acceptanceDateTime": ["", ""],
                                  "primaryDocument": ["a.htm", "b.htm"], "reportDate": ["", ""]},
                       "files": [{"name": "CIK0000000001-submissions-001.json", "filingFrom": "2015-01-01", "filingTo": "2024-12-31"},
                                 {"name": "CIK0000000001-submissions-002.json", "filingFrom": "2001-01-01", "filingTo": "2010-12-31"}]}}
    fetched = []

    def getter(url):
        fetched.append(url)
        return {"form": ["10-K", "10-K", "10-Q"], "accessionNumber": ["a23", "a24", "q"],
                "filingDate": ["2023-03-01", "2024-03-01", "2024-05-01"], "acceptanceDateTime": ["", "", ""],
                "primaryDocument": ["k23.htm", "k24.htm", "q.htm"], "reportDate": ["", "", ""]}
    fl = all_annual_filings(sub, date(2020, 1, 1), getter)
    assert [f["accession"] for f in fl] == ["a23", "a24", "r25"]
    assert len(fetched) == 1 and fetched[0].endswith("submissions-001.json")  # the 2001-2010 archive is out of range


@db
@pytest.mark.usefixtures("clean", "offline_prices")
def test_tenk_successor_can_be_rebased_twice(engine, monkeypatch):
    """Two late predecessors for the same successor (e.g. a 10-KT then a 10-K) must not collide."""
    from app.db.models import events, tenk_filings
    from app.tenk import engine as te
    base = _doc([_risk(i) for i in range(20)], BASE_MDNA)
    gc = _doc([_risk(i) for i in range(20)], BASE_MDNA + [
        "There is substantial doubt about our ability to continue as a going concern within one year after issuance. ",
        "Our independent registered public accounting firm has expressed substantial doubt about our ability to "
        "continue as a going concern. "])
    docs = {"c0": base, "c1": base, "c2": base, "c3": gc}
    monkeypatch.setattr(te, "fetch_text", lambda cik, f: docs[f["accession"]])

    def f(acc, d):
        return {"accession": acc, "form": "10-K", "filing_date": d, "accepted_at": None, "primary_doc": "k.htm", "period": None}
    te.process_filing(engine, "0001953926", "ZBIO", "Z", f("c0", date(2022, 3, 1)), is_backfill=True)
    te.process_filing(engine, "0001953926", "ZBIO", "Z", f("c3", date(2023, 6, 1)), is_backfill=True)  # compared with c0
    te.process_filing(engine, "0001953926", "ZBIO", "Z", f("c1", date(2022, 9, 1)), is_backfill=True)  # rebase #1
    te.process_filing(engine, "0001953926", "ZBIO", "Z", f("c2", date(2023, 1, 1)), is_backfill=True)  # rebase #2
    with engine.connect() as c:
        live = c.execute(select(tenk_filings.c.accession, tenk_filings.c.prev_accession).where(tenk_filings.c.status == "ok")).all()
        n_sup = c.execute(text("SELECT count(*) FROM tenk_filings WHERE status = 'superseded'")).scalar()
        evs = c.execute(select(events.c.payload).where(events.c.superseded.is_(False))).scalars().all()
    assert dict(live)["c3"] == "c2" and n_sup == 2
    assert len(evs) == 1 and evs[0]["prev_accession"] == "c2"



# ------------------------------------------------------------------ review fixes, round 2

@db
@pytest.mark.usefixtures("clean", "offline_prices")
def test_no_mixed_basis_cache_fallback_when_the_provider_fails(engine, monkeypatch):
    """Provider down + a cache that mixes bases (entry 100 pre-split, recent rows 50 post-split): the last
    trusted valuation is kept and no stop / sale is decided from the mixed cache."""
    import app.marketdata.provider as mp
    from app.marketdata.provider import store_bars
    from app.paper import ledger
    from app.paper.proposals import create_proposal, decide
    with engine.begin() as c:
        _universe(c)
        _bars(c, "ZBIO", date(2024, 1, 2), 200, 100.0, 0.0)
        _bars(c, "SPY", date(2024, 1, 2), 200, 400.0, 0.5)
        eid = _event(c)
    p = create_proposal(engine, eid, research_only=True, now=datetime(2024, 3, 1, 15, tzinfo=timezone.utc))
    decide(engine, p["code"], True, by="t", now=datetime(2024, 3, 1, 16, tzinfo=timezone.utc))
    with engine.begin() as c:
        ledger.fill_orders(c, date(2024, 3, 10))
        # later rows re-fetched after a 2:1 split, older rows untouched: a mixed-basis cache
        d, post = date(2024, 4, 15), []
        while d <= date(2024, 6, 20):
            if d.weekday() < 5:
                post.append(Bar(d, 50.0, 50.0, 50.0, 50.0, 50.0, 1))
            d += timedelta(days=1)
        store_bars(c, "ZBIO", post)

    class Down:
        def get_historical_prices(self, *a):
            raise RuntimeError("provider down")
    monkeypatch.setattr(mp, "get_provider", lambda: Down())
    r = ledger.run(engine, today=date(2024, 6, 20))
    assert r["exits"].get("deferred") == 5
    with engine.connect() as c:
        pos = c.execute(text("SELECT p.*, a.strategy FROM paper_positions p JOIN paper_accounts a ON a.id = p.account_id")).mappings().all()
        assert all(p["status"] == "OPEN" for p in pos)  # neither the 15% stop nor a sale at a mixed price
        e = dict(next(p for p in pos if p["strategy"] == "E_STOP_TIME"))
        value, px, _ = ledger.position_value(c, e, date(2024, 6, 20))
        assert px == pytest.approx(e["last_price"]) and px > 99  # last trusted valuation, not 50


@db
@pytest.mark.usefixtures("clean", "offline_prices")
def test_superseded_risk_event_does_not_trigger_a_thesis_exit(engine, monkeypatch):
    from app.db.store import create_event
    from app.paper import ledger
    from app.paper.proposals import create_proposal, decide
    with engine.begin() as c:
        _universe(c)
        zb = _bars(c, "ZBIO", date(2024, 1, 2), 200, 10.0, 0.01)
        spy = _bars(c, "SPY", date(2024, 1, 2), 200, 400.0, 0.5)
        eid = _event(c)
    _serve(monkeypatch, ZBIO=zb, SPY=spy)
    p = create_proposal(engine, eid, research_only=True, now=datetime(2024, 3, 1, 15, tzinfo=timezone.utc))
    decide(engine, p["code"], True, by="t", now=datetime(2024, 3, 1, 16, tzinfo=timezone.utc))
    with engine.begin() as c:
        ledger.fill_orders(c, date(2024, 3, 10))
        old = create_event(c, event_type="TENK_CHANGE", dedupe_key="tenk:x#rp1", public_date=date(2024, 4, 1), ticker="ZBIO",
                           issuer_cik="0001953926", title="t", source_url="u",
                           payload={"change_types": ["NEW_GOING_CONCERN"], "direction": "NEGATIVE"})
        c.execute(text("UPDATE events SET superseded = true, superseded_reason = 'TENK_PREDECESSOR_ARRIVED' WHERE id = :i"), {"i": old})
        create_event(c, event_type="TENK_CHANGE", dedupe_key="tenk:x", public_date=date(2024, 4, 1), ticker="ZBIO",
                     issuer_cik="0001953926", title="t", source_url="u",
                     payload={"change_types": ["NEW_LEGAL_RISK"], "direction": "NEGATIVE"})
        pos = dict(c.execute(text("SELECT p.* FROM paper_positions p JOIN paper_accounts a ON a.id = p.account_id "
                                  "WHERE a.strategy = 'D_THESIS'")).mappings().one())
        assert ledger.thesis_invalidated(c, pos, date(2024, 5, 1)) is None
        ledger.check_exits(c, date(2024, 5, 1))
        n_sell = c.execute(text("SELECT count(*) FROM paper_orders WHERE side = 'SELL' AND position_id = :i"), {"i": pos["id"]}).scalar()
    assert n_sell == 0


def _all_days(y, q):
    d, out = date(y, 3 * q - 2, 1), set()
    while d.month in range(3 * q - 2, 3 * q + 1) and d.year == y:
        out.add(f"form.{d:%Y%m%d}.idx")
        d += timedelta(days=1)
    return out


def _sub(filings: list[tuple[str, str]]) -> dict:
    return {"name": "Z", "filings": {"recent": {
        "form": ["10-K"] * len(filings), "accessionNumber": [a for a, _ in filings], "filingDate": [d for _, d in filings],
        "acceptanceDateTime": [""] * len(filings), "primaryDocument": ["k.htm"] * len(filings), "reportDate": [""] * len(filings)}}}


@db
@pytest.mark.usefixtures("clean", "offline_prices")
def test_tenk_document_cap_leaves_the_company_open_until_all_reports_are_done(engine, monkeypatch):
    import app.securitymaster.provider as smp
    from app.db.models import tenk_filings
    from app.db.store import job_state
    from app.tenk import engine as te

    class SM:
        def resolve_ticker_at_date(self, cik, d):
            return "ZBIO"
    monkeypatch.setattr(smp, "get_security_master", lambda conn: SM())
    years = [2021, 2022, 2023, 2024, 2025]
    monkeypatch.setattr(te, "fetch_submissions", lambda cik: _sub([(f"d{y}", f"{y}-03-01") for y in years]))
    monkeypatch.setattr(te, "fetch_text", lambda cik, f: _doc([_risk(i) for i in range(20)], BASE_MDNA))
    monkeypatch.setattr(te, "_quarters", lambda y: ["2025Q1"])
    monkeypatch.setattr(te, "quarter_10k_ciks", lambda y, q: [("0001953926", date(2025, 3, 1))])
    real = te.process_cik
    monkeypatch.setattr(te, "process_cik", lambda *a, **k: real(*a, **{**k, "max_docs": 2}))  # cap below the backlog
    te.run_backfill(engine, years=6)
    with engine.connect() as c:
        st = job_state(c, "tenk_backfill")
        n = len(c.execute(select(tenk_filings.c.id)).all())
    assert n == 2 and "0001953926" not in st["done_ciks"] and "2025Q1" not in st["done_quarters"]
    for _ in range(3):  # restarts
        te.run_backfill(engine, years=6)
    with engine.connect() as c:
        st = job_state(c, "tenk_backfill")
        rows = dict(c.execute(select(tenk_filings.c.accession, tenk_filings.c.prev_accession)).all())
    assert set(rows) == {f"d{y}" for y in years} and "0001953926" in st["done_ciks"]
    assert rows["d2025"] == "d2024" and rows["d2022"] == "d2021"  # oldest first: every report has its predecessor


@db
@pytest.mark.usefixtures("clean", "offline_prices")
def test_live_retry_queue_recovers_a_failed_filing_after_the_index_window(engine, monkeypatch):
    from app.db.models import tenk_filings
    from app.tenk import engine as te
    old = (date.today() - timedelta(days=30)).isoformat()
    monkeypatch.setattr(te, "fetch_submissions", lambda cik: _sub([("f1", old)]))
    state = {"down": True}

    def fetch(cik, f):
        if state["down"]:
            raise te.FetchError("HTTP 503")
        return _doc([_risk(i) for i in range(20)], BASE_MDNA)
    monkeypatch.setattr(te, "fetch_text", fetch)
    monkeypatch.setattr(te, "_universe", lambda conn: {})
    monkeypatch.setattr(te, "daily_10k_ciks", lambda d: set())  # the filing is long out of the 4-day window
    monkeypatch.setattr(te, "quarter_index_listing", _all_days)
    r = te.process_cik(engine, "0001953926", "ZBIO", "Z", history_from=date.today() - timedelta(days=400),
                       live_from=date.today(), max_docs=5)
    assert r["failed"] == 1
    out = te.run_live(engine)  # still down: visible, not lost
    assert out["retry_queue"]["queued"] == 1 and out["retry_queue"]["still_failed"] == 1
    state["down"] = False  # e.g. after a restart
    out = te.run_live(engine)
    assert out["retry_queue"]["recovered"] == 1 and out["retry_queue"]["remaining"] == 0
    with engine.connect() as c:
        assert c.execute(select(tenk_filings.c.status).where(tenk_filings.c.accession == "f1")).scalar() == "ok"


# ------------------------------------------------------------------ review fixes, round 3

@db
@pytest.mark.usefixtures("clean", "offline_prices")
def test_live_queue_survives_submissions_error_and_budget_after_the_window(engine, monkeypatch):
    from app.db.models import tenk_filings
    from app.db.store import job_state
    from app.tenk import engine as te
    recent = (date.today() - timedelta(days=1)).isoformat()
    monkeypatch.setattr(te, "_universe", lambda conn: {"0001953926": {"ticker": "ZBIO"}, "0001737450": {"ticker": "OPRA"}})
    window = {"on": True}
    monkeypatch.setattr(te, "daily_10k_ciks", lambda d: {"0001953926", "0001737450"} if window["on"] else set())
    sec_down = {"on": True}

    def subs(cik):
        if sec_down["on"] and cik == "0001953926":
            raise RuntimeError("HTTP 503 submissions")
        return _sub([(f"{cik}-k", recent)])
    monkeypatch.setattr(te, "fetch_submissions", subs)
    monkeypatch.setattr(te, "fetch_text", lambda cik, f: _doc([_risk(i) for i in range(20)], BASE_MDNA))
    monkeypatch.setattr(te, "retry_failed", lambda engine_, uni=None: {})
    monkeypatch.setattr(te, "quarter_index_listing", _all_days)
    out = te.run_live(engine, max_docs=10)  # ZBIO: submissions error; OPRA: stored
    assert out["errors"] == 1 and out["queue_after"] == 1
    window["on"] = False  # the 4-day index window moved on, process restarted
    out = te.run_live(engine, max_docs=10)
    assert out["queue_after"] == 1  # still failing -> still queued, never dropped
    sec_down["on"] = False
    out = te.run_live(engine, max_docs=10)
    with engine.connect() as c:
        accs = {a for (a,) in c.execute(select(tenk_filings.c.accession)).all()}
        st = job_state(c, "tenk_live_queue")
    assert accs == {"0001953926-k", "0001737450-k"} and st["queue"] == {} and out["queue_after"] == 0


@db
@pytest.mark.usefixtures("clean", "offline_prices")
def test_live_queue_keeps_companies_beyond_the_document_budget(engine, monkeypatch):
    from app.tenk import engine as te
    recent = (date.today() - timedelta(days=1)).isoformat()
    ciks = {f"{i:010d}" for i in range(1, 4)}
    monkeypatch.setattr(te, "_universe", lambda conn: {c: {"ticker": None} for c in ciks})
    seen = {"on": True}
    monkeypatch.setattr(te, "daily_10k_ciks", lambda d: set(ciks) if seen["on"] else set())
    monkeypatch.setattr(te, "fetch_submissions", lambda cik: _sub([(f"{cik}-k", recent)]))
    monkeypatch.setattr(te, "fetch_text", lambda cik, f: _doc([_risk(i) for i in range(20)], BASE_MDNA))
    monkeypatch.setattr(te, "retry_failed", lambda engine_, uni=None: {})
    monkeypatch.setattr(te, "quarter_index_listing", _all_days)
    out = te.run_live(engine, max_docs=1)
    assert out["docs"] == 1 and out["queue_after"] == 2 and out["budget_left_in_queue"] == 2
    seen["on"] = False
    te.run_live(engine, max_docs=1)
    out = te.run_live(engine, max_docs=1)
    assert out["queue_after"] == 0


@db
@pytest.mark.usefixtures("clean")
def test_unlisted_in_a_recent_quarter_is_still_processed_where_it_was_listed(engine, monkeypatch):
    import app.securitymaster.provider as smp
    from app.db.store import job_state
    from app.tenk import engine as te

    class SM:  # delisted in 2023, kept filing 10-Ks
        def resolve_ticker_at_date(self, cik, d):
            return "OLDC" if d < date(2023, 1, 1) else None
    monkeypatch.setattr(smp, "get_security_master", lambda conn: SM())
    monkeypatch.setattr(te, "_quarters", lambda y: ["2025Q1", "2021Q1"])
    monkeypatch.setattr(te, "quarter_10k_ciks",
                        lambda y, q: [("0000000777", date(2025, 3, 1) if y == 2025 else date(2021, 3, 1))])
    calls = []
    monkeypatch.setattr(te, "process_cik", lambda engine_, cik, *a, **k: calls.append(cik) or {"docs": 3, "events": 0, "failed": 0,
                                                                                               "remaining": 0})
    out = te.run_backfill(engine)
    with engine.connect() as c:
        st = job_state(c, "tenk_backfill")
    assert out["not_listed"] == 1 and calls == ["0000000777"]  # history processed via the listed 2021 quarter
    assert "0000000777" in st["done_ciks"] and set(st["done_quarters"]) == {"2025Q1", "2021Q1"}


# ------------------------------------------------------------------ review fixes, round 4

def test_missing_section_in_the_new_report_is_not_a_removal():
    from app.tenk.diff import build_profile, compare, extract_items
    gc = ["There is substantial doubt about our ability to continue as a going concern within one year after issuance. ",
          "Our independent registered public accounting firm has expressed substantial doubt about our ability to "
          "continue as a going concern. "]
    old = build_profile(extract_items(_doc([_risk(i) for i in range(20)], BASE_MDNA + gc)))
    new_items = extract_items(_doc([_risk(i) for i in range(20)], BASE_MDNA))
    new_items.pop("MD&A")  # the parser could not find MD&A this year
    assert "MD&A" in old and not any(c.change_type == "REMOVED_GOING_CONCERN" for c in compare(old, build_profile(new_items)))


@db
@pytest.mark.usefixtures("clean", "offline_prices")
def test_missing_section_creates_no_positive_event_or_proposal(engine, monkeypatch):
    from app.db.models import events, proposed_trades
    from app.paper.proposals import scan_new_events
    from app.tenk import engine as te
    gc = ["There is substantial doubt about our ability to continue as a going concern within one year after issuance. ",
          "Our independent registered public accounting firm has expressed substantial doubt about our ability to "
          "continue as a going concern. "]
    old_doc = _doc([_risk(i) for i in range(20)], BASE_MDNA + gc)
    new_doc = old_doc.split("Item 7. Management's Discussion and Analysis\n")[0] + \
        "Item 7. Management's Discussion\n(incorporated by reference)\nItem 8. Financial Statements\n" + FILLER * 3
    docs = {"m1": old_doc, "m2": new_doc}
    monkeypatch.setattr(te, "fetch_text", lambda cik, f: docs[f["accession"]])
    today = date.today()
    f1 = {"accession": "m1", "form": "10-K", "filing_date": today - timedelta(days=366), "accepted_at": None, "primary_doc": "k.htm",
          "period": None}
    f2 = {**f1, "accession": "m2", "filing_date": today - timedelta(days=1)}
    te.process_filing(engine, "0001953926", "ZBIO", "Z", f1, is_backfill=True)
    r = te.process_filing(engine, "0001953926", "ZBIO", "Z", f2, is_backfill=False)
    assert r["status"] in ("no_material_change", "first") or r.get("direction") != "POSITIVE"
    monkeypatch.setenv("PAPER_RESEARCH_MODE", "1")
    with engine.begin() as c:
        _universe(c)
    scan_new_events(engine, days=5)
    with engine.connect() as c:
        pos = c.execute(select(events.c.id).where(events.c.payload["direction"].astext == "POSITIVE")).all()
        props = c.execute(select(proposed_trades.c.id)).all()
    assert pos == [] and props == []


@db
@pytest.mark.usefixtures("clean", "offline_prices")
def test_stop_breach_on_the_entry_day_sells_at_the_next_open(engine, monkeypatch):
    from app.paper import ledger
    from app.paper.proposals import create_proposal, decide
    with engine.begin() as c:
        _universe(c)
        zb = _bars(c, "ZBIO", date(2024, 1, 2), 120, 100.0, 0.0)
        spy = _bars(c, "SPY", date(2024, 1, 2), 120, 400.0, 0.5)
        eid = _event(c)
    # entry day 2024-03-04: opens at 100, closes at 80 (below the 15% stop), recovers to 100 afterwards
    zb = [Bar(b.date, b.open, b.high, b.low, 80.0, 80.0, b.volume) if b.date == date(2024, 3, 4) else b for b in zb]
    _serve(monkeypatch, ZBIO=zb, SPY=spy)
    p = create_proposal(engine, eid, research_only=True, now=datetime(2024, 3, 1, 15, tzinfo=timezone.utc))
    decide(engine, p["code"], True, by="t", now=datetime(2024, 3, 1, 16, tzinfo=timezone.utc))
    with engine.begin() as c:
        ledger.fill_orders(c, date(2024, 3, 4))
        ledger.check_exits(c, date(2024, 3, 8))
        o = c.execute(text("SELECT o.not_before, o.note FROM paper_orders o JOIN paper_accounts a ON a.id = o.account_id "
                           "WHERE o.side = 'SELL' AND a.strategy = 'E_STOP_TIME'")).mappings().one()
    assert o["note"] == "STOP" and o["not_before"] == date(2024, 3, 5)


@db
@pytest.mark.usefixtures("clean", "offline_prices")
def test_failed_daily_index_is_retried_after_the_window(engine, monkeypatch):
    from app.db.models import tenk_filings
    from app.db.store import job_state
    from app.tenk import engine as te
    day = date(2026, 9, 23)  # Wednesday
    monkeypatch.setattr(te, "_universe", lambda conn: {"0001953926": {"ticker": "ZBIO"}})
    sec = {"down": True}

    def idx(d):
        if sec["down"]:
            raise RuntimeError("HTTP 403")
        return {"0001953926"} if d == day else set()
    monkeypatch.setattr(te, "daily_10k_ciks", idx)
    monkeypatch.setattr(te, "fetch_submissions", lambda cik: _sub([("w1", day.isoformat())]))
    monkeypatch.setattr(te, "fetch_text", lambda cik, f: _doc([_risk(i) for i in range(20)], BASE_MDNA))
    monkeypatch.setattr(te, "retry_failed", lambda engine_, uni=None: {})
    monkeypatch.setattr(te, "quarter_index_listing", _all_days)
    out = te.run_live(engine, today=date(2026, 9, 24))
    assert day.isoformat() in out["index_failed"]
    out = te.run_live(engine, today=date(2026, 10, 5))  # outage outlasted the 4-day window
    with engine.connect() as c:
        assert day.isoformat() in job_state(c, "tenk_live_queue")["index_pending"]
    sec["down"] = False  # restart after the outage
    out = te.run_live(engine, today=date(2026, 10, 6))
    with engine.connect() as c:
        st = job_state(c, "tenk_live_queue")
        got = c.execute(select(tenk_filings.c.accession)).scalars().all()
    assert got == ["w1"] and st["index_pending"] == [] and out["index_failed"] == []



@db
@pytest.mark.usefixtures("clean", "offline_prices")
def test_unpublished_today_and_holidays_are_not_index_failures(engine, monkeypatch):
    from app.db.store import job_state
    from app.tenk import engine as te
    monkeypatch.setattr(te, "_universe", lambda conn: {})
    monkeypatch.setattr(te, "retry_failed", lambda engine_, uni=None: {})
    called = []
    monkeypatch.setattr(te, "daily_10k_ciks", lambda d: called.append(d) or set())
    # 07 Sep = Labor Day (no index ever); 09 Sep = today (not published yet); the server clock is UTC,
    # so "yesterday" may still be the US business day in progress and also waits one run
    published = {"form.20260902.idx", "form.20260903.idx", "form.20260904.idx", "form.20260908.idx"}
    out = te.run_live(engine, today=date(2026, 9, 9), days=8, listing_fn=lambda y, q: published)
    assert out["index_failed"] == [] and out["index_not_published_yet"] == ["2026-09-09"]
    with engine.connect() as c:
        assert job_state(c, "tenk_live_queue")["index_pending"] == ["2026-09-09"]  # the holiday is not retried
    assert date(2026, 9, 7) not in called and date(2026, 9, 8) in called

    def down(y, q):
        raise RuntimeError("HTTP 503")
    out = te.run_live(engine, today=date(2026, 9, 10), days=2, listing_fn=down)
    assert "2026-09-09" in out["index_failed"]  # a listing failure is a real, visible failure


# ------------------------------------------------------------------ review fixes, round 5

GC_SENTENCES = ["There is substantial doubt about our ability to continue as a going concern within one year after issuance. ",
                "Our independent registered public accounting firm has expressed substantial doubt about our ability to "
                "continue as a going concern. "]


def _moved_warning_docs():
    """Old report: warning in MD&A. New report: MD&A unreadable, the same warning now in Item 1A."""
    old = _doc([_risk(i) for i in range(20)], BASE_MDNA + GC_SENTENCES)
    new = _doc([_risk(i) for i in range(20)] + GC_SENTENCES, BASE_MDNA)
    new = new.split("Item 7. Management's Discussion and Analysis\n")[0] + \
        "Item 7. Management's Discussion\n(see exhibit)\nItem 8. Financial Statements\n" + FILLER * 3
    return old, new


def test_going_concern_moved_to_another_section_is_not_removed():
    from app.tenk.diff import build_profile, compare, extract_items
    old_doc, new_doc = _moved_warning_docs()
    new_items = extract_items(new_doc)
    assert "MD&A" not in new_items and "RISK_FACTORS" in new_items
    types = {c.change_type for c in compare(build_profile(extract_items(old_doc)), build_profile(new_items), new_items)}
    assert "REMOVED_GOING_CONCERN" not in types and "NEW_GOING_CONCERN" not in types


@db
@pytest.mark.usefixtures("clean", "offline_prices")
def test_moved_warning_creates_no_positive_event_or_proposal(engine, monkeypatch):
    from app.db.models import events, proposed_trades
    from app.paper.proposals import scan_new_events
    from app.tenk import engine as te
    old_doc, new_doc = _moved_warning_docs()
    docs = {"v1": old_doc, "v2": new_doc}
    monkeypatch.setattr(te, "fetch_text", lambda cik, f: docs[f["accession"]])
    f1 = {"accession": "v1", "form": "10-K", "filing_date": date.today() - timedelta(days=366), "accepted_at": None,
          "primary_doc": "k.htm", "period": None}
    te.process_filing(engine, "0001953926", "ZBIO", "Z", f1, is_backfill=True)
    te.process_filing(engine, "0001953926", "ZBIO", "Z", {**f1, "accession": "v2", "filing_date": date.today() - timedelta(days=1)},
                      is_backfill=False)
    monkeypatch.setenv("PAPER_RESEARCH_MODE", "1")
    with engine.begin() as c:
        _universe(c)
    scan_new_events(engine, days=5)
    with engine.connect() as c:
        assert c.execute(select(events.c.id).where(events.c.payload["direction"].astext == "POSITIVE")).all() == []
        assert c.execute(select(proposed_trades.c.id)).all() == []


def _approved_setup(engine, monkeypatch, approve=True):
    from app.paper.proposals import create_proposal, decide
    with engine.begin() as c:
        _universe(c)
        zb = _bars(c, "ZBIO", date(2024, 1, 2), 120, 10.0, 0.01)
        spy = _bars(c, "SPY", date(2024, 1, 2), 120, 400.0, 0.5)
        eid = _event(c)
    _serve(monkeypatch, ZBIO=zb, SPY=spy)
    p = create_proposal(engine, eid, research_only=True, now=datetime(2024, 3, 1, 15, tzinfo=timezone.utc))
    r = decide(engine, p["code"], True, by="t", now=datetime(2024, 3, 1, 16, tzinfo=timezone.utc)) if approve else None
    return eid, p, r


@db
@pytest.mark.usefixtures("clean", "offline_prices")
def test_superseded_source_blocks_approval(engine, monkeypatch):
    from app.db.models import paper_orders, proposed_trades
    from app.paper.proposals import decide
    eid, p, _ = _approved_setup(engine, monkeypatch, approve=False)
    with engine.begin() as c:
        c.execute(text("UPDATE events SET superseded = true WHERE id = :i"), {"i": eid})
    r = decide(engine, p["code"], True, by="t", now=datetime(2024, 3, 1, 16, tzinfo=timezone.utc))
    assert r == {"ok": False, "error": "SOURCE_SUPERSEDED"}
    with engine.connect() as c:
        assert c.execute(select(paper_orders.c.id)).all() == []
        st = c.execute(select(proposed_trades.c.approval_status, proposed_trades.c.decided_by)).one()
    assert st == ("EXPIRED", "system:source_superseded")


@db
@pytest.mark.usefixtures("clean", "offline_prices")
def test_superseded_source_cancels_unfilled_buys(engine, monkeypatch):
    from app.db.models import paper_orders, paper_positions
    from app.paper import ledger
    from app.paper.proposals import create_proposal, expire
    eid, p, r = _approved_setup(engine, monkeypatch)
    assert r["ok"] and len(r["orders"]) == 5
    with engine.begin() as c:
        c.execute(text("UPDATE events SET superseded = true WHERE id = :i"), {"i": eid})
        assert ledger.fill_orders(c, date(2024, 3, 10))["cancelled"] == 5  # guard at fill time
        assert c.execute(select(paper_positions.c.id)).all() == []
        assert {n for (n,) in c.execute(select(paper_orders.c.note)).all()} == {"source event superseded"}
    # and the periodic sweep catches a PENDING proposal of a superseded event before anyone approves it
    with engine.begin() as c:
        e2 = _event(c, ticker="OPRA", cik="0001737450", key="e-opra")
        _universe(c, cik="0001737450", ticker="OPRA")
        _bars(c, "OPRA", date.today() - timedelta(days=90), 80)
    p2 = create_proposal(engine, e2, research_only=True)
    assert p2["created"]
    with engine.begin() as c:
        c.execute(text("UPDATE events SET superseded = true WHERE id = :i"), {"i": e2})
    assert expire(engine) == 1


# ------------------------------------------------------------------ review fixes, round 6

NEG_LIQUIDITY = ["Our liquidity is insufficient to fund our operations for the next twelve months. ",
                 "Our liquidity is not sufficient to fund our operations for the next twelve months. "]


@pytest.mark.parametrize("sentence", NEG_LIQUIDITY)
def test_negated_sufficiency_is_never_a_liquidity_improvement(sentence):
    from app.tenk.diff import build_profile, compare, extract_items
    base = [BASE_MDNA[0], BASE_MDNA[2]]  # no "sufficient" sentence in the old report
    old = build_profile(extract_items(_doc([_risk(i) for i in range(20)], base)))
    items = extract_items(_doc([_risk(i) for i in range(20)], base + [sentence]))
    assert "LIQUIDITY_IMPROVEMENT" not in {c.change_type for c in compare(old, build_profile(items), items)}


@db
@pytest.mark.parametrize("sentence", NEG_LIQUIDITY)
@pytest.mark.usefixtures("clean", "offline_prices")
def test_negated_sufficiency_creates_no_positive_event_or_proposal(engine, monkeypatch, sentence):
    from app.db.models import events, proposed_trades
    from app.paper.proposals import scan_new_events
    from app.tenk import engine as te
    base = [BASE_MDNA[0], BASE_MDNA[2]]
    docs = {"n1": _doc([_risk(i) for i in range(20)], base), "n2": _doc([_risk(i) for i in range(20)], base + [sentence])}
    monkeypatch.setattr(te, "fetch_text", lambda cik, f: docs[f["accession"]])
    f1 = {"accession": "n1", "form": "10-K", "filing_date": date.today() - timedelta(days=366), "accepted_at": None,
          "primary_doc": "k.htm", "period": None}
    te.process_filing(engine, "0001953926", "ZBIO", "Z", f1, is_backfill=True)
    te.process_filing(engine, "0001953926", "ZBIO", "Z", {**f1, "accession": "n2", "filing_date": date.today() - timedelta(days=1)},
                      is_backfill=False)
    monkeypatch.setenv("PAPER_RESEARCH_MODE", "1")
    with engine.begin() as c:
        _universe(c)
    scan_new_events(engine, days=5)
    with engine.connect() as c:
        assert c.execute(select(events.c.id).where(events.c.payload["direction"].astext == "POSITIVE")).all() == []
        assert c.execute(select(proposed_trades.c.id)).all() == []


@db
@pytest.mark.usefixtures("clean", "offline_prices")
def test_fill_locks_the_source_event_against_concurrent_supersession(engine, monkeypatch):
    from sqlalchemy.exc import OperationalError

    from app.paper import ledger
    eid, p, r = _approved_setup(engine, monkeypatch)
    conn_a = engine.connect()
    tx = conn_a.begin()
    try:
        assert ledger.fill_orders(conn_a, date(2024, 3, 10))["filled"] == 5  # fill in progress, not committed
        with engine.connect() as conn_b:  # a concurrent reclassification tries to supersede the source event
            conn_b.execute(text("SET lock_timeout = '300ms'"))
            with pytest.raises(OperationalError):
                conn_b.execute(text("UPDATE events SET superseded = true WHERE id = :i"), {"i": eid})
            conn_b.rollback()
    finally:
        tx.commit()
        conn_a.close()
    with engine.begin() as conn_b:  # after the fill committed the supersession proceeds (and later fills see it)
        conn_b.execute(text("UPDATE events SET superseded = true WHERE id = :i"), {"i": eid})
    with engine.connect() as c:
        assert c.execute(text("SELECT superseded FROM events WHERE id = :i"), {"i": eid}).scalar() is True



@db
@pytest.mark.usefixtures("clean")
def test_reclassify_twice_does_not_collide(engine):
    """v1 -> v2 -> v3: every supersession of the same accession gets its own key."""
    from app.db.models import events, tenk_filings
    from app.tenk.engine import supersede_old_classifier
    for _ in range(2):
        with engine.begin() as c:
            c.execute(tenk_filings.insert().values(cik="0001953926", accession="0000000001-24-000001", form="10-K",
                                                   filing_date=date(2024, 3, 1), source_url="u", profile={}, status="ok"))
            _event(c, et="TENK_CHANGE", key="tenk:0000000001-24-000001", payload={"direction": "NEGATIVE"})
        supersede_old_classifier(engine, dry_run=False)
    with engine.connect() as c:
        keys = c.execute(select(events.c.dedupe_key).where(events.c.superseded.is_(True))).scalars().all()
    assert len(keys) == 2 and len(set(keys)) == 2


def test_tenk_extraction_runs_isolated_and_matches_inprocess():
    from app.tenk import engine as te
    doc = _doc([_risk(i) for i in range(20)], BASE_MDNA).encode()
    out = te.extract_isolated(doc)
    assert isinstance(out, te.Extracted) and set(out.items) >= {"RISK_FACTORS", "MD&A"}
    _, items, prof = te._extract_inproc(doc)
    assert out.items == items and out.profile["RISK_FACTORS"]["hashes"] == prof["RISK_FACTORS"]["hashes"]
    bad = te.extract_isolated(b"\x00" * 10)
    assert bad.items == {}  # a broken document yields no sections, never an exception into the backfill
