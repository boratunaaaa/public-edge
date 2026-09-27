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


def _weekly_walkforward(engine, now: datetime) -> None:
    """Walk-forward threshold study once a week (Sunday): test-window results only."""
    if now.weekday() != 6 or now.hour < 18:
        return
    y, w, _ = now.isocalendar()
    key = f"{y}-W{w:02d}"
    with engine.begin() as conn:
        st = job_state(conn, "walkforward")
    if st.get("last_week") == key:
        return
    try:
        from app.backtest.walkforward import run_all
        out = run_all(engine)
        with engine.begin() as conn:
            save_job_state(conn, "walkforward", {"last_week": key, "run_id": out["run_id"]}, status="idle")
    except Exception:
        log.exception("walk-forward failed")


PAPER_HOUR_UTC = 22  # after the US close, once the day's prices exist
TENK_HOUR_UTC = 11
PROPOSAL_EVERY_S = 1800


def _alpha_tasks(engine, now: datetime, last_scan: list[float]) -> None:
    """MEDIUM priority: 10-K live pass, insider profiles, paper proposals and the paper ledger.
    Every task is isolated: one failing never blocks the others or the discovery schedule."""
    with engine.begin() as conn:
        st = job_state(conn, "alpha_daily")
    today = now.date().isoformat()
    tasks = []
    if st.get("tenk") != today and now.hour >= TENK_HOUR_UTC:
        from app.tenk.engine import run_live
        tasks.append(("tenk", lambda: run_live(engine)))
    if st.get("insider_profiles") != today and now.hour >= TENK_HOUR_UTC:
        from app.signals.insider_profile import refresh_all
        tasks.append(("insider_profiles", lambda: refresh_all(engine)))
    if st.get("paper") != today and now.hour >= PAPER_HOUR_UTC:
        from app.paper.ledger import run as paper_run
        tasks.append(("paper", lambda: paper_run(engine)))
    for name, fn in tasks:
        try:
            res = fn()
            log.info("%s done: %s", name, res)
            if isinstance(res, dict) and res.get("index_failed"):
                # SEC index days failed: they stay in the persistent retry list; the task is re-run on the
                # next loop instead of being marked done for today, and the failure is visible in health
                with engine.begin() as conn:
                    health_failure(conn, f"alpha:{name}", f"daily index unavailable: {res['index_failed']}")
                continue
            with engine.begin() as conn:
                st = job_state(conn, "alpha_daily")
                save_job_state(conn, "alpha_daily", {**st, name: today, f"{name}_result": res}, status="idle")
                health_success(conn, f"alpha:{name}")
        except Exception as e:
            log.exception("%s failed", name)
            with engine.begin() as conn:
                health_failure(conn, f"alpha:{name}", f"{type(e).__name__}: {e}")
    try:  # forward market snapshot of fresh live events (V2 #73)
        from app.core.snapshots import capture_pending
        snap = capture_pending(engine, limit=50)
        if snap:
            log.info("market snapshots %s", snap)
    except Exception:
        log.exception("market snapshots failed")
    if st.get("paper") == today and not st.get("paper_report_sent") == today:
        try:
            from app.notifications.dispatch import deliver
            from app.reports_v2 import paper_report
            msg = paper_report(engine, now.date())
            if msg:
                deliver(engine, f"report:paper:{today}", "report", msg)
            with engine.begin() as conn:
                st2 = job_state(conn, "alpha_daily")
                save_job_state(conn, "alpha_daily", {**st2, "paper_report_sent": today}, status="idle")
        except Exception:
            log.exception("paper report failed")
    if time.time() - last_scan[0] >= PROPOSAL_EVERY_S:
        last_scan[0] = time.time()
        try:
            from app.paper.proposals import expire, notify_pending, scan_new_events
            expire(engine)
            r = scan_new_events(engine)
            n = notify_pending(engine)
            if r.get("created") or n:
                log.info("paper proposals %s, notified %s", r, n)
        except Exception:
            log.exception("paper proposal scan failed")


def main() -> None:
    logging_setup.setup()
    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)
    engine = get_engine()
    import threading

    from app.pricesources.loop import free_prices_loop
    threading.Thread(target=free_prices_loop, args=(engine, lambda: _stop), name="free-prices", daemon=True).start()
    last_scan = [0.0]
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
        _weekly_walkforward(engine, now)
        _alpha_tasks(engine, now, last_scan)
        HEARTBEAT.write_text(str(int(time.time())))
        for _ in range(30):
            if _stop:
                break
            time.sleep(10)


if __name__ == "__main__":
    main()
