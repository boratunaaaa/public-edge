"""Paper fill realism (V2 #52): liquidity class, spread, square-root market impact, volume participation cap.

LIQUID    20-day median dollar volume >= $20M      half-spread  5 bps, participation cap 5 %
NORMAL    >= $1M                                   half-spread 25 bps, cap 5 %
ILLIQUID  < $1M (or unknown volume)                half-spread 100 bps, cap 2 %
impact_bps = 10 * sqrt(participation / 1 %). Total cost = max(legacy market-cap floor, half-spread + impact).
A fill day with zero volume never fills (the order waits); an order above the cap is reduced to the cap."""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass

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


def quote(prior_bars, fill_bar, desired_shares: float, floor_bps: float) -> FillQuote | None:
    """None = cannot fill on this bar (no volume)."""
    if not fill_bar.volume:
        return None
    model, adv = liquidity_class(prior_bars)
    half_spread, cap = CLASSES[model]
    max_shares = cap * fill_bar.volume
    shares = min(desired_shares, max_shares)
    part = shares / fill_bar.volume
    impact = 10.0 * math.sqrt(part / 0.01)
    cost = max(floor_bps, half_spread + impact)
    return FillQuote(model, half_spread, impact, cost, part, shares, adv, shares < desired_shares)
