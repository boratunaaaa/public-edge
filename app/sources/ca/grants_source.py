"""Canada grants & contributions as a DAILY_MEDIUM source: one streamed pass of the 2.3 GB CSV per week (never stored
on disk); only for-profit recipients are kept. The first pass marks everything as HISTORICAL (needs_review, not point
in time); later passes record newly appearing agreements at first sight (forward, usable)."""
from __future__ import annotations

from datetime import timedelta

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
        from app.db.store import job_state, save_job_state
        from app.sources.ca.official import stream_grants
        with engine.connect() as c:
            st = job_state(c, "v2_ca_grants_initial")
        first = not st.get("complete")  # an interrupted initial load stays HISTORICAL until one full pass finishes
        out = stream_grants(engine, forward=not first)
        if first:
            with engine.begin() as c:
                save_job_state(c, "v2_ca_grants_initial", {"complete": True, "rows": out["rows"]})
        return RunResult(items=out["stored"], watermark={"rows_scanned": out["rows"], "initial_load": first})
