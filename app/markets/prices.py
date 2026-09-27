"""Price-provider abstraction for non-US markets.

Evaluated 2026-09-26 (real requests):
  * Yahoo chart API: BIST (.IS), TSX (.TO), TSXV (.V), CSE (.CN), ASX (.AX) - daily OHLCV + adjusted close,
    history 2000+ for large caps. Currently listed securities only (delisted symbols 404, as in the US).
    BIST: Yahoo inserts ZERO-VOLUME FLAT bars on exchange holidays (e.g. Kurban Bayramı 27-29 May 2026) -
    they must be dropped, otherwise a holiday looks like a trading session with a 0% return.
  * Stooq / Nasdaq-style endpoints: anti-bot / timeouts from both the server and a residential IP - not used.
  * Borsa Istanbul official end-of-day data, ASX/TMX licensed feeds, EODHD, Tiingo (no BIST): need a key or
    licence - adapters are prepared through KeyedProvider and skip gracefully without a key.
"""
from __future__ import annotations

import os

from app.marketdata.provider import Bar, MarketDataProvider

YAHOO_SUFFIX = {"XIST": ".IS", "XTSE": ".TO", "XTSX": ".V", "XCNQ": ".CN", "XASX": ".AX", "XNYS": "", "XNAS": ""}


def yahoo_symbol(exchange: str, ticker: str) -> str:
    t = ticker.upper()
    if exchange in ("XTSE", "XTSX") and "." in t:
        t = t.replace(".", "-")  # TSX class/unit tickers: BBD.B -> BBD-B.TO, HR.UN -> HR-UN.TO
    return t + YAHOO_SUFFIX[exchange]


def drop_synthetic_bars(bars: list[Bar]) -> list[Bar]:
    """Remove provider fill-ins: zero volume AND open=high=low=close equal to the previous close."""
    out, prev = [], None
    for b in sorted(bars, key=lambda x: x.date):
        flat = b.open == b.high == b.low == b.close
        if (b.volume == 0 or b.volume is None) and flat and prev is not None and b.close == prev.close:
            continue
        out.append(b)
        prev = b
    return out


class MarketPriceProvider(MarketDataProvider):
    """Wraps the shared Yahoo provider with exchange-aware symbols and the synthetic-bar filter."""
    name = "yahoo_global"

    def __init__(self, exchange: str, base: MarketDataProvider | None = None):
        from app.marketdata.provider import get_provider
        self.exchange = exchange
        self.base = base or get_provider()

    def get_historical_prices(self, symbol, start, end):
        return drop_synthetic_bars(self.base.get_historical_prices(yahoo_symbol(self.exchange, symbol), start, end))


class KeyedProvider(MarketDataProvider):
    """Licensed / keyed vendor placeholder: without its key it is simply not available (no exception, no guess)."""
    name = "keyed"

    def __init__(self, env_var: str):
        self.env_var = env_var

    @property
    def available(self) -> bool:
        return bool(os.environ.get(self.env_var, "").strip())

    def get_historical_prices(self, symbol, start, end):
        if not self.available:
            return []
        raise NotImplementedError("vendor adapter not implemented until a licence/key is chosen")
