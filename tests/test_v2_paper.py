"""V2 paper realism: liquidity classes, participation cap, zero-volume days, research books kept apart."""
from datetime import date, timedelta

from app.marketdata.provider import Bar
from app.paper.fills import liquidity_class, quote


def _bars(n, close, vol, start=date(2026, 1, 5)):
    return [Bar(start + timedelta(days=i), close, close, close, close, close, vol) for i in range(n)]


def test_liquidity_classes():
    assert liquidity_class(_bars(20, 50.0, 1_000_000))[0] == "LIQUID"  # $50M/day
    assert liquidity_class(_bars(20, 5.0, 400_000))[0] == "NORMAL"  # $2M/day
    assert liquidity_class(_bars(20, 1.0, 50_000))[0] == "ILLIQUID"
    assert liquidity_class(_bars(3, 50.0, 1_000_000))[0] == "ILLIQUID"  # unknown history is illiquid


def test_illiquid_fill_is_capped_and_expensive():
    prior = _bars(20, 1.0, 50_000)
    q = quote(prior, Bar(date(2026, 2, 2), 1.0, 1.0, 1.0, 1.0, 1.0, 50_000), desired_shares=10_000, floor_bps=50)
    assert q.capped and q.shares == 1_000 and q.participation == 0.02  # 2 % cap
    assert q.cost_bps >= 100 + 10 * (2 ** 0.5) - 1e-9


def test_zero_volume_day_never_fills():
    assert quote(_bars(20, 10.0, 1e6), Bar(date(2026, 2, 2), 10, 10, 10, 10, 10, 0), 100, 10) is None


def test_liquid_small_order_costs_the_floor():
    q = quote(_bars(20, 100.0, 5_000_000), Bar(date(2026, 2, 2), 100, 100, 100, 100, 100, 5_000_000), 100, 10)
    assert q.model == "LIQUID" and q.cost_bps == 10 and not q.capped


def test_research_and_main_books_are_separate(engine, clean):
    from sqlalchemy import text
    from app.paper import ledger
    with engine.begin() as c:
        main = ledger.ensure_accounts(c)
        res = ledger.ensure_accounts(c, book="RESEARCH")
        books = dict(c.execute(text("SELECT book, count(*) FROM paper_accounts GROUP BY 1")).all())
    assert set(main.values()).isdisjoint(res.values()) and books == {"MAIN": 5, "RESEARCH": 5}
