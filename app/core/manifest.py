"""Dataset manifests (row counts, watermarks, source health, content hash) + optional Parquet exports.

The production database stays the source of truth; a manifest makes any analysis reproducible: the same dataset
at the same manifest hash is the same data."""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import Connection

EXPORT_DIR = Path(os.environ.get("EXPORT_DIR", "/data/exports"))

# dataset -> (table/view SQL, time column, order key, source names feeding it, schema version)
DATASETS: dict[str, dict] = {
    "events": {"sql": "SELECT id, country, exchange, event_type, ticker, issuer_cik, security_id, public_date, available_at, "
                      "date_quality, source_url, superseded FROM events", "time": "available_at", "order": "id",
               "sources": ["sec_form4", "sec_13d", "sec_form144", "congress_house", "usaspending"], "schema": "2"},
    "event_returns": {"sql": "SELECT event_id, horizon_days, entry_date, exit_date, ret FROM event_returns",
                      "time": None, "order": "event_id, horizon_days", "sources": [], "schema": "1"},
    "short_volume_daily": {"sql": "SELECT * FROM short_volume_daily", "time": "available_at", "order": "trade_date, symbol",
                           "sources": ["finra_short_volume"], "schema": "1"},
    "fails_to_deliver": {"sql": "SELECT * FROM fails_to_deliver", "time": "available_at", "order": "settlement_date, cusip",
                         "sources": ["sec_ftd"], "schema": "1"},
    "inst_filings_13f": {"sql": "SELECT * FROM inst_filings_13f", "time": "available_at", "order": "accession",
                         "sources": ["sec_13f"], "schema": "1"},
    "short_interest": {"sql": "SELECT * FROM short_interest", "time": "available_at", "order": "country, settlement_date, symbol",
                       "sources": ["finra_short_interest", "ciro_short_position"], "schema": "1"},
    "dataset_records": {"sql": "SELECT id, country, dataset, record_key, record_version, as_of_date, available_at, issuer_cik, "
                               "parser_version, license_status FROM dataset_records", "time": "available_at", "order": "id",
                        "sources": [], "schema": "1"},
}


def build(conn: Connection, dataset: str, *, export: bool = False, export_dir: Path | None = None, batch: int = 50_000) -> dict:
    spec = DATASETS[dataset]
    h = hashlib.sha256()
    n = 0
    first = last = None
    writer = None
    out_path = None
    cur = conn.execution_options(stream_results=True).execute(text(f"{spec['sql']} ORDER BY {spec['order']}"))
    cols = list(cur.keys())
    while True:
        rows = cur.fetchmany(batch)
        if not rows:
            break
        for r in rows:
            h.update(json.dumps(list(r), default=str).encode())
        n += len(rows)
        if spec["time"]:
            ti = cols.index(spec["time"])
            ts = [r[ti] for r in rows if r[ti] is not None]
            if ts:
                first = min([first, min(ts)] if first else [min(ts)])
                last = max([last, max(ts)] if last else [max(ts)])
        if export:
            import pyarrow as pa
            import pyarrow.parquet as pq
            tbl = pa.Table.from_pylist([{c: (json.dumps(v, default=str) if isinstance(v, (dict, list)) else v)
                                         for c, v in zip(cols, r)} for r in rows])
            if writer is None:
                d = (export_dir or EXPORT_DIR) / dataset
                d.mkdir(parents=True, exist_ok=True)
                out_path = d / f"{dataset}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.parquet"
                tmp = out_path.with_suffix(".parquet.tmp")
                writer = pq.ParquetWriter(tmp, tbl.schema, compression="zstd")
            writer.write_table(tbl.cast(writer.schema) if tbl.schema != writer.schema else tbl)
    if writer is not None:
        writer.close()
        os.replace(out_path.with_suffix(".parquet.tmp"), out_path)
    health = {r["source"]: {"health_status": r["health_status"], "last_success": r["last_success"], "watermark": r["watermark"]}
              for r in conn.execute(text("SELECT source, health_status, last_success, watermark FROM source_health "
                                         "WHERE source = ANY(:s)"), {"s": spec["sources"]}).mappings()} if spec["sources"] else {}
    m = {"dataset": dataset, "schema_version": spec["schema"], "row_count": n, "first_event": first, "last_event": last,
         "watermark": {"last_event": last}, "source_health": health, "content_hash": h.hexdigest(),
         "export_path": str(out_path) if out_path else None,
         "export_bytes": out_path.stat().st_size if out_path else None}
    conn.execute(text("""INSERT INTO dataset_manifests (dataset, schema_version, row_count, first_event, last_event, watermark,
                                                        source_health, content_hash, export_path, export_bytes)
                         VALUES (:dataset, :schema_version, :row_count, :first_event, :last_event, CAST(:wm AS jsonb),
                                 CAST(:sh AS jsonb), :content_hash, :export_path, :export_bytes)"""),
                 {**m, "wm": json.dumps(m["watermark"], default=str), "sh": json.dumps(health, default=str)})
    return m
