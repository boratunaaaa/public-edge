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

from app.config import settings
from app.db.models import events, notifications
from app.notifications import telegram
from app.notifications.format import format_alert, format_discovery

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


def deliver(engine: Engine, key: str, kind: str, text_: str, event_id: int | None = None,
            photo: bytes | None = None, photo_caption: str = "") -> bool | None:
    """Claim -> send -> mark. Returns True sent, False failed (will retry), None not claimed.
    The optional chart is best-effort: the alert counts as delivered once its text is delivered."""
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
    if photo:
        try:
            telegram.send_photo(photo, photo_caption)
        except Exception as ex:
            log.warning("chart send failed (%s): %s", key, ex)
    return True


def _sent_last_hour(conn) -> int:
    return conn.execute(select(func.count()).select_from(notifications).where(and_(
        notifications.c.kind == "event", notifications.c.status == "sent",
        notifications.c.sent_at > _now() - timedelta(hours=1)))).scalar_one()



THESIS_TR = {"THESIS_STRENGTHENING": "şirket hakkındaki olumlu görüş güçlendi", "THESIS_WEAKENING": "şirket hakkındaki görüş zayıfladı",
             "THESIS_INVALIDATION": "şirket hakkındaki görüş ciddi şekilde bozuldu", "NEUTRAL": "görüşte değişiklik yok"}
NO_CHART = {"TRUMP_FAMILY_SEC_EVENT", "TRUMP_OGE_DISCLOSURE"}


def build_message(engine: Engine, ev: dict) -> tuple[str, bytes | None]:
    """Turkish, plain-language alert text + optional price chart."""
    from app.backtest.probability import success_rate
    from app.db.models import discovery_risks, thesis_changes, universe_snapshots
    from app.notifications.chart import price_chart
    from app.notifications.format import one_line
    p = ev.get("payload") or {}
    with engine.begin() as qconn:  # the quality verdict is cached (metrics_quality) for 6 hours
        rate = success_rate(qconn, ev["event_type"], p, exclude_event_id=ev["id"], persist=True)
    with engine.connect() as conn:
        risks = [dict(r) for r in conn.execute(select(discovery_risks).where(discovery_risks.c.cik == ev.get("issuer_cik"))
                                               ).mappings().all()] if ev.get("issuer_cik") else []
        uni = conn.execute(select(universe_snapshots.c.market_cap, universe_snapshots.c.bucket).where(
            universe_snapshots.c.cik == ev.get("issuer_cik")).order_by(universe_snapshots.c.as_of.desc()).limit(1)).first() \
            if ev.get("issuer_cik") else None
        rel_rows = conn.execute(select(events).where(and_(
            events.c.ticker == ev["ticker"], events.c.id != ev["id"], events.c.is_backfill.is_(False),
            events.c.superseded.is_(False),
            events.c.public_date >= ev["public_date"] - timedelta(days=60)))
            .order_by(events.c.public_date.desc()).limit(4)).mappings().all() if ev.get("ticker") else []
        related = [f"{one_line(dict(r))} ({(ev['public_date'] - r['public_date']).days} gün önce)" for r in rel_rows]
        thesis = None
        if p.get("cluster_id"):
            tc = conn.execute(select(thesis_changes.c.change).where(thesis_changes.c.news_cluster_id == p["cluster_id"])).first()
            thesis = THESIS_TR.get(tc[0]) if tc else None
        chart = None
        if ev.get("ticker") and ev["event_type"] not in NO_CHART:  # also loads/stores the prices used below
            chart = price_chart(conn, ev["ticker"], ev["public_date"], f"{ev['ticker']} — son 6 ay fiyat (turuncu çizgi: olay günü)")
        price_ctx = _price_context(conn, ev.get("ticker"), ev["public_date"])
    mcap, bucket = (uni[0], uni[1]) if uni else (p.get("market_cap"), p.get("bucket") or p.get("mcap_bucket"))
    if ev["event_type"] == "EARLY_DISCOVERY":
        srcs = ["SEC resmi belgeleri"] + (["Form 4 (yönetici alımları)"] if p.get("why") and any("insider" in w for w in p["why"]) else [])
        return format_discovery(ev, risks, srcs + [ev.get("source_url") or ""], rate=rate), chart
    return format_alert(ev, rate=rate, risks=risks, related=related, thesis=thesis, market_cap=mcap, bucket=bucket,
                        price_ctx=price_ctx), chart


def _price_context(conn, ticker: str | None, day) -> dict | None:
    """3-month change and position inside the 1-year range, from stored prices on/before the event day."""
    if not ticker:
        return None
    from app.marketdata.provider import load_bars
    bars = [b for b in load_bars(conn, ticker, day - timedelta(days=370), day) if b.close]
    if len(bars) < 60:
        return None
    last = bars[-1].adj_close or bars[-1].close
    past = [b for b in bars if b.date <= day - timedelta(days=90)]
    base = (past[-1].adj_close or past[-1].close) if past else None
    closes = [b.adj_close or b.close for b in bars]
    lo, hi = min(closes), max(closes)
    return {"change_3m": (last / base - 1) if base else None, "range_pos": (last - lo) / (hi - lo) if hi > lo else None}


def _candidates(engine: Engine) -> list:
    now = _now()
    n = notifications.alias("n")
    deliverable = or_(
        n.c.id.is_(None),
        and_(n.c.status == "failed", n.c.next_retry_at <= now, n.c.attempts < MAX_ATTEMPTS),
        and_(n.c.status == "sending", n.c.claimed_at < now - CLAIM_TTL),
    )
    q = (select(events).select_from(events.outerjoin(n, n.c.dedupe_key == func.concat("event:", events.c.id)))
         .where(and_(events.c.is_backfill.is_(False), events.c.superseded.is_(False),
                     events.c.priority >= settings.alert_min_priority,
                     events.c.public_date >= date.today() - timedelta(days=MAX_AGE_DAYS), deliverable))
         .order_by(events.c.priority.desc(), events.c.id).limit(50))
    with engine.connect() as conn:
        return conn.execute(q).mappings().all()


def dispatch(engine: Engine) -> dict:
    if not telegram.configured():
        return {"skipped": "telegram not configured"}
    sent = deferred = failed = merged_n = 0
    cands = _candidates(engine)
    # One message per ticker per dispatch: the highest-priority event is sent; the others are marked
    # 'merged' and still appear in that message's "Aynı şirkette son zamanlarda" section.
    top_for_ticker: dict[str, int] = {}
    for ev in cands:  # priority desc
        tk = ev["ticker"]
        if tk and tk in top_for_ticker:
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
        msg, chart = build_message(engine, dict(ev))
        r = deliver(engine, f"event:{ev['id']}", "event", msg, ev["id"], photo=chart,
                    photo_caption=f"{ev['ticker']} fiyat grafiği" if ev.get("ticker") else "")
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
