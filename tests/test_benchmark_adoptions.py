"""Regression tests for ideas adopted from the GitHub benchmark (own implementations)."""
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from app.backtest import quality as Q
from app.marketdata.provider import Bar
from tests.test_db import URL


def _bars(start: date, n: int, price=100.0, step=0.0):
    out, d, p = [], start, price
    while len(out) < n:
        if d.weekday() < 5:
            out.append(Bar(d, p, p, p, p, p, 1))
            p += step
        d += timedelta(days=1)
    return out


# ------------------------------------------------------------------ sector-matched benchmark

def test_sector_etf_mapping():
    from app.backtest.sector import etf_for_sector
    assert etf_for_sector("TECHNOLOGY") == "XLK" and etf_for_sector("PHARMA_BIOTECH") == "XBI"
    assert etf_for_sector(None) is None and etf_for_sector("SHELL_SPAC") is None


def test_sector_benchmark_uses_the_same_holding_period():
    from app.backtest.sector import benchmark_return
    bars = _bars(date(2024, 1, 1), 120, price=100.0, step=0.5)
    by = {b.date: b for b in bars}
    entry, exit_ = bars[5].date, bars[60].date
    r = benchmark_return(by, bars, entry, exit_)
    assert r == pytest.approx(bars[60].close / bars[5].open - 1)
    assert benchmark_return(by, bars, entry, date(2030, 1, 1)) is None  # no silent nearest-date substitution


def test_sector_benchmark_for_a_delisted_exit_runs_the_full_horizon():
    from app.backtest.sector import benchmark_return
    bars = _bars(date(2024, 1, 1), 200, price=100.0, step=1.0)
    by = {b.date: b for b in bars}
    entry = bars[0].date
    full = benchmark_return(by, bars, entry, bars[10].date, "delisting_terminal", 90)
    short = benchmark_return(by, bars, entry, bars[10].date, "horizon", 90)
    assert full > short  # cash after the cash-out earns nothing; the sector keeps running


def test_quality_reports_sector_benchmark_rate_and_coverage():
    rows = [Q.Row(i, f"C{i}", "TECHNOLOGY", date(2021, 1, 1), "win", None, True, 0.05, 0.05, None,
                  0.02 if i % 4 else -0.01) for i in range(40)]
    rows += [Q.Row(100 + i, f"D{i}", None, date(2021, 1, 1), "loss", None, True, -0.05, -0.05, None, None) for i in range(10)]
    st = Q.summarize(rows, 90, 90)
    assert st.sector_coverage == pytest.approx(40 / 50)
    assert st.sector_benchmark_rate == pytest.approx(30 / 40)
    assert st.company_equal_rate == pytest.approx(40 / 50)  # SPY-relative figure unchanged


def test_telegram_shows_sector_rate_only_when_provided():
    from app.backtest.probability import SuccessRate
    from app.notifications.format import history_block
    base = dict(probability=0.6, raw_rate=0.6, n=300, horizon_days=90, group="g", median_excess=0.02, median_return=0.05,
                typical_drawdown=None, best_horizon=None, company_n=120, status="TRUSTED", episodes=200)
    assert "sektör endeksine" not in "\n".join(history_block(SuccessRate(**base)))
    txt = "\n".join(history_block(SuccessRate(**base, sector_benchmark_rate=0.47)))
    assert "Kendi sektör endeksine göre geçme oranı: %47" in txt


# ------------------------------------------------------------------ walk-forward multiple-testing guard

def test_bonferroni_guard_rejects_small_improvements_and_scales_with_trials():
    from app.backtest.walkforward import beats_baseline
    base = {"rate": 0.50, "companies": 400}
    ok, z, req = beats_baseline({"rate": 0.53, "companies": 60}, base, n_trials=5)
    assert not ok and z < req  # +3pp on 60 companies is noise
    ok, z, _ = beats_baseline({"rate": 0.70, "companies": 200}, base, n_trials=5)
    assert ok
    _, _, req2 = beats_baseline({"rate": 0.70, "companies": 200}, base, n_trials=2)
    _, _, req5 = beats_baseline({"rate": 0.70, "companies": 200}, base, n_trials=5)
    assert req5 > req2  # more thresholds tried -> stricter bar
    assert beats_baseline({"rate": None, "companies": 0}, base, 3)[0] is False


# ------------------------------------------------------------------ canaries

def test_business_days_between():
    from app.integrity.canary import business_days_between
    assert business_days_between(date(2026, 9, 25), date(2026, 9, 28)) == 1  # Fri -> Mon
    assert business_days_between(date(2026, 9, 21), date(2026, 9, 25)) == 4
    assert business_days_between(date(2026, 9, 25), date(2026, 9, 25)) == 0


# ------------------------------------------------------------------ DB: crowding + canary + delay buckets

pytestmark_db = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")


def _13d(c, acc, investor, filed, amend=False, cls="ACTIVIST_13D", conf="HIGH"):
    from app.db.models import activist_filings, thirteen_d_classification
    c.execute(activist_filings.insert().values(accession=acc, form_type="SC 13D", is_13d=True, is_amendment=amend,
                                               issuer_cik="0001737450", ticker="OPRA", investor_cik=investor,
                                               investor_name=f"Fund {investor}", percent=6.0, filed_date=filed,
                                               item4_class="BOARD_SEAT", source_url="u"))
    c.execute(thirteen_d_classification.insert().values(accession=acc, issuer_cik="0001737450", investor_key=investor,
                                                        filing_class=cls, confidence=conf, version="t"))


@pytestmark_db
@pytest.mark.usefixtures("clean")
def test_insider_control_and_low_confidence_13ds_never_make_a_pack(engine):
    from app.signals.crowding import crowding_check
    with engine.begin() as c:
        _13d(c, "k1", "0000000301", date(2025, 6, 1))
        _13d(c, "k2", "0000000302", date(2025, 6, 5), cls="INSIDER_13D")
        _13d(c, "k3", "0000000303", date(2025, 6, 6), cls="CONTROL_HOLDER_13D")
        _13d(c, "k4", "0000000304", date(2025, 6, 7), conf="LOW")
        assert crowding_check(c, "0001737450", date(2025, 6, 7), ticker="OPRA") is None
        _13d(c, "k5", "0000000305", date(2025, 6, 8), conf="MEDIUM")
        assert crowding_check(c, "0001737450", date(2025, 6, 8), ticker="OPRA") is not None


@pytestmark_db
@pytest.mark.usefixtures("clean")
def test_activist_crowding_needs_independent_investors(engine):
    from app.db.models import beneficial_owner_links, events
    from app.signals.crowding import crowding_check
    with engine.begin() as c:
        _13d(c, "c1", "0000000101", date(2025, 3, 1))
        _13d(c, "c2", "0000000102", date(2025, 3, 20), amend=True)  # an amendment is not a new 5% crossing
        assert crowding_check(c, "0001737450", date(2025, 3, 20), ticker="OPRA") is None
        _13d(c, "c3", "0000000103", date(2025, 4, 10))
        c.execute(beneficial_owner_links.insert().values(entity_key="0000000103", entity_name="x", beneficial_owner_key="0000000101",
                                                         beneficial_owner_name="y", relationship_type="JOINT_FILER",
                                                         confidence="HIGH", source="t"))
        assert crowding_check(c, "0001737450", date(2025, 4, 10), ticker="OPRA") is None  # same economic group
        _13d(c, "c4", "0000000104", date(2025, 4, 20))
        eid = crowding_check(c, "0001737450", date(2025, 4, 20), ticker="OPRA")
        ev = c.execute(select(events).where(events.c.id == eid)).mappings().first()
    assert ev["event_type"] == "ACTIVIST_CROWDING" and ev["payload"]["n_investors"] == 2 and ev["public_date"] == date(2025, 4, 20)


@pytestmark_db
@pytest.mark.usefixtures("clean")
def test_activist_crowding_window_is_point_in_time(engine):
    from app.signals.crowding import crowding_check
    with engine.begin() as c:
        _13d(c, "d1", "0000000201", date(2025, 1, 2))
        _13d(c, "d2", "0000000202", date(2025, 5, 2))  # 120 days later: not a pack
        assert crowding_check(c, "0001737450", date(2025, 5, 2), ticker="OPRA") is None
        _13d(c, "d3", "0000000203", date(2025, 5, 30))
        assert crowding_check(c, "0001737450", date(2025, 5, 3), ticker="OPRA") is None  # d3 is in the future at that date


@pytestmark_db
@pytest.mark.usefixtures("clean")
def test_canary_flags_a_silent_source(engine):
    from app.db.models import insider_transactions
    from app.integrity.canary import evaluate
    with engine.begin() as c:
        c.execute(insider_transactions.insert().values(accession="x", line_key="N0", issuer_cik="1", owner_name="o",
                                                       category="OPEN_MARKET_BUY", filed_date=date(2026, 9, 14),
                                                       source="live", source_url="u"))
        res = {r["name"]: r for r in evaluate(c, today=date(2026, 9, 25))}
    assert res["sec_form4"]["status"] == "FAIL" and "9 business days" in res["sec_form4"]["detail"]
    assert res["sec_13d"]["status"] == "FAIL" and res["sec_13d"]["detail"] == "no stored data"
    assert res["sec_parse_rate"]["status"] == "OK"


@pytestmark_db
@pytest.mark.usefixtures("clean")
def test_delay_bucket_condition_selects_by_disclosure_lag(engine):
    from app.db.models import events
    from app.db.store import create_event
    with engine.begin() as c:
        for i, lag in enumerate((1, 1, 5, 40)):
            create_event(c, event_type="INSIDER_OPEN_MARKET_BUY", dedupe_key=f"L{i}", public_date=date(2023, 3, 1),
                         transaction_date=date(2023, 3, 1) - timedelta(days=lag), ticker="ZBIO", issuer_cik=f"{i:010d}",
                         title="t", payload={}, source_url="u")
        cond = "e.transaction_date IS NOT NULL AND (e.public_date - e.transaction_date) BETWEEN :lo AND :hi"
        fast = Q.load_rows(c, "INSIDER_OPEN_MARKET_BUY", cond, {"lo": 0, "hi": 2}, 90)
        late = Q.load_rows(c, "INSIDER_OPEN_MARKET_BUY", cond, {"lo": 8, "hi": 10000}, 90)
        assert c.execute(select(events.c.id)).first() is not None
    assert len(fast) == 2 and len(late) == 1
