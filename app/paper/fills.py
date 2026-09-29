"""Paper fill realism (V2 #52, point-in-time since fills.v3): liquidity class, spread, square-root market impact,
volume participation cap - all sized on what is KNOWN AT THE FILL (the open):

LIQUID    20-day median dollar volume of PRIOR sessions >= $20M     half-spread  5 bps, participation cap 5 %
NORMAL    >= $1M                                                   half-spread 25 bps, cap 5 %
ILLIQUID  < $1M (or fewer than 5 prior sessions with volume)        half-spread 100 bps, cap 2 %
participation = shares / prior-session median share volume (ADV); impact_bps = 10 * sqrt(participation / 1 %).
Total cost = max(legacy market-cap floor, half-spread + impact). An order above the cap is reduced to the cap.

The fill session's own total volume is known only at its close and is NEVER used (fills.v2 did: an intra-day
look-ahead). The only same-session fact is whether an opening trade printed at all (open present, volume > 0) -
known at 09:30: a session without it never fills (the order waits for the next session). Without any prior-session
volume estimate a BUY is not sized blind: no fill."""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass

MODEL_VERSION = "fills.v3-prior-session-adv"
CLASSES = {"LIQUID": (5.0, 0.05), "NORMAL": (25.0, 0.05), "ILLIQUID": (100.0, 0.02)}


@dataclass
class FillQuote:
    model: str
    spread_bps: float
    impact_bps: float
    cost_bps: float
    participation: float
    shares: float
    adv_shares: float | None
    capped: bool


def liquidity_class(prior_bars) -> tuple[str, float | None]:
    dv = [b.close * b.volume for b in prior_bars[-20:] if b.close and b.volume]
    adv = statistics.median([b.volume for b in prior_bars[-20:] if b.volume]) if any(b.volume for b in prior_bars[-20:]) else None
    if len(dv) < 5:
        return "ILLIQUID", adv
    m = statistics.median(dv)
    return ("LIQUID" if m >= 20e6 else "NORMAL" if m >= 1e6 else "ILLIQUID"), adv


def fillable(prior_bars, fill_bar) -> bool:
    """Can an order fill on this bar at all (sizing aside)? An opening trade printed and a prior-session volume
    estimate exists. The single rule used by quote() and by the ledger's chronological ordering."""
    return bool(fill_bar.open and fill_bar.volume and liquidity_class(prior_bars)[1])


def quote(prior_bars, fill_bar, desired_shares: float, floor_bps: float) -> FillQuote | None:
    """None = cannot fill on this bar (no opening trade, or no prior-session liquidity estimate)."""
    if not fillable(prior_bars, fill_bar):
        return None
    model, adv = liquidity_class(prior_bars)
    half_spread, cap = CLASSES[model]
    max_shares = cap * adv
    shares = min(desired_shares, max_shares)
    part = shares / adv
    impact = 10.0 * math.sqrt(part / 0.01)
    cost = max(floor_bps, half_spread + impact)
    return FillQuote(model, half_spread, impact, cost, part, shares, adv, shares < desired_shares)
