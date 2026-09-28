"""Per-source health: HEALTHY | STALE | DEGRADED | BLOCKED | SCHEMA_CHANGED | AUTH_REQUIRED (+ DISABLED / UNKNOWN).

Every run is recorded twice: the rolling state in source_health and one row per UTC day in source_daily_status, so
"the source failed" (SOURCE_UNAVAILABLE / API_QUOTA / AUTH_REQUIRED / LICENSE_BLOCK) is never confused with
"the source answered and there was nothing" (TRUE_NO_EVENT)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.engine import Connection

HEALTH = ("HEALTHY", "STALE", "DEGRADED", "BLOCKED", "SCHEMA_CHANGED", "AUTH_REQUIRED", "DISABLED", "UNKNOWN")
# failure kinds a run can end with
FAILURE_KINDS = ("ERROR", "BLOCKED", "AUTH_REQUIRED", "API_QUOTA", "LICENSE_BLOCK", "SCHEMA_CHANGED")


@dataclass
class RunOutcome:
    ok: bool
    items: int = 0
    events: int = 0
    failure_kind: str | None = None  # one of FAILURE_KINDS when not ok
    error: str | None = None
    response_status: int | None = None
    latency_ms: int | None = None
    fingerprint: str | None = None
    parser_version: str | None = None
    watermark: dict | None = None


def classify_exception(e: BaseException) -> tuple[str, int | None]:
    """Map an exception to (failure_kind, http status)."""
    from app.core.ratelimit import QuotaExhausted
    if isinstance(e, QuotaExhausted):
        return "API_QUOTA", None
    status = getattr(e, "status", None)
    if status is None and hasattr(e, "response") and getattr(e, "response", None) is not None:
        status = getattr(e.response, "status_code", None)
    msg = str(e).lower()
    name = type(e).__name__
    if name in ("FormatChanged", "SchemaChanged"):
        return "SCHEMA_CHANGED", status
    if name in ("SourceBlocked", "KapBlocked"):
        return "BLOCKED", status
    if status in (401,) or "api key" in msg or "unauthorized" in msg:
        return "AUTH_REQUIRED", status
    if status in (403, 666) or "captcha" in msg or "bot" in msg and "manager" in msg or "waf" in msg:
        return "BLOCKED", status
    if status == 429:
        return "API_QUOTA", status
    return "ERROR", status


def health_status(*, failure_kind: str | None, consecutive_failures: int, expected_fp: str | None, fp: str | None,
                  last_success: datetime | None, stale_after: timedelta | None, now: datetime) -> str:
    if failure_kind == "AUTH_REQUIRED":
        return "AUTH_REQUIRED"
    if failure_kind in ("BLOCKED", "LICENSE_BLOCK"):
        return "BLOCKED"
    if failure_kind == "SCHEMA_CHANGED":
        return "SCHEMA_CHANGED"
    if expected_fp and fp and fp != expected_fp:
        return "SCHEMA_CHANGED"
    if consecutive_failures >= 1:
        return "DEGRADED"
    if stale_after is not None and (last_success is None or now - last_success > stale_after):
        return "STALE"
    return "HEALTHY"


def daily_status(runs: int, failures: int, events: int, last_kind: str | None) -> str:
    if events > 0:
        return "OK_EVENTS" if failures == 0 else "PARTIAL"
    if runs > failures:  # at least one successful run answered with nothing new
        return "TRUE_NO_EVENT" if failures == 0 else "PARTIAL"
    return {"API_QUOTA": "API_QUOTA", "AUTH_REQUIRED": "AUTH_REQUIRED", "LICENSE_BLOCK": "LICENSE_BLOCK"}.get(
        last_kind or "", "SOURCE_UNAVAILABLE")


def record_run(conn: Connection, source: str, out: RunOutcome, *, country: str | None = None, dataset: str | None = None,
               license_status: str | None = None, stale_after: timedelta | None = None, now: datetime | None = None) -> str:
    """Persist one run (after the legacy health_success / health_failure bookkeeping). Returns the health status."""
    now = now or datetime.now(timezone.utc)
    row = conn.execute(text("""SELECT consecutive_failures, last_success, expected_fingerprint FROM source_health
                               WHERE source = :s"""), {"s": source}).mappings().first()
    fails = (row["consecutive_failures"] if row else 0) if not out.ok else 0
    last_success = now if out.ok else (row["last_success"] if row else None)
    expected = (row["expected_fingerprint"] if row else None) or (out.fingerprint if out.ok else None)
    hs = health_status(failure_kind=None if out.ok else out.failure_kind, consecutive_failures=fails, expected_fp=expected,
                       fp=out.fingerprint, last_success=last_success, stale_after=stale_after, now=now)
    import json
    conn.execute(text("""
        INSERT INTO source_health (source, health_status, country, dataset, license_status, response_status, latency_ms,
                                   schema_fingerprint, expected_fingerprint, parser_version, watermark)
        VALUES (:s, :hs, :c, :d, :l, :rs, :lat, :fp, :efp, :pv, CAST(:wm AS jsonb))
        ON CONFLICT (source) DO UPDATE SET health_status = :hs,
          country = COALESCE(:c, source_health.country), dataset = COALESCE(:d, source_health.dataset),
          license_status = COALESCE(:l, source_health.license_status), response_status = :rs, latency_ms = :lat,
          schema_fingerprint = COALESCE(:fp, source_health.schema_fingerprint),
          expected_fingerprint = COALESCE(source_health.expected_fingerprint, :efp),
          parser_version = COALESCE(:pv, source_health.parser_version),
          watermark = COALESCE(CAST(:wm AS jsonb), source_health.watermark)"""),
                 {"s": source, "hs": hs, "c": country, "d": dataset, "l": license_status, "rs": out.response_status,
                  "lat": out.latency_ms, "fp": out.fingerprint, "efp": expected, "pv": out.parser_version,
                  "wm": json.dumps(out.watermark, default=str) if out.watermark is not None else None})
    day = now.date()
    r = conn.execute(text("""
        INSERT INTO source_daily_status (source, day, runs, failures, items, events, status)
        VALUES (:s, :d, 1, :f, :i, :e, 'PENDING')
        ON CONFLICT (source, day) DO UPDATE SET runs = source_daily_status.runs + 1,
          failures = source_daily_status.failures + :f, items = source_daily_status.items + :i,
          events = source_daily_status.events + :e
        RETURNING runs, failures, events"""),
                     {"s": source, "d": day, "f": 0 if out.ok else 1, "i": out.items, "e": out.events}).first()
    conn.execute(text("UPDATE source_daily_status SET status = :st WHERE source = :s AND day = :d"),
                 {"st": daily_status(r[0], r[1], r[2], None if out.ok else out.failure_kind), "s": source, "d": day})
    return hs


def accept_fingerprint(conn: Connection, source: str) -> None:
    """Operator reviewed a changed format: the current fingerprint becomes the baseline."""
    conn.execute(text("""UPDATE source_health SET expected_fingerprint = schema_fingerprint, health_status = 'HEALTHY'
                         WHERE source = :s"""), {"s": source})


def refresh_staleness(conn: Connection, stale_after: dict[str, timedelta], now: datetime | None = None) -> int:
    """Mark sources STALE when they have not succeeded within their expected cadence (worker died, job stuck)."""
    now = now or datetime.now(timezone.utc)
    n = 0
    for src, td in stale_after.items():
        n += conn.execute(text("""UPDATE source_health SET health_status = 'STALE'
                                  WHERE source = :s AND health_status IN ('HEALTHY', 'UNKNOWN')
                                    AND (last_success IS NULL OR last_success < :cut)"""),
                          {"s": src, "cut": now - td}).rowcount or 0
    return n
