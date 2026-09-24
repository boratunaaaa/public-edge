"""News item -> story cluster -> classification -> materiality -> event."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, desc, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection

from app.db.models import fundamentals_quarterly, news_items, news_story_clusters, universe_snapshots
from app.db.store import create_event, jsonable
from app.entities.registry import resolver
from app.news import classify as C
from app.news.cluster import choose_primary, public_time, same_story, tokens
from app.news.extract import clean_text, contract_value, tickers_in

log = logging.getLogger(__name__)
ALERT_MATERIALITY = {"HIGH", "CRITICAL"}


@dataclass
class RawItem:
    source: str
    tier: int
    guid: str
    url: str
    headline: str
    snippet: str
    published_at: datetime | None
    publisher: str | None = None
    tickers: list[str] | None = None  # when the source already knows the company (SEC 8-K, per-ticker feeds)
    ciks: list[str] | None = None
    exact_time: bool = False  # published_at is an authoritative timestamp (e.g. SEC acceptance)
    backfill: bool = False  # historical load: stored + backtested, never alerted


def _now() -> datetime:
    return datetime.now(timezone.utc)


def annual_revenue(conn: Connection, cik: str | None) -> float | None:
    """Trailing-4-quarter revenue from stored XBRL frames (None if fewer than 4 quarters)."""
    if not cik:
        return None
    rows = conn.execute(select(fundamentals_quarterly.c.val).where(and_(
        fundamentals_quarterly.c.cik == cik, fundamentals_quarterly.c.concept == "Revenue"))
        .order_by(desc(fundamentals_quarterly.c.period_end)).limit(4)).all()
    return float(sum(r[0] for r in rows)) if len(rows) == 4 else None


def market_cap_at(conn: Connection, cik: str | None, at: datetime) -> float | None:
    """Latest universe snapshot on/before the story date (point-in-time)."""
    if not cik:
        return None
    r = conn.execute(select(universe_snapshots.c.market_cap, universe_snapshots.c.as_of).where(and_(
        universe_snapshots.c.cik == cik, universe_snapshots.c.as_of <= at.date()))
        .order_by(desc(universe_snapshots.c.as_of)).limit(1)).first()
    if r and r[0] and (at.date() - r[1]).days <= 10:
        return float(r[0])
    # historical story: point-in-time shares (filed before the story) x price on that day
    ticker = resolver.ticker_for_cik(cik)
    if not ticker:
        return None
    try:
        from app.marketdata.provider import get_market_cap
        return get_market_cap(conn, ticker, at.date())
    except Exception as e:
        log.info("point-in-time market cap unavailable for %s: %s", ticker, e)
        return None


def resolve_companies(item: RawItem) -> list[tuple[str, str]]:
    """[(ticker, cik)] - explicit exchange tickers first, strict registry mapping only."""
    out: list[tuple[str, str]] = []
    if item.tickers:
        for t in item.tickers:
            cik = (item.ciks or [None])[0] or resolver.ticker_to_cik.get(t)
            if cik:
                out.append((t, cik))
        return out
    for t in tickers_in(f"{item.headline} {item.snippet}"):
        cik = resolver.ticker_to_cik.get(t)
        if cik and (t, cik) not in out:
            out.append((t, cik))
    return out


def ingest(conn: Connection, item: RawItem) -> list[int]:
    """Store one news item and update its story cluster(s). Returns new event ids."""
    headline = clean_text(item.headline)[:1000]
    snippet = clean_text(item.snippet)[:4000]
    companies = resolve_companies(item)
    if not companies:
        return []  # only news about US-listed companies is kept
    seen = _now()
    pub = item.published_at
    if pub is not None and pub.tzinfo is None:
        pub = pub.replace(tzinfo=timezone.utc)
    row = conn.execute(pg_insert(news_items).values(
        source=item.source, source_tier=item.tier, url=item.url, guid=item.guid[:300], headline=headline, snippet=snippet,
        publisher=item.publisher, original_published_at=pub, first_seen_at=seen,
        tickers=[t for t, _ in companies], ciks=[c for _, c in companies],
    ).on_conflict_do_nothing(index_elements=["source", "guid"]).returning(news_items.c.id)).first()
    if row is None:
        # already stored: a changed pubDate is an UPDATE time and never replaces the original time
        if pub is not None:
            conn.execute(news_items.update().where(and_(news_items.c.source == item.source, news_items.c.guid == item.guid[:300],
                                                        news_items.c.original_published_at != pub)).values(updated_at=pub))
        return []
    item_id = row[0]
    created: list[int] = []
    for ticker, cik in companies[:3]:  # a release naming many companies is attached to the first three
        created += _attach(conn, item_id, item, headline, snippet, pub, seen, ticker, cik)
    return created


def _attach(conn, item_id, item: RawItem, headline, snippet, pub, seen, ticker, cik) -> list[int]:
    toks = tokens(headline)
    t_item = pub if (pub and pub <= seen + timedelta(minutes=5)) else seen
    cands = conn.execute(select(news_story_clusters).where(and_(
        news_story_clusters.c.ticker == ticker,
        news_story_clusters.c.public_at >= t_item - timedelta(hours=72),
        news_story_clusters.c.public_at <= t_item + timedelta(hours=72),
    ))).mappings().all()
    cluster = next((c for c in cands if same_story(toks, t_item, list(c["title_tokens"]), c["public_at"])), None)
    if cluster is None:
        cid = conn.execute(news_story_clusters.insert().values(
            ticker=ticker, cik=cik, primary_item_id=item_id, public_at=t_item, headline=headline, title_tokens=toks,
            sources=[item.source]).returning(news_story_clusters.c.id)).scalar_one()
    else:
        cid = cluster["id"]
    conn.execute(news_items.update().where(news_items.c.id == item_id).values(cluster_id=cid))
    return refresh_cluster(conn, cid, is_backfill=item.backfill)


def refresh_cluster(conn: Connection, cid: int, is_backfill: bool = False) -> list[int]:
    cl = conn.execute(select(news_story_clusters).where(news_story_clusters.c.id == cid)).mappings().first()
    items = [dict(r) for r in conn.execute(select(news_items).where(news_items.c.cluster_id == cid)).mappings().all()]
    primary = choose_primary(items)
    pub_at = public_time(items)
    # classification uses the most authoritative text; a low-tier teaser cannot add hype on its own
    auth = [it for it in items if it["source_tier"] <= min(i["source_tier"] for i in items) + 1]
    text = " ".join(f"{it['headline']}. {it['snippet'] or ''}" for it in auth)
    classes = C.classify(text)
    agr = C.agreement_type(text)
    # a dollar figure is only a "contract value" when the story is about a contract
    value = contract_value(text) if set(classes) & {"NEW_CONTRACT", "GOVERNMENT_CONTRACT", "MAJOR_CUSTOMER", "DESIGN_WIN"} else None
    rev = annual_revenue(conn, cl["cik"])
    mcap = market_cap_at(conn, cl["cik"], pub_at)
    tier = primary["source_tier"]
    m = C.materiality(classes, agreement=agr, contract_value=value, market_cap=mcap, annual_revenue=rev, tier=tier)
    vals = dict(primary_item_id=primary["id"], public_at=pub_at, headline=primary["headline"], n_items=len(items),
                sources=sorted({it["source"] for it in items}), classes=classes, agreement=agr, binding=agr in C.BINDING if agr else None,
                contract_value=value, annual_revenue=rev, value_to_revenue=(value / rev) if (value and rev) else None,
                market_cap=mcap, value_to_mcap=(value / mcap) if (value and mcap) else None,
                mcap_bucket=C.mcap_bucket(mcap), materiality=m.level)
    conn.execute(news_story_clusters.update().where(news_story_clusters.c.id == cid).values(**vals))
    created: list[int] = []
    if m.level in ALERT_MATERIALITY and cl["event_id"] is None:
        exact = any(it["source"] == "sec_8k" for it in items) or (primary.get("original_published_at") is not None)
        payload = {"cluster_id": cid, "classes": classes, "agreement": agr, "binding": vals["binding"], "contract_value": value,
                   "annual_revenue": rev, "value_to_revenue": vals["value_to_revenue"], "market_cap": mcap,
                   "value_to_mcap": vals["value_to_mcap"], "mcap_bucket": vals["mcap_bucket"], "materiality": m.level,
                   "materiality_reasons": m.reasons, "n_items": len(items), "sources": vals["sources"],
                   "primary_source": primary["source"], "source_tier": tier, "headline": primary["headline"],
                   "snippet": (primary["snippet"] or "")[:400]}
        # focus is discovery-size companies: scaled/unknown-size names alert only when CRITICAL
        small = vals["mcap_bucket"] in ("MICRO", "EARLY_SMALL", "SMALL", "GROWTH")
        if vals["mcap_bucket"] == "BELOW_UNIVERSE":
            prio = 2  # under $50M: stored for research/backtest, never alerted
        elif small:
            prio = 7 if m.level == "CRITICAL" else 5
        else:
            prio = 5 if m.level == "CRITICAL" else 3
        eid = create_event(
            conn, event_type="MATERIAL_NEWS", dedupe_key=f"NEWS:{cid}", public_date=pub_at.date(), public_at=pub_at,
            ticker=cl["ticker"], company_name=resolver.cik_to_name.get(cl["cik"] or "", None), issuer_cik=cl["cik"],
            priority=prio, title=f"${cl['ticker']}: {' + '.join(c for c in classes if c != 'OTHER')[:80]} — {primary['headline'][:140]}",
            payload=jsonable(payload), source_url=primary["url"], date_quality="exact" if exact else "first_seen",
            is_backfill=is_backfill,
        )
        if eid:
            conn.execute(news_story_clusters.update().where(news_story_clusters.c.id == cid).values(event_id=eid))
            created.append(eid)
            from app.discovery.thesis import on_material_news  # late import: discovery depends on news
            on_material_news(conn, cl["cik"], cl["ticker"], cid, classes, agr, m.level, primary["url"], primary["headline"])
            from app.signals.detect import multi_signal_check
            ms = multi_signal_check(conn, cl["ticker"], pub_at.date(), is_backfill=is_backfill)
            if ms:
                created.append(ms)
    return created
