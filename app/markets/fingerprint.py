"""Schema fingerprints + per-source health, so a silently changed page/feed format is detected.

A fingerprint is a hash of the STRUCTURE a parser depends on (table headers, JSON keys, KAP oda_* field
keys, sitemap tags) - not of the content. When it changes, or when a parser suddenly yields nothing from
a response that is not empty, the source is flagged instead of producing wrong data quietly.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone


def fingerprint(parts: list[str]) -> str:
    return hashlib.sha256("\x1f".join(sorted(set(parts))).encode()).hexdigest()[:16]


def json_paths(obj, prefix: str = "", depth: int = 3) -> list[str]:
    """Key paths of a JSON document (first element of lists), values ignored."""
    out = []
    if depth < 0:
        return out
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{prefix}.{k}" if prefix else k
            out.append(p)
            out += json_paths(v, p, depth - 1)
    elif isinstance(obj, list) and obj:
        out += json_paths(obj[0], prefix + "[]", depth - 1)
    return out


def html_table_headers(page: str) -> list[str]:
    return [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", h)).strip() for h in re.findall(r"<th[^>]*>(.*?)</th>", page, re.S)]


def sitemap_tags(xml: str) -> list[str]:
    return sorted(set(re.findall(r"<(news:[a-z_]+)>", xml)))


@dataclass
class SourceHealth:
    source: str
    expected_fingerprint: str | None = None
    last_fingerprint: str | None = None
    last_success: str | None = None
    last_event: str | None = None
    consecutive_failures: int = 0
    format_changed: bool = False
    status: str = "UNKNOWN"  # OK | DEGRADED | FORMAT_CHANGED | FAILING | BLOCKED
    notes: list[str] = field(default_factory=list)

    def observe(self, fp: str | None, n_items: int, n_events: int, now: datetime | None = None, blocked: bool = False,
                error: str | None = None) -> "SourceHealth":
        now_s = (now or datetime.now(timezone.utc)).isoformat()
        if blocked:
            self.status, self.consecutive_failures = "BLOCKED", self.consecutive_failures + 1
            self.notes = [f"blocked at {now_s}"]
            return self
        if error:
            self.consecutive_failures += 1
            self.status = "FAILING" if self.consecutive_failures >= 3 else "DEGRADED"
            self.notes = [error[:200]]
            return self
        self.last_fingerprint = fp
        if self.expected_fingerprint is None:
            self.expected_fingerprint = fp  # first successful observation defines the baseline
        self.format_changed = fp != self.expected_fingerprint
        if self.format_changed:
            self.status = "FORMAT_CHANGED"
            self.notes = [f"fingerprint {fp} != expected {self.expected_fingerprint}: parser output not trusted until reviewed"]
            return self
        if n_items > 0 and n_events == 0 and self.last_event is not None:
            self.status = "DEGRADED"  # the source answers, the parser suddenly extracts nothing
            self.notes = [f"{n_items} items but 0 events"]
        else:
            self.status, self.notes = "OK", []
        self.consecutive_failures = 0
        self.last_success = now_s
        if n_events:
            self.last_event = now_s
        return self

    def to_json(self) -> str:
        return json.dumps(asdict(self))


# baselines captured from the real fixtures (tests/fixtures/global) on 2026-09-26
EXPECTED = {
    "asx_list": ["ASX Code", "Date", "Headline", "PDF", "Pages", "Price sens."],
    "asx_markit": ["data", "data.displayName", "data.issueType", "data.items", "data.items[].announcementType", "data.items[].date",
                   "data.items[].documentKey", "data.items[].fileSize", "data.items[].headline", "data.items[].isPriceSensitive",
                   "data.items[].url", "data.symbol", "data.xid"],
    "newswire_ca": ["news:genres", "news:keywords", "news:language", "news:name", "news:news", "news:publication",
                    "news:publication_date", "news:stock_tickers", "news:title"],
}
