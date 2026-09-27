"""Point-in-time generic records (dataset_records): a new record_version is a new row, never an overwrite, so a
backtest can see exactly what was known at available_at."""
from __future__ import annotations

import json
from datetime import date, datetime

from sqlalchemy import text
from sqlalchemy.engine import Connection


def put(conn: Connection, *, country: str, dataset: str, record_key: str, available_at: datetime, payload: dict,
        parser_version: str, license_status: str, record_version: str = "", as_of_date: date | None = None,
        issuer_cik: str | None = None, security_id: str | None = None, entity_name: str | None = None,
        identity_confidence: str | None = None, source_url: str | None = None, raw_document_id: int | None = None,
        needs_review: bool = False) -> int | None:
    """Insert once per (dataset, key, version). Returns the new row id, None when this version already exists."""
    r = conn.execute(text("""
        INSERT INTO dataset_records (country, dataset, record_key, record_version, as_of_date, available_at, issuer_cik,
          security_id, entity_name, identity_confidence, payload, source_url, raw_document_id, parser_version,
          license_status, needs_review)
        VALUES (:country, :dataset, :key, :ver, :asof, :avail, :cik, :sid, :ename, :ic, CAST(:payload AS jsonb), :url, :raw,
                :pv, :lic, :nr)
        ON CONFLICT (dataset, record_key, record_version) DO NOTHING RETURNING id"""),
                     {"country": country, "dataset": dataset, "key": record_key[:160], "ver": record_version[:64],
                      "asof": as_of_date, "avail": available_at, "cik": issuer_cik, "sid": security_id,
                      "ename": (entity_name or "")[:300] or None, "ic": identity_confidence,
                      "payload": json.dumps(payload, default=str), "url": source_url, "raw": raw_document_id,
                      "pv": parser_version, "lic": license_status, "nr": needs_review}).first()
    return r[0] if r else None


def latest(conn: Connection, dataset: str, record_key: str, as_of: datetime | None = None) -> dict | None:
    """The version known at `as_of` (default: now)."""
    row = conn.execute(text("""SELECT * FROM dataset_records WHERE dataset = :d AND record_key = :k
                               AND (CAST(:t AS timestamptz) IS NULL OR available_at <= :t)
                               ORDER BY available_at DESC, id DESC LIMIT 1"""),
                       {"d": dataset, "k": record_key, "t": as_of}).mappings().first()
    return dict(row) if row else None
