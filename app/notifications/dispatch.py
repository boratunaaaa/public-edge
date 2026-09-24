"""Decide which events deserve a Telegram message and deliver them reliably.

Delivery state machine (notifications.status):
  (none) --claim--> sending --ok--> sent
                    sending --error--> failed (next_retry_at = now + backoff) --claim--> sending ...
                    failed after MAX_ATTEMPTS -> dead
  a 'sending' claim older than CLAIM_TTL (process died mid-send) can be re-claimed.
  merged = folded into another alert of the same ticker (terminal).
Rows are marked 'sent' only AFTER Telegram confirmed delivery.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine

from app.backtest.jobs import event_type_stats, subgroup_for
from app.config import settings
from app.db.models import events, notifications
from app.notifications import telegram
from app.notifications.format import format_event, money

log = logging.getLogger(__name__)
MAX_AGE_DAYS = 2
MAX_ATTEMPTS = 6
CLAIM_TTL = timedelta(minutes=5)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def backoff(attempts: int) -> timedelta:
    return timedelta(minutes=min(60, 2 ** max(0, attempts - 1)))  # 1, 2, 4, 8, 16, 32/60 min


def claim(conn, key: str, kind: str, event_id: int | None = None) -> bool:
    """Atomically take ownership of a delivery. True = caller must send now."""
    now = _now()
    ins = pg_insert(notifications).values(dedupe_key=key, event_id=event_id, kind=kind, status="sending",
                                          attempts=1, claimed_at=now)
    stmt = ins.on_conflict_do_update(
        index_elements=["dedupe_key"],
        set_={"status": "sending", "claimed_at": now, "attempts": notifications.c.attempts + 1, "error": None},
        where=or_(
            and_(notifications.c.status == "failed", notifications.c.next_retry_at <= now,
                 notifications.c.attempts < MAX_ATTEMPTS),
            and_(notifications.c.status == "sending", notifications.c.claimed_at < now - CLAIM_TTL),
        ),
    ).returning(notifications.c.id)
    return conn.execute(stmt).first() is not None


def mark_sent(conn, key: str) -> None:
    conn.execute(notifications.update().where(notifications.c.dedupe_key == key)
                 .values(status="sent", sent_at=_now(), error=None, next_retry_at=None))


def mark_failed(conn, key: str, error: str) -> None:
    row = conn.execute(select(notifications.c.attempts).where(notifications.c.dedupe_key == key)).first()
    attempts = row[0] if row else 1
    status = "dead" if attempts >= MAX_ATTEMPTS else "failed"
    conn.execute(notifications.update().where(notifications.c.dedupe_key == key)
                 .values(status=status, error=error[:500], next_retry_at=_now() + backoff(attempts)))


def mark_terminal(conn, key: str, status: str, event_id: int | None, note: str) -> None:
    conn.execute(pg_insert(notifications).values(dedupe_key=key, event_id=event_id, kind="event", status=status, error=note)
                 .on_conflict_do_nothing(index_elements=["dedupe_key"]))


def deliver(engine: Engine, key: str, kind: str, text_: str, event_id: int | None = None) -> bool | None:
    """Claim -> send -> mark. Returns True sent, False failed (will retry), None not claimed."""
    with engine.begin() as conn:
        if not claim(conn, key, kind, event_id):
            return None
    try:
        telegram.send(text_)
    except Exception as ex:
        with engine.begin() as conn:
            mark_failed(conn, key, str(ex))
        log.warning("telegram send failed (%s): %s", key, ex)
        return False
    with engine.begin() as conn:
        mark_sent(conn, key)
    return True


def _sent_last_hour(conn) -> int:
    return conn.execute(select(func.count()).select_from(notifications).where(and_(
        notifications.c.kind == "event", notifications.c.status == "sent",
        notifications.c.sent_at > _now() - timedelta(hours=1)))).scalar_one()


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


def _candidates(engine: Engine) -> list:
    now = _now()
    n = notifications.alias("n")
    deliverable = or_(
        n.c.id.is_(None),
        and_(n.c.status == "failed", n.c.next_retry_at <= now, n.c.attempts < MAX_ATTEMPTS),
        and_(n.c.status == "sending", n.c.claimed_at < now - CLAIM_TTL),
    )
    q = (select(events).select_from(events.outerjoin(n, n.c.dedupe_key == func.concat("event:", events.c.id)))
         .where(and_(events.c.is_backfill.is_(False), events.c.priority >= settings.alert_min_priority,
                     events.c.public_date >= date.today() - timedelta(days=MAX_AGE_DAYS), deliverable))
         .order_by(events.c.priority.desc(), events.c.id).limit(50))
    with engine.connect() as conn:
        return conn.execute(q).mappings().all()


def dispatch(engine: Engine) -> dict:
    if not telegram.configured():
        return {"skipped": "telegram not configured"}
    sent = deferred = failed = merged_n = 0
    cands = _candidates(engine)
    # One message per ticker per dispatch: highest priority is sent, related ones folded in.
    merged: dict[int, list[str]] = {}
    top_for_ticker: dict[str, int] = {}
    for ev in cands:  # priority desc
        tk = ev["ticker"]
        if tk and tk in top_for_ticker:
            merged.setdefault(top_for_ticker[tk], []).append(ev["title"][:120])
            with engine.begin() as conn:
                mark_terminal(conn, f"event:{ev['id']}", "merged", ev["id"], f"merged into event {top_for_ticker[tk]}")
            merged_n += 1
            continue
        if tk:
            top_for_ticker[tk] = ev["id"]
    for ev in cands:
        if ev["ticker"] and top_for_ticker.get(ev["ticker"]) != ev["id"]:
            continue
        with engine.connect() as conn:
            if _sent_last_hour(conn) >= settings.max_alerts_per_hour:
                deferred += 1  # left untouched: picked up by a later run while still fresh
                continue
            sg = subgroup_for(ev["event_type"], ev["payload"] or {})
            try:
                stats = event_type_stats(conn, ev["event_type"], sg, horizons=(30, 90))
            except Exception:
                stats = None
            extra = merged.get(ev["id"], []) + [x for x in _extra_signals(conn, ev) if x not in merged.get(ev["id"], [])]
        msg = format_event(dict(ev), stats, f"{ev['event_type']}/{sg}", extra[:6], (ev["payload"] or {}).get("market_cap"))
        r = deliver(engine, f"event:{ev['id']}", "event", msg, ev["id"])
        sent += r is True
        failed += r is False
    return {"sent": sent, "deferred": deferred, "failed": failed, "merged": merged_n, "candidates": len(cands)}


def send_system(engine: Engine, key: str, text_: str, kind: str = "system") -> bool:
    """One-off system message, delivered at most once per key; failures are retried on the next call.
    Returns True only when this call delivered it."""
    r = deliver(engine, key, kind, text_)
    if r is False:
        raise RuntimeError(f"telegram delivery failed for {key} (will retry)")
    return bool(r)


def is_delivered(engine: Engine, key: str) -> bool:
    with engine.connect() as conn:
        return conn.execute(text("SELECT 1 FROM notifications WHERE dedupe_key=:k AND status='sent'"), {"k": key}).first() is not None
