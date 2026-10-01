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
    # exact acceptance time of the filing behind a share fact (accession -> aware datetime), where known (10-K
    # filings: tenk_filings.accepted_at). A fact without one is known by its filing DATE only (visibility.share_fact_visible)
    acceptance: dict[str, datetime] = field(default_factory=dict)
    security_master: list[dict] = field(default_factory=list)  # cik, name, sector, delisting_date, delisting_reason
    # cik, action, action_date, accession, source, accepted_at (exact SEC acceptance time when known; a row without one
    # is public at the end of its filing day). actions_source = where they come from: "sec_filings" = derived with the
    # production mapping from the COMPLETE SEC filing history (app.replay.actions) - the only source that can make
    # FINANCING_ACTIONS complete; "corporate_actions" = production's table (recent submissions block only)
    corporate_actions: list[dict] = field(default_factory=list)
    actions_source: str = "corporate_actions"
    # price identity catalog: which stored vendor series holds WHICH issuer's prices for WHICH dates
    # ({"series", "cik", "valid_from", "valid_to"}; None = open-ended). PriceIndex adds a symbol-period entry for every
    # (issuer, stored symbol) the catalog does not list explicitly (derive_series).
    series: list[dict] = field(default_factory=list)
    # point-in-time reconstructed risk labels (app.replay.risk_history.RiskLabel, each with source provenance) and
    # the per-family coverage of the reconstruction for this snapshot's issuers (risk_history.Coverage; None = none)
    risk_labels: list = field(default_factory=list)
    risk_cov: object | None = None
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


BENCHMARK_SERIES = frozenset({"SPY", "XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY"})
SERIES_GRACE_DAYS = 120  # same grace as a filing-observed symbol period


def derive_series(symbols: list[dict], bars: dict) -> list[dict]:
    """Without an explicit catalog: a series stored under a symbol belongs to the issuer that used that symbol, for
    that period only (plus the usual grace) - never to whoever uses the symbol at another time."""
    from datetime import timedelta
    out = []
    for s in symbols:
        if s["symbol"] in bars:
            out.append({"series": s["symbol"], "cik": s["cik"],
                        "valid_from": s["first_seen"] - timedelta(days=SERIES_GRACE_DAYS),
                        "valid_to": s["last_seen"] + timedelta(days=SERIES_GRACE_DAYS)})
    return out


def _plain(src: SourceData) -> dict:
    return {"events": src.events, "symbols": src.symbols, "companies": src.companies, "series": src.series,
            "risk_labels": sorted(tuple(str(v) for v in x.__dict__.values()) for x in src.risk_labels),
            "risk_cov": getattr(src.risk_cov, "families", None),
            "security_master": src.security_master, "corporate_actions": src.corporate_actions,
            # only when not the default: snapshots exported before the field keep their id
            **({"actions_source": src.actions_source} if src.actions_source != "corporate_actions" else {}),
            "bars": {s: [list(b.__dict__.values()) for b in bs] for s, bs in sorted(src.bars.items())},
            "share_facts": {c: [list(f.__dict__.values()) for f in fs] for c, fs in sorted(src.share_facts.items())},
            "acceptance": {k: str(v) for k, v in sorted(src.acceptance.items())}}


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
    ca = [{**r, "action_date": _d(r["action_date"]), **({"accepted_at": _dt(r["accepted_at"])} if r.get("accepted_at") else {})}
          for r in raw.get("corporate_actions", [])]
    series = [{**x, "valid_from": _d(x.get("valid_from")), "valid_to": _d(x.get("valid_to"))} for x in raw.get("series", [])]
    acceptance = {k: _dt(v) for k, v in (raw.get("share_fact_acceptance") or {}).items() if v}
    return SourceData(events=events, bars=bars, symbols=symbols, companies=raw.get("companies", {}), share_facts=facts,
                      acceptance=acceptance, security_master=sm, corporate_actions=ca, series=series,
                      actions_source=raw.get("actions_source", "corporate_actions"),
                      label=raw.get("label", Path(path).name))
