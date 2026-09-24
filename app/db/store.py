"""Idempotent persistence helpers."""
from __future__ import annotations

import gzip
import hashlib
import json
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection

from app.db.models import events, filings, processing_jobs, signal_features, source_health


def _json_default(o: Any):
    if isinstance(o, (date, datetime)):
        return o.isoformat()
    return str(o)


def jsonable(d: dict) -> dict:
    return json.loads(json.dumps(d, default=_json_default))


def upsert_filing(conn: Connection, *, source: str, accession: str, form_type: str, url: str, issuer_cik: str | None = None,
                  filed_date: date | None = None, accepted_at: datetime | None = None, raw: bytes | str | None = None) -> tuple[int, bool]:
    """Insert a filing once. Returns (filing_id, is_new)."""
    raw_b = raw.encode("utf-8", "replace") if isinstance(raw, str) else raw
    vals = dict(source=source, accession=accession, form_type=form_type, url=url, issuer_cik=issuer_cik,
                filed_date=filed_date, accepted_at=accepted_at,
                raw_gz=gzip.compress(raw_b, 6) if raw_b else None,
                raw_sha256=hashlib.sha256(raw_b).hexdigest() if raw_b else None)
    res = conn.execute(pg_insert(filings).values(**vals).on_conflict_do_nothing(
        index_elements=["source", "accession"]).returning(filings.c.id)).first()
    if res is not None:
        return res[0], True
    fid = conn.execute(select(filings.c.id).where(filings.c.source == source, filings.c.accession == accession)).scalar_one()
    return fid, False


def filing_exists(conn: Connection, source: str, accession: str) -> bool:
    return conn.execute(select(filings.c.id).where(filings.c.source == source, filings.c.accession == accession)).first() is not None


def mark_filing(conn: Connection, filing_id: int, status: str, error: str | None = None) -> None:
    conn.execute(filings.update().where(filings.c.id == filing_id).values(parse_status=status, parse_error=(error or None) and error[:2000]))


def insert_rows(conn: Connection, table, rows: list[dict], conflict_cols: list[str]) -> int:
    if not rows:
        return 0
    n = 0
    pk = list(table.primary_key.columns)[0]
    for i in range(0, len(rows), 500):
        res = conn.execute(pg_insert(table).values(rows[i:i + 500]).on_conflict_do_nothing(index_elements=conflict_cols)
                           .returning(pk))
        n += len(res.fetchall())  # rowcount is -1 under psycopg3 for ON CONFLICT
    return n


def create_event(conn: Connection, *, event_type: str, dedupe_key: str, public_date: date, title: str, payload: dict,
                 source_url: str, ticker: str | None = None, company_name: str | None = None, issuer_cik: str | None = None,
                 public_at: datetime | None = None, transaction_date: date | None = None, priority: int = 0,
                 is_backfill: bool = False, features: dict | None = None, date_quality: str | None = None) -> int | None:
    """Insert an event exactly once (dedupe_key). Returns the new id or None if it already existed.

    date_quality: exact (timestamp) | date (official date only) | first_seen (our observation) |
    estimated (inferred - excluded from verified statistics and multi-signal)."""
    if date_quality is None:
        date_quality = "exact" if public_at is not None else "date"
    res = conn.execute(pg_insert(events).values(
        event_type=event_type, dedupe_key=dedupe_key[:200], ticker=ticker, company_name=company_name, issuer_cik=issuer_cik,
        public_date=public_date, public_at=public_at, transaction_date=transaction_date, priority=priority, title=title[:500],
        payload=jsonable(payload), source_url=source_url, is_backfill=is_backfill, date_quality=date_quality,
    ).on_conflict_do_nothing(index_elements=["dedupe_key"]).returning(events.c.id)).first()
    if res is None:
        return None
    eid = res[0]
    conn.execute(pg_insert(signal_features).values(event_id=eid, features=jsonable(features or payload))
                 .on_conflict_do_nothing(index_elements=["event_id"]))
    return eid


# ---------- source health ----------

def health_success(conn: Connection, source: str, items: int = 0, had_event: bool = False) -> None:
    now = datetime.now(timezone.utc)
    vals = {"last_run": now, "last_success": now, "consecutive_failures": 0, "alerted": False, "status": "WORKING",
            "items_last_run": items}
    if had_event:
        vals["last_event"] = now
    conn.execute(pg_insert(source_health).values(source=source, **vals)
                 .on_conflict_do_update(index_elements=["source"], set_=vals))


def health_failure(conn: Connection, source: str, error: str) -> int:
    now = datetime.now(timezone.utc)
    row = conn.execute(select(source_health.c.consecutive_failures).where(source_health.c.source == source)).first()
    fails = (row[0] if row else 0) + 1
    vals = {"last_run": now, "last_error_at": now, "last_error": error[:2000], "consecutive_failures": fails,
            "status": "ERROR" if fails >= 3 else "DEGRADED", "items_last_run": 0}
    conn.execute(pg_insert(source_health).values(source=source, **vals)
                 .on_conflict_do_update(index_elements=["source"], set_=vals))
    return fails


def health_mark(conn: Connection, source: str, status: str, error: str | None = None) -> None:
    vals = {"status": status, "last_error": error}
    conn.execute(pg_insert(source_health).values(source=source, **vals)
                 .on_conflict_do_update(index_elements=["source"], set_=vals))


# ---------- job state ----------

def job_state(conn: Connection, name: str) -> dict:
    row = conn.execute(select(processing_jobs.c.state).where(processing_jobs.c.name == name)).first()
    return dict(row[0]) if row else {}


def save_job_state(conn: Connection, name: str, state: dict, status: str = "idle", error: str | None = None,
                   started: datetime | None = None, finished: bool = True) -> None:
    now = datetime.now(timezone.utc)
    vals: dict[str, Any] = {"state": jsonable(state), "status": status, "last_error": error}
    if started is not None:
        vals["last_started"] = started
    if finished:
        vals["last_finished"] = now
    ins = pg_insert(processing_jobs).values(name=name, runs=1, **vals)
    conn.execute(ins.on_conflict_do_update(index_elements=["name"], set_={**vals, "runs": processing_jobs.c.runs + 1}))
