"""INTELLIGENCE process: discovery engine schedule + daily / weekly Telegram reports."""
from __future__ import annotations

import logging
import signal
import time
from datetime import date, datetime, timezone
from pathlib import Path

from app import logging_setup
from app.db.session import get_engine
from app.db.store import health_failure, health_success, job_state, save_job_state
from app.discovery.engine import run_discovery
from app.notifications import telegram
from app.notifications.dispatch import deliver
from app.reports import daily_report, weekly_report

log = logging.getLogger("intelligence")
HEARTBEAT = Path("/tmp/public_edge_intel_heartbeat")
DISCOVERY_HOUR_UTC = 10  # after overnight filings, before the US open
DAILY_REPORT_UTC = (12, 30)  # 08:30 New York (EDT) - pre-market
WEEKLY_REPORT_UTC = (16, 0)  # Sunday
_stop = False


def _sig(*_):
    global _stop
    _stop = True


def discovery_due(st: dict, now: datetime) -> tuple[bool, bool]:
    last = st.get("last_run")
    ran_today = last == now.date().isoformat()
    if not st.get("initialized"):
        return True, True
    if ran_today or now.hour < DISCOVERY_HOUR_UTC:
        return False, False
    last_full = st.get("last_full")
    full = not last_full or (now.date() - date.fromisoformat(last_full)).days >= 7
    return True, full


def main() -> None:
    logging_setup.setup()
    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)
    engine = get_engine()
    while not _stop:
        now = datetime.now(timezone.utc)
        with engine.begin() as conn:
            st = job_state(conn, "discovery_engine")
        run, full = discovery_due(st, now)
        if run:
            t0 = time.time()
            try:
                out = run_discovery(engine, full=full)
                with engine.begin() as conn:
                    st = job_state(conn, "discovery_engine")
                    save_job_state(conn, "discovery_engine", {**st, "last_run": now.date().isoformat()})
                    health_success(conn, "discovery_engine", items=(out.get("evaluation") or {}).get("evaluated", 0))
                log.info("discovery done in %.0fs: %s", time.time() - t0, out)
            except Exception as e:
                log.exception("discovery failed")
                with engine.begin() as conn:
                    health_failure(conn, "discovery_engine", f"{type(e).__name__}: {e}")
        if telegram.configured():
            if now.weekday() < 5 and (now.hour, now.minute) >= DAILY_REPORT_UTC and now.hour < 20:
                try:
                    deliver(engine, f"report:daily:{now.date()}", "report", daily_report(engine, now))
                except Exception:
                    log.exception("daily report failed")
            if now.weekday() == 6 and (now.hour, now.minute) >= WEEKLY_REPORT_UTC:
                try:
                    y, w, _ = now.isocalendar()
                    deliver(engine, f"report:weekly:{y}-W{w:02d}", "report", weekly_report(engine, now))
                except Exception:
                    log.exception("weekly report failed")
        HEARTBEAT.write_text(str(int(time.time())))
        for _ in range(30):
            if _stop:
                break
            time.sleep(10)


if __name__ == "__main__":
    main()
