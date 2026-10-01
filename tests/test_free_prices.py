"""Free historical price recovery: identity gate, provenance, terminal values, quotas, provider fallback."""
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text

from app.marketdata.provider import Bar
from tests.test_db import URL

db = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")
OLD = "0000000555"  # a delisted company that is not in today's SEC ticker file


def series(start: date, n: int, price: float = 20.0, step: float = 0.1, adj_factor: float = 1.0) -> list[Bar]:
    out, d, p = [], start, price
    while len(out) < n:
        if d.weekday() < 5:
            out.append(Bar(d, p, p * 1.01, p * 0.99, p, p * adj_factor, 1000))
            p += step
        d += timedelta(days=1)
    return out


# ------------------------------------------------------------------ pure gate pieces

def test_returns_agree_ignores_split_and_raw_vs_adjusted_levels():
    from app.pricesources.gate import returns_agree
    a = series(date(2023, 1, 2), 60, 100.0, 0.5)
    # the same security, raw prices after a 2:1 split on day 30 but a correct adjusted series
    b = [Bar(x.date, x.open / (2 if i >= 30 else 1), x.high, x.low, x.close / (2 if i >= 30 else 1), x.close, x.volume)
         for i, x in enumerate(a)]
    assert returns_agree(a, b)["agree"] is True
    c = [Bar(x.date, x.open, x.high, x.low, x.close * (1.05 if i % 2 else 0.95), x.close * (1.05 if i % 2 else 0.95), 1)
         for i, x in enumerate(a)]
    assert returns_agree(a, c)["agree"] is False


def test_name_similarity():
    from app.pricesources.gate import name_similarity
    assert name_similarity("SVB Financial Group", "SVB FINANCIAL GROUP") == 1.0
    assert name_similarity("Twitter Inc", "TWITTER, INC.") == 1.0
    assert name_similarity("Silicon Valley Bancorp", "Acme Mining Corp") == 0.0


def test_wayback_probe_counts_only_daily_history(monkeypatch):
    import httpx

    from app.pricesources import report

    class R:
        status_code = 200

        def __init__(self, rows):
            self.rows = rows
            self.text = "x"

        def json(self):
            return [["urlkey", "timestamp", "original"]] + self.rows

    def fake(url, params=None, **k):
        if "v8/finance/chart" in params["url"]:
            return R([["k", "20230310", "https://query1.finance.yahoo.com/v8/finance/chart/sivb?range=5d&interval=15m"],
                      ["k", "20230101", "https://query1.finance.yahoo.com/v8/finance/chart/sivb?range=1y&interval=1d"]])
        return R([])
    monkeypatch.setattr(httpx, "get", fake)
    out = report.probe_wayback(["SIVB"], pause_s=0)
    assert out["SIVB"]["v8_chart"] == {"snapshots": 2, "daily_history_candidates": 1}
    assert out["SIVB"]["ichart_csv"]["snapshots"] == 0


def test_tiingo_prices_parse_uses_adjusted_close():
    from app.pricesources.tiingo import parse_prices
    bars = parse_prices(b'[{"date":"2023-03-09T00:00:00.000Z","open":270.0,"high":271.0,"low":100.0,"close":106.04,'
                        b'"adjClose":106.04,"volume":1}]')
    assert bars[0].date == date(2023, 3, 9) and bars[0].adj_close == 106.04


# ------------------------------------------------------------------ DB fixtures

def _company(c, cik=OLD, name="Old Bancorp Inc", symbol="OLDB", delisted: date | None = date(2023, 3, 17),
             reason_marker: str | None = "BANKRUPTCY", first=date(2019, 1, 1), last=date(2023, 3, 10)):
    c.execute(text("INSERT INTO companies (cik, name, name_norm) VALUES (:c, :n, lower(:n)) ON CONFLICT DO NOTHING"),
              {"c": cik, "n": name})
    c.execute(text("""INSERT INTO security_symbols (cik, symbol, first_seen, last_seen, n_obs, source)
                      VALUES (:c, :s, :f, :l, 5, 'form345_filing')"""), {"c": cik, "s": symbol, "f": first, "l": last})
    if delisted:
        c.execute(text("""INSERT INTO corporate_actions (cik, action, action_date, accession, source)
                          VALUES (:c, 'FORM25', :d, 'a1', 'sec_full_index')"""), {"c": cik, "d": delisted})
        if reason_marker:
            c.execute(text("""INSERT INTO corporate_actions (cik, action, action_date, accession, source)
                              VALUES (:c, :a, :d, 'a2', 'sec_full_index')"""),
                      {"c": cik, "a": reason_marker, "d": delisted - timedelta(days=20)})


def _unresolved_event(c, cik=OLD, symbol="OLDB", d=date(2023, 1, 10), key="x1", et="INSIDER_OPEN_MARKET_BUY"):
    from app.db.store import create_event
    eid = create_event(c, event_type=et, dedupe_key=key, public_date=d, ticker=symbol, issuer_cik=cik, title="t",
                       payload={}, source_url="u", public_at=datetime(d.year, d.month, d.day, 21, tzinfo=timezone.utc))
    c.execute(text("UPDATE events SET returns_status = 'no_data', missing_reason = 'DELISTED_BANKRUPTCY' WHERE id = :i"),
              {"i": eid})
    return eid


@pytest.fixture
def offline(monkeypatch):
    import app.marketdata.provider as mp

    class NoFetch:
        def get_historical_prices(self, *a):
            return []
    monkeypatch.setattr(mp, "get_provider", lambda: NoFetch())


# ------------------------------------------------------------------ DB tests

@db
@pytest.mark.usefixtures("clean", "offline")
def test_delisted_price_recovery_end_to_end_with_bankruptcy_terminal(engine):
    """A trusted vendor series for a delisted CIK prices its events; horizons past the delisting exit at the
    last exchange price (terminal loss), the benchmark keeps running."""
    from app.backtest.jobs import compute_pending
    from app.db.models import event_returns, events
    from app.marketdata.provider import store_bars
    from app.pricesources.store import save_source
    with engine.begin() as c:
        _company(c)
        eid = _unresolved_event(c)
        store_bars(c, "SPY", series(date(2022, 12, 1), 600, 380.0, 0.2))
    bars = series(date(2022, 6, 1), 205, 50.0, -0.24)  # decays to ~1 $ into the March 2023 failure
    with engine.begin() as c:
        _, v = save_source(c, cik=OLD, symbol="OLDB", provider="finsaber", bars=bars, identity="cik", source_ref="t")
    assert v.status == "TRUSTED", v.reasons
    with engine.connect() as c:
        assert c.execute(select(events.c.returns_status).where(events.c.id == eid)).scalar() == "retry"  # only affected events
    compute_pending(engine, backfill=None)
    with engine.connect() as c:
        ev = c.execute(select(events).where(events.c.id == eid)).mappings().one()
        r365 = c.execute(select(event_returns).where(event_returns.c.event_id == eid, event_returns.c.horizon_days == 365)).mappings().one()
    assert ev["price_symbol_basis"] == "vendor_trusted" and ev["returns_status"] in ("complete", "partial")
    assert r365["exit_basis"] == "delisting_terminal" and r365["exit_date"] <= date(2023, 3, 17) and r365["ret"] < -0.5


@db
@pytest.mark.usefixtures("clean", "offline")
def test_merger_cash_out_terminal_value_is_the_final_trading_price(engine):
    from app.backtest.jobs import compute_pending
    from app.db.models import event_returns
    from app.marketdata.provider import store_bars
    from app.pricesources.store import save_source
    with engine.begin() as c:
        _company(c, delisted=date(2023, 4, 20), reason_marker="MERGER_PROXY", last=date(2023, 4, 1))
        eid = _unresolved_event(c)
        store_bars(c, "SPY", series(date(2022, 12, 1), 600, 380.0, 0.2))
    bars = series(date(2022, 9, 1), 160, 30.0, 0.0)
    bars = [b if b.date < date(2023, 2, 1) else Bar(b.date, 42.0, 42.1, 41.9, 42.0, 42.0, 1000) for b in bars]  # deal price
    with engine.begin() as c:
        save_source(c, cik=OLD, symbol="OLDB", provider="finsaber", bars=bars, identity="cik", source_ref="t")
    compute_pending(engine, backfill=None)
    with engine.connect() as c:
        r = c.execute(select(event_returns).where(event_returns.c.event_id == eid, event_returns.c.horizon_days == 180)).mappings().one()
    assert r["exit_basis"] == "delisting_terminal" and r["exit_price"] == pytest.approx(42.0)
    assert r["ret"] == pytest.approx(42.0 / 30.0 - 1, rel=0.02)  # not "no price forever"


@db
@pytest.mark.usefixtures("clean")
def test_ticker_reuse_is_rejected(engine):
    from app.pricesources.store import save_source
    with engine.begin() as c:
        _company(c)
        # the symbol belongs to ANOTHER company during the series' range
        c.execute(text("""INSERT INTO security_symbols (cik, symbol, first_seen, last_seen, n_obs, source)
                          VALUES ('0000000999', 'OLDB', '2022-01-01', '2024-01-01', 3, 'form345_filing')"""))
        _, v = save_source(c, cik=OLD, symbol="OLDB", provider="tiingo", bars=series(date(2022, 6, 1), 200),
                           identity="name", vendor_name="Old Bancorp", source_ref="t")
    assert v.status == "REJECTED" and "TICKER_REUSED_IN_RANGE" in v.reasons


@db
@pytest.mark.usefixtures("clean")
def test_vendor_name_mismatch_and_successor_series_are_rejected(engine):
    from app.pricesources.store import save_source
    with engine.begin() as c:
        _company(c)
        _, wrong = save_source(c, cik=OLD, symbol="OLDB", provider="tiingo", bars=series(date(2022, 6, 1), 200),
                               identity="name", vendor_name="Acme Mining Corp", source_ref="t")
        # a series that keeps trading long after the equity was delisted (OTC successor / new security)
        _, succ = save_source(c, cik=OLD, symbol="OLDBQ", provider="tiingo", bars=series(date(2022, 6, 1), 400),
                              identity="name", vendor_name="Old Bancorp Inc", source_ref="t")
    assert wrong.status == "REJECTED" and "VENDOR_NAME_MISMATCH" in wrong.reasons
    assert succ.status == "REJECTED" and "TRADES_AFTER_DELISTING" in succ.reasons


@db
@pytest.mark.usefixtures("clean")
def test_cross_provider_disagreement_is_low_confidence_and_never_used(engine):
    from app.pricesources.store import save_source, vendor_series
    a = series(date(2022, 6, 1), 200)
    b = [Bar(x.date, x.open, x.high, x.low, x.close * (1.06 if i % 2 else 0.94), x.close * (1.06 if i % 2 else 0.94), 1)
         for i, x in enumerate(a)]
    with engine.begin() as c:
        _company(c)
        _, va = save_source(c, cik=OLD, symbol="OLDB", provider="finsaber", bars=a, identity="cik", source_ref="t")
        _, vb = save_source(c, cik=OLD, symbol="OLDB", provider="tiingo", bars=b, identity="name",
                            vendor_name="Old Bancorp Inc", source_ref="t")
        assert va.status == "TRUSTED"
        assert vb.status == "LOW_CONFIDENCE" and "CROSS_PROVIDER_DISAGREEMENT" in vb.reasons
        got = vendor_series(c, OLD, date(2023, 1, 10))
    assert got is not None and got[1] == "vendor_trusted"  # the disagreeing series is never selected


@db
@pytest.mark.usefixtures("clean")
def test_low_confidence_alone_is_never_used(engine):
    from app.pricesources.store import save_source, vendor_series
    with engine.begin() as c:
        _company(c, delisted=None)  # no SEC delisting record and only a symbol-level identity
        _, v = save_source(c, cik=OLD, symbol="OLDB", provider="tiingo", bars=series(date(2022, 6, 1), 150),
                           identity="symbol", source_ref="t")
        assert v.status == "LOW_CONFIDENCE"
        assert vendor_series(c, OLD, date(2022, 9, 1)) is None


@db
@pytest.mark.usefixtures("clean")
def test_provider_fallback_prefers_trusted_then_dataset_with_cik(engine):
    from app.pricesources.store import save_source, vendor_series
    bars = series(date(2022, 6, 1), 200)
    with engine.begin() as c:
        _company(c)
        _, vt = save_source(c, cik=OLD, symbol="OLDB", provider="tiingo", bars=bars, identity="name",
                            vendor_name="Old Bancorp", source_ref="t")
        _, vf = save_source(c, cik=OLD, symbol="OLDB", provider="finsaber", bars=bars, identity="cik", source_ref="t")
        got = vendor_series(c, OLD, date(2023, 1, 10))
    assert vt.status == vf.status == "TRUSTED" and got[1] == "vendor_trusted"
    with engine.connect() as c:
        pid = c.execute(text("SELECT provider FROM price_sources WHERE id = (SELECT source_id FROM vendor_prices LIMIT 1)")).scalar()
    assert pid in ("tiingo", "finsaber")


@db
@pytest.mark.usefixtures("clean")
def test_partial_coverage_keeps_the_family_gated(engine):
    """Vendor-resolved events count as identity-verified, but a family with many unresolved events stays
    PARTIAL/BLOCKED - recovered prices never force a metric open."""
    from app.backtest import quality as Q
    from app.db.store import create_event
    with engine.begin() as c:
        for i in range(60):
            eid = create_event(c, event_type="INSIDER_OPEN_MARKET_BUY", dedupe_key=f"p{i}", public_date=date(2022, 1, 3),
                               ticker=f"T{i}", issuer_cik=f"{i + 1000:010d}", title="t", payload={}, source_url="u")
            if i < 40:
                c.execute(text("""INSERT INTO event_returns (event_id, horizon_days, entry_date, entry_price, exit_date,
                                  exit_price, ret, spy_ret, excess_ret) VALUES (:e, 90, '2022-01-04', 10, '2022-04-04', 11,
                                  0.1, 0.02, 0.08)"""), {"e": eid})
                c.execute(text("UPDATE events SET returns_status = 'complete', price_symbol_basis = 'vendor_trusted' WHERE id = :e"),
                          {"e": eid})
            else:
                c.execute(text("UPDATE events SET returns_status = 'no_data', missing_reason = 'DELISTED_MERGER' WHERE id = :e"),
                          {"e": eid})
        rows = Q.load_rows(c, "INSIDER_OPEN_MARKET_BUY", "TRUE", {}, 90)
        st = Q.summarize(rows, 90, 90)
    assert all(r.verified for r in rows if r.outcome != "unresolved")
    assert st.status in ("PARTIAL", "BLOCKED") and st.unresolved_share > 0.3


@db
@pytest.mark.usefixtures("clean")
def test_tiingo_quota_stops_at_the_free_tier_and_other_providers_continue(engine, monkeypatch):
    from app.pricesources import quota, tiingo
    monkeypatch.setenv("TIINGO_API_KEY", "test-not-a-key")
    with engine.begin() as c:
        now = datetime.now(timezone.utc)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if now - month_start < timedelta(hours=1, minutes=5):
            pytest.skip("the first hour of a month cannot hold 'earlier this month, not this hour' usage")
        # earlier THIS month and outside the last hour (2 days back is last month on the 1st / 2nd)
        seen = max(now - timedelta(days=2), month_start + timedelta(minutes=1))
        for i in range(475):  # 95% of 500 unique symbols this month already used
            quota.record(c, "tiingo", "prices", 200, f"S{i}", now=seen)
        assert quota.can_request(c, "tiingo", "NEWSYM")[1] == "MONTHLY_SYMBOL_LIMIT"
        assert quota.can_request(c, "tiingo", "S1")[0] is True  # an already-used symbol costs no new slot
        for _ in range(47):
            quota.record(c, "tiingo", "prices", 200, None, now=now)
        assert quota.can_request(c, "tiingo", "S1")[1] == "PER_HOUR_LIMIT"
    calls = []
    monkeypatch.setattr(tiingo, "_get", lambda *a, **k: calls.append(a) or (200, b"[]"))
    monkeypatch.setattr(tiingo, "candidates", lambda engine_, n=0: [{"cik": OLD, "symbol": "NEWSYM"}])
    out = tiingo.run(engine)
    assert out["stopped"] in ("MONTHLY_SYMBOL_LIMIT", "PER_HOUR_LIMIT") and calls == []
    assert out["quota"]["remaining_symbols_this_month"] == 25


@db
@pytest.mark.usefixtures("clean")
def test_tiingo_symbol_not_found_is_recorded_and_not_retried(engine, monkeypatch):
    from app.pricesources import tiingo
    monkeypatch.setenv("TIINGO_API_KEY", "test-not-a-key")
    monkeypatch.setattr(tiingo, "_get", lambda *a, **k: (404, None))
    with engine.begin() as c:
        _company(c)
    r = tiingo.fetch_symbol(engine, OLD, "OLDB")
    assert r["status"] == "NOT_FOUND"
    with engine.connect() as c:
        st = c.execute(text("SELECT validation_status FROM price_sources WHERE provider = 'tiingo'")).scalar()
        used = c.execute(text("SELECT count(*) FROM provider_usage WHERE provider = 'tiingo'")).scalar()
    assert st == "REJECTED" and used == 1


@db
@pytest.mark.usefixtures("clean")
def test_finsaber_rows_are_filtered_to_unresolved_ciks_and_identity_is_the_cik(engine):
    import io

    import pyarrow as pa
    import pyarrow.parquet as pq

    from app.pricesources import finsaber
    with engine.begin() as c:
        _company(c)
        _unresolved_event(c)
    bars = series(date(2022, 6, 1), 200)
    tbl = pa.table({"date": [b.date for b in bars] * 2, "symbol": ["OLDB"] * 200 + ["AAPL"] * 200,
                    "cik": [OLD] * 200 + ["0000320193"] * 200, "open": [b.open for b in bars] * 2,
                    "high": [b.high for b in bars] * 2, "low": [b.low for b in bars] * 2, "close": [b.close for b in bars] * 2,
                    "adjusted_close": [b.adj_close for b in bars] * 2, "volume": [1000] * 400})
    buf = io.BytesIO()
    pq.write_table(tbl, buf)
    out = finsaber.run(engine, years=(2022,), fetch=lambda url: buf.getvalue())
    assert out["series"] == 1 and out["verdicts"] == {"TRUSTED": 1}
    with engine.connect() as c:
        ref = c.execute(text("SELECT source_ref, identity_basis FROM price_sources")).one()
    assert ref[1] == "cik" and "2022:" in ref[0]


def test_ticker_change_is_spliced_but_a_long_gap_is_not():
    from app.pricesources.finsaber import splice_ticker_changes
    a = series(date(2021, 1, 4), 230, 20.0, 0.0)  # ends 2021-11-19
    b = series(a[-1].date + timedelta(days=3), 100, 20.2, 0.0)  # renamed next trading day, continuous price
    c = series(date(2022, 9, 1), 50, 5.0, 0.0)  # months later: new equity after a reorganisation
    out = splice_ticker_changes({"COG": a, "CTRA": b, "NEWC": c})
    assert [(s, ch) for s, _, ch in out] == [("CTRA", ["COG", "CTRA"]), ("NEWC", ["NEWC"])]
    jump = series(a[-1].date + timedelta(days=3), 50, 60.0, 0.0)  # +200% at the junction: not the same security
    assert len(splice_ticker_changes({"COG": a, "XYZ": jump})) == 2


@db
@pytest.mark.usefixtures("clean")
def test_series_cut_by_the_dataset_end_is_accepted_but_never_cashes_out(engine):
    from app.pricesources.store import save_source
    bars = series(date(2025, 6, 2), 150)  # ends at the snapshot end 2025-12-26..31, company delisted later
    with engine.begin() as c:
        _company(c, delisted=date(2026, 4, 8), reason_marker="MERGER_PROXY", last=date(2026, 3, 1))
        _, v = save_source(c, cik=OLD, symbol="OLDB", provider="finsaber", bars=bars, identity="cik", source_ref="t",
                           source_end=bars[-1].date)
    assert v.status == "TRUSTED" and v.checks["cut_by_source_end"] is True


@db
@pytest.mark.usefixtures("clean")
def test_otc_tail_after_delisting_is_truncated_and_downgraded(engine):
    from app.pricesources import finsaber
    from app.pricesources.store import load_bars
    with engine.begin() as c:
        _company(c, delisted=date(2023, 3, 17))
        _unresolved_event(c)
    bars = series(date(2022, 6, 1), 300)  # keeps "trading" until 2023-07 (OTC quotes after the delisting)
    out_calls = {}

    def fake_load(y, ciks, fetch=None):
        out_calls[y] = True
        return ({(OLD, "OLDB"): bars}, "sha") if y == 2022 else ({}, "sha")
    import app.pricesources.finsaber as fm
    orig = fm.load_year
    fm.load_year = fake_load
    try:
        out = finsaber.run(engine, years=(2022,))
    finally:
        fm.load_year = orig
    with engine.connect() as c:
        row = c.execute(text("SELECT id, validation_status, last_date, reasons FROM price_sources")).mappings().one()
        kept = load_bars(c, row["id"])
    assert out["truncated"] == 1 and row["validation_status"] == "VALIDATED_FALLBACK"
    assert row["last_date"] <= date(2023, 3, 17) and kept[-1].date <= date(2023, 3, 17)
    assert "TRUNCATED_AT_SEC_DELISTING" in row["reasons"]


@db
@pytest.mark.usefixtures("clean")
def test_quota_reservation_is_atomic_across_connections(engine):
    """Two processes racing for the last slot: exactly one gets it (advisory lock + reserve)."""
    from app.pricesources import quota
    now = datetime.now(timezone.utc)
    with engine.begin() as c:
        for _ in range(45):
            quota.record(c, "tiingo", "prices", 200, None, now=now)
    c1, c2 = engine.connect(), engine.connect()
    t1 = c1.begin()
    ok1, _, _ = quota.reserve(c1, "tiingo", "AAA", "symbol", n=2)  # takes the lock and slots 46-47
    import threading
    res = {}

    def other():
        t2 = c2.begin()
        res["ok2"], res["why2"], _ = quota.reserve(c2, "tiingo", "BBB", "symbol", n=2)  # waits for the lock
        t2.commit()
    th = threading.Thread(target=other)
    th.start()
    t1.commit()
    th.join(10)
    c1.close()
    c2.close()
    assert ok1 is True and res["ok2"] is False and res["why2"] == "PER_HOUR_LIMIT"


# ------------------------------------------------------------------ OTC continuation + tradeability

OTC_EVIDENCE = {"status": "CONFIRMED_OTC", "evidence": ["8-K item 3.01 names the OTC symbol"], "source": "sec"}


def _otc_bars():
    """Exchange trading until the 2023-03-17 delisting, then the same security quoted OTC at half the price."""
    bars = series(date(2022, 6, 1), 400, 20.0, 0.0)
    return [b if b.date <= date(2023, 3, 17) else Bar(b.date, 10.0, 10.1, 9.9, 10.0, 10.0, 500) for b in bars]


@db
@pytest.mark.usefixtures("clean")
def test_otc_continuation_needs_confirmed_independent_evidence(engine):
    from app.pricesources.store import save_source
    with engine.begin() as c:
        _company(c, reason_marker=None)
        _, none = save_source(c, cik=OLD, symbol="OLDB", provider="antigravity_yf", bars=_otc_bars(), identity="symbol",
                              source_ref="t")
        _, weak = save_source(c, cik=OLD, symbol="OLDB", provider="antigravity_yf2", bars=_otc_bars(), identity="symbol",
                              source_ref="t", otc_continuation={"status": "INSUFFICIENT_EVIDENCE"})
        _, ok = save_source(c, cik=OLD, symbol="OLDB", provider="antigravity_yf3", bars=_otc_bars(), identity="symbol",
                            source_ref="t", otc_continuation=OTC_EVIDENCE)
    assert none.status == weak.status == "REJECTED" and "TRADES_AFTER_DELISTING" in weak.reasons
    assert ok.status == "VALIDATED_FALLBACK" and "OTC_CONTINUATION_CONFIRMED" in ok.reasons  # never TRUSTED
    assert ok.checks["otc_continuation"] == OTC_EVIDENCE


@db
@pytest.mark.usefixtures("clean")
def test_otc_continuation_with_a_reused_symbol_is_still_rejected(engine):
    from app.pricesources.store import save_source
    with engine.begin() as c:
        _company(c, reason_marker=None)
        c.execute(text("""INSERT INTO security_symbols (cik, symbol, first_seen, last_seen, n_obs, source)
                          VALUES ('0000000999', 'OLDB', '2023-06-01', '2024-01-01', 3, 'form345_filing')"""))
        _, v = save_source(c, cik=OLD, symbol="OLDB", provider="antigravity_yf", bars=_otc_bars(), identity="symbol",
                           source_ref="t", otc_continuation=OTC_EVIDENCE)
    assert v.status == "REJECTED" and "TICKER_REUSED_IN_RANGE" in v.reasons


@db
@pytest.mark.usefixtures("clean", "offline")
def test_confirmed_otc_holding_is_valued_on_otc_quotes_not_cashed_out_at_delisting(engine):
    from app.backtest.jobs import compute_pending
    from app.db.models import event_returns
    from app.db.models import events as events_t
    from app.marketdata.provider import store_bars
    from app.pricesources.store import save_source
    with engine.begin() as c:
        _company(c, reason_marker=None)
        eid = _unresolved_event(c)
        store_bars(c, "SPY", series(date(2022, 12, 1), 600, 380.0, 0.2))
        save_source(c, cik=OLD, symbol="OLDB", provider="antigravity_yf", bars=_otc_bars(), identity="symbol",
                    source_ref="t", otc_continuation=OTC_EVIDENCE)
        other = _unresolved_event(c, cik="0000000777", symbol="ZZZ", key="other")
        c.execute(text("UPDATE events SET returns_status = 'retry', returns_checked_at = NULL WHERE id = :i"), {"i": other})
    out = compute_pending(engine, backfill=None, ciks=[OLD])  # targeted: the other issuer is not touched
    with engine.connect() as c:
        assert c.execute(select(events_t.c.returns_checked_at).where(events_t.c.id == other)).scalar() is None
    assert out["candidates"] == 1
    assert compute_pending(engine, backfill=None, event_ids=[other])["candidates"] == 1  # only the listed event
    with engine.connect() as c:
        r = c.execute(select(event_returns).where(event_returns.c.event_id == eid, event_returns.c.horizon_days == 180)).mappings().one()
    assert r["exit_basis"] != "delisting_terminal" and r["exit_date"] > date(2023, 6, 1)
    assert r["exit_price"] == pytest.approx(10.0) and r["ret"] == pytest.approx(-0.5, abs=0.01)  # the OTC loss is kept


@db
@pytest.mark.usefixtures("clean")
def test_not_tradeable_and_pre_listing_are_flagged_audited_and_outside_the_denominator(engine):
    from app.backtest import quality as Q
    from app.integrity import tradeability
    with engine.begin() as c:
        _company(c, delisted=None)
        a = _unresolved_event(c, key="a", d=date(2023, 1, 10))
        b = _unresolved_event(c, key="b", d=date(2023, 1, 11))
        p = _unresolved_event(c, key="p", d=date(2023, 1, 12))
        dry = tradeability.mark(c, [(a, "NOT_TRADEABLE", {"sec_exchanges": []})], "t-dry", dry_run=True)
        n0 = len(Q.load_rows(c, "INSIDER_OPEN_MARKET_BUY", "TRUE", {}, 30, as_of=date(2024, 6, 1)))
        out = tradeability.mark(c, [(a, "NOT_TRADEABLE", {"sec_exchanges": []}), (p, "PRE_LISTING", {"listing_start": "2023-05-01"})],
                                "t-run", dry_run=False)
        again = tradeability.mark(c, [(a, "NOT_TRADEABLE", {"sec_exchanges": []})], "t-run2", dry_run=False)
        rows = Q.load_rows(c, "INSIDER_OPEN_MARKET_BUY", "TRUE", {}, 30, as_of=date(2024, 6, 1))
        audit = c.execute(text("SELECT count(*) FROM data_cleanup_audit WHERE action = 'FLAG_TRADEABILITY'")).scalar()
        kept = c.execute(text("SELECT count(*) FROM events WHERE NOT superseded")).scalar()
    assert dry["NOT_TRADEABLE"] == 1 and n0 == 3
    assert out["NOT_TRADEABLE"] == 1 and out["PRE_LISTING"] == 1 and again["unchanged"] == 1
    assert [r.event_id for r in rows] == [b] and audit == 2 and kept == 3  # flagged, never deleted
