"""One-off scoring of news rows stored before timestamp confidence / story metadata existed."""
from __future__ import annotations

import json

from sqlalchemy import select, text
from sqlalchemy.engine import Engine

from app.db.models import news_items
from app.news.cluster import canonical_url, lead_signature, source_lineage, timestamp_confidence


def score_existing(engine: Engine, batch: int = 2000) -> dict:
    n = 0
    while True:
        with engine.begin() as conn:
            rows = conn.execute(select(news_items).where(news_items.c.timestamp_confidence.is_(None)).limit(batch)).mappings().all()
            if not rows:
                break
            for r in rows:
                # stored rows: SEC 8-K items carry the exact acceptance time; historical 8-K loads were seen long
                # after publication, which is expected for a backfill (not a timestamp problem)
                exact = r["source"] == "sec_8k"
                conf = timestamp_confidence(r["source"], r["original_published_at"], r["first_seen_at"], exact=exact,
                                            backfill=exact, tier=r["source_tier"])
                conn.execute(news_items.update().where(news_items.c.id == r["id"]).values(
                    timestamp_confidence=conf, canonical_url=canonical_url(r["url"]),
                    lead_signature=lead_signature(r["headline"], r["snippet"]),
                    source_lineage=source_lineage(r["source"], r["headline"], r["snippet"], r["url"])))
                n += 1
    with engine.begin() as conn:
        clusters = conn.execute(text("""
            UPDATE news_story_clusters c SET
              first_published_at = x.fp, first_seen_at = x.fs, syndicated_count = GREATEST(x.n - 1, 0),
              timestamp_confidence = x.conf, canonical_story = COALESCE(c.canonical_story, lower(c.headline))
            FROM (SELECT cluster_id, min(original_published_at) fp, min(first_seen_at) fs, count(*) n,
                         CASE WHEN bool_or(timestamp_confidence = 'HIGH') THEN 'HIGH'
                              WHEN bool_or(timestamp_confidence = 'MEDIUM') THEN 'MEDIUM' ELSE 'LOW' END conf
                  FROM news_items WHERE cluster_id IS NOT NULL GROUP BY cluster_id) x
            WHERE x.cluster_id = c.id AND c.timestamp_confidence IS NULL""")).rowcount
        events = conn.execute(text("""
            UPDATE events e SET payload = e.payload || jsonb_build_object('timestamp_confidence', c.timestamp_confidence,
                                                                          'syndicated_count', c.syndicated_count)
            FROM news_story_clusters c
            WHERE e.event_type = 'MATERIAL_NEWS' AND (e.payload->>'cluster_id')::bigint = c.id
              AND NOT (e.payload ? 'timestamp_confidence')""")).rowcount
    return {"items_scored": n, "clusters_updated": clusters, "events_updated": events}


def summary(engine: Engine) -> str:
    with engine.connect() as conn:
        return json.dumps([dict(r) for r in conn.execute(text(
            "SELECT timestamp_confidence, count(*) n FROM news_items GROUP BY 1")).mappings()], default=str)
