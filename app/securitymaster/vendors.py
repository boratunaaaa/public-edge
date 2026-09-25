"""Optional third-party security-master inputs.

Evaluated 2026-09-25 from the production server (see docs/DATA_INTEGRITY.md):
  * Yahoo chart API - current listings only: SIVB, TWTR, ATVI, FRC all 404 (no delisted history).
  * Stooq - JavaScript bot challenge; not bypassed.
  * Alpha Vantage LISTING_STATUS - delisted symbols + dates, but needs a (free) key; demo key only
    answers the documented example date.
  * Tiingo - public supported_tickers file (no key) has start/end dates incl. delisted symbols
    (TWTR, ATVI present; bankrupt SIVB/FRC/BBBY absent); EOD prices incl. delisted need a free key.
  * Polygon / EODHD / FMP / Nasdaq Data Link (Sharadar) - delisted coverage is paid or key-bound.
So: SEC primary sources are the backbone; Tiingo's public listing file adds listing periods; Tiingo
prices are used only when the operator configures TIINGO_API_KEY (never required, never logged).
"""
from __future__ import annotations

import csv
import io
import logging
import os
import zipfile
from datetime import date

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection

from app import http
from app.db.models import listing_periods
from app.marketdata.provider import Bar, MarketDataProvider

log = logging.getLogger(__name__)
TIINGO_LISTING_URL = "https://apimedia.tiingo.com/docs/tiingo/daily/supported_tickers.zip"
TIINGO_PRICES_URL = "https://api.tiingo.com/tiingo/daily/{symbol}/prices"
US_EXCHANGES = {"NYSE", "NASDAQ", "NYSE MKT", "NYSE ARCA", "AMEX", "BATS", "NYSE NAT"}


def parse_listing_file(raw: bytes) -> list[dict]:
    z = zipfile.ZipFile(io.BytesIO(raw))
    out = []
    with z.open(z.namelist()[0]) as f:
        for r in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8", errors="replace")):
            if r.get("exchange") not in US_EXCHANGES or r.get("assetType") not in ("Stock", "ETF"):
                continue
            try:
                s = date.fromisoformat(r["startDate"]) if r.get("startDate") else None
                e = date.fromisoformat(r["endDate"]) if r.get("endDate") else None
            except ValueError:
                continue
            out.append({"symbol": r["ticker"].upper()[:16], "exchange": r["exchange"][:24], "start_date": s, "end_date": e,
                        "asset_type": r["assetType"][:16], "source": "tiingo_listing_file"})
    return out


def load_listing_periods(conn: Connection) -> int:
    rows = parse_listing_file(http.get(TIINGO_LISTING_URL, retries=2).content)
    for i in range(0, len(rows), 1000):
        st = pg_insert(listing_periods).values(rows[i:i + 1000])
        conn.execute(st.on_conflict_do_update(index_elements=["symbol", "exchange"], set_={
            "start_date": st.excluded.start_date, "end_date": st.excluded.end_date, "asset_type": st.excluded.asset_type}))
    return len(rows)


class TiingoPriceProvider(MarketDataProvider):
    """End-of-day prices including delisted symbols (requires the operator's own API key)."""
    name = "tiingo"

    def __init__(self, key: str):
        self._key = key

    def get_historical_prices(self, symbol: str, start: date, end: date) -> list[Bar]:
        try:
            r = http.get(TIINGO_PRICES_URL.format(symbol=symbol.lower()), retries=1,
                         params={"startDate": start.isoformat(), "endDate": end.isoformat()},
                         headers={"Authorization": f"Token {self._key}"})
        except http.HttpError as e:
            # never echo the request headers; the status is enough
            raise http.HttpError(TIINGO_PRICES_URL.format(symbol=symbol), e.status, "tiingo request failed") from None
        out = []
        for x in r.json() or []:
            try:
                d = date.fromisoformat(x["date"][:10])
            except (KeyError, ValueError):
                continue
            close, adj = x.get("close"), x.get("adjClose")
            out.append(Bar(d, x.get("open"), x.get("high"), x.get("low"), close, adj, x.get("volume")))
        return out


def delisted_price_provider() -> MarketDataProvider | None:
    key = os.environ.get("TIINGO_API_KEY", "").strip()
    return TiingoPriceProvider(key) if key else None
