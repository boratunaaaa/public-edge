"""One-off repair for the 0013 return-queue / 13F identity fixes. DRY-RUN BY DEFAULT.

Every item selects its rows deterministically from the database alone, so a dry run and a later --apply see the same
population (modulo events that changed in between). Items:

  partial_next_at   partial events without returns_next_at -> the next missing horizon's end (no price request)
  vendor_retry      events stuck in `retry` on a delisted-vendor error (incl. the "string indices" parser bug)
                    -> vendor_wait, priced by the vendor lane
  identity_13f      13F events re-resolved with the 9-character CUSIP + name check:
                      SAME       unchanged
                      REVIEW     LOW identity (prefix-only CUSIP / name contradiction) -> needs_review, wrong returns
                                 removed, parked as no_data IDENTITY_REVIEW (kept for audit, never validated)
                      CHANGED    a different issuer -> issuer_cik corrected, returns removed, re-queued
                      UNRESOLVED no issuer any more -> as REVIEW
  ticker_13f        13F events with ticker NULL -> display ticker of the same CIK (never used for prices)
  ca_grants_health  source_health still HEALTHY without any recorded run -> DEGRADED (initial load incomplete)
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from app.backtest.jobs import next_eligible_at

ITEMS = ("partial_next_at", "vendor_retry", "identity_13f", "ticker_13f", "ca_grants_health")
REVIEW_PARK_DAYS = 365  # informational only: needs_review keeps an event out of every return lane permanently
VENDOR_ERR = "returns_status = 'retry' AND returns_error LIKE '%delisted vendor%'"


def _has_next_at(conn: Connection) -> bool:
    """Dry runs also work before migration 0013 (every partial event then counts as unscheduled)."""
    return bool(conn.execute(text("""SELECT 1 FROM information_schema.columns WHERE table_name = 'events'
                                     AND column_name = 'returns_next_at'""")).first())


def _partial_next_at(conn: Connection, apply: bool, now: datetime) -> dict:
    unscheduled = "AND e.returns_next_at IS NULL" if _has_next_at(conn) else ""
    rows = conn.execute(text(f"""
        SELECT e.id, min(r.entry_date) entry, array_agg(r.horizon_days) hs FROM events e
        JOIN event_returns r ON r.event_id = e.id
        WHERE e.returns_status = 'partial' {unscheduled} AND NOT e.superseded
        GROUP BY e.id""")).all()
    today = now.date()
    due_now = later = 0
    upd = []
    for eid, entry, hs in rows:
        nxt = next_eligible_at(entry, set(hs), today, now)
        if nxt is None or nxt <= now:
            due_now += 1
        else:
            later += 1
        upd.append({"i": eid, "n": nxt})
    if apply:
        for i in range(0, len(upd), 5000):
            conn.execute(text("UPDATE events SET returns_next_at = :n WHERE id = :i"), upd[i:i + 5000])
    return {"rows": len(rows), "due_now": due_now, "deferred_to_next_horizon": later}


def _vendor_retry(conn: Connection, apply: bool, now: datetime) -> dict:
    by = dict(conn.execute(text(f"""
        SELECT CASE WHEN returns_error LIKE '%string indices must be integers%' THEN 'parser_bug'
                    WHEN returns_error LIKE '%404%' THEN 'vendor_404' ELSE 'other_vendor_error' END k, count(*)
        FROM events WHERE {VENDOR_ERR} GROUP BY 1""")).all())
    if apply:
        conn.execute(text(f"""UPDATE events SET returns_status = 'vendor_wait', returns_next_at = NULL,
                              returns_error = 'VENDOR_QUEUED (0013 repair)' WHERE {VENDOR_ERR}"""))
    return {"rows": sum(by.values()), **by}


def _identity_13f(conn: Connection, apply: bool, now: datetime, sample: int = 12) -> dict:
    from app.markets.identity import cusip_to_cik
    rows = conn.execute(text("""
        SELECT id, issuer_cik, identity_confidence, payload->>'cusip' cusip, company_name, returns_status,
               EXISTS (SELECT 1 FROM event_returns r WHERE r.event_id = e.id) has_ret
        FROM events e WHERE event_type LIKE 'INST_13F%' AND NOT superseded AND NOT needs_review""")).all()
    cache: dict[tuple, tuple] = {}
    cnt: Counter = Counter()
    priced: Counter = Counter()
    examples: dict[str, list] = {}
    changes = []
    for eid, cik, conf, cusip, name, status, has_ret in rows:
        k = (cusip, name)
        if k not in cache:
            cache[k] = cusip_to_cik(conn, cusip, name)
        ncik, nconf, why = cache[k]
        if ncik is None:
            kind = "UNRESOLVED"
        elif nconf == "LOW":
            kind = "REVIEW"
        elif ncik != cik:
            kind = "CHANGED"
        else:
            kind = "SAME"
        cnt[kind] += 1
        if has_ret:
            priced[kind] += 1
        if kind != "SAME":
            changes.append((eid, kind, ncik, nconf, why))
            ex = examples.setdefault(kind, [])
            if len(ex) < sample and not any(e["cusip"] == cusip for e in ex):
                ex.append({"cusip": cusip, "issuer_name": name, "old_cik": cik, "new_cik": ncik, "why": why})
    if apply:
        from app.entities.registry import resolver
        resolver.load(conn, force=True)
        park = now + timedelta(days=REVIEW_PARK_DAYS)
        for eid, kind, ncik, nconf, why in changes:
            # same lock order as the return worker (_fence): the event row first. A worker that is pricing this event
            # holds the lock until its returns are committed; we wait here, THEN delete - so nothing it wrote survives.
            conn.execute(text("SELECT 1 FROM events WHERE id = :i FOR UPDATE"), {"i": eid})
            conn.execute(text("DELETE FROM event_returns WHERE event_id = :i"), {"i": eid})
            if kind == "CHANGED":
                # the display ticker follows the corrected issuer (never the old company's symbol); NULL when the new
                # issuer has no current ticker - prices resolve by CIK either way
                conn.execute(text("""UPDATE events SET issuer_cik = :c, issuer_id = :c, identity_confidence = :cf,
                                     ticker = :t, returns_status = 'pending', returns_checked_at = NULL,
                                     returns_next_at = NULL, price_symbol = NULL, price_symbol_basis = NULL,
                                     missing_reason = NULL, payload = payload || CAST(:p AS jsonb) WHERE id = :i"""),
                             {"c": ncik, "cf": nconf, "i": eid, "t": resolver.ticker_for_cik(ncik),
                              "p": json.dumps({"identity_repair": why})})
            else:
                conn.execute(text("""UPDATE events SET needs_review = true, identity_confidence = 'LOW',
                                     returns_status = 'no_data', missing_reason = 'IDENTITY_REVIEW', returns_next_at = :n,
                                     returns_checked_at = :now, price_symbol = NULL, price_symbol_basis = NULL,
                                     payload = payload || CAST(:p AS jsonb) WHERE id = :i"""),
                             {"i": eid, "n": park, "now": now, "p": json.dumps({"identity_review": why})})
    return {"rows": len(rows), "by_kind": dict(cnt), "with_returns_by_kind": dict(priced),
            "stale_or_wrong_returns": sum(v for k, v in priced.items() if k != "SAME"), "examples": examples}


def _ticker_13f(conn: Connection, apply: bool, now: datetime) -> dict:
    from app.entities.registry import resolver
    resolver.load(conn, force=True)
    rows = conn.execute(text("""SELECT id, issuer_cik FROM events WHERE event_type LIKE 'INST_13F%' AND ticker IS NULL
                                AND issuer_cik IS NOT NULL AND NOT superseded""")).all()
    upd = [{"i": eid, "t": t} for eid, cik in rows if (t := resolver.ticker_for_cik(cik))]
    if apply:
        for i in range(0, len(upd), 5000):
            conn.execute(text("UPDATE events SET ticker = :t WHERE id = :i"), upd[i:i + 5000])
    return {"rows": len(rows), "would_fill": len(upd), "no_current_ticker": len(rows) - len(upd)}


def _ca_grants_health(conn: Connection, apply: bool, now: datetime) -> dict:
    row = conn.execute(text("""SELECT health_status, last_run, last_error_at, consecutive_failures FROM source_health
                               WHERE source = 'ca_grants'""")).mappings().first()
    initial = conn.execute(text("SELECT state FROM processing_jobs WHERE name = 'v2_ca_grants_initial'")).scalar()
    complete = bool(initial and dict(initial).get("complete"))
    wrong = bool(row) and row["health_status"] == "HEALTHY" and row["last_run"] is None and not complete
    if apply and wrong:
        conn.execute(text("""UPDATE source_health SET health_status = 'DEGRADED', status = 'DEGRADED',
                             consecutive_failures = GREATEST(consecutive_failures, 1), last_error_at = :now,
                             last_error = 'historical initial load incomplete (0013 repair)' WHERE source = 'ca_grants'"""),
                     {"now": now})
    return {"current": dict(row) if row else None, "initial_load_complete": complete, "would_mark_degraded": wrong}


FUNCS = {"partial_next_at": _partial_next_at, "vendor_retry": _vendor_retry, "identity_13f": _identity_13f,
         "ticker_13f": _ticker_13f, "ca_grants_health": _ca_grants_health}


def run(engine: Engine, items=ITEMS, apply: bool = False, now: datetime | None = None) -> dict:
    """Dry run: one READ ONLY transaction, nothing written. apply=True: one transaction per item."""
    now = now or datetime.now(timezone.utc)
    out = {"mode": "APPLY" if apply else "DRY_RUN"}
    if not apply:
        with engine.connect() as c:
            c.execute(text("SET TRANSACTION READ ONLY"))
            for it in items:
                out[it] = FUNCS[it](c, False, now)
            c.rollback()
        return out
    for it in items:
        with engine.begin() as c:
            out[it] = FUNCS[it](c, True, now)
    return out
