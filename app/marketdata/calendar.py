"""NYSE trading calendar (rule-based holidays + known special closures)."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)

# One-off closures (national days of mourning, etc.)
SPECIAL_CLOSURES = {
    date(2012, 10, 29), date(2012, 10, 30),  # Hurricane Sandy
    date(2018, 12, 5),  # G.H.W. Bush
    date(2025, 1, 9),  # J. Carter
}


def _easter(y: int) -> date:
    a = y % 19
    b, c = divmod(y, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(y, month, day)


def _nth_weekday(y: int, month: int, weekday: int, n: int) -> date:
    d = date(y, month, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def _last_weekday(y: int, month: int, weekday: int) -> date:
    d = date(y, month + 1, 1) - timedelta(days=1) if month < 12 else date(y, 12, 31)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _observed(d: date) -> date:
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


@lru_cache(maxsize=64)
def holidays(y: int) -> frozenset[date]:
    h = {
        _nth_weekday(y, 1, 0, 3),  # MLK
        _nth_weekday(y, 2, 0, 3),  # Presidents
        _easter(y) - timedelta(days=2),  # Good Friday
        _last_weekday(y, 5, 0),  # Memorial
        _observed(date(y, 7, 4)),
        _nth_weekday(y, 9, 0, 1),  # Labor
        _nth_weekday(y, 11, 3, 4),  # Thanksgiving
        _observed(date(y, 12, 25)),
    }
    ny = date(y, 1, 1)
    if ny.weekday() != 5:  # NYSE does not observe New Year's on the prior Friday
        h.add(_observed(ny))
    if y >= 2022:
        h.add(_observed(date(y, 6, 19)))
    h |= {d for d in SPECIAL_CLOSURES if d.year == y}
    return frozenset(h)


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in holidays(d.year)


def next_trading_day(d: date) -> date:
    d += timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d


def first_tradable_session(public_date: date, public_at: datetime | None) -> date:
    """First session whose OPEN occurs strictly after the information became public.

    - exact timestamp known and before 09:30 ET on a trading day -> that day's open
    - otherwise (during/after session, or only a date known) -> next trading day's open
    Using only a date is treated conservatively as "published after the close".
    """
    if public_at is not None:
        local = public_at.astimezone(NY)
        d = local.date()
        if is_trading_day(d) and local.time() < MARKET_OPEN:
            return d
        return next_trading_day(d)
    return next_trading_day(public_date)
