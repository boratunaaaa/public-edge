"""Cross-process rate limits and quotas (worker / backfill / intelligence share ONE budget per key).

acquire(key) reserves the next slot atomically in Postgres (single UPDATE ... RETURNING), then sleeps until it.
Daily / hourly quotas are enforced in the same statement; an exhausted quota raises QuotaExhausted so the caller
records API_QUOTA instead of an empty result. Without a database (unit tests, scripts) it degrades to a
process-local limiter."""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

from sqlalchemy import text

log = logging.getLogger(__name__)

# key -> (min interval seconds, daily limit, hourly limit). Budgets are shared by every container.
DEFAULTS: dict[str, tuple[float, int | None, int | None]] = {
    "sec": (1 / 8.0, None, None),  # SEC fair access: <= 10 req/s for the whole host -> 8 shared
    "tiingo": (72.0, 1000, 50),  # free tier: 50/hour, 1000/day (and 500 unique symbols/month, tracked elsewhere)
    "alphavantage": (15.0, 25, None),
    "yahoo": (0.5, None, None),
    "usaspending": (0.5, None, None),
    "finra": (0.5, None, None),
    "clinicaltrials": (0.5, None, None),
    "openfda": (0.3, 1000, None),  # no key: 240/min, 1000/day
    "lda": (1.0, None, 120),  # Senate LDA anonymous
    "openfigi": (2.5, None, 25 * 60),  # no key: 25 requests / minute
    "cftc": (0.5, None, None),
    "canada_open": (1.0, None, None),
    "boc_valet": (1.0, None, None),
    "ciro": (2.0, None, None),
    "kap": (6.0, None, 550),  # KAP: 429 above ~0.5 req/s; hourly cap keeps a live scan well below that
}


class QuotaExhausted(RuntimeError):
    pass


_local: dict[str, float] = {}
_local_lock = threading.Lock()
_engine = None


def _get_engine():
    global _engine
    if _engine is None:
        from app.db.session import get_engine
        _engine = get_engine()
    return _engine


def ensure(conn, key: str) -> None:
    iv, day, hour = DEFAULTS.get(key, (1.0, None, None))
    conn.execute(text("""INSERT INTO rate_limits (key, min_interval_s, daily_limit, hourly_limit) VALUES (:k, :i, :d, :h)
                         ON CONFLICT (key) DO NOTHING"""), {"k": key, "i": iv, "d": day, "h": hour})


SQL = text("""
UPDATE rate_limits SET
  used_today = CASE WHEN day = CURRENT_DATE THEN used_today + 1 ELSE 1 END,
  day = CURRENT_DATE,
  used_hour = CASE WHEN hour = date_trunc('hour', clock_timestamp()) THEN used_hour + 1 ELSE 1 END,
  hour = date_trunc('hour', clock_timestamp()),
  next_at = GREATEST(next_at, clock_timestamp()) + make_interval(secs => min_interval_s)
WHERE key = :k
  AND (daily_limit IS NULL OR day IS DISTINCT FROM CURRENT_DATE OR used_today < daily_limit)
  AND (hourly_limit IS NULL OR hour IS DISTINCT FROM date_trunc('hour', clock_timestamp()) OR used_hour < hourly_limit)
RETURNING EXTRACT(EPOCH FROM (next_at - make_interval(secs => min_interval_s) - clock_timestamp()))
""")


def acquire(key: str, conn=None, block: bool = True) -> float:
    """Reserve one request for `key`; returns the seconds waited. Raises QuotaExhausted."""
    try:
        if conn is None:
            with _get_engine().begin() as c:
                wait = _reserve(c, key)
        else:
            wait = _reserve(conn, key)
    except QuotaExhausted:
        raise
    except Exception as e:  # noqa: BLE001 - DB unavailable: local fallback, never block ingestion on the limiter
        log.debug("rate limiter DB unavailable (%s) - local fallback", e)
        iv = DEFAULTS.get(key, (1.0, None, None))[0]
        with _local_lock:
            now = time.monotonic()
            nxt = max(_local.get(key, now), now)
            _local[key] = nxt + iv
            wait = nxt - now
    if wait > 0 and block:
        time.sleep(wait)
    return max(wait, 0.0)


def _reserve(conn, key: str) -> float:
    row = conn.execute(SQL, {"k": key}).first()
    if row is None:
        exists = conn.execute(text("SELECT 1 FROM rate_limits WHERE key = :k"), {"k": key}).first()
        if not exists:
            ensure(conn, key)
            row = conn.execute(SQL, {"k": key}).first()
        if row is None:
            raise QuotaExhausted(key)
    return float(row[0] or 0.0)


def record_429(key: str, conn=None, backoff_s: float = 60.0) -> None:
    """A 429 pushes the shared next slot out for everyone."""
    stmt = text("""UPDATE rate_limits SET last_429_at = now(), n_429 = n_429 + 1,
                   next_at = GREATEST(next_at, now() + make_interval(secs => :b)) WHERE key = :k""")
    try:
        if conn is None:
            with _get_engine().begin() as c:
                c.execute(stmt, {"k": key, "b": backoff_s})
        else:
            conn.execute(stmt, {"k": key, "b": backoff_s})
    except Exception:  # noqa: BLE001
        log.debug("record_429 without DB")


def status(conn) -> list[dict]:
    rows = conn.execute(text("""SELECT key, min_interval_s, daily_limit, used_today, day, hourly_limit, used_hour,
                                       last_429_at, n_429, next_at FROM rate_limits ORDER BY key""")).mappings().all()
    today = datetime.now(timezone.utc).date()
    out = []
    for r in rows:
        d = dict(r)
        d["remaining_today"] = (r["daily_limit"] - (r["used_today"] if r["day"] == today else 0)) if r["daily_limit"] else None
        out.append(d)
    return out
