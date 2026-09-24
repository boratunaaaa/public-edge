"""Event-study backtest.

NO LOOK-AHEAD: the only date that may drive entry is the moment the information became
public (filing acceptance / publication). The transaction date is deliberately not an
argument of any function here, so it cannot leak into prices.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from app.marketdata.calendar import first_tradable_session
from app.marketdata.provider import Bar

HORIZONS = (1, 7, 30, 60, 90, 180, 365)


@dataclass
class HorizonReturn:
    horizon_days: int
    entry_date: date
    entry_price: float
    exit_date: date
    exit_price: float
    ret: float
    spy_ret: float | None
    excess_ret: float | None
    mae: float | None
    mfe: float | None


def _adj_factor(b: Bar) -> float:
    if b.adj_close and b.close:
        return b.adj_close / b.close
    return 1.0


def _adj_open(b: Bar) -> float | None:
    return b.open * _adj_factor(b) if b.open else None


def _adj_close(b: Bar) -> float | None:
    return b.adj_close if b.adj_close else b.close


def entry_bar(bars: list[Bar], public_date: date, public_at: datetime | None) -> Bar | None:
    """First bar on/after the first session that opens after publication.

    If the theoretical session has no bar (halt / missing data) we take the next available
    bar - never an earlier one.
    """
    target = first_tradable_session(public_date, public_at)
    for b in bars:
        if b.date >= target and b.open:
            return b
    return None


def _exit_bar(bars: list[Bar], entry: Bar, horizon: int, as_of: date) -> Bar | None:
    target = entry.date + timedelta(days=horizon)
    if target > as_of:
        return None  # horizon not yet elapsed - never extrapolate
    cand = [b for b in bars if entry.date < b.date <= target and b.close]
    if horizon == 1 and not cand:
        cand = [b for b in bars if b.date > entry.date and b.close][:1]
    if not cand:
        return None
    last = cand[-1]
    # Require the exit bar to be reasonably close to the target (data gap guard).
    if (target - last.date).days > 7 and horizon > 1:
        return None
    return last


def compute_returns(bars: list[Bar], spy_bars: list[Bar] | None, public_date: date, public_at: datetime | None,
                    as_of: date | None = None, horizons: tuple[int, ...] = HORIZONS) -> list[HorizonReturn]:
    bars = sorted((b for b in bars if b.close), key=lambda b: b.date)
    if not bars:
        return []
    as_of = as_of or bars[-1].date
    e = entry_bar(bars, public_date, public_at)
    if e is None:
        return []
    ep = _adj_open(e)
    if not ep or ep <= 0:
        return []
    spy = {b.date: b for b in (spy_bars or [])}
    spy_e = spy.get(e.date)
    out = []
    for h in horizons:
        x = _exit_bar(bars, e, h, as_of)
        if x is None:
            continue
        xp = _adj_close(x)
        if not xp:
            continue
        ret = xp / ep - 1.0
        window = [b for b in bars if e.date <= b.date <= x.date]
        lows = [b.low * _adj_factor(b) for b in window if b.low]
        highs = [b.high * _adj_factor(b) for b in window if b.high]
        mae = (min(lows) / ep - 1.0) if lows else None
        mfe = (max(highs) / ep - 1.0) if highs else None
        spy_ret = None
        spy_x = spy.get(x.date)
        if spy_e is not None and spy_x is not None and _adj_open(spy_e) and _adj_close(spy_x):
            spy_ret = _adj_close(spy_x) / _adj_open(spy_e) - 1.0
        out.append(HorizonReturn(h, e.date, ep, x.date, xp, ret, spy_ret,
                                 (ret - spy_ret) if spy_ret is not None else None, mae, mfe))
    return out


@dataclass
class Stats:
    n: int
    mean: float | None
    median: float | None
    std: float | None
    win_rate: float | None
    spy_beat_rate: float | None
    mean_excess: float | None
    median_excess: float | None
    avg_mae: float | None
    avg_mfe: float | None
    sufficient: bool

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def summarize(rows: list[dict], min_sample: int) -> Stats:
    """rows: dicts with ret, excess_ret, mae, mfe."""
    rets = [r["ret"] for r in rows if r.get("ret") is not None and math.isfinite(r["ret"])]
    n = len(rets)
    if n == 0:
        return Stats(0, None, None, None, None, None, None, None, None, None, False)
    ex = [r["excess_ret"] for r in rows if r.get("excess_ret") is not None]
    maes = [r["mae"] for r in rows if r.get("mae") is not None]
    mfes = [r["mfe"] for r in rows if r.get("mfe") is not None]
    return Stats(
        n=n,
        mean=statistics.fmean(rets),
        median=statistics.median(rets),
        std=statistics.stdev(rets) if n > 1 else None,
        win_rate=sum(1 for r in rets if r > 0) / n,
        spy_beat_rate=(sum(1 for r in ex if r > 0) / len(ex)) if ex else None,
        mean_excess=statistics.fmean(ex) if ex else None,
        median_excess=statistics.median(ex) if ex else None,
        avg_mae=statistics.fmean(maes) if maes else None,
        avg_mfe=statistics.fmean(mfes) if mfes else None,
        sufficient=n >= min_sample,
    )
