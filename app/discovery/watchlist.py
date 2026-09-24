"""Watchlist with automatic promotion/demotion (reason always recorded) and manual override."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection

from app.db.models import watchlist, watchlist_history

CATEGORIES = ("DISCOVERY", "HIGH_INTEREST", "MONITOR", "THESIS_WEAKENING", "ARCHIVED")


def set_category(conn: Connection, cik: str, ticker: str | None, category: str, reason: str, *, actor: str = "auto",
                 manual_override: bool | None = None) -> bool:
    if category not in CATEGORIES:
        raise ValueError(category)
    row = conn.execute(select(watchlist).where(watchlist.c.cik == cik)).mappings().first()
    if row and actor == "auto" and row["manual_override"]:
        return False  # a human decision is never overwritten automatically
    if row and row["category"] == category and actor == "auto":
        return False
    now = datetime.now(timezone.utc)
    vals = {"ticker": ticker, "category": category, "reason": reason[:1000], "updated_at": now}
    if manual_override is not None:
        vals["manual_override"] = manual_override
    conn.execute(pg_insert(watchlist).values(cik=cik, **vals).on_conflict_do_update(index_elements=["cik"], set_=vals))
    conn.execute(watchlist_history.insert().values(cik=cik, from_category=row["category"] if row else None, to_category=category,
                                                   reason=reason[:1000], actor=actor))
    return True


def auto_update(conn: Connection, cik: str, ticker: str | None, *, level: str, candidate: bool, why: list[str],
                bucket: str | None) -> str | None:
    row = conn.execute(select(watchlist).where(watchlist.c.cik == cik)).mappings().first()
    cur = row["category"] if row else None
    reason = "; ".join(why)[:500] or "discovery scan"
    if bucket == "ALREADY_SCALED":
        target = "MONITOR" if cur else None
        reason = f"already scaled (market cap >= $10B); {reason}"
    elif level == "HIGH" and candidate:
        target = "HIGH_INTEREST"
    elif level in ("HIGH", "MEDIUM") and candidate:
        target = "DISCOVERY" if cur in (None, "ARCHIVED", "MONITOR", "DISCOVERY") else cur
    elif cur in ("HIGH_INTEREST", "DISCOVERY"):
        target = "MONITOR"
        reason = f"interest fell to {level}; {reason}"
    elif cur == "MONITOR" and row and row["updated_at"] and row["updated_at"] < datetime.now(timezone.utc) - timedelta(days=60):
        target = "ARCHIVED"
        reason = "no renewed interest for 60 days"
    else:
        target = cur
    if cur == "THESIS_WEAKENING" and target in ("DISCOVERY", "HIGH_INTEREST"):
        target = cur  # needs new evidence after a weakening event, not just a re-score
    if target and target != cur:
        set_category(conn, cik, ticker, target, reason, actor="auto")
    return target
