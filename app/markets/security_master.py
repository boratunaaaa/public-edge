"""Global security master (non-US): one record per listed security, with symbol history so a renamed or
reused ticker never maps an old event to the wrong company.

Identity key: security_id = "<MIC>:<company_id>" where company_id is the most stable local id available
(BIST: ISIN of the share class when known, else the KAP member code; ASX: the ASX code at first sight;
TSX/TSXV: the directory symbol at first sight). Tickers are attributes with validity periods.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass
class SymbolPeriod:
    symbol: str
    start: date | None
    end: date | None = None  # None = current


@dataclass
class GlobalSecurity:
    country: str
    exchange: str
    ticker: str  # current ticker
    company_name: str
    company_id: str
    historical_tickers: list[SymbolPeriod] = field(default_factory=list)
    listing_date: date | None = None
    delisting_date: date | None = None
    sector: str | None = None
    market: str | None = None  # BIST: Yıldız / Ana / Alt / Yakın İzleme Pazarı ; ASX/TSX: board
    free_float: float | None = None
    index_membership: list[str] = field(default_factory=list)
    corporate_action_history: list[dict] = field(default_factory=list)

    @property
    def security_id(self) -> str:
        return f"{self.exchange}:{self.company_id}"


class SecurityMaster:
    def __init__(self, securities: list[GlobalSecurity] | None = None):
        self.by_id: dict[str, GlobalSecurity] = {}
        for s in securities or []:
            self.add(s)

    def add(self, s: GlobalSecurity) -> None:
        if not s.historical_tickers:
            s.historical_tickers = [SymbolPeriod(s.ticker, s.listing_date, None)]
        self.by_id[s.security_id] = s

    def rename(self, security_id: str, new_ticker: str, on: date) -> None:
        s = self.by_id[security_id]
        for p in s.historical_tickers:
            if p.end is None:
                p.end = on
        s.historical_tickers.append(SymbolPeriod(new_ticker, on, None))
        s.corporate_action_history.append({"type": "TICKER_CHANGE", "date": on.isoformat(), "from": s.ticker, "to": new_ticker})
        s.ticker = new_ticker

    def resolve(self, exchange: str, ticker: str, on: date) -> GlobalSecurity | None:
        """The security that used `ticker` on `on` (ticker reuse safe: periods must contain the date)."""
        hits = []
        for s in self.by_id.values():
            if s.exchange != exchange:
                continue
            for p in s.historical_tickers:
                if p.symbol == ticker and (p.start is None or p.start <= on) and (p.end is None or on < p.end):
                    hits.append(s)
        if len(hits) != 1:
            return None  # unknown or ambiguous: never guess
        return hits[0]

    def reuse_conflicts(self) -> list[tuple[str, str, str]]:
        """(exchange, ticker, security_ids) where two securities claim overlapping periods of one symbol."""
        seen: dict[tuple[str, str], list[tuple[SymbolPeriod, str]]] = {}
        out = []
        for s in self.by_id.values():
            for p in s.historical_tickers:
                for q, sid in seen.get((s.exchange, p.symbol), []):
                    a0, a1 = p.start or date.min, p.end or date.max
                    b0, b1 = q.start or date.min, q.end or date.max
                    if a0 < b1 and b0 < a1:
                        out.append((s.exchange, p.symbol, f"{sid}|{s.security_id}"))
                seen.setdefault((s.exchange, p.symbol), []).append((p, s.security_id))
        return out


def from_asx_csv(rows: list[dict], as_of: date) -> list[GlobalSecurity]:
    return [GlobalSecurity(country="AU", exchange="XASX", ticker=r["ticker"], company_name=r["company_name"], company_id=r["ticker"],
                           sector=r.get("sector"), historical_tickers=[SymbolPeriod(r["ticker"], None, None)]) for r in rows]


def from_tsx_directory(rows: list[dict]) -> list[GlobalSecurity]:
    return [GlobalSecurity(country="CA", exchange=r["exchange"], ticker=r["ticker"], company_name=r["company_name"],
                           company_id=r["ticker"], market="TSX" if r["exchange"] == "XTSE" else "TSXV") for r in rows]


def from_kap_members(rows: list[dict]) -> list[GlobalSecurity]:
    """rows: {'codes': 'AKBNK' or 'KRDMA, KRDMB, KRDMD', 'name': ...} - one security per share code."""
    out = []
    for r in rows:
        for code in [c.strip() for c in r["codes"].split(",") if c.strip()]:
            out.append(GlobalSecurity(country="TR", exchange="XIST", ticker=code, company_name=r["name"], company_id=code,
                                      market=r.get("market")))
    return out
