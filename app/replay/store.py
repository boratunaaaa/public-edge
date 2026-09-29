"""Isolated replay state: one Postgres schema per run (replay_<id>) in a database that is NOT the production database.

The run's engine sets search_path to its own schema only, so every table name the reused production code writes
(paper_accounts, paper_orders, proposed_trades, events, ...) resolves inside that schema - the production ledger, even
if it lived in the same database, is not reachable by name. A second run gets another schema: runs cannot share
state. Run metadata lives in replay_meta.replay_runs of the replay database."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.pool import NullPool

from app.db.models import metadata
from app.replay.errors import ReplayIsolationError

META_SCHEMA = "replay_meta"


def _norm(url: str) -> tuple:
    u = make_url(url)
    return ((u.host or "localhost").lower(), u.port or 5432, (u.database or "").lower())


def production_urls() -> list[str]:
    from app.config import settings
    out = [settings.database_url]
    if os.environ.get("DATABASE_URL"):
        out.append(os.environ["DATABASE_URL"])
    return out


def guard_isolation(url: str, schema: str) -> None:
    """Refuse (before any write) a production database or a non-replay schema."""
    if not schema.startswith("replay_") or schema == META_SCHEMA:
        raise ReplayIsolationError(f"schema {schema!r} is not a replay schema")
    target = _norm(url)
    for p in production_urls():
        prod = _norm(p)
        if target == prod or (target[2] and target[2] == prod[2] and target[0] == prod[0]):
            raise ReplayIsolationError(f"replay database {target[2]!r} on {target[0]} is the production database")


class ReplayStore:
    def __init__(self, url: str, schema: str):
        guard_isolation(url, schema)
        self.url, self.schema = url, schema
        # NullPool: no pooled connection can outlive the run with this search_path
        self.engine: Engine = create_engine(url, future=True, poolclass=NullPool,
                                            connect_args={"options": f"-csearch_path={schema}"})
        self.admin: Engine = create_engine(url, future=True, poolclass=NullPool)

    def create(self) -> None:
        """Fresh schema with the production table definitions (no rows: data is staged point in time)."""
        with self.admin.begin() as c:
            c.execute(text(f'DROP SCHEMA IF EXISTS "{self.schema}" CASCADE'))
            c.execute(text(f'CREATE SCHEMA "{self.schema}"'))
            c.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{META_SCHEMA}"'))
            c.execute(text(f"""CREATE TABLE IF NOT EXISTS {META_SCHEMA}.replay_runs (
                replay_id TEXT PRIMARY KEY, schema_name TEXT NOT NULL, status TEXT NOT NULL, period_role TEXT NOT NULL,
                config JSONB NOT NULL, config_hash TEXT NOT NULL, code_commit TEXT NOT NULL, data_snapshot TEXT NOT NULL,
                result JSONB, leakage JSONB, sealed BOOLEAN NOT NULL DEFAULT false,
                started_at TIMESTAMPTZ NOT NULL, finished_at TIMESTAMPTZ)"""))
            # FULL only when the risk-label history covers the window (engine.risk_history); LIMITED runs never feed a
            # holdout verdict as "fully comparable" (campaign.holdout_verdict_inputs)
            c.execute(text(f"ALTER TABLE {META_SCHEMA}.replay_runs ADD COLUMN IF NOT EXISTS comparability TEXT"))
        metadata.create_all(self.engine)
        with self.engine.connect() as c:  # belt and braces: every table really is in the run's schema
            where = c.execute(text("""SELECT DISTINCT table_schema FROM information_schema.tables
                                      WHERE table_name IN ('paper_orders', 'proposed_trades', 'events')
                                        AND table_schema = current_schema()""")).scalars().all()
            if where != [self.schema]:
                raise ReplayIsolationError(f"replay tables resolved to {where}, expected [{self.schema}]")

    def record(self, *, replay_id: str, status: str, period_role: str, config: dict, config_hash: str, code_commit: str,
               data_snapshot: str, result: dict | None = None, leakage: dict | None = None, finished: bool = False,
               comparability: str = "LIMITED") -> None:
        now = datetime.now(timezone.utc)  # wall clock on purpose: bookkeeping of the run, never an input
        with self.admin.begin() as c:
            c.execute(text(f"""INSERT INTO {META_SCHEMA}.replay_runs (replay_id, schema_name, status, period_role, config,
                                   config_hash, code_commit, data_snapshot, result, leakage, sealed, started_at, finished_at,
                                   comparability)
                               VALUES (:i, :s, :st, :r, CAST(:c AS jsonb), :h, :cc, :d, CAST(:res AS jsonb), CAST(:lk AS jsonb),
                                       :sealed, :now, :fin, :cmp)
                               ON CONFLICT (replay_id) DO UPDATE SET status = :st, result = CAST(:res AS jsonb),
                                   leakage = CAST(:lk AS jsonb), finished_at = :fin, sealed = :sealed, comparability = :cmp"""),
                      {"i": replay_id, "s": self.schema, "st": status, "r": period_role, "c": json.dumps(config),
                       "h": config_hash, "cc": code_commit, "d": data_snapshot,
                       "res": json.dumps(result, default=str) if result is not None else None,
                       "lk": json.dumps(leakage, default=str) if leakage is not None else None,
                       # a holdout run is sealed: its results are never tuning input (campaign.tuning_results)
                       "sealed": period_role == "holdout", "now": now, "fin": now if finished else None,
                       "cmp": comparability})
