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


class PriceFetchError(RuntimeError):
    """Transient provider failure (network, 5xx, rate limit). Callers must retry later - this is
    NOT evidence that the symbol has no data."""


ABSENCE_TTL_DAYS = 7  # a confirmed 404 is not asked again for a week (return queue only, see ensure_prices)


@dataclass(frozen=True)
class AbsenceScope:
    """Whose price series a request is for. A ticker is never an identity: an absence recorded for one issuer /
    market / period never suppresses a request for another issuer that reuses the symbol, another market, or an
    earlier window (predecessor period)."""
    identity: str  # security_id when known, else "CIK:<cik>"
    market: str = "US"


def absence_fresh(conn: Connection, provider: str, scope: AbsenceScope, symbol: str, start: date,
                  ttl_days: int = ABSENCE_TTL_DAYS) -> bool:
    from app.db.models import price_absences as pa
    q = select(pa.c.checked_at).where(pa.c.provider == provider[:16], pa.c.identity == scope.identity[:48],
                                      pa.c.market == scope.market[:8], pa.c.symbol == symbol[:16],
                                      pa.c.window_start <= start,
                                      pa.c.checked_at > datetime.now(timezone.utc) - timedelta(days=ttl_days)).limit(1)
    return conn.execute(q).first() is not None


def record_absence(conn: Connection, provider: str, scope: AbsenceScope, symbol: str, start: date) -> None:
    from app.db.models import price_absences as pa
    st = pg_insert(pa).values(provider=provider[:16], identity=scope.identity[:48], market=scope.market[:8],
                              symbol=symbol[:16], window_start=start, checked_at=datetime.now(timezone.utc), hits=1)
    conn.execute(st.on_conflict_do_update(index_elements=["provider", "identity", "market", "symbol", "window_start"],
                                          set_={"checked_at": st.excluded.checked_at, "hits": pa.c.hits + 1}))


def ensure_prices(conn: Connection, symbol: str, start: date, end: date, provider: MarketDataProvider | None = None,
                  absence: AbsenceScope | None = None) -> list[Bar]:
    """Return bars for [start, end], fetching from the provider only when coverage is missing.

    A provider 404 ("no data / delisted") is a confirmed absence -> returns what is cached (maybe []).
    Any other failure raises PriceFetchError so the caller can schedule a retry.
    absence (return queue): a 404 is recorded in price_absences for THIS identity / market / symbol / window start, and
    the same scoped request is not repeated for ABSENCE_TTL_DAYS - the result is exactly what that 404 returned (the
    cached bars). Temporary failures are never recorded, so they stay retryable. No scope -> no negative cache.
    """
    have = load_bars(conn, symbol, start, end)
    today = date.today()
    need_end = min(end, today)
    covered = bool(have) and have[0].date <= start + timedelta(days=5) and have[-1].date >= need_end - timedelta(days=4)
    if covered:
        return have
    provider = provider or get_provider()
    pname = getattr(provider, "name", type(provider).__name__)[:16]
    if absence is not None and absence_fresh(conn, pname, absence, symbol, start):
        return have
    fetch_from = start if not have or have[0].date > start + timedelta(days=5) else have[-1].date - timedelta(days=3)
    try:
        bars = provider.get_historical_prices(symbol, fetch_from, need_end)
    except http.HttpError as e:
        if e.status == 404:
            log.info("no price data for %s (provider 404)", symbol)
            if absence is not None:
                record_absence(conn, pname, absence, symbol, start)
            return have
        raise PriceFetchError(f"{symbol}: {e}") from e
    except Exception as e:
        raise PriceFetchError(f"{symbol}: {type(e).__name__}: {e}") from e
    store_bars(conn, symbol, bars)
    return load_bars(conn, symbol, start, end)


# ---------- shares outstanding / market cap (official SEC XBRL, point-in-time) ----------

SHARES_URL = "https://data.sec.gov/api/xbrl/companyconcept/CIK{cik}/dei/EntityCommonStockSharesOutstanding.json"
MAX_SHARES_AGE_DAYS = 400  # older cover-page data is not a usable market-cap basis


@dataclass(frozen=True)
class ShareFact:
    accession: str
    end: date
    filed: date
    form: str | None
    val: int


def parse_share_facts(data: dict) -> list[ShareFact]:
    out = set()
    for f in (data.get("units", {}).get("shares") or []):
        try:
            out.add(ShareFact(f["accn"], date.fromisoformat(f["end"]), date.fromisoformat(f["filed"]), f.get("form"), int(f["val"])))
        except (KeyError, ValueError, TypeError):
            continue
    return sorted(out, key=lambda x: (x.filed, x.end))


def fetch_share_facts(cik: str) -> list[ShareFact]:
    try:
        data = http.get(SHARES_URL.format(cik=cik.zfill(10)), retries=1).json()
    except http.HttpError as e:
        if e.status == 404:
            return []
        raise
    return parse_share_facts(data)


def shares_as_of(facts: list[ShareFact], on: date) -> tuple[int, ShareFact] | None:
    """Shares outstanding an investor could know on `on`.

    * only facts FILED on/before `on` (no look-ahead into later filings/restatements)
    * latest period end among them; if several filings report that same end, the most recently
      filed one wins (a re-report or amendment REPLACES, it is never added)
    * distinct values inside that single filing are distinct share classes -> summed
    """
    known = [f for f in facts if f.filed <= on]
    if not known:
        return None
    end = max(f.end for f in known)
    if (on - end).days > MAX_SHARES_AGE_DAYS:
        return None
    same_end = [f for f in known if f.end == end]
    latest_filed = max(f.filed for f in same_end)
    accs = sorted({f.accession for f in same_end if f.filed == latest_filed})
    chosen = [f for f in same_end if f.accession == accs[-1]]
    total = sum({f.val for f in chosen})
    return total, chosen[0]


def load_share_facts(conn: Connection, cik: str, refresh_days: int = 7) -> list[ShareFact]:
    from app.db.models import shares_outstanding_facts as sof
    cik = cik.zfill(10)
    row = conn.execute(select(companies.c.id, companies.c.shares_checked_at).where(companies.c.cik == cik)).first()
    stale = row is None or row.shares_checked_at is None or \
        (datetime.now(timezone.utc) - row.shares_checked_at).days >= refresh_days
    if stale:
        facts = fetch_share_facts(cik)
        if facts:
            st = pg_insert(sof).values([{"cik": cik, "accession": f.accession, "end_date": f.end, "filed": f.filed,
                                         "form": f.form, "val": f.val} for f in facts])
            conn.execute(st.on_conflict_do_nothing(index_elements=["cik", "accession", "end_date", "val"]))
        if row is not None:
            conn.execute(companies.update().where(companies.c.id == row.id).values(shares_checked_at=datetime.now(timezone.utc)))
    rows = conn.execute(select(sof.c.accession, sof.c.end_date, sof.c.filed, sof.c.form, sof.c.val).where(sof.c.cik == cik)).all()
    return [ShareFact(*r) for r in rows]


def get_market_cap(conn: Connection, ticker: str, on: date | None = None) -> float | None:
    """Point-in-time market cap: shares known on `on` x close on/just before `on`. None = unknown."""
    on = on or date.today()
    row = conn.execute(select(companies.c.cik).join(tickers, tickers.c.company_id == companies.c.id)
                       .where(tickers.c.symbol == ticker)).first()
    if row is None:
        return None
    got = shares_as_of(load_share_facts(conn, row.cik), on)
    if got is None:
        return None
    shares, _fact = got
    bars = ensure_prices(conn, ticker, on - timedelta(days=10), on)
    closes = [b for b in bars if b.date <= on and b.close]
    if not closes:
        return None
    return float(shares) * float(closes[-1].close)
