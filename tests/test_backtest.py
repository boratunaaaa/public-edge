"""Backtest engine + trading calendar tests, including NO LOOK-AHEAD guarantees."""
import inspect
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.backtest import engine as bt
from app.backtest.engine import compute_returns, entry_bar, summarize
from app.marketdata.calendar import first_tradable_session, holidays, is_trading_day, next_trading_day
from app.marketdata.provider import Bar

NY = ZoneInfo("America/New_York")


def series(start: date, days: int, base: float = 100.0, step: float = 1.0) -> list[Bar]:
    """Synthetic *test-only* price path: open = base+i, close = open+0.5 on every trading day."""
    out, d, i = [], start, 0
    while len(out) < days:
        if is_trading_day(d):
            o = base + i * step
            out.append(Bar(d, o, o + 2, o - 2, o + 0.5, o + 0.5, 1000))
            i += 1
        d += timedelta(days=1)
    return out


# ---------------- calendar ----------------

def test_known_nyse_holidays():
    h = holidays(2026)
    assert date(2026, 1, 1) in h and date(2026, 1, 19) in h  # New Year, MLK
    assert date(2026, 4, 3) in h  # Good Friday 2026
    assert date(2026, 6, 19) in h and date(2026, 7, 3) in h  # Juneteenth, July 4 observed (Fri)
    assert date(2026, 11, 26) in h and date(2026, 12, 25) in h
    assert date(2025, 1, 9) in holidays(2025)  # special closure
    assert date(2021, 6, 18) not in holidays(2021)  # Juneteenth not observed before 2022


def test_new_year_on_saturday_not_observed_on_friday():
    assert date(2021, 12, 31) not in holidays(2021) and date(2022, 1, 1) not in holidays(2022)


def test_weekend_and_holiday_skipping():
    assert not is_trading_day(date(2026, 9, 26))  # Saturday
    assert next_trading_day(date(2026, 9, 25)) == date(2026, 9, 28)  # Fri -> Mon
    assert next_trading_day(date(2026, 4, 2)) == date(2026, 4, 6)  # Thu -> (Good Friday) -> Mon


def test_first_tradable_session_uses_timestamp():
    pre_open = datetime(2026, 9, 24, 8, 0, tzinfo=NY)
    in_session = datetime(2026, 9, 24, 10, 0, tzinfo=NY)
    after_close = datetime(2026, 9, 24, 16, 31, tzinfo=NY)
    assert first_tradable_session(date(2026, 9, 24), pre_open) == date(2026, 9, 24)
    assert first_tradable_session(date(2026, 9, 24), in_session) == date(2026, 9, 25)
    assert first_tradable_session(date(2026, 9, 24), after_close) == date(2026, 9, 25)
    # Friday evening filing -> Monday
    assert first_tradable_session(date(2026, 9, 25), datetime(2026, 9, 25, 18, 0, tzinfo=NY)) == date(2026, 9, 28)
    # date only -> conservatively next session, never same day
    assert first_tradable_session(date(2026, 9, 24), None) == date(2026, 9, 25)
    # UTC timestamp converted to NY before comparing to the open
    assert first_tradable_session(date(2026, 9, 24), datetime(2026, 9, 24, 13, 0, tzinfo=ZoneInfo("UTC"))) == date(2026, 9, 24)


# ---------------- NO LOOK-AHEAD ----------------

def test_no_look_ahead_transaction_date_cannot_influence_entry():
    # the engine API has no transaction-date parameter at all
    for fn in (bt.compute_returns, bt.entry_bar):
        assert "transaction" not in " ".join(inspect.signature(fn).parameters)


def test_no_look_ahead_entry_is_after_filing_not_transaction():
    bars = series(date(2026, 1, 2), 300)
    transaction_date = date(2026, 3, 2)  # insider bought here...
    filing_date = date(2026, 3, 4)  # ...but the public learned of it on the 4th (after close, date only)
    res = compute_returns(bars, None, filing_date, None)
    e = res[0]
    assert e.entry_date == date(2026, 3, 5)
    assert e.entry_date > filing_date > transaction_date
    tx_bar = next(b for b in bars if b.date == transaction_date)
    assert e.entry_price != tx_bar.open and e.entry_price > tx_bar.close  # rising series: later entry costs more


def test_entry_uses_open_after_publication_timestamp():
    bars = series(date(2026, 1, 2), 60)
    ts = datetime(2026, 2, 3, 16, 5, tzinfo=NY)  # after close
    e = entry_bar(bars, ts.date(), ts)
    assert e.date == date(2026, 2, 4)


def test_missing_entry_bar_takes_next_available_never_earlier():
    bars = [b for b in series(date(2026, 1, 2), 60) if b.date != date(2026, 2, 4)]  # halt on entry day
    e = entry_bar(bars, date(2026, 2, 3), None)
    assert e.date == date(2026, 2, 5)


def test_horizon_not_elapsed_is_not_computed():
    bars = series(date(2026, 1, 2), 40)
    res = compute_returns(bars, None, date(2026, 2, 2), None, as_of=date(2026, 2, 20))
    assert {r.horizon_days for r in res} == {1, 7}


def test_returns_and_spy_excess():
    bars = series(date(2025, 1, 2), 400, base=100, step=1.0)
    spy = series(date(2025, 1, 2), 400, base=500, step=0.5)
    res = {r.horizon_days: r for r in compute_returns(bars, spy, date(2025, 1, 6), None)}
    r30 = res[30]
    assert r30.entry_date == date(2025, 1, 7)
    assert abs(r30.ret - (r30.exit_price / r30.entry_price - 1)) < 1e-12
    assert r30.spy_ret is not None and abs(r30.excess_ret - (r30.ret - r30.spy_ret)) < 1e-12
    assert r30.mae <= 0 <= r30.mfe or r30.mae < r30.mfe
    assert set(res) == {1, 7, 30, 60, 90, 180, 365}


def test_adjusted_prices_used_for_splits():
    bars = series(date(2026, 1, 2), 30)
    # 2:1 split halfway: raw prices halve, adj_close continuous
    for b in bars[15:]:
        b.open, b.close, b.high, b.low = b.open / 2, b.close / 2, b.high / 2, b.low / 2
    for b in bars[:15]:
        b.adj_close = b.close / 2  # history adjusted down
    for b in bars[15:]:
        b.adj_close = b.close
    res = {r.horizon_days: r for r in compute_returns(bars, None, date(2026, 1, 5), None, as_of=date(2026, 3, 1))}
    assert res[7].ret > -0.2  # no fake -50% crash from the split


def test_summarize_min_sample():
    rows = [{"ret": 0.1, "excess_ret": 0.05, "mae": -0.02, "mfe": 0.12}] * 10
    s = summarize(rows, min_sample=30)
    assert s.n == 10 and not s.sufficient and s.win_rate == 1.0 and s.spy_beat_rate == 1.0
    assert summarize(rows * 3, min_sample=30).sufficient
    assert summarize([], 30).n == 0
