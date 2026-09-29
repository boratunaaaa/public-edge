"""Replay input data: an immutable snapshot, identified by a content hash (reproducibility).

A snapshot holds everything the run may EVER see; visibility.py decides WHEN each piece becomes visible. Future labels
that production attached later (supersession, needs_review / identity audits, computed returns, price-symbol choices)
are deliberately not part of an event record - the replay re-derives what it needs point in time."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from app.marketdata.provider import Bar, ShareFact

# the event columns a replay may know at publication time (no returns / review / supersession labels)
EVENT_FIELDS = ("id", "event_type", "dedupe_key", "ticker", "company_name", "issuer_cik", "public_date", "public_at",
                "transaction_date", "title", "payload", "source_url", "date_quality", "available_at", "event_family",
                "country", "exchange", "source_accession", "accepted_at", "priority")


@dataclass
class SourceData:
    events: list[dict] = field(default_factory=list)
    bars: dict[str, list[Bar]] = field(default_factory=dict)
    symbols: list[dict] = field(default_factory=list)  # security_symbols rows: cik, symbol, first_seen, last_seen, source
    companies: dict[str, str] = field(default_factory=dict)  # cik -> name
    share_facts: dict[str, list[ShareFact]] = field(default_factory=dict)
    security_master: list[dict] = field(default_factory=list)  # cik, name, sector, delisting_date, delisting_reason
    corporate_actions: list[dict] = field(default_factory=list)  # cik, action, action_date, accession, source
    label: str = "fixture"

    def __post_init__(self) -> None:
        for e in self.events:
            unknown = set(e) - set(EVENT_FIELDS)
            if unknown:
                raise ValueError(f"event {e.get('id')}: fields not allowed in a replay snapshot {sorted(unknown)}")
            if e.get("available_at") is None:
                raise ValueError(f"event {e.get('id')}: available_at is required (the only visibility anchor)")

    def snapshot_id(self) -> str:
        return hashlib.sha256(json.dumps(_plain(self), sort_keys=True, default=str).encode()).hexdigest()[:16]


def _plain(src: SourceData) -> dict:
    return {"events": src.events, "symbols": src.symbols, "companies": src.companies,
            "security_master": src.security_master, "corporate_actions": src.corporate_actions,
            "bars": {s: [list(b.__dict__.values()) for b in bs] for s, bs in sorted(src.bars.items())},
            "share_facts": {c: [list(f.__dict__.values()) for f in fs] for c, fs in sorted(src.share_facts.items())}}


def _d(x):
    return date.fromisoformat(x) if isinstance(x, str) and len(x) == 10 else x


def _dt(x):
    return datetime.fromisoformat(x) if isinstance(x, str) else x


def load_snapshot(path: str | Path) -> SourceData:
    """JSON written by scripts/replay_export_snapshot.py (read-only export)."""
    raw = json.loads(Path(path).read_text())
    events = []
    for e in raw["events"]:
        e = {k: v for k, v in e.items() if k in EVENT_FIELDS}
        for k in ("public_date", "transaction_date"):
            e[k] = _d(e.get(k))
        for k in ("public_at", "available_at", "accepted_at"):
            e[k] = _dt(e.get(k))
        events.append(e)
    bars = {s: [Bar(_d(b[0]), *b[1:]) for b in bs] for s, bs in raw["bars"].items()}
    facts = {c: [ShareFact(f[0], _d(f[1]), _d(f[2]), f[3], int(f[4])) for f in fs] for c, fs in raw.get("share_facts", {}).items()}
    symbols = [{**s, "first_seen": _d(s["first_seen"]), "last_seen": _d(s["last_seen"])} for s in raw.get("symbols", [])]
    sm = [{**r, "delisting_date": _d(r.get("delisting_date"))} for r in raw.get("security_master", [])]
    ca = [{**r, "action_date": _d(r["action_date"])} for r in raw.get("corporate_actions", [])]
    return SourceData(events=events, bars=bars, symbols=symbols, companies=raw.get("companies", {}), share_facts=facts,
                      security_master=sm, corporate_actions=ca, label=raw.get("label", Path(path).name))
