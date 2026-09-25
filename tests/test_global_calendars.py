"""Exchange calendars: validated against the trading days present in real daily price data (Yahoo, 2021-2026,
zero-volume provider fill-ins removed)."""
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.markets.calendars import CALENDARS, calendar_for

FX = Path(__file__).parent / "fixtures" / "global" / "prices"


def _days(sym):
    return {date.fromisoformat(x) for x in json.load(open(FX / f"{sym}.json"))["days"]}


def _mismatch(sym, code):
    days, cal = _days(sym), CALENDARS[code]
    b, x, out = max(days), min(days), []
    while x <= b:
        if cal.is_trading_day(x) != (x in days):
            out.append(x)
        x += timedelta(days=1)
    return out


@pytest.mark.parametrize("sym,code", [("THYAO.IS", "XIST"), ("ASELS.IS", "XIST"), ("RY.TO", "XTSE"), ("SHOP.TO", "XTSE"),
                                      ("BHP.AX", "XASX"), ("CBA.AX", "XASX")])
def test_calendar_matches_real_trading_days_2021_2026(sym, code):
    assert _mismatch(sym, code) == []


def test_bist_lunar_and_decree_closures():
    cal = CALENDARS["XIST"]
    assert not cal.is_trading_day(date(2024, 4, 9))  # arife declared a full holiday
    assert not cal.is_trading_day(date(2026, 5, 27))  # Kurban Bayramı (Yahoo shows a zero-volume fill-in)
    assert cal.is_trading_day(date(2023, 2, 8)) and not cal.is_trading_day(date(2023, 2, 9))  # earthquake halt
    assert not cal.is_trading_day(date(2025, 10, 29))


def test_asx_anzac_day_on_weekend_is_not_moved_and_kings_birthday():
    cal = CALENDARS["XASX"]
    assert cal.is_trading_day(date(2026, 4, 27))  # Anzac Day 2026 is a Saturday: no Monday substitute
    assert not cal.is_trading_day(date(2025, 4, 25))
    assert not cal.is_trading_day(date(2026, 6, 8))  # 2nd Monday of June


def test_tsx_victoria_day_and_boxing_day():
    cal = CALENDARS["XTSE"]
    assert not cal.is_trading_day(date(2026, 5, 18))  # Monday before May 25
    assert not cal.is_trading_day(date(2025, 12, 26))


def test_next_trading_day_skips_holidays():
    assert CALENDARS["XIST"].next_trading_day(date(2026, 5, 26)) == date(2026, 6, 1)
    assert CALENDARS["XASX"].next_trading_day(date(2025, 12, 24)) == date(2025, 12, 29)


def test_first_session_istanbul_before_and_after_open():
    cal = CALENDARS["XIST"]
    before = datetime(2026, 9, 25, 6, 30, tzinfo=timezone.utc)  # 09:30 Istanbul, before the 10:00 open
    after = datetime(2026, 9, 25, 15, 6, tzinfo=timezone.utc)  # 18:06 Istanbul (KAP evening disclosure)
    assert cal.first_tradable_session(date(2026, 9, 25), before) == date(2026, 9, 25)
    assert cal.first_tradable_session(date(2026, 9, 25), after) == date(2026, 9, 28)


def test_first_session_sydney_across_dst_and_date_line():
    cal = CALENDARS["XASX"]
    # 25 Sep 2026 19:29 Sydney (AEST, UTC+10) = 09:29 UTC; published after the close -> Monday 28 Sep
    assert cal.first_tradable_session(date(2026, 9, 25), datetime(2026, 9, 25, 9, 29, tzinfo=timezone.utc)) == date(2026, 9, 28)
    # 07:30 Sydney on a January (AEDT, UTC+11) trading day is before the 10:00 open -> same day
    assert cal.first_tradable_session(date(2026, 1, 13), datetime(2026, 1, 12, 20, 30, tzinfo=timezone.utc)) == date(2026, 1, 13)


def test_first_session_toronto_timestamp_conversion():
    cal = CALENDARS["XTSX"]
    ts = datetime.fromisoformat("2026-09-25T18:10:00-04:00")  # newswire sitemap timestamp with offset
    assert cal.local_date(ts) == date(2026, 9, 25)
    assert cal.first_tradable_session(date(2026, 9, 25), ts) == date(2026, 9, 28)


def test_nyse_delegates_to_existing_engine_calendar():
    from app.marketdata import calendar as nyse
    cal = calendar_for(None)
    assert cal.code == "XNYS"
    for d in (date(2025, 1, 9), date(2026, 7, 3), date(2026, 9, 25)):
        assert cal.is_trading_day(d) == nyse.is_trading_day(d)


def test_backtest_engine_uses_the_market_calendar():
    from app.backtest.engine import compute_returns
    from app.marketdata.provider import Bar
    days = sorted(d for d in _days("THYAO.IS") if date(2026, 5, 1) <= d <= date(2026, 9, 1))
    bars = [Bar(d, 10 + i * 0.1, 10 + i * 0.1, 10 + i * 0.1, 10 + i * 0.1, 10 + i * 0.1, 1) for i, d in enumerate(days)]
    pub = datetime(2026, 5, 26, 15, 0, tzinfo=timezone.utc)  # arife evening in Istanbul
    res = compute_returns(bars, None, date(2026, 5, 26), pub, as_of=date(2026, 9, 1), horizons=(30,), calendar=CALENDARS["XIST"])
    assert res and res[0].entry_date == date(2026, 6, 1)  # first session after Kurban Bayramı
