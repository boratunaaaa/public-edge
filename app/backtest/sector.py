"""Sector-matched benchmark (in addition to SPY).

Why: a SPY-relative result can be sector beta in disguise. An open-source defense-contract event
study (TanushReddy111/Defense_contracts_event_study, MIT) found that switching the benchmark from
SPY to the sector ETF moved the 1-month abnormal return from clearly positive to statistically
zero. PUBLIC EDGE therefore stores both: excess vs SPY and excess vs the issuer's sector ETF
(SPDR sector funds; SIC-based sector from the security master). Idea only - no code was copied.
"""
from __future__ import annotations

from datetime import date, timedelta

from app.marketdata.provider import Bar

SECTOR_ETF = {
    "TECHNOLOGY": "XLK", "HEALTH_CARE": "XLV", "PHARMA_BIOTECH": "XBI", "FINANCIALS": "XLF", "ENERGY_MINING": "XLE",
    "MATERIALS": "XLB", "INDUSTRIALS": "XLI", "CONSUMER": "XLY", "COMMUNICATIONS": "XLC", "UTILITIES": "XLU",
    "REAL_ESTATE": "XLRE", "AGRICULTURE": "XLP", "SERVICES": "XLI",
}


def etf_for_sector(sector: str | None) -> str | None:
    return SECTOR_ETF.get(sector or "")


def _adj_open(b: Bar) -> float | None:
    if not b.open:
        return None
    return b.open * (b.adj_close / b.close) if (b.adj_close and b.close) else b.open


def _adj_close(b: Bar) -> float | None:
    return b.adj_close or b.close


def benchmark_return(bars_by_date: dict[date, Bar], sorted_bars: list[Bar], entry: date, exit_: date,
                     exit_basis: str = "horizon", horizon: int | None = None) -> float | None:
    """Benchmark return over exactly the stock's holding period (same entry session open -> same exit
    session close). A delisting-terminal exit is benchmarked over the full horizon (cash earns 0)."""
    e = bars_by_date.get(entry)
    if exit_basis == "delisting_terminal" and horizon is not None:
        target = entry + timedelta(days=horizon)
        later = [b for b in sorted_bars if entry < b.date <= target]
        x = later[-1] if later and (target - later[-1].date).days <= 7 else None
    else:
        x = bars_by_date.get(exit_)
    if e is None or x is None:
        return None
    o, c = _adj_open(e), _adj_close(x)
    if not o or not c:
        return None
    return c / o - 1.0
