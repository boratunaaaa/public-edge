"""EDGAR "latest filings" Atom feed + document fetch."""
from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import datetime

from app import http
from app.parsers.sec_common import accession_from_url, full_text_url

FEED_URL = "https://www.sec.gov/cgi-bin/browse-edgar"


@dataclass
class FeedEntry:
    accession: str
    form_type: str
    cik: str
    role: str  # Issuer | Reporting | Subject | Filed by
    name: str
    updated: datetime | None
    url: str


_ENTRY_RE = re.compile(r"<entry>(.*?)</entry>", re.S)


def parse_feed(xml: str) -> list[FeedEntry]:
    out = []
    for m in _ENTRY_RE.finditer(xml):
        e = m.group(1)
        title = html.unescape(re.search(r"<title>(.*?)</title>", e, re.S).group(1))
        link = re.search(r'href="([^"]+)"', e).group(1)
        term = re.search(r'term="([^"]+)"', e)
        upd = re.search(r"<updated>([^<]+)</updated>", e)
        tm = re.match(r"(.+?) - (.*) \((\d{10})\) \(([^)]+)\)\s*$", title.strip())
        acc = accession_from_url(link)
        if not acc or not tm:
            continue
        out.append(FeedEntry(
            accession=acc, form_type=(term.group(1) if term else tm.group(1)).strip(), cik=tm.group(3), role=tm.group(4),
            name=tm.group(2).strip(), updated=datetime.fromisoformat(upd.group(1)) if upd else None, url=link,
        ))
    return out


def fetch_feed(form_type: str, *, count: int = 100, start: int = 0, owner: str = "include") -> list[FeedEntry]:
    r = http.get(FEED_URL, params={"action": "getcurrent", "type": form_type, "company": "", "dateb": "",
                                   "owner": owner, "start": start, "count": count, "output": "atom"})
    return parse_feed(r.text)


def fetch_new(form_type: str, is_known, max_pages: int = 6, owner: str = "include") -> list[FeedEntry]:
    """Walk the feed newest-first until a page is fully known. Deduplicate by accession."""
    seen: dict[str, FeedEntry] = {}
    for page in range(max_pages):
        entries = fetch_feed(form_type, start=page * 100, owner=owner)
        if not entries:
            break
        fresh = 0
        for e in entries:
            if e.accession in seen:
                continue
            if is_known(e.accession):
                continue
            seen[e.accession] = e
            fresh += 1
        if fresh == 0:
            break
    return list(seen.values())


def fetch_full_text(cik: str, accession: str) -> str:
    return http.get(full_text_url(cik, accession)).text
