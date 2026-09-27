"""Raw data lake: the original bytes of every source document, content-addressed and compressed.

Layout: <RAW_LAKE_DIR>/<country>/<source>/<yyyy>/<mm>/<dd>/<sha256[:2]>/<sha256>.gz (atomic write: tmp + rename).
Identical content is stored once (sha256 dedupe across doc keys). raw_documents indexes it with provenance, so a
parser change can re-derive the canonical rows from the lake. Retention: HOT (parse inputs, may expire after
`hot_days`), COLD (kept, recompressed at the highest level), EVIDENCE (primary evidence of an event - never expires)."""
from __future__ import annotations

import gzip
import hashlib
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import Connection

ROOT = Path(os.environ.get("RAW_LAKE_DIR", "/data/raw"))


def _path(root: Path, country: str, source: str, sha: str, when: datetime) -> Path:
    return root / country.lower() / source / f"{when:%Y}" / f"{when:%m}" / f"{when:%d}" / sha[:2] / f"{sha}.gz"


def store(conn: Connection, *, country: str, source: str, doc_key: str, content: bytes | str, url: str | None = None,
          content_type: str | None = None, parser_version: str | None = None, retention: str = "HOT",
          root: Path | None = None, now: datetime | None = None) -> int:
    """Store once; returns raw_documents.id (existing row for identical source+key+content)."""
    root = root or ROOT
    data = content.encode("utf-8", "replace") if isinstance(content, str) else content
    sha = hashlib.sha256(data).hexdigest()
    row = conn.execute(text("SELECT id FROM raw_documents WHERE source = :s AND doc_key = :k AND sha256 = :h"),
                       {"s": source, "k": doc_key[:2000], "h": sha}).first()
    if row:
        return row[0]
    now = now or datetime.now(timezone.utc)
    same = conn.execute(text("SELECT storage_path, stored_bytes FROM raw_documents WHERE sha256 = :h LIMIT 1"), {"h": sha}).first()
    if same and Path(same[0]).exists():
        path, stored = Path(same[0]), same[1]  # dedupe: identical bytes already on disk
    else:
        path = _path(root, country, source, sha, now)
        path.parent.mkdir(parents=True, exist_ok=True)
        blob = gzip.compress(data, 9 if retention != "HOT" else 6)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        with os.fdopen(fd, "wb") as f:
            f.write(blob)
        os.replace(tmp, path)
        stored = len(blob)
    return conn.execute(text("""
        INSERT INTO raw_documents (country, source, doc_key, url, sha256, storage_path, bytes, stored_bytes, content_type,
                                   retrieved_at, parser_version, retention_class)
        VALUES (:c, :s, :k, :u, :h, :p, :b, :sb, :ct, :t, :pv, :r)
        ON CONFLICT (source, doc_key, sha256) DO UPDATE SET retrieved_at = raw_documents.retrieved_at RETURNING id"""),
                        {"c": country, "s": source, "k": doc_key[:2000], "u": url, "h": sha, "p": str(path), "b": len(data),
                         "sb": stored, "ct": content_type, "t": now, "pv": parser_version, "r": retention}).scalar_one()


def load(conn: Connection, raw_id: int) -> bytes:
    p = conn.execute(text("SELECT storage_path, sha256 FROM raw_documents WHERE id = :i"), {"i": raw_id}).first()
    data = gzip.decompress(Path(p[0]).read_bytes())
    if hashlib.sha256(data).hexdigest() != p[1]:
        raise ValueError(f"raw document {raw_id}: hash mismatch (corrupt lake file)")
    return data


def mark_evidence(conn: Connection, raw_id: int) -> None:
    conn.execute(text("UPDATE raw_documents SET retention_class = 'EVIDENCE' WHERE id = :i"), {"i": raw_id})


def apply_retention(conn: Connection, *, hot_days: int = 90, dry_run: bool = True, now: datetime | None = None) -> dict:
    """Expire HOT documents older than hot_days whose bytes no EVIDENCE/COLD row shares. Rows stay (provenance),
    storage_path is set to '' so a missing file is explicit, never silent."""
    now = now or datetime.now(timezone.utc)
    rows = conn.execute(text("""
        SELECT r.id, r.storage_path, r.stored_bytes FROM raw_documents r
        WHERE r.retention_class = 'HOT' AND r.retrieved_at < :cut AND r.storage_path <> ''
          AND NOT EXISTS (SELECT 1 FROM raw_documents k WHERE k.sha256 = r.sha256 AND k.retention_class <> 'HOT')"""),
                        {"cut": now - timedelta(days=hot_days)}).all()
    freed = 0
    for rid, path, sb in rows:
        freed += sb
        if not dry_run:
            others = conn.execute(text("SELECT count(*) FROM raw_documents WHERE storage_path = :p AND id <> :i AND storage_path <> ''"),
                                  {"p": path, "i": rid}).scalar()
            if not others:
                Path(path).unlink(missing_ok=True)
            conn.execute(text("UPDATE raw_documents SET storage_path = '' WHERE id = :i"), {"i": rid})
    return {"expired": len(rows), "bytes": freed, "dry_run": dry_run}


def usage(conn: Connection) -> list[dict]:
    return [dict(r) for r in conn.execute(text("""
        SELECT country, source, retention_class, count(*) docs, sum(bytes) bytes, sum(stored_bytes) stored
        FROM raw_documents WHERE storage_path <> '' GROUP BY 1, 2, 3 ORDER BY 5 DESC""")).mappings()]
