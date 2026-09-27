"""Idempotent bulk insert for dataset tables (ON CONFLICT DO NOTHING on the natural key)."""
from __future__ import annotations

from sqlalchemy import text


def upsert_rows(conn, table: str, rows: list[dict], keys: tuple[str, ...], extra: dict) -> int:
    if not rows:
        return 0
    cols = list(rows[0].keys()) + list(extra.keys())
    n = 0
    for i in range(0, len(rows), 2000):
        chunk = [{**r, **extra} for r in rows[i:i + 2000]]
        vals = ", ".join(f"({', '.join(f':{c}_{j}' for c in cols)})" for j in range(len(chunk)))
        params = {f"{c}_{j}": r[c] for j, r in enumerate(chunk) for c in cols}
        n += len(conn.execute(text(f"INSERT INTO {table} ({', '.join(cols)}) VALUES {vals} "
                                   f"ON CONFLICT ({', '.join(keys)}) DO NOTHING RETURNING 1"), params).fetchall())
    return n
