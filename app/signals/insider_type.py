"""Point-in-time heuristic inspired by Cohen, Malloy & Pomorski, "Decoding Inside Information".

This is not a replication of the paper's portfolio construction or full empirical method.  It
borrows the recurring-calendar idea for transaction-level alert triage and is labelled that way.

An insider with open-market trades in each of the 3 calendar years before the current trade is
  * ROUTINE if those 3 years all contain a trade in the SAME calendar month as the current trade
    (predictable calendar pattern);
  * OPPORTUNISTIC otherwise.
Insiders without 3 prior years of history stay UNCLASSIFIED (never guessed).
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import date

from sqlalchemy import and_, select, text
from sqlalchemy.engine import Connection, Engine

from app.db.models import events, insider_transactions

TRADE_CATEGORIES = ("OPEN_MARKET_BUY", "OPEN_MARKET_SALE")


def classify(history: set[tuple[int, int]], trade_date: date) -> str:
    y, m = trade_date.year, trade_date.month
    prior_years = [y - 1, y - 2, y - 3]
    years_with_trades = {yy for yy, _ in history}
    if not all(py in years_with_trades for py in prior_years):
        return "unclassified"
    if all((py, m) in history for py in prior_years):
        return "routine"
    return "opportunistic"


def owner_history(conn: Connection, owner_cik: str, before: date) -> set[tuple[int, int]]:
    rows = conn.execute(select(insider_transactions.c.trans_date).where(and_(
        insider_transactions.c.owner_cik == owner_cik, insider_transactions.c.category.in_(TRADE_CATEGORIES),
        insider_transactions.c.trans_date < date(before.year, 1, 1)))).all()
    return {(r[0].year, r[0].month) for r in rows if r[0]}


def holdings_increase(shares: float | None, shares_after: float | None) -> float | None:
    """Purchase as % of what the insider held before (buying 40k on top of 100k = +40%)."""
    if not shares or shares_after is None or shares_after <= shares:
        return None
    return shares / (shares_after - shares)


def annotate_existing(engine: Engine, batch: int = 5000) -> int:
    """Add insider_type / holdings_increase to INSIDER_OPEN_MARKET_BUY events that lack them."""
    with engine.connect() as conn:
        hist: dict[str, set] = defaultdict(set)
        for owner, d in conn.execute(select(insider_transactions.c.owner_cik, insider_transactions.c.trans_date).where(
                insider_transactions.c.category.in_(TRADE_CATEGORIES))):
            if owner and d:
                hist[owner].add((d.year, d.month))
        todo = conn.execute(select(events.c.id, events.c.payload).where(and_(
            events.c.event_type == "INSIDER_OPEN_MARKET_BUY", ~events.c.payload.has_key("insider_type")))).all()
    n = 0
    for i in range(0, len(todo), batch):
        with engine.begin() as conn:
            for eid, p in todo[i:i + batch]:
                dates = [date.fromisoformat(x) for x in (p.get("transaction_dates") or [])]
                t = classify(hist.get(p.get("owner_cik") or "", set()), dates[0]) if dates else "unclassified"
                inc = holdings_increase(p.get("shares"), p.get("shares_after"))
                conn.execute(text("UPDATE events SET payload = payload || CAST(:extra AS jsonb) WHERE id = :id"),
                             {"id": eid, "extra": json.dumps({"insider_type": t, "holdings_increase": inc,
                               "insider_type_method": "cmp_inspired_point_in_time_heuristic"})})
                n += 1
    return n
