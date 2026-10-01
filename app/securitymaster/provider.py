"""SecurityMasterProvider abstraction + the SEC-primary-source implementation.

Identity rules (never violated):
  * the filing-time symbol is the DISPLAY identity of a historical event;
  * the PRICE series is chosen by CIK, not by symbol: the same CIK's current exchange ticker
    (price vendors keep a renamed security's history under its current symbol); a historical
    symbol is used only when no other CIK owns it today (ticker reuse would silently splice
    another company's prices into the sample);
  * today's ticker/revenue/market cap are never projected onto a past filing.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import and_, or_, select
from sqlalchemy.engine import Connection

from app.db.models import corporate_actions, listing_periods, security_master, security_symbols
from app.entities.registry import resolver
from app.marketdata.provider import Bar, MarketDataProvider, PriceFetchError, ensure_prices
from app.securitymaster.delisting import Delisting, classify_delisting
from app.core import clock as _clock

log = logging.getLogger(__name__)
SYMBOL_GRACE_DAYS = 120  # a filing symbol stays valid this long after its last observation

# basis values, from strongest to weakest
VERIFIED_BASES = {"current_ticker_same_cik", "historical_symbol_verified", "vendor_trusted", "vendor_validated"}


VENDOR_ABSENCE_TTL_DAYS = 30  # delisted-vendor quota is scarce (50/hour): a confirmed absence waits a month


class VendorDeferred(Exception):
    """The price needs the delisted-price vendor, which this caller does not call inline (the return queue hands the
    event to the vendor lane instead of sleeping on the vendor's rate limit)."""

    def __init__(self, symbol: str):
        super().__init__(symbol)
        self.symbol = symbol


class VendorUnavailable(PriceFetchError):
    """Temporary delisted-vendor failure (quota, 429, 5xx, network, unexpected payload) - retry later."""

    def __init__(self, msg: str, quota: bool = False):
        super().__init__(msg)
        self.quota = quota


@dataclass
class PriceSeries:
    symbol: str | None
    basis: str | None  # current_ticker_same_cik | historical_symbol_verified | ticker_only_unverified
    bars: list[Bar] = field(default_factory=list)
    missing_reason: str | None = None  # TICKER_REUSED | TICKER_UNRESOLVED | NO_PRICE_DATA | SYMBOL_UNVERIFIED
    delisting: Delisting | None = None

    @property
    def verified(self) -> bool:
        return self.basis in VERIFIED_BASES


class SecurityMasterProvider(ABC):
    @abstractmethod
    def resolve_security_at_date(self, symbol: str, on: date) -> str | None:
        """CIK that used `symbol` on `on` (None = unknown / ambiguous)."""

    @abstractmethod
    def resolve_ticker_at_date(self, cik: str, on: date) -> str | None:
        """Symbol the CIK used on `on` (filing-time identity, never today's ticker for the past)."""

    @abstractmethod
    def get_listing_period(self, cik: str) -> tuple[date | None, date | None] | None: ...

    @abstractmethod
    def get_delisting_date(self, cik: str) -> date | None: ...

    @abstractmethod
    def get_corporate_actions(self, cik: str, start: date | None = None, end: date | None = None) -> list[dict]: ...

    @abstractmethod
    def get_historical_price(self, cik: str | None, start: date, end: date, *, on: date | None = None,
                             ticker_hint: str | None = None) -> PriceSeries: ...


class SecSecurityMaster(SecurityMasterProvider):
    """Backed by: SEC Form 3/4/5 filing symbols (security_symbols), SEC full-index / submissions
    corporate actions, SEC exchange ticker file (current listings) and public listing periods."""

    def __init__(self, conn: Connection, market: MarketDataProvider | None = None,
                 delisted_prices: MarketDataProvider | None = None, *, allow_vendor: bool = True,
                 skip_known_absent: bool = False):
        self.conn = conn
        self.market = market
        self.delisted_prices = delisted_prices  # optional vendor with delisted coverage (e.g. Tiingo)
        # allow_vendor=False: raise VendorDeferred instead of calling the vendor inline (return queue main lane)
        self.allow_vendor = allow_vendor
        # skip_known_absent: confirmed 404s (price_absences) are not requested again (return queue)
        self.skip_known_absent = skip_known_absent
        resolver.load(conn)

    # ---------------------------------------------------------------- identity
    def _symbol_rows(self, **where) -> list:
        q = select(security_symbols)
        for k, v in where.items():
            q = q.where(getattr(security_symbols.c, k) == v)
        return self.conn.execute(q).mappings().all()

    def resolve_security_at_date(self, symbol: str, on: date) -> str | None:
        symbol = symbol.upper()
        rows = [r for r in self._symbol_rows(symbol=symbol) if r["source"] != "sec_registry"
                and r["first_seen"] - timedelta(days=30) <= on <= r["last_seen"] + timedelta(days=SYMBOL_GRACE_DAYS)]
        ciks = {r["cik"] for r in rows}
        if len(ciks) == 1:
            return next(iter(ciks))
        if len(ciks) > 1:  # two issuers reported the same symbol around that date: pick the closest interval
            best = min(rows, key=lambda r: 0 if r["first_seen"] <= on <= r["last_seen"] else
                       min(abs((on - r["first_seen"]).days), abs((on - r["last_seen"]).days)))
            return best["cik"]
        cur = resolver.ticker_to_cik.get(symbol)
        if cur:
            lp = self.get_listing_period(cur)
            if lp is None or lp[0] is None or lp[0] <= on:
                return cur
        return None

    def resolve_ticker_at_date(self, cik: str, on: date) -> str | None:
        cik = cik.zfill(10)
        rows = [r for r in self._symbol_rows(cik=cik) if r["source"] != "sec_registry"]
        covering = [r for r in rows if r["first_seen"] <= on <= r["last_seen"] + timedelta(days=SYMBOL_GRACE_DAYS)]
        if covering:
            return max(covering, key=lambda r: (r["n_obs"], r["last_seen"]))["symbol"]
        before = [r for r in rows if r["last_seen"] < on and (on - r["last_seen"]).days <= 365]
        if before:
            return max(before, key=lambda r: r["last_seen"])["symbol"]
        cur = resolver.ticker_for_cik(cik)
        if cur and not rows:  # no filing history at all: only the current listing is known
            lp = self.get_listing_period(cik)
            if lp and lp[0] and lp[0] <= on:
                return cur
        return None

    def _master(self, cik: str) -> dict | None:
        r = self.conn.execute(select(security_master).where(security_master.c.cik == cik.zfill(10))).mappings().first()
        return dict(r) if r else None

    def get_corporate_actions(self, cik: str, start: date | None = None, end: date | None = None) -> list[dict]:
        q = select(corporate_actions).where(corporate_actions.c.cik == cik.zfill(10))
        if start:
            q = q.where(corporate_actions.c.action_date >= start)
        if end:
            q = q.where(corporate_actions.c.action_date <= end)
        return [dict(r) for r in self.conn.execute(q.order_by(corporate_actions.c.action_date)).mappings().all()]

    def delisting(self, cik: str) -> Delisting:
        cik = cik.zfill(10)
        listed = resolver.ticker_for_cik(cik) is not None
        last = self.conn.execute(select(security_symbols.c.last_seen).where(and_(
            security_symbols.c.cik == cik, security_symbols.c.source != "sec_registry"))
            .order_by(security_symbols.c.last_seen.desc()).limit(1)).scalar()
        return classify_delisting(self.get_corporate_actions(cik), currently_listed=listed, last_symbol_seen=last)

    def get_delisting_date(self, cik: str) -> date | None:
        return self.delisting(cik).date

    def get_listing_period(self, cik: str) -> tuple[date | None, date | None] | None:
        cik = cik.zfill(10)
        rows = [r for r in self._symbol_rows(cik=cik) if r["source"] != "sec_registry"]
        start = min((r["first_seen"] for r in rows), default=None)
        cur = resolver.ticker_for_cik(cik)
        if cur:
            lp = self.conn.execute(select(listing_periods.c.start_date).where(listing_periods.c.symbol == cur)
                                   .order_by(listing_periods.c.start_date)).scalar()
            if lp and (start is None or lp < start):
                start = lp
            return start, None
        if start is None:
            return None
        return start, self.get_delisting_date(cik)

    # ---------------------------------------------------------------- prices
    def price_symbol(self, cik: str | None, on: date, ticker_hint: str | None = None) -> tuple[str | None, str | None, str | None]:
        """(symbol, basis, missing_reason) for the price series of `cik` around `on`."""
        if not cik:
            if not ticker_hint:
                return None, None, "TICKER_UNRESOLVED"
            owner = self.resolve_security_at_date(ticker_hint, on)
            if owner is None:
                return ticker_hint, "ticker_only_unverified", None
            cik = owner
        cik = cik.zfill(10)
        cur = resolver.ticker_for_cik(cik)
        if cur:
            return cur, "current_ticker_same_cik", None
        cands: list[str] = []
        pit = self.resolve_ticker_at_date(cik, on)
        for s in (pit, ticker_hint):
            if s and s not in cands:
                cands.append(s)
        if not cands:
            return None, None, "TICKER_UNRESOLVED"
        reused = False
        for s in cands:
            other = resolver.ticker_to_cik.get(s)
            if other and other != cik:
                reused = True  # the symbol belongs to another company today
                continue
            return s, "historical_symbol_verified", None
        return None, None, "TICKER_REUSED" if reused else "TICKER_UNRESOLVED"

    def get_historical_price(self, cik: str | None, start: date, end: date, *, on: date | None = None,
                             ticker_hint: str | None = None, security_id: str | None = None) -> PriceSeries:
        on = on or start
        sym, basis, missing = self.price_symbol(cik, on, ticker_hint)
        dl = self.delisting(cik) if cik else None
        if sym is None:
            return PriceSeries(None, None, [], missing, dl)
        scope = self._absence_scope(cik, sym, basis, on, security_id)
        bars = ensure_prices(self.conn, sym, start, end, provider=self.market, absence=scope)
        if basis == "historical_symbol_verified":
            bars, missing = _validate_historical(bars, on, dl)
            if not bars and self.delisted_prices is not None:
                alt = self._vendor_bars(sym, start, end, scope)
                bars, missing = _validate_historical(alt, on, dl)
        if not bars and missing is None:
            missing = "NO_PRICE_DATA"
        return PriceSeries(sym, basis if bars else None, bars, missing, dl)


    def _absence_scope(self, cik: str | None, sym: str, basis: str | None, on: date, security_id: str | None = None):
        """Negative-cache identity (see AbsenceScope); None = no negative cache. Only the return queue opts in.
          * security_id known -> the security itself;
          * else the issuer + the period in which it used this symbol: "CUR" for its current exchange ticker, the
            first filing observation of the (cik, symbol) period covering `on` for a historical symbol - so a
            predecessor and a successor security period never share an absence;
          * no issuer, or no symbol period covering the date -> no cache (conservative)."""
        from app.marketdata.provider import AbsenceScope
        if not self.skip_known_absent:
            return None
        if security_id:
            return AbsenceScope(identity=f"SEC:{security_id}", market="US")
        if not cik:
            return None
        cik = cik.zfill(10)
        if basis == "current_ticker_same_cik":
            period = "CUR"
        else:
            rows = [r for r in self._symbol_rows(cik=cik, symbol=sym.upper())
                    if r["first_seen"] and r["first_seen"] - timedelta(days=SYMBOL_GRACE_DAYS) <= on
                    and (r["last_seen"] is None or on <= r["last_seen"] + timedelta(days=SYMBOL_GRACE_DAYS))]
            if not rows:
                return None  # no symbol period covers the date: never cache
            period = min(r["first_seen"] for r in rows).isoformat()  # sources overlap: the period's earliest sighting
        return AbsenceScope(identity=f"CIK:{cik}|SYM:{period}", market="US")

    def _vendor_bars(self, sym: str, start: date, end: date, scope=None) -> list[Bar]:
        """Delisted-vendor series. A 404 / "ticker not found" is a confirmed absence (-> [] and recorded for this
        identity, so the scarce quota is not spent on it again for VENDOR_ABSENCE_TTL_DAYS); everything else is
        temporary."""
        from app import http
        from app.core.ratelimit import QuotaExhausted
        from app.marketdata.provider import absence_fresh, record_absence
        name = getattr(self.delisted_prices, "name", "vendor")
        if scope is not None and absence_fresh(self.conn, name, scope, sym, start, end, VENDOR_ABSENCE_TTL_DAYS):
            return []
        if not self.allow_vendor:
            raise VendorDeferred(sym)
        try:
            return self.delisted_prices.get_historical_prices(sym, start, end)
        except http.HttpError as e:
            if e.status == 404:
                if scope is not None:
                    record_absence(self.conn, name, scope, sym, start, end)
                return []
            raise VendorUnavailable(f"{sym}: delisted vendor: {e}", quota=e.status == 429) from e
        except QuotaExhausted as e:
            raise VendorUnavailable(f"{sym}: delisted vendor: quota exhausted", quota=True) from e
        except Exception as e:  # noqa: BLE001 - optional vendor: never fatal, always retryable
            raise VendorUnavailable(f"{sym}: delisted vendor: {type(e).__name__}: {e}") from e


def _validate_historical(bars: list[Bar], on: date, dl: Delisting | None) -> tuple[list[Bar], str | None]:
    """A historical symbol's series must cover the event and must END around the documented
    delisting; a series that keeps trading long after the issuer delisted belongs to someone else."""
    if not bars:
        return [], None
    if not any(on - timedelta(days=10) <= b.date <= on + timedelta(days=10) for b in bars):
        return [], "NO_PRICE_DATA"
    last = bars[-1].date
    if dl is not None and dl.date is not None:
        if last > dl.date + timedelta(days=30):
            return [], "TICKER_REUSED"
        return bars, None
    if last > _clock.today() - timedelta(days=30):
        return [], "SYMBOL_UNVERIFIED"  # still trading but not in the SEC ticker file: cannot confirm identity
    return bars, None


def get_security_master(conn: Connection, *, allow_vendor: bool = True, skip_known_absent: bool = False) -> SecSecurityMaster:
    from app.marketdata.provider import get_provider
    from app.securitymaster.vendors import delisted_price_provider
    return SecSecurityMaster(conn, get_provider(), delisted_price_provider(), allow_vendor=allow_vendor,
                             skip_known_absent=skip_known_absent)


def symbol_filter(conn: Connection, cik: str) -> list[str]:
    """All symbols a CIK has ever used (for dashboards)."""
    return [r[0] for r in conn.execute(select(security_symbols.c.symbol).where(or_(security_symbols.c.cik == cik.zfill(10)))
                                       .distinct())]
