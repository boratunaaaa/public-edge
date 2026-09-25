"""Exchange calendars behind one interface; the shared backtest asks the calendar of the event's market.

XNYS delegates to the existing NYSE calendar (US behaviour unchanged). XIST / XTSE / XASX are rule-based
plus explicit tables for lunar and decree-based closures; every table was checked against the trading days
present in real daily price data (tests/fixtures/global/prices/*.json) - see tests/test_global_calendars.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.marketdata import calendar as nyse


def _easter(y: int) -> date:
    return nyse._easter(y)


def _nth(y, m, wd, n):
    return nyse._nth_weekday(y, m, wd, n)


def _mon_after_weekend(d: date) -> date:
    """Saturday/Sunday holiday observed on the following Monday."""
    return d + timedelta(days=(7 - d.weekday()) % 7) if d.weekday() >= 5 else d


@dataclass(frozen=True)
class MarketCalendar:
    code: str
    tz: str
    open_t: time
    close_t: time
    extra_closures: frozenset = field(default_factory=frozenset)
    half_days: frozenset = field(default_factory=frozenset)  # sessions that close early (still tradable)

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.tz)

    def holidays(self, y: int) -> set[date]:
        raise NotImplementedError

    def is_trading_day(self, d: date) -> bool:
        return d.weekday() < 5 and d not in self.holidays(d.year) and d not in self.extra_closures

    def next_trading_day(self, d: date) -> date:
        d += timedelta(days=1)
        while not self.is_trading_day(d):
            d += timedelta(days=1)
        return d

    def local(self, ts: datetime) -> datetime:
        return ts.astimezone(self.zone)

    def local_date(self, ts: datetime) -> date:
        return self.local(ts).date()

    def first_tradable_session(self, public_date: date, public_at: datetime | None) -> date:
        """First session whose OPEN is strictly after publication (same rule as the US engine)."""
        if public_at is not None:
            loc = self.local(public_at)
            if self.is_trading_day(loc.date()) and loc.time() < self.open_t:
                return loc.date()
            return self.next_trading_day(loc.date())
        return self.next_trading_day(public_date)


class NYSE(MarketCalendar):
    def holidays(self, y):
        return set(nyse.holidays(y))

    def is_trading_day(self, d):
        return nyse.is_trading_day(d)

    def first_tradable_session(self, public_date, public_at):
        return nyse.first_tradable_session(public_date, public_at)


# Turkey: Ramazan / Kurban Bayramı (lunar) - official dates; BIST also closes the afternoon of each arife
# (half day, still tradable) and on government "köprü" days when declared by decree.
TR_LUNAR = {
    2020: [(5, 24, 3), (7, 31, 4)], 2021: [(5, 13, 3), (7, 20, 4)], 2022: [(5, 2, 3), (7, 9, 4)],
    2023: [(4, 21, 3), (6, 28, 4)], 2024: [(4, 10, 3), (6, 16, 4)], 2025: [(3, 30, 3), (6, 6, 4)],
    2026: [(3, 20, 3), (5, 27, 4)], 2027: [(3, 9, 3), (5, 16, 4)],
}


class BIST(MarketCalendar):
    def holidays(self, y):
        h = {date(y, 1, 1), date(y, 4, 23), date(y, 5, 1), date(y, 5, 19), date(y, 7, 15), date(y, 8, 30), date(y, 10, 29)}
        if y < 2017:
            h.discard(date(y, 7, 15))  # Democracy Day is a public holiday from 2017
        for m, dd, n in TR_LUNAR.get(y, []):
            start = date(y, m, dd)
            h |= {start + timedelta(days=i) for i in range(n)}
        return h

    def arife(self, y) -> set[date]:
        return {date(y, m, dd) - timedelta(days=1) for m, dd, _ in TR_LUNAR.get(y, [])} | {date(y, 10, 28)}


class TSX(MarketCalendar):
    def holidays(self, y):
        h = {_mon_after_weekend(date(y, 1, 1)), _nth(y, 2, 0, 3), _easter(y) - timedelta(days=2),
             date(y, 5, 25) - timedelta(days=(date(y, 5, 25).weekday() - 0) % 7 or 7),  # Victoria Day: Monday before May 25
             _mon_after_weekend(date(y, 7, 1)), _nth(y, 8, 0, 1), _nth(y, 9, 0, 1), _nth(y, 10, 0, 2)}
        xmas, box = date(y, 12, 25), date(y, 12, 26)
        if xmas.weekday() == 5:
            h |= {date(y, 12, 27), date(y, 12, 28)}
        elif xmas.weekday() == 6:
            h |= {date(y, 12, 26), date(y, 12, 27)}
        elif xmas.weekday() == 4:
            h |= {xmas, date(y, 12, 28)}
        else:
            h |= {xmas, box}
        return h


class ASX(MarketCalendar):
    def holidays(self, y):
        e = _easter(y)
        h = {_mon_after_weekend(date(y, 1, 1)), _mon_after_weekend(date(y, 1, 26)), e - timedelta(days=2), e + timedelta(days=1),
             _nth(y, 6, 0, 2)}  # King's (Queen's) Birthday: 2nd Monday of June
        if date(y, 4, 25).weekday() < 5:
            h.add(date(y, 4, 25))  # Anzac Day: not moved when it falls on a weekend
        xmas, box = date(y, 12, 25), date(y, 12, 26)
        if xmas.weekday() == 5:
            h |= {date(y, 12, 27), date(y, 12, 28)}
        elif xmas.weekday() == 6:
            h |= {date(y, 12, 26), date(y, 12, 27)}
        elif xmas.weekday() == 4:
            h |= {xmas, date(y, 12, 28)}
        else:
            h |= {xmas, box}
        return h


# Closures that no rule produces, each confirmed by missing sessions in exchange price data.
BIST_DECREE_CLOSURES = frozenset({
    date(2023, 2, 9), date(2023, 2, 10), date(2023, 2, 13), date(2023, 2, 14),  # earthquake: halted after the 8 Feb session
    date(2024, 4, 9),  # Ramazan Bayramı arife declared a full holiday
})
ASX_EXTRA = frozenset({date(2022, 9, 22)})  # National Day of Mourning (Queen Elizabeth II)
TSX_EXTRA = frozenset()

CALENDARS: dict[str, MarketCalendar] = {
    "XNYS": NYSE("XNYS", "America/New_York", time(9, 30), time(16, 0)),
    "XIST": BIST("XIST", "Europe/Istanbul", time(10, 0), time(18, 0), BIST_DECREE_CLOSURES),
    "XTSE": TSX("XTSE", "America/Toronto", time(9, 30), time(16, 0), TSX_EXTRA),
    "XTSX": TSX("XTSX", "America/Toronto", time(9, 30), time(16, 0), TSX_EXTRA),
    "XCNQ": TSX("XCNQ", "America/Toronto", time(9, 30), time(16, 0), TSX_EXTRA),
    "XASX": ASX("XASX", "Australia/Sydney", time(10, 0), time(16, 0), ASX_EXTRA),
}


def calendar_for(exchange: str | None) -> MarketCalendar:
    return CALENDARS.get(exchange or "XNYS", CALENDARS["XNYS"])
