"""HISTORICAL BACKFILL process - separate from live ingestion, resumable, disk-guarded."""
from __future__ import annotations

import logging
import signal
import sys
import time

from app import logging_setup
from app.backtest.jobs import compute_pending
from app.config import settings
from app.db.session import get_engine
from app.db.store import save_job_state
from app.entities.registry import resolver, sync_registry
from app.sources.congress.house import HousePtrSource
from app.sources.sec.backfill import backfill_13d, backfill_form4_gap, backfill_form345, disk_ok
from app.sources.trump.tracker import TrumpFamilySource
from app.sources.usaspending.awards import UsaSpendingSource

log = logging.getLogger("backfill")
_stop = False


def _sig(*_):
    global _stop
    _stop = True


def step(engine, name, fn):
    if _stop:
        return
    t0 = time.time()
    log.info("backfill step %s started", name)
    try:
        res = fn()
        log.info("backfill step %s done in %.0fs: %s", name, time.time() - t0, res)
    except Exception as e:
        log.exception("backfill step %s failed", name)
        with engine.begin() as conn:
            save_job_state(conn, f"backfill_{name}_error", {"error": str(e)[:1000]}, status="error", error=str(e)[:1000])


def run_once(engine) -> None:
    years = settings.backfill_years
    with engine.begin() as conn:
        sync_registry(conn)
        resolver.load(conn, force=True)
    step(engine, "trump", lambda: TrumpFamilySource().run_backfill(engine).items)
    step(engine, "form345", lambda: backfill_form345(engine, years))
    step(engine, "form4_gap", lambda: backfill_form4_gap(engine))
    step(engine, "house", lambda: HousePtrSource().run_backfill(engine, years).items)
    step(engine, "usaspending", lambda: UsaSpendingSource().run_backfill(engine, years).items)
    step(engine, "sc13d", lambda: backfill_13d(engine, years))
    # returns for every historical event (prices are fetched per ticker, cached in market_prices)
    while not _stop and disk_ok():
        r = compute_pending(engine, limit=2000, time_budget_s=1800)
        with engine.begin() as conn:
            save_job_state(conn, "backfill_returns", r, status="running")
        log.info("backfill returns batch %s", r)
        if not r.get("processed") and not r.get("no_data"):
            break
    with engine.begin() as conn:
        save_job_state(conn, "backfill_returns", {"finished": True}, status="done")


def main() -> None:
    logging_setup.setup()
    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)
    engine = get_engine()
    loop = "--once" not in sys.argv
    while not _stop:
        run_once(engine)
        if not loop:
            break
        # new quarterly datasets / late filings: re-run daily (all steps are idempotent + checkpointed)
        for _ in range(24 * 360):
            if _stop:
                break
            time.sleep(10)


if __name__ == "__main__":
    main()
