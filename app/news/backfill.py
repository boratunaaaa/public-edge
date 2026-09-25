"""Historical official news for the news backtest: 8-K current reports (exact SEC acceptance time).

Point-in-time universe (no current-universe survivorship):
  * every 8-K filer in the EDGAR daily index is a candidate - today's company list is never used;
  * a filer is included when the security master shows it LISTED on the filing date (a symbol on
    its own Form 3/4/5 filings around that date), so companies that were later delisted, acquired
    or renamed are in the sample with the symbol they used then;
  * market cap and revenue are computed from facts filed on/before the story date only.
Events carry universe_quality='point_in_time'. The earlier current-universe load stays flagged
'current_universe_survivorship_biased' and is excluded from every hit rate.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

from sqlalchemy.engine import Engine

from app import http
from app.db.store import job_state, save_job_state
from app.entities.registry import resolver
from app.news.sources import process_8k
from app.sources.sec import edgar
from app.sources.sec.backfill import DAILY_INDEX_URL, disk_ok

log = logging.getLogger(__name__)
JOB = "backfill_news_8k_pit"


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


def backfill_news_8k(engine: Engine, days: int = 365, max_filings: int = 20000) -> dict:
    from app.securitymaster.provider import SecSecurityMaster
    with engine.begin() as conn:
        st = job_state(conn, JOB)
        resolver.load(conn)
    done = set(st.get("done_days", []))
    totals = {k: st.get(k, 0) for k in ("filings", "events", "errors", "skipped_unlisted")}
    budget = max_filings
    d = date.today() - timedelta(days=1)
    stop = date.today() - timedelta(days=days)
    while d >= stop and budget > 0:  # newest first: recent history is most useful soonest
        key = d.isoformat()
        if key not in done and d.weekday() < 5:
            if not disk_ok():
                break
            for cik, acc in daily_8k(d):
                with engine.connect() as conn:
                    status = _filing_status(conn, acc)
                    if status == "pit":
                        continue
                    ticker = SecSecurityMaster(conn).resolve_ticker_at_date(cik, d)
                if not ticker:
                    totals["skipped_unlisted"] += 1  # not listed on that date per its own filings
                    continue
                try:
                    with engine.begin() as conn:
                        if status is not None:
                            # stored by the old current-universe load or live: rebuild from the stored
                            # headline + lead with point-in-time market cap / revenue (no re-download)
                            totals["events"] += len(rebuild_point_in_time(conn, acc))
                        else:
                            text = edgar.fetch_full_text(cik, acc)
                            totals["events"] += len(process_8k(conn, text, cik, ticker, backfill=True,
                                                               universe_quality="point_in_time"))
                            budget -= 1
                        _mark_pit(conn, acc)
                    totals["filings"] += 1
                except Exception as e:
                    totals["errors"] += 1
                    log.warning("8-K backfill %s: %s", acc, e)
            done.add(key)
            with engine.begin() as conn:
                save_job_state(conn, JOB, {"done_days": sorted(done), **totals, "last_day": key}, status="running")
        d -= timedelta(days=1)
    with engine.begin() as conn:
        save_job_state(conn, JOB, {"done_days": sorted(done), **totals}, status="done" if budget > 0 else "running")
    return totals


def _filing_status(conn, acc: str) -> str | None:
    from sqlalchemy import select
    from app.db.models import filings
    return conn.execute(select(filings.c.parse_status).where(filings.c.source == "sec8k", filings.c.accession == acc)).scalar()


def _mark_pit(conn, acc: str) -> None:
    from app.db.models import filings
    conn.execute(filings.update().where(filings.c.source == "sec8k", filings.c.accession == acc).values(parse_status="pit"))


def rebuild_point_in_time(conn, acc: str) -> list[int]:
    """Re-derive a stored historical 8-K story with point-in-time inputs. The old (current-universe)
    event is superseded, never deleted; live events are left untouched."""
    import json
    from sqlalchemy import and_, select
    from app.db.models import events, news_items, news_story_clusters
    from app.news.pipeline import refresh_cluster
    created: list[int] = []
    cids = {c for (c,) in conn.execute(select(news_items.c.cluster_id).where(and_(
        news_items.c.source == "sec_8k", news_items.c.guid == acc))).all() if c}
    for cid in cids:
        cl = conn.execute(select(news_story_clusters.c.event_id).where(news_story_clusters.c.id == cid)).first()
        if cl and cl[0]:
            ev = conn.execute(select(events.c.id, events.c.is_backfill, events.c.payload).where(events.c.id == cl[0])).mappings().first()
            if ev is None or not ev["is_backfill"] or (ev["payload"] or {}).get("universe_quality") == "point_in_time":
                continue
            conn.execute(events.update().where(events.c.id == ev["id"]).values(
                superseded=True, superseded_reason="NEWS_REBUILT_POINT_IN_TIME"))
            from app.db.models import data_cleanup_audit
            conn.execute(data_cleanup_audit.insert().values(
                run_id="news-pit-rebuild", action="SUPERSEDE", table_name="events", row_id=ev["id"],
                before={"universe_quality": (ev["payload"] or {}).get("universe_quality")},
                after=json.loads(json.dumps({"superseded": True})), reason="NEWS_REBUILT_POINT_IN_TIME", dry_run=False))
            conn.execute(news_story_clusters.update().where(news_story_clusters.c.id == cid).values(event_id=None))
        created += refresh_cluster(conn, cid, is_backfill=True, universe_quality="point_in_time")
        new_id = conn.execute(select(news_story_clusters.c.event_id).where(news_story_clusters.c.id == cid)).scalar()
        if cl and cl[0] and new_id:
            conn.execute(events.update().where(events.c.id == cl[0]).values(canonical_id=new_id))
    return created
