"""Historical official news for the news backtest: 8-K current reports (exact SEC acceptance time).

Restricted to companies in the latest discovery universe below $10B to bound the request budget.
Delisted companies are not in today's universe - a known survivorship limitation of this sample
(documented in the report); the backtest therefore labels its sample 'current-universe'.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

from sqlalchemy.engine import Engine

from app import http
from app.db.store import filing_exists, job_state, save_job_state
from app.discovery.evidence import universe_ciks
from app.entities.registry import resolver
from app.news.sources import process_8k
from app.sources.sec import edgar
from app.sources.sec.backfill import DAILY_INDEX_URL, disk_ok

log = logging.getLogger(__name__)


def daily_8k(d: date) -> list[tuple[str, str]]:
    url = DAILY_INDEX_URL.format(y=d.year, q=(d.month - 1) // 3 + 1, d=d.strftime("%Y%m%d"))
    try:
        txt = http.get(url, retries=2).text
    except http.HttpError as e:
        if e.status in (403, 404):
            return []
        raise
    out: dict[str, tuple[str, str]] = {}
    for line in txt.splitlines():
        if not line.startswith("8-K "):
            continue
        path = line.split()[-1]
        if path.startswith("edgar/"):
            acc = path.rsplit("/", 1)[-1].replace(".txt", "")
            out.setdefault(acc, (path.split("/")[2].zfill(10), acc))
    return list(out.values())


def backfill_news_8k(engine: Engine, days: int = 365) -> dict:
    name = "backfill_news_8k"
    with engine.begin() as conn:
        st = job_state(conn, name)
        uni = universe_ciks(conn, lo=50e6, hi=10e9)
        resolver.load(conn)
    if not uni:
        return {"skipped": "no universe snapshot yet"}
    done = set(st.get("done_days", []))
    totals = {"filings": st.get("filings", 0), "events": st.get("events", 0), "errors": st.get("errors", 0)}
    d = date.today() - timedelta(days=1)
    stop = date.today() - timedelta(days=days)
    while d >= stop:  # newest first: recent history is most useful soonest
        key = d.isoformat()
        if key not in done and d.weekday() < 5:
            if not disk_ok():
                break
            for cik, acc in daily_8k(d):
                if cik not in uni:
                    continue
                with engine.connect() as conn:
                    if filing_exists(conn, "sec8k", acc):
                        continue
                ticker = resolver.ticker_for_cik(cik)
                if not ticker:
                    continue
                try:
                    text = edgar.fetch_full_text(cik, acc)
                    with engine.begin() as conn:
                        totals["events"] += len(process_8k(conn, text, cik, ticker, backfill=True))
                    totals["filings"] += 1
                except Exception as e:
                    totals["errors"] += 1
                    log.warning("8-K backfill %s: %s", acc, e)
            done.add(key)
            with engine.begin() as conn:
                save_job_state(conn, name, {"done_days": sorted(done), **totals, "last_day": key}, status="running")
        d -= timedelta(days=1)
    with engine.begin() as conn:
        save_job_state(conn, name, {"done_days": sorted(done), **totals}, status="done")
    return totals
