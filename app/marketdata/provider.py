"""Market data provider abstraction + Yahoo chart implementation + SEC shares outstanding."""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection

from app import http
from app.db.models import companies, market_prices, tickers

log = logging.getLogger(__name__)
BENCHMARK = "SPY"


@dataclass
class Bar:
    date: date
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    adj_close: float | None
    volume: int | None


class MarketDataProvider(ABC):
    name = "abstract"

    @abstractmethod
    def get_historical_prices(self, symbol: str, start: date, end: date) -> list[Bar]: ...

    def get_price(self, symbol: str) -> float | None:
        bars = self.get_historical_prices(symbol, date.today() - timedelta(days=10), date.today())
        closes = [b.close for b in bars if b.close]
        return closes[-1] if closes else None

    def get_spy_price(self) -> float | None:
        return self.get_price(BENCHMARK)


class YahooChartProvider(MarketDataProvider):
    """Yahoo Finance public chart endpoint (daily OHLC + split/dividend-adjusted close)."""
    name = "yahoo"
    URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

    @staticmethod
    def _yahoo_symbol(symbol: str) -> str:
        return symbol.replace(".", "-").replace("/", "-")

    def get_historical_prices(self, symbol: str, start: date, end: date) -> list[Bar]:
        p1 = int(datetime(start.year, start.month, start.day, tzinfo=timezone.utc).timestamp())
        p2 = int(datetime(end.year, end.month, end.day, tzinfo=timezone.utc).timestamp()) + 86400
        r = http.get(self.URL.format(symbol=self._yahoo_symbol(symbol)),
                     params={"period1": p1, "period2": p2, "interval": "1d", "events": "div,splits"}, retries=2)
        res = (r.json().get("chart") or {}).get("result") or []
        if not res:
            return []
        res = res[0]
        ts = res.get("timestamp") or []
        q = (res.get("indicators", {}).get("quote") or [{}])[0]
        adj = ((res.get("indicators", {}).get("adjclose") or [{}])[0]).get("adjclose") or [None] * len(ts)
        gmt = res.get("meta", {}).get("gmtoffset", -14400)
        out = []
        for i, t in enumerate(ts):
            d = datetime.fromtimestamp(t + gmt, tz=timezone.utc).date()
            c = q.get("close", [None] * len(ts))[i]
            if c is None:
                continue
            out.append(Bar(d, q.get("open", [None])[i], q.get("high", [None])[i], q.get("low", [None])[i], c,
                           adj[i] if i < len(adj) else None, q.get("volume", [None])[i]))
        return out


def get_provider() -> MarketDataProvider:
    return YahooChartProvider()


# ---------- persistence / cache ----------

def store_bars(conn: Connection, symbol: str, bars: list[Bar]) -> int:
    if not bars:
        return 0
    rows = [{"symbol": symbol, "date": b.date, "open": b.open, "high": b.high, "low": b.low, "close": b.close,
             "adj_close": b.adj_close, "volume": b.volume} for b in bars]
    for i in range(0, len(rows), 1000):
        chunk = rows[i:i + 1000]
        st = pg_insert(market_prices).values(chunk)
        conn.execute(st.on_conflict_do_update(index_elements=["symbol", "date"], set_={
            "open": st.excluded.open, "high": st.excluded.high, "low": st.excluded.low, "close": st.excluded.close,
            "adj_close": st.excluded.adj_close, "volume": st.excluded.volume}))
    return len(rows)


def load_bars(conn: Connection, symbol: str, start: date, end: date) -> list[Bar]:
    q = select(market_prices).where(market_prices.c.symbol == symbol, market_prices.c.date >= start,
                                    market_prices.c.date <= end).order_by(market_prices.c.date)
    return [Bar(r.date, r.open, r.high, r.low, r.close, r.adj_close, r.volume) for r in conn.execute(q)]


def ensure_prices(conn: Connection, symbol: str, start: date, end: date, provider: MarketDataProvider | None = None) -> list[Bar]:
    """Return bars for [start, end], fetching from the provider only when coverage is missing."""
    have = load_bars(conn, symbol, start, end)
    today = date.today()
    need_end = min(end, today)
    covered = bool(have) and have[0].date <= start + timedelta(days=5) and have[-1].date >= need_end - timedelta(days=4)
    if covered:
        return have
    provider = provider or get_provider()
    fetch_from = start if not have or have[0].date > start + timedelta(days=5) else have[-1].date - timedelta(days=3)
    try:
        bars = provider.get_historical_prices(symbol, fetch_from, need_end)
    except Exception as e:  # provider failure must not break ingestion
        log.warning("price fetch failed %s: %s", symbol, e)
        return have
    store_bars(conn, symbol, bars)
    return load_bars(conn, symbol, start, end)


# ---------- shares outstanding / market cap (official SEC XBRL) ----------

SHARES_URL = "https://data.sec.gov/api/xbrl/companyconcept/CIK{cik}/dei/EntityCommonStockSharesOutstanding.json"


def fetch_shares_outstanding(cik: str) -> tuple[int, date] | None:
    try:
        data = http.get(SHARES_URL.format(cik=cik.zfill(10)), retries=1).json()
    except http.HttpError as e:
        if e.status == 404:
            return None
        raise
    best: dict[date, int] = {}
    for f in (data.get("units", {}).get("shares") or []):
        try:
            d = date.fromisoformat(f["end"])
            best[d] = best.get(d, 0) + int(f["val"])  # multiple share classes on the same date are summed
        except (KeyError, ValueError):
            continue
    if not best:
        return None
    d = max(best)
    return best[d], d


def get_market_cap(conn: Connection, ticker: str, on: date | None = None) -> float | None:
    """shares outstanding (latest SEC cover-page value) x close price on/just before `on`."""
    row = conn.execute(select(companies.c.id, companies.c.cik, companies.c.shares_outstanding, companies.c.shares_outstanding_date)
                       .join(tickers, tickers.c.company_id == companies.c.id).where(tickers.c.symbol == ticker)).first()
    if row is None:
        return None
    shares, sdate = row.shares_outstanding, row.shares_outstanding_date
    if shares is None or sdate is None or (date.today() - sdate).days > 120:
        got = fetch_shares_outstanding(row.cik)
        if got:
            shares, sdate = got
            conn.execute(companies.update().where(companies.c.id == row.id).values(shares_outstanding=shares, shares_outstanding_date=sdate))
    if not shares:
        return None
    on = on or date.today()
    bars = ensure_prices(conn, ticker, on - timedelta(days=10), on)
    closes = [b for b in bars if b.date <= on and b.close]
    if not closes:
        return None
    return float(shares) * float(closes[-1].close)
