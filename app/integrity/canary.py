"""Source canaries: catch SILENT failures (a source that "succeeds" but stops producing data or starts
misparsing after a format change). Idea from LuxAlgo/market-trackers (MIT) per-source canaries
(freshness + parse success rate); own implementation, no code copied.

Each canary reads the data this system actually stored, not the HTTP status:
  freshness - newest stored record vs an expected cadence (business days for market sources)
  parse     - share of recent filings whose parser failed
Status OK / WARN / FAIL is written to source_health as "canary:<name>"; a FAIL alerts once per day.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from app.db.store import health_mark

log = logging.getLogger(__name__)


def business_days_between(a: date, b: date) -> int:
    """Weekdays strictly after a up to and including b."""
    if b <= a:
        return 0
    n, d = 0, a
    while d < b:
        d += timedelta(days=1)
        n += d.weekday() < 5
    return n


@dataclass(frozen=True)
class Freshness:
    name: str
    sql: str  # returns the newest date/timestamp of stored data
    warn_bd: int  # business days without new data before WARN
    fail_bd: int


FRESHNESS = [
    Freshness("sec_form4", "SELECT max(filed_date) FROM insider_transactions WHERE source = 'live'", 2, 4),
    Freshness("sec_13d", "SELECT max(filed_date) FROM activist_filings", 4, 8),
    Freshness("news_sec_8k", "SELECT max(first_seen_at)::date FROM news_items WHERE source = 'sec_8k'", 2, 4),
    Freshness("congress_house", "SELECT max(filing_date) FROM congress_transactions WHERE chamber = 'house'", 10, 20),
    Freshness("usaspending", "SELECT max(first_seen_date) FROM government_awards", 4, 8),
]
PARSE_SQL = """SELECT count(*) FILTER (WHERE parse_status = 'error') err, count(*) n
               FROM filings WHERE source = 'sec' AND created_at > now() - interval '7 days'"""
PARSE_WARN, PARSE_FAIL = 0.02, 0.10


def evaluate(conn: Connection, today: date | None = None) -> list[dict]:
    today = today or datetime.now(timezone.utc).date()
    out = []
    for f in FRESHNESS:
        newest = conn.execute(text(f.sql)).scalar()
        if newest is None:
            out.append({"name": f.name, "status": "FAIL", "detail": "no stored data"})
            continue
        newest_d = newest.date() if isinstance(newest, datetime) else newest
        gap = business_days_between(newest_d, today)
        st = "FAIL" if gap >= f.fail_bd else "WARN" if gap >= f.warn_bd else "OK"
        out.append({"name": f.name, "status": st, "detail": f"newest {newest_d}, {gap} business days ago"})
    err, n = conn.execute(text(PARSE_SQL)).first()
    rate = (err / n) if n else 0.0
    st = "FAIL" if (n >= 50 and rate >= PARSE_FAIL) else "WARN" if (n >= 50 and rate >= PARSE_WARN) else "OK"
    out.append({"name": "sec_parse_rate", "status": st, "detail": f"{err}/{n} SEC filings failed to parse in 7 days ({rate:.1%})"})
    return out


def run(engine: Engine) -> list[dict]:
    from app.notifications.dispatch import send_system
    with engine.begin() as conn:
        res = evaluate(conn)
        for r in res:
            health_mark(conn, f"canary:{r['name']}", {"OK": "WORKING", "WARN": "DEGRADED", "FAIL": "ERROR"}[r["status"]], r["detail"])
    for r in res:
        if r["status"] == "FAIL":
            try:
                send_system(engine, f"canary:{r['name']}:{date.today()}",
                            f"⚠️ <b>PUBLIC EDGE veri kanaryası</b>\nKaynak: {r['name']}\nDurum: veri gelmiyor veya bozuk\n{r['detail']}")
            except Exception as e:  # retried on the next run
                log.warning("canary alert failed: %s", e)
    return res
