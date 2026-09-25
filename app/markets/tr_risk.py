"""BIST market-specific risk FEATURES (feature extraction only - no production score yet).

Inputs are canonical TR events (KAP) + optional price bars + security master fields. Every feature records
the evidence it came from, so a later scoring layer can be validated like the US risk engine.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from app.markets.schema import CanonicalEvent

BIST_DAILY_LIMIT = 0.10  # ±10% price band on the equity market (main segments)


@dataclass
class RiskFeatures:
    ticker: str
    as_of: date
    flags: dict = field(default_factory=dict)
    evidence: dict = field(default_factory=dict)


def extract(ticker: str, as_of: date, events: list[CanonicalEvent], bars=None, free_float: float | None = None,
            adv_try: float | None = None, lookback_days: int = 365) -> RiskFeatures:
    """Point-in-time: only events published on/before `as_of` and within the lookback are used."""
    f = RiskFeatures(ticker, as_of)
    evs = [e for e in events if e.ticker == ticker and e.published_at.date() <= as_of
           and e.published_at.date() >= as_of - timedelta(days=lookback_days)]

    def add(name, value, ev):
        f.flags[name] = value
        f.evidence.setdefault(name, []).extend(ev)

    tr = [e for e in evs if e.family == "TRADING_RESTRICTION"]
    active = []
    for e in tr:
        to = e.fields.get("to")
        if to and date.fromisoformat(to) >= as_of:
            active.append(e)
    if any(e.fields.get("measure") == "VBTS" for e in active):
        add("vbts_active", True, [e.source_id for e in active if e.fields.get("measure") == "VBTS"])
    if any(e.fields.get("gross_settlement") for e in active):
        add("gross_settlement", True, [e.source_id for e in active if e.fields.get("gross_settlement")])
    if any(e.fields.get("margin_ban") for e in active):
        add("credit_ban", True, [e.source_id for e in active if e.fields.get("margin_ban")])
    if any(e.fields.get("short_sale_ban") for e in active):
        add("short_sale_ban", True, [e.source_id for e in active if e.fields.get("short_sale_ban")])
    if any(e.fields.get("single_price") or e.fields.get("order_restriction") for e in active):
        add("order_restrictions", True, [e.source_id for e in active])
    vbts_12m = [e for e in tr if e.fields.get("measure") == "VBTS"]
    if vbts_12m:
        add("vbts_count_12m", len(vbts_12m), [e.source_id for e in vbts_12m])
    cb = [e for e in tr if e.fields.get("measure") == "CIRCUIT_BREAKER"]
    if cb:
        add("circuit_breaker_count_12m", len(cb), [e.source_id for e in cb])
    rights = [e for e in evs if e.family in ("RIGHTS_ISSUE", "PRIVATE_PLACEMENT", "CAPITAL_RAISE")]
    if rights:
        add("capital_raise_count_12m", len(rights), [e.source_id for e in rights])
        big = [e for e in rights if (e.fields.get("rate_pct") or 0) >= 100]
        if big:
            add("large_dilution_raise", True, [e.source_id for e in big])
    rp = [e for e in evs if e.fields.get("subtype") == "RELATED_PARTY_TRANSACTION"]
    if rp:
        add("related_party_disclosures_12m", len(rp), [e.source_id for e in rp])
    sells = [e for e in evs if e.family in ("MAJOR_HOLDER_SELL", "INSIDER_SELL")]
    if sells:
        add("major_holder_or_insider_selling_12m", len(sells), [e.source_id for e in sells])
    if free_float is not None:
        f.flags["free_float_pct"] = free_float
        if free_float < 0.15:
            add("low_free_float", True, ["security_master.free_float"])
    if adv_try is not None:
        f.flags["adv_try_30d"] = adv_try
        if adv_try < 5_000_000:
            add("low_liquidity", True, ["prices.adv_try_30d"])
    if bars:
        hist = sorted((b for b in bars if b.date <= as_of), key=lambda b: b.date)[-60:]
        cur, best = 0, 0
        for p, b in zip(hist, hist[1:]):
            r = b.close / p.close - 1 if p.close else 0
            cur = cur + 1 if abs(r) >= BIST_DAILY_LIMIT - 0.003 else 0  # closes at (or within 0.3% of) the band
            best = max(best, cur)
        if best >= 3:
            add("limit_run_days_max_60d", best, ["prices"])
    return f
