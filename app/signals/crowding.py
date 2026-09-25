"""Activist crowding ("wolf pack"): two or more INDEPENDENT activist investors crossing 5% (original
Schedule 13D) in the same issuer within a short window.

Idea from nncl12345/investor-holdings (crowded activist trades); academic basis: Brav, Dasgupta &
Mathews (2022) "Wolf pack activism", Coffee & Palia (2016). Own implementation - no code copied.
Investors of one economic group (joint filers / linked entities in the beneficial-owner graph) count
once, the public date is the filing date of the 13D that completes the pack (no look-ahead).
"""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import and_, select
from sqlalchemy.engine import Connection, Engine

from app.db.models import activist_filings
from app.db.store import create_event
from app.signals.owners import beneficial_roots

WINDOW_DAYS = 60
MIN_INVESTORS = 2
PRIORITY = 6


def crowding_check(conn: Connection, issuer_cik: str | None, as_of: date, *, ticker: str | None,
                   issuer_name: str | None = None, is_backfill: bool = False) -> int | None:
    if not issuer_cik or not ticker:
        return None
    rows = conn.execute(select(activist_filings.c.investor_cik, activist_filings.c.investor_name, activist_filings.c.filed_date,
                               activist_filings.c.percent, activist_filings.c.item4_class, activist_filings.c.accession,
                               activist_filings.c.source_url).where(and_(
        activist_filings.c.issuer_cik == issuer_cik, activist_filings.c.is_13d.is_(True),
        activist_filings.c.is_amendment.is_(False), activist_filings.c.investor_cik.isnot(None),
        activist_filings.c.filed_date > as_of - timedelta(days=WINDOW_DAYS), activist_filings.c.filed_date <= as_of))
        .order_by(activist_filings.c.filed_date)).all()
    if not rows:
        return None
    roots = beneficial_roots(conn, {r.investor_cik for r in rows})
    groups: dict[str, list] = {}
    for r in rows:
        groups.setdefault(roots[r.investor_cik], []).append(r)
    if len(groups) < MIN_INVESTORS:
        return None
    first = min(r.filed_date for r in rows)
    members = [{"investor": g[0].investor_name, "investor_cik": g[0].investor_cik, "filed": str(g[0].filed_date),
                "percent": g[0].percent, "item4_class": g[0].item4_class} for g in groups.values()]
    last = max(rows, key=lambda r: r.filed_date)
    return create_event(
        conn, event_type="ACTIVIST_CROWDING", dedupe_key=f"CROWD:{issuer_cik}:{first}:{len(groups)}", public_date=as_of,
        ticker=ticker, company_name=issuer_name, issuer_cik=issuer_cik, priority=PRIORITY,
        title=f"${ticker}: {len(groups)} independent activists filed 13D within {WINDOW_DAYS}d",
        payload={"n_investors": len(groups), "window_days": WINDOW_DAYS, "first_filed": str(first), "investors": members},
        source_url=last.source_url, is_backfill=is_backfill)


def backfill_crowding(engine: Engine) -> dict:
    """Replay the stored 13D history in filing order (each check sees only filings up to that date)."""
    with engine.connect() as conn:
        pairs = conn.execute(select(activist_filings.c.issuer_cik, activist_filings.c.filed_date, activist_filings.c.ticker,
                                    activist_filings.c.issuer_name).where(and_(
            activist_filings.c.is_13d.is_(True), activist_filings.c.is_amendment.is_(False),
            activist_filings.c.issuer_cik.isnot(None))).distinct().order_by(activist_filings.c.filed_date)).all()
    n = 0
    for cik, d, ticker, name in pairs:
        with engine.begin() as conn:
            n += crowding_check(conn, cik, d, ticker=ticker, issuer_name=name, is_backfill=True) is not None
    return {"checked": len(pairs), "events": n}
