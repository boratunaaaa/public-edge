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
from app.sources.sec.backfill import backfill_13d, backfill_form4_gap, backfill_form345
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


def returns_loop(engine) -> None:
    """Historical returns run continuously next to the other steps (prices come from Yahoo, not SEC).
    First the price identity of already computed returns is verified, then pending events are priced."""
    from app.backtest.jobs import revalidate_price_identity
    while not _stop:
        try:
            r = revalidate_price_identity(engine, batch=5000)
            if r["batch"]:
                log.info("returns identity revalidation %s", r)
                continue
            from app.backtest.jobs import fill_sector_benchmark
            r = fill_sector_benchmark(engine)
            if r["batch"]:
                log.info("sector benchmark fill %s", r)
                continue
            r = compute_pending(engine, limit=3000, time_budget_s=1800, backfill=True)
            with engine.begin() as conn:
                save_job_state(conn, "backfill_returns", r, status="running")
            log.info("backfill returns batch %s", r)
            if r.get("processed") or r.get("no_data"):
                continue
        except Exception:
            log.exception("backfill returns loop failed")
        for _ in range(90):  # idle / after an error: 15 minutes
            if _stop:
                return
            time.sleep(10)


def tenk_loop(engine) -> None:
    """LOW priority: survivorship-free 10-K history, in small resumable batches with pauses, so it never
    competes with live ingestion (worker) or the daily steps for long."""
    from app.tenk.engine import run_backfill
    while not _stop:
        pause = 120
        try:
            r = run_backfill(engine, max_docs=150, time_budget_s=900)
            log.info("10-K backfill batch %s", r)
            if r.get("skipped") or (not r.get("docs") and not r.get("not_listed")):
                pause = 6 * 3600  # history complete: look again for late filers every 6 hours
        except Exception:
            log.exception("10-K backfill failed")
            pause = 900
        for _ in range(pause // 10):
            if _stop:
                return
            time.sleep(10)


def tiingo_loop(engine) -> None:
    """Delisted prices from Tiingo, one free-tier hour of quota per batch (resumable). Started only
    when the operator configured TIINGO_API_KEY."""
    from app.securitymaster.tiingo_backfill import run as tiingo_run
    while not _stop:
        try:
            r = tiingo_run(engine)
            log.info("tiingo backfill batch %s", r)
        except Exception:
            log.exception("tiingo backfill failed")
        for _ in range(360):
            if _stop:
                return
            time.sleep(10)


def run_once(engine) -> None:
    years = settings.backfill_years
    with engine.begin() as conn:
        sync_registry(conn)
        resolver.load(conn, force=True)
    step(engine, "trump", lambda: TrumpFamilySource().run_backfill(engine).items)
    step(engine, "form345", lambda: backfill_form345(engine, years))
    # data integrity: point-in-time security master (symbols, owners, delistings, sectors), joint-filing
    # attribution of XML rows, then legacy duplicate cleanup (superseded, never deleted; audited)
    from app.securitymaster.build import run_security_master
    step(engine, "security_master", lambda: run_security_master(engine, years))
    from app.integrity.reattribute import reattribute_xml_rows
    step(engine, "reattribute_xml", lambda: reattribute_xml_rows(engine, batch=100000))
    from app.integrity.cleanup import run_cleanup_safe
    step(engine, "integrity_cleanup", lambda: run_cleanup_safe(engine))
    step(engine, "form4_gap", lambda: backfill_form4_gap(engine))
    # routine vs opportunistic label (CMP-inspired point-in-time heuristic) for historical buy events
    from app.signals.insider_type import annotate_existing
    step(engine, "insider_type", lambda: annotate_existing(engine))
    step(engine, "house", lambda: HousePtrSource().run_backfill(engine, years).items)
    step(engine, "usaspending", lambda: UsaSpendingSource().run_backfill(engine, years).items)
    step(engine, "sc13d", lambda: backfill_13d(engine, years))
    from app.signals.crowding import backfill_crowding
    step(engine, "activist_crowding", lambda: backfill_crowding(engine))
    # official 8-K history for the news backtest: point-in-time listed universe (not today's list)
    from app.news.backfill import backfill_news_8k
    step(engine, "news_8k", lambda: backfill_news_8k(engine, days=settings.news_backfill_days))


def main() -> None:
    logging_setup.setup()
    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)
    engine = get_engine()
    loop = "--once" not in sys.argv
    import threading
    rt = threading.Thread(target=returns_loop, args=(engine,), name="returns", daemon=True)
    rt.start()
    if loop:
        threading.Thread(target=tenk_loop, args=(engine,), name="tenk", daemon=True).start()
        from app.securitymaster.tiingo_backfill import configured as tiingo_configured
        if tiingo_configured():
            threading.Thread(target=tiingo_loop, args=(engine,), name="tiingo", daemon=True).start()
        else:
            log.info("TIINGO_API_KEY not set: delisted prices stay unresolved (reported in data quality)")
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
