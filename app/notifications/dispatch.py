"""Decide which events deserve a Telegram message and send them (dedup + rate limit)."""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import and_, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine

from app.backtest.jobs import event_type_stats, subgroup_for
from app.config import settings
from app.db.models import events, notifications
from app.notifications import telegram
from app.notifications.format import format_event, money

log = logging.getLogger(__name__)
MAX_AGE_DAYS = 2


def _record(conn, key: str, kind: str, status: str, event_id: int | None = None, error: str | None = None) -> bool:
    """True only if this call created the row (psycopg3 reports rowcount=-1 for ON CONFLICT, so use RETURNING)."""
    res = conn.execute(pg_insert(notifications).values(
        dedupe_key=key, event_id=event_id, kind=kind, status=status, error=error,
        sent_at=datetime.now(timezone.utc) if status == "sent" else None,
    ).on_conflict_do_nothing(index_elements=["dedupe_key"]).returning(notifications.c.id))
    return res.first() is not None


def _sent_last_hour(conn) -> int:
    return conn.execute(select(func.count()).select_from(notifications).where(and_(
        notifications.c.kind == "event", notifications.c.status == "sent",
        notifications.c.sent_at > datetime.now(timezone.utc) - timedelta(hours=1)))).scalar_one()


def _extra_signals(conn, ev) -> list[str]:
    if not ev["ticker"]:
        return []
    rows = conn.execute(select(events.c.event_type, events.c.title, events.c.payload).where(and_(
        events.c.ticker == ev["ticker"], events.c.id != ev["id"], events.c.event_type != "MULTI_SIGNAL_EVENT",
        events.c.public_date >= ev["public_date"] - timedelta(days=30), events.c.public_date <= ev["public_date"],
    )).limit(5)).all()
    out = []
    for et, title, p in rows:
        if et == "GOV_CONTRACT":
            out.append(f"Government contract: {money(p.get('amount'))} ({(p.get('value_to_mcap') or 0) * 100:.1f}% of mcap)")
        else:
            out.append(title[:120])
    return out


def dispatch(engine: Engine) -> dict:
    if not telegram.configured():
        return {"skipped": "telegram not configured"}
    sent = suppressed = failed = 0
    with engine.connect() as conn:
        cands = conn.execute(select(events).where(and_(
            events.c.is_backfill.is_(False), events.c.priority >= settings.alert_min_priority,
            events.c.public_date >= date.today() - timedelta(days=MAX_AGE_DAYS),
            ~select(notifications.c.id).where(notifications.c.event_id == events.c.id).exists(),
        )).order_by(events.c.priority.desc(), events.c.id).limit(50)).mappings().all()
    # One message per ticker per dispatch: the highest-priority event is sent, the related ones
    # (e.g. single buy + the cluster it completed) are folded into it as "Ek sinyaller".
    merged: dict[int, list[str]] = {}
    top_for_ticker: dict[str, int] = {}
    for ev in cands:  # already ordered by priority desc
        tk = ev["ticker"]
        if tk and tk in top_for_ticker:
            merged.setdefault(top_for_ticker[tk], []).append(ev["title"][:120])
            with engine.begin() as conn:
                _record(conn, f"event:{ev['id']}", "event", "merged", ev["id"], f"merged into event {top_for_ticker[tk]}")
            continue
        if tk:
            top_for_ticker[tk] = ev["id"]
    for ev in cands:
        key = f"event:{ev['id']}"
        if ev["ticker"] and top_for_ticker.get(ev["ticker"]) != ev["id"]:
            continue
        with engine.begin() as conn:
            if _sent_last_hour(conn) >= settings.max_alerts_per_hour:
                _record(conn, key, "event", "suppressed", ev["id"], "hourly alert cap")
                suppressed += 1
                continue
            sg = subgroup_for(ev["event_type"], ev["payload"] or {})
            try:
                stats = event_type_stats(conn, ev["event_type"], sg, horizons=(30, 90))
            except Exception:
                stats = None
            mcap = (ev["payload"] or {}).get("market_cap")
            extra = merged.get(ev["id"], []) + [x for x in _extra_signals(conn, ev) if x not in merged.get(ev["id"], [])]
            msg = format_event(dict(ev), stats, f"{ev['event_type']}/{sg}", extra[:6], mcap)
            if not _record(conn, key, "event", "sent", ev["id"]):
                continue  # another worker got it
        try:
            telegram.send(msg)
            sent += 1
        except Exception as ex:
            failed += 1
            with engine.begin() as conn:
                conn.execute(notifications.update().where(notifications.c.dedupe_key == key).values(status="failed", error=str(ex)[:500]))
            log.warning("telegram send failed: %s", ex)
    return {"sent": sent, "suppressed": suppressed, "failed": failed, "candidates": len(cands)}


def send_system(engine: Engine, key: str, text: str, kind: str = "system") -> bool:
    """Send a one-off system message exactly once per dedupe key."""
    with engine.begin() as conn:
        if not _record(conn, key, kind, "sent"):
            return False
    try:
        telegram.send(text)
        return True
    except Exception as ex:
        with engine.begin() as conn:
            conn.execute(notifications.update().where(notifications.c.dedupe_key == key).values(status="failed", error=str(ex)[:500]))
        raise
