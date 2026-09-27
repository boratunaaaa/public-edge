"""Collectors: fetch -> parse -> canonical events, with source health + schema fingerprints.

Development only (not wired into the production worker). A blocked or failing source never raises into the
caller: it returns an empty batch with a health record that says why.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from app import http
from app.markets import fingerprint as fpm
from app.markets.schema import CanonicalEvent

log = logging.getLogger(__name__)


@dataclass
class Batch:
    source: str
    events: list[CanonicalEvent] = field(default_factory=list)
    items: int = 0
    health: fpm.SourceHealth | None = None


def _get(url: str) -> tuple[int | None, str]:
    try:
        r = http.get(url, retries=1)
        return r.status_code, r.text
    except http.HttpError as e:
        return e.status, ""
    except Exception as e:  # network
        return None, type(e).__name__


def collect_asx_list(health: fpm.SourceHealth, page: str = "todayAnns", get=_get) -> Batch:
    from app.sources.au import asx
    st, body = get(asx.LIST_URL.format(page=page))
    b = Batch("asx_list", health=health)
    if st != 200:
        health.observe(None, 0, 0, error=f"HTTP {st}")
        return b
    fp = fpm.fingerprint(fpm.html_table_headers(body))
    anns = asx.parse_list_page(body)
    b.items = len(anns)
    b.events = [e for e in (asx.to_canonical(a) for a in anns) if e]
    health.observe(fp, b.items if "Company announcements published" not in body else 0, len(b.events))
    return b


def collect_newswire_ca(health: fpm.SourceHealth, pages: int = 3, get=_get) -> Batch:
    from app.sources.ca import newswire
    b = Batch("newswire_ca", health=health)
    tags: set[str] = set()
    for p in range(1, pages + 1):
        st, body = get(newswire.SITEMAP_URL.format(page=p))
        if st != 200:
            health.observe(None, 0, 0, error=f"HTTP {st} page {p}")
            return b
        tags |= set(fpm.sitemap_tags(body))
        rel = newswire.parse_sitemap(body)
        b.items += len(rel)
        b.events += [e for r in rel for e in newswire.to_canonical(r)]
    health.observe(fpm.fingerprint(sorted(tags)), b.items, len(b.events))
    return b


def collect_kap(health: fpm.SourceHealth, ids: list[int], get=_get) -> Batch:
    from app.sources.tr import kap
    b = Batch("kap", health=health)
    keys: set[str] = set()
    for i in ids:
        st, body = get(kap.PAGE_URL.format(id=i))
        if st == 666 or any(m in body for m in kap.WAF_MARKERS):
            health.observe(None, 0, 0, blocked=True)
            return b  # stop at the first block: never hammer a WAF
        if st != 200:
            continue
        try:
            d = kap.parse_page(body, str(i))
        except ValueError as e:
            health.notes.append(str(e)[:120])
            continue
        keys |= set(kap.skeleton_keys(body))
        b.items += 1
        b.events += kap.to_canonical(d)
    b.events = kap.dedupe(b.events)
    health.observe(fpm.fingerprint(sorted(keys)) if keys else None, b.items, len(b.events))
    return b


def to_jsonl(events: list[CanonicalEvent]) -> str:
    return "\n".join(json.dumps(e.to_json(), ensure_ascii=False, default=str) for e in events)
