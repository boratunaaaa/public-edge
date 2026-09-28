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


def test_paper_report_silent_without_activity_and_books_apart(engine, clean):
    from datetime import date
    from sqlalchemy import text
    from app.paper import ledger
    from app.reports_v2 import paper_report
    assert paper_report(engine, date(2026, 9, 28)) is None
    with engine.begin() as c:
        m = ledger.ensure_accounts(c)["B_90D"]
        r = ledger.ensure_accounts(c, book="RESEARCH")["B_90D"]
        for aid, t in ((m, "MAIN1"), (r, "RES1")):
            c.execute(text("""INSERT INTO paper_positions (account_id, ticker, shares, entry_date, entry_price, cost, status)
                              VALUES (:a, :t, 1, '2026-09-28', 10, 11, 'OPEN')"""), {"a": aid, "t": t})
    msg = paper_report(engine, date(2026, 9, 28))
    assert "PUBLIC EDGE PAPER RESEARCH" in msg and "$MAIN1" in msg and "$RES1 B_90D @ 10.00 [araştırma]" in msg


def test_morning_brief_shows_only_actionable_source_problems(engine, clean):
    from datetime import datetime, timezone
    from sqlalchemy import text
    from app.reports_v2 import morning_sections
    with engine.begin() as c:
        c.execute(text("""INSERT INTO source_health (source, status, health_status) VALUES
                          ('sec_form4', 'WORKING', 'HEALTHY'), ('finra_short_volume', 'DEGRADED', 'STALE'),
                          ('sedi', 'DISABLED', 'DISABLED')"""))
        s = "\n".join(morning_sections(c, datetime(2026, 9, 28, 12, tzinfo=timezone.utc)))
    assert "finra_short_volume: STALE" in s and "sec_form4" not in s and "sedi" not in s and "BIST" in s


def test_research_auto_approve_goes_only_to_research_books(engine, clean, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import text
    from app.paper import proposals as P
    monkeypatch.setenv("PAPER_RESEARCH_MODE", "1")
    now = datetime(2026, 9, 28, 14, tzinfo=timezone.utc)
    with engine.begin() as c:
        c.execute(text("""INSERT INTO events (id, event_type, dedupe_key, public_date, title, payload, source_url)
                          VALUES (1, 'GOV_CONTRACT', 'k', '2026-09-27', 't', '{}', 'u')"""))
        for i, ro in ((1, True), (2, False)):
            c.execute(text("""INSERT INTO proposed_trades (id, code, ticker, event_id, signal_family, event_type, proposal_created_at,
                              reference_date, entry_reference_price, suggested_position_pct, max_position_pct, thesis, data_quality,
                              approval_status, expires_at, research_only)
                              VALUES (:i, :c, 'TST', 1, 'GOV_CONTRACT', 'GOV_CONTRACT', :n, '2026-09-27', 10, 0.02, 0.05, 't', 'OK',
                                      'PENDING', :x, :ro)"""), {"i": i, "c": f"PE{i}", "n": now, "x": now + timedelta(days=1), "ro": ro})
    assert P.auto_approve_research(engine, now=now) == 1
    with engine.connect() as c:
        st = dict(c.execute(text("SELECT code, approval_status FROM proposed_trades")).all())
        books = c.execute(text("""SELECT DISTINCT a.book FROM paper_orders o JOIN paper_accounts a ON a.id = o.account_id""")).scalars().all()
    assert st == {"PE1": "APPROVED", "PE2": "PENDING"} and books == ["RESEARCH"]
    monkeypatch.setenv("PAPER_RESEARCH_AUTO_APPROVE", "0")
    assert P.auto_approve_research(engine, now=now) == 0
