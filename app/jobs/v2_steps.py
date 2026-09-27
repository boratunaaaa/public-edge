"""V2 dataset jobs. Queues (V2 #61):
  LIVE_HIGH      worker fast loop  - SEC Form 4 / 13D / 144, news (unchanged)
  DAILY_MEDIUM   worker slow thread - FINRA short volume, FTD, ClinicalTrials, openFDA, CFTC, CanadaBuys, Valet
  HISTORICAL_LOW backfill container - dataset history (13F data sets, FINRA / FTD / openFDA / CFTC backfills)
Every historical step checks free disk first and is idempotent / checkpointed (job state)."""
from __future__ import annotations

import logging
import os
import shutil
from datetime import date, timedelta

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.db.store import job_state, save_job_state

log = logging.getLogger(__name__)
MIN_FREE_GB = float(os.environ.get("PE_V2_MIN_FREE_GB", "9"))


def disk_ok() -> bool:
    return shutil.disk_usage("/").free / 1024**3 >= MIN_FREE_GB


def after_finra(engine: Engine, trade_dates: list[date], *, is_backfill: bool) -> int:
    from app.signals.v2_families import short_pressure
    n = 0
    for d in trade_dates:
        with engine.begin() as c:
            n += len(short_pressure(c, d, is_backfill=is_backfill))
    return n


def after_ftd(engine: Engine, files: list[str], *, is_backfill: bool) -> int:
    from app.signals.v2_families import ftd_spikes
    n = 0
    for f in sorted(files):
        with engine.begin() as c:
            n += len(ftd_spikes(c, f, is_backfill=is_backfill))
    return n


def after_13f(engine: Engine, since_filed: date | None = None, *, is_backfill: bool) -> int:
    from app.signals.v2_families import thirteen_f
    with engine.connect() as c:
        accs = c.execute(text("""SELECT accession FROM inst_filings_13f WHERE submission_type = '13F-HR'
                                 AND entries BETWEEN 10 AND 150 AND (CAST(:d AS date) IS NULL OR filed_date >= :d)
                                 ORDER BY available_at"""), {"d": since_filed}).scalars().all()
    n = 0
    for a in accs:
        with engine.begin() as c:
            n += len(thirteen_f(c, a, is_backfill=is_backfill))
    return n


def backfill_13f(engine: Engine, quarters: int = 9) -> dict:
    """Newest `quarters` data sets (filing-date windows); events after all are stored (needs the prior quarter)."""
    from app.sources.us.form13f import dataset_urls, ingest_dataset
    if not disk_ok():
        return {"skipped": "LOW_DISK"}
    min_period = date.today() - timedelta(days=quarters * 92 + 60)
    out = [ingest_dataset(engine, u, min_period=min_period) for u in dataset_urls()[:quarters]]
    new = [o for o in out if o.get("status") != "ALREADY_INGESTED"]
    ev = after_13f(engine, is_backfill=True) if new else 0
    return {"datasets": len(out), "new": len(new), "events": ev}


def backfill_short_data(engine: Engine, start: date = date(2024, 1, 2)) -> dict:
    from app.sources.us.short_data import FinraShortVolumeSource, SecFtdSource
    if not disk_ok():
        return {"skipped": "LOW_DISK"}
    with engine.connect() as c:
        st = job_state(c, "v2_short_data")
    FinraShortVolumeSource().run_backfill(engine, start)
    with engine.connect() as c:
        days = c.execute(text("SELECT DISTINCT trade_date FROM short_volume_daily WHERE trade_date > :d ORDER BY 1"),
                         {"d": date.fromisoformat(st.get("sv_events_through", "2024-03-01"))}).scalars().all()
    n_sv = after_finra(engine, days, is_backfill=True)
    SecFtdSource().run_live(engine, backfill_since=f"cnsfails{start:%Y%m}a")
    with engine.connect() as c:
        files = c.execute(text("SELECT DISTINCT source_file FROM fails_to_deliver WHERE source_file > :f"),
                          {"f": st.get("ftd_events_through", "")}).scalars().all()
    n_ftd = after_ftd(engine, files, is_backfill=True)
    with engine.begin() as c:
        save_job_state(c, "v2_short_data", {**st, "sv_events_through": str(days[-1]) if days else st.get("sv_events_through", "2024-03-01"),
                                            "ftd_events_through": max(files) if files else st.get("ftd_events_through", "")})
    return {"short_pressure_events": n_sv, "ftd_events": n_ftd}


def backfill_health_gov(engine: Engine) -> dict:
    from app.sources.us.cftc import CftcCotSource
    from app.sources.us.clinical import ClinicalTrialsSource
    from app.sources.us.fda import OpenFdaSource
    if not disk_ok():
        return {"skipped": "LOW_DISK"}
    with engine.connect() as c:
        st = job_state(c, "v2_health_gov")
    out = {}
    if not st.get("openfda_done"):
        out["openfda"] = OpenFdaSource().run_backfill(engine).items
    if not st.get("cftc_done"):
        out["cftc"] = CftcCotSource().run_backfill(engine).items
    if not st.get("trials_done"):
        out["trials"] = ClinicalTrialsSource().run_live(engine, since=date(2000, 1, 1), max_pages=400).items
    with engine.begin() as c:
        save_job_state(c, "v2_health_gov", {"openfda_done": True, "cftc_done": True, "trials_done": True})
    return out


def run_historical(engine: Engine, step) -> None:
    """Called from app.jobs.backfill.run_once (HISTORICAL_LOW)."""
    step(engine, "v2_short_data", lambda: backfill_short_data(engine))
    step(engine, "v2_health_gov", lambda: backfill_health_gov(engine))
    step(engine, "v2_13f", lambda: backfill_13f(engine))
