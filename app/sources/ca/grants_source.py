"""Canada grants & contributions as a DAILY_MEDIUM source: one streamed pass of the 2.3 GB CSV per week (never stored
on disk); only for-profit recipients are kept. The first pass marks everything as HISTORICAL (needs_review, not point
in time); later passes record newly appearing agreements at first sight (forward, usable)."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.sources.base import RunResult, Source


class CanadaGrantsSource(Source):
    name = "ca_grants"
    interval_seconds = 7 * 86400
    critical = False
    slow = True
    country, dataset, license_status, parser_version = "CA", "ca_grants", "OPEN", "ca-official-v1"
    stale_after = timedelta(days=10)

    def run_live(self, engine: Engine) -> RunResult:
        from app.sources.ca.official import stream_grants
        with engine.connect() as c:
            first = c.execute(text("SELECT count(*) FROM dataset_records WHERE dataset = 'ca_grants'")).scalar() == 0
        out = stream_grants(engine, forward=not first)
        return RunResult(items=out["stored"], watermark={"rows_scanned": out["rows"], "initial_load": first})
