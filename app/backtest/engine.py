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
    exit_basis: str = "horizon"  # horizon | delisting_terminal


def _adj_factor(b: Bar) -> float:
    if b.adj_close and b.close:
        return b.adj_close / b.close
    return 1.0


def _adj_open(b: Bar) -> float | None:
    return b.open * _adj_factor(b) if b.open else None


def _adj_close(b: Bar) -> float | None:
    return b.adj_close if b.adj_close else b.close


def entry_bar(bars: list[Bar], public_date: date, public_at: datetime | None, calendar=None) -> Bar | None:
    """First bar on/after the first session that opens after publication.

    If the theoretical session has no bar (halt / missing data) we take the next available
    bar - never an earlier one. `calendar` (app.markets.calendars.MarketCalendar) selects the
    exchange; None = NYSE (US behaviour unchanged).
    """
    target = calendar.first_tradable_session(public_date, public_at) if calendar is not None \
        else first_tradable_session(public_date, public_at)
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


def _terminal_exit(bars: list[Bar], entry: Bar, horizon: int, as_of: date, terminal_date: date | None) -> Bar | None:
    """Delisted security: when the horizon ends after the documented delisting, the position ends at
    the last traded price (cash-out / final quote) - the CRSP-style delisting return - instead of
    silently disappearing from the sample."""
    if terminal_date is None or entry.date + timedelta(days=horizon) <= terminal_date:
        return None
    if entry.date + timedelta(days=horizon) > as_of:
        return None
    cand = [b for b in bars if entry.date < b.date <= terminal_date + timedelta(days=3) and b.close]
    if not cand or (terminal_date - cand[-1].date).days > 10:
        return None
    return cand[-1]


def compute_returns(bars: list[Bar], spy_bars: list[Bar] | None, public_date: date, public_at: datetime | None,
                    as_of: date | None = None, horizons: tuple[int, ...] = HORIZONS,
                    terminal_date: date | None = None, corporate_exit=None, calendar=None) -> list[HorizonReturn]:
    """terminal_date: documented delisting date (security master). Horizons that end after it exit at
    the last traded price; the benchmark still runs to the full horizon (cash earns nothing).
    corporate_exit (app.corpactions.CorporateExit): a verified merger consideration. A horizon that ends after
    the merger's effective date - with no price of the predecessor at the horizon - exits at that value
    (it takes precedence over the last-traded-price terminal exit)."""
    # vendor sanity: a non-positive close / adjusted close is corrupt data (seen: Yahoo adj_close < 0 for a
    # bankrupt issuer), never a price - such bars are dropped instead of producing a return below -100 %
    bars = sorted((b for b in bars if b.close and b.close > 0 and (b.adj_close is None or b.adj_close > 0)),
                  key=lambda b: b.date)
    if not bars:
        return []
    as_of = as_of or bars[-1].date
    e = entry_bar(bars, public_date, public_at, calendar)
    if e is None:
        return []
    ep = _adj_open(e)
    if not ep or ep <= 0:
        return []
    spy = {b.date: b for b in (spy_bars or [])}
    spy_e = spy.get(e.date)
    out = []
    spy_sorted = sorted(spy.values(), key=lambda b: b.date)
    for h in horizons:
        basis = "horizon"
        x = _exit_bar(bars, e, h, as_of)
        xp = None
        ce = corporate_exit
        if x is None and ce is not None and e.date < ce.effective_date < e.date + timedelta(days=h) \
                and e.date + timedelta(days=h) <= as_of:
            last = [b for b in bars if e.date <= b.date <= ce.effective_date]
            if last:  # value per share on the predecessor's adjusted basis of its last session
                xp = ce.value * _adj_factor(last[-1])
                x = Bar(ce.exit_date, xp, xp, xp, xp, xp, 0)
                basis = ce.basis
        if x is None:
            x = _terminal_exit(bars, e, h, as_of, terminal_date)
            basis = "delisting_terminal"
        if x is None:
            continue
        xp = xp or _adj_close(x)
        if not xp:
            continue
        ret = xp / ep - 1.0
        window = [b for b in bars if e.date <= b.date <= x.date]
        lows = [b.low * _adj_factor(b) for b in window if b.low]
        highs = [b.high * _adj_factor(b) for b in window if b.high]
        mae = (min(lows) / ep - 1.0) if lows else None
        mfe = (max(highs) / ep - 1.0) if highs else None
        spy_ret = None
        if basis == "delisting_terminal" or basis.startswith("merger_"):
            target = e.date + timedelta(days=h)
            later = [b for b in spy_sorted if e.date < b.date <= target]
            spy_x = later[-1] if later and (target - later[-1].date).days <= 7 else None
        else:
            spy_x = spy.get(x.date)
        if spy_e is not None and spy_x is not None and _adj_open(spy_e) and _adj_close(spy_x):
            spy_ret = _adj_close(spy_x) / _adj_open(spy_e) - 1.0
        out.append(HorizonReturn(h, e.date, ep, x.date, xp, ret, spy_ret,
                                 (ret - spy_ret) if spy_ret is not None else None, mae, mfe, basis))
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
