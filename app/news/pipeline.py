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
from app.news.cluster import (CONF_ORDER, canonical_url, choose_primary, is_same_story, lead_signature, public_time,
                              source_lineage, timestamp_confidence, tokens)
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
    universe_quality: str | None = None  # point_in_time for the listed-at-the-time historical load


def _now() -> datetime:
    return datetime.now(timezone.utc)


def annual_revenue(conn: Connection, cik: str | None, as_of: datetime | None = None) -> float | None:
    """Trailing revenue known at the story time.

    Live stories use the latest stored XBRL frames. A historical story uses only XBRL facts FILED
    on/before its date (app.discovery.pit_facts) - today's revenue is never applied to the past;
    when those facts are unavailable the value fails closed to None."""
    if not cik:
        return None
    if as_of is not None and as_of.date() < _now().date():
        from app.discovery.pit_facts import ttm_revenue_as_of
        return ttm_revenue_as_of(conn, cik, as_of.date())
    rows = conn.execute(select(fundamentals_quarterly.c.val).where(and_(
        fundamentals_quarterly.c.cik == cik, fundamentals_quarterly.c.concept == "Revenue"))
        .order_by(desc(fundamentals_quarterly.c.period_end)).limit(4)).all()
    return float(sum(r[0] for r in rows)) if len(rows) == 4 else None


def market_cap_at(conn: Connection, cik: str | None, at: datetime) -> float | None:
    """Latest universe snapshot on/before the story date, else point-in-time shares x the same CIK's
    price on that day (security master identity: never another company's reused symbol)."""
    if not cik:
        return None
    r = conn.execute(select(universe_snapshots.c.market_cap, universe_snapshots.c.as_of).where(and_(
        universe_snapshots.c.cik == cik, universe_snapshots.c.as_of <= at.date()))
        .order_by(desc(universe_snapshots.c.as_of)).limit(1)).first()
    if r and r[0] and (at.date() - r[1]).days <= 10:
        return float(r[0])
    try:
        from app.discovery.pit_facts import market_cap_pit
        return market_cap_pit(conn, cik, at.date())
    except Exception as e:
        log.info("point-in-time market cap unavailable for %s: %s", cik, e)
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
    tconf = timestamp_confidence(item.source, pub, seen, exact=item.exact_time, backfill=item.backfill, tier=item.tier)
    row = conn.execute(pg_insert(news_items).values(
        source=item.source, source_tier=item.tier, url=item.url, guid=item.guid[:300], headline=headline, snippet=snippet,
        publisher=item.publisher, original_published_at=pub, first_seen_at=seen,
        tickers=[t for t, _ in companies], ciks=[c for _, c in companies],
        timestamp_confidence=tconf, canonical_url=canonical_url(item.url),
        lead_signature=lead_signature(headline, snippet), source_lineage=source_lineage(item.source, headline, snippet, item.url),
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
    me = {"tokens": toks, "time": t_item, "canonical_url": canonical_url(item.url), "signature": lead_signature(headline, snippet),
          "lineage": source_lineage(item.source, headline, snippet, item.url)}
    cands = conn.execute(select(news_story_clusters).where(and_(
        news_story_clusters.c.ticker == ticker,
        news_story_clusters.c.public_at >= t_item - timedelta(hours=72),
        news_story_clusters.c.public_at <= t_item + timedelta(hours=72),
    ))).mappings().all()
    cluster = None
    for c in cands:
        members = conn.execute(select(news_items.c.headline, news_items.c.canonical_url, news_items.c.lead_signature,
                                      news_items.c.source_lineage, news_items.c.original_published_at, news_items.c.first_seen_at)
                               .where(news_items.c.cluster_id == c["id"])).mappings().all()
        views = [{"tokens": list(c["title_tokens"]), "time": c["public_at"], "canonical_url": None, "signature": None,
                  "lineage": None}] + [
            {"tokens": tokens(m["headline"]), "time": c["public_at"], "canonical_url": m["canonical_url"],
             "signature": m["lead_signature"], "lineage": m["source_lineage"]} for m in members]
        if any(is_same_story(me, v) for v in views):
            cluster = c
            break
    if cluster is None:
        cid = conn.execute(news_story_clusters.insert().values(
            ticker=ticker, cik=cik, primary_item_id=item_id, public_at=t_item, headline=headline, title_tokens=toks,
            sources=[item.source]).returning(news_story_clusters.c.id)).scalar_one()
    else:
        cid = cluster["id"]
    conn.execute(news_items.update().where(news_items.c.id == item_id).values(cluster_id=cid))
    return refresh_cluster(conn, cid, is_backfill=item.backfill, universe_quality=item.universe_quality)


def refresh_cluster(conn: Connection, cid: int, is_backfill: bool = False, universe_quality: str | None = None) -> list[int]:
    cl = conn.execute(select(news_story_clusters).where(news_story_clusters.c.id == cid)).mappings().first()
    items = [dict(r) for r in conn.execute(select(news_items).where(news_items.c.cluster_id == cid)).mappings().all()]
    primary = choose_primary(items)
    pub_at = public_time(items)
    # confidence of the timestamp that defines the story's public time (the best copy wins)
    tconf = max((it.get("timestamp_confidence") for it in items), key=lambda c: CONF_ORDER.get(c, 0)) if items else None
    firsts = [it["original_published_at"] for it in items if it.get("original_published_at")]
    # classification uses the most authoritative text; a low-tier teaser cannot add hype on its own
    auth = [it for it in items if it["source_tier"] <= min(i["source_tier"] for i in items) + 1]
    text = " ".join(f"{it['headline']}. {it['snippet'] or ''}" for it in auth)
    classes = C.classify(text)
    agr = C.agreement_type(text)
    # a dollar figure is only a "contract value" when the story is about a contract
    value = contract_value(text) if set(classes) & {"NEW_CONTRACT", "GOVERNMENT_CONTRACT", "MAJOR_CUSTOMER", "DESIGN_WIN"} else None
    rev = annual_revenue(conn, cl["cik"], pub_at)
    mcap = market_cap_at(conn, cl["cik"], pub_at)
    tier = primary["source_tier"]
    m = C.materiality(classes, agreement=agr, contract_value=value, market_cap=mcap, annual_revenue=rev, tier=tier)
    vals = dict(primary_item_id=primary["id"], public_at=pub_at, headline=primary["headline"], n_items=len(items),
                canonical_story=" ".join(tokens(primary["headline"]))[:500], first_published_at=min(firsts) if firsts else None,
                first_seen_at=min(it["first_seen_at"] for it in items), syndicated_count=max(0, len(items) - 1),
                timestamp_confidence=tconf,
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
                   "snippet": (primary["snippet"] or "")[:400], "timestamp_confidence": tconf,
                   "syndicated_count": vals["syndicated_count"],
                   "universe_quality": universe_quality or ("current_universe_survivorship_biased" if is_backfill else "live")}
        # focus is discovery-size companies: scaled/unknown-size names alert only when CRITICAL
        small = vals["mcap_bucket"] in ("MICRO", "EARLY_SMALL", "SMALL", "GROWTH")
        if vals["mcap_bucket"] == "BELOW_UNIVERSE":
            prio = 2  # under $50M: stored for research/backtest, never alerted
        elif small:
            prio = 7 if m.level == "CRITICAL" else 5
        else:
            prio = 5 if m.level == "CRITICAL" else 3
        eid = create_event(
            conn, event_type="MATERIAL_NEWS", dedupe_key=f"NEWS:{cid}" + (":pit" if universe_quality == "point_in_time" else ""),
            public_date=pub_at.date(), public_at=pub_at,
            ticker=cl["ticker"], company_name=resolver.cik_to_name.get(cl["cik"] or "", None), issuer_cik=cl["cik"],
            priority=prio, title=f"${cl['ticker']}: {' + '.join(c for c in classes if c != 'OTHER')[:80]} — {primary['headline'][:140]}",
            payload=jsonable(payload), source_url=primary["url"],
            date_quality="exact" if (exact and tconf == "HIGH") else ("date" if tconf == "MEDIUM" else "first_seen"),
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
