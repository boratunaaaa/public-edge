"""Free historical price recovery loop (identity-gated). Runs as a thread of the INTELLIGENCE process:
the backfill process parses multi-MB 10-K HTML with regexes that hold the GIL for long stretches, which
halved the Tiingo request rate there."""
from __future__ import annotations

import logging
import shutil
import time

from app.db.store import job_state, save_job_state

log = logging.getLogger(__name__)


def free_prices_loop(engine, stop=lambda: False) -> None:
    """FINSABER open dataset once, Alpha Vantage delisting list monthly (key optional), then Tiingo free-tier
    batches while quota remains. Stops while free disk is under 10 GB."""
    from app.pricesources import alphavantage, finsaber, tiingo
    while not stop():
        pause = 3600
        try:
            if shutil.disk_usage("/").free < 10 * 1024**3:
                log.warning("free price recovery paused: < 10 GB free disk")
            else:
                with engine.begin() as conn:
                    st = job_state(conn, "free_prices")
                if not st.get("finsaber_done"):
                    r = finsaber.run(engine)
                    log.info("finsaber recovery %s", r)
                    with engine.begin() as conn:
                        save_job_state(conn, "free_prices", {**st, "finsaber_done": True, "finsaber": r}, status="idle")
                log.info("alpha vantage listing %s", alphavantage.refresh(engine))
                r = tiingo.run(engine)
                log.info("tiingo free batch %s", r)
                # keep going while there is quota (the host bucket + quota ledger enforce the free-tier rate);
                # back off only when a limit is reached or nothing is left to try
                stopped = r.get("stopped")
                if r.get("skipped"):
                    pause = 6 * 3600
                elif stopped == "MONTHLY_SYMBOL_LIMIT":
                    pause = 12 * 3600
                elif stopped in ("PER_HOUR_LIMIT", "PER_DAY_LIMIT"):
                    pause = 900
                elif r.get("symbols"):
                    pause = 30
                else:
                    pause = 6 * 3600
        except Exception:
            log.exception("free price recovery failed")
        for _ in range(pause // 10):
            if stop():
                return
            time.sleep(10)
