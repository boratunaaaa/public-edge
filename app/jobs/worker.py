"""LIVE INGESTION worker: runs every source on its own interval, then backtest + alerts."""
from __future__ import annotations

import logging
import signal
import threading
import time
from pathlib import Path

from sqlalchemy import select

from app import logging_setup
from app.backtest.jobs import compute_pending
from app.config import settings
from app.db.models import source_health
from app.db.session import get_engine
from app.db.store import health_failure, health_mark, health_success, save_job_state
from app.entities.registry import resolver, sync_registry
from app.notifications import telegram
from app.notifications.dispatch import deliver, dispatch, is_delivered
from app.sources.registry import SOURCES

log = logging.getLogger("worker")
HEARTBEAT = Path("/tmp/public_edge_worker_heartbeat")
_stop = False


def _sig(*_):
    global _stop
    _stop = True


def _alert_source_failure(engine, source: str, fails: int, err: str) -> None:
    """One alert per failure episode. `alerted` is set only after Telegram confirmed delivery;
    a failed send is retried (with backoff) on the next failing run."""
    with engine.connect() as conn:
        row = conn.execute(select(source_health.c.alerted, source_health.c.last_success)
                           .where(source_health.c.source == source)).first()
    if row and row[0]:
        return
    episode = row[1].isoformat() if row and row[1] else "never"
    key = f"source_error:{source}:{episode}"
    r = deliver(engine, key, "source_error",
                f"⚠️ <b>PUBLIC EDGE source error</b>\nKaynak: {source}\nArdışık hata: {fails}\nSon hata: {err[:300]}")
    if r is True or (r is None and is_delivered(engine, key)):
        with engine.begin() as conn:
            conn.execute(source_health.update().where(source_health.c.source == source).values(alerted=True))


def run_source(engine, src) -> None:
    t0 = time.time()
    try:
        res = src.run_live(engine)
        with engine.begin() as conn:
            health_success(conn, src.name, items=res.items, had_event=bool(res.events))
        log.info("source %s ok items=%s events=%s (%.1fs)", src.name, res.items, len(res.events), time.time() - t0)
    except Exception as e:
        log.exception("source %s failed", src.name)
        with engine.begin() as conn:
            fails = health_failure(conn, src.name, f"{type(e).__name__}: {e}")
        if src.critical and fails >= settings.source_failure_alert_threshold:
            _alert_source_failure(engine, src.name, fails, str(e))


def _dispatch(engine) -> None:
    try:
        r = dispatch(engine)
        if r.get("sent") or r.get("failed") or r.get("suppressed"):
            log.info("dispatch %s", r)
    except Exception:
        log.exception("dispatch failed")


def _source_loop(engine, sources) -> None:
    """Slow thread: long-running sources + incremental backtest + daily registry refresh."""
    next_run = {s.name: 0.0 for s in sources}
    last_backtest = 0.0
    last_registry = time.time()
    while not _stop:
        for s in sources:
            if _stop or time.time() < next_run[s.name]:
                continue
            run_source(engine, s)
            next_run[s.name] = time.time() + s.interval_seconds
            _dispatch(engine)
        if not _stop and time.time() - last_backtest > 1800:
            try:
                r = compute_pending(engine, limit=300, time_budget_s=240, backfill=False)  # live events only
                with engine.begin() as conn:
                    save_job_state(conn, "backtest_incremental", r)
                log.info("backtest incremental %s", r)
            except Exception:
                log.exception("backtest incremental failed")
            last_backtest = time.time()
        if not _stop and time.time() - last_registry > 86400:
            try:
                with engine.begin() as conn:
                    sync_registry(conn)
                    resolver.load(conn, force=True)
            except Exception:
                log.exception("registry sync failed")
            last_registry = time.time()
        time.sleep(5)


def main() -> None:
    logging_setup.setup()
    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)
    engine = get_engine()
    with engine.begin() as conn:
        n = sync_registry(conn)
        resolver.load(conn, force=True)
        for s in SOURCES:
            if not s.enabled:
                health_mark(conn, s.name, "DISABLED", s.disabled_reason)
    log.info("registry synced: %s tickers", n)
    if telegram.configured():
        try:
            info = telegram.check()
            log.info("telegram startup check OK: bot=@%s chat_type=%s", info["bot_username"], info["chat_type"])
            with engine.begin() as conn:
                health_success(conn, "telegram")
        except Exception as e:
            log.error("telegram startup check FAILED: %s", e)
            with engine.begin() as conn:
                health_failure(conn, "telegram", str(e))
    else:
        log.warning("telegram not configured - alerts disabled")

    slow_thread = threading.Thread(target=_source_loop, args=(engine, [s for s in SOURCES if s.enabled and s.slow]),
                                   name="slow-sources", daemon=True)
    slow_thread.start()
    fast = [s for s in SOURCES if s.enabled and not s.slow]
    next_run = {s.name: 0.0 for s in fast}
    while not _stop:
        now = time.time()
        for s in fast:
            if _stop or now < next_run[s.name]:
                continue
            run_source(engine, s)
            next_run[s.name] = time.time() + s.interval_seconds
            _dispatch(engine)
        if not slow_thread.is_alive() and not _stop:
            log.error("slow-sources thread died - restarting it")
            slow_thread = threading.Thread(target=_source_loop, args=(engine, [s for s in SOURCES if s.enabled and s.slow]),
                                           name="slow-sources", daemon=True)
            slow_thread.start()
        HEARTBEAT.write_text(str(int(time.time())))
        time.sleep(5)
    log.info("worker stopped")


if __name__ == "__main__":
    main()
