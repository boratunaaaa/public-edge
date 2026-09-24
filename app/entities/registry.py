"""Company / ticker registry sourced from SEC's official ticker files."""
from __future__ import annotations

import logging
import threading
import time

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection

from app import http
from app.db.models import companies, tickers
from app.entities.names import normalize_company

log = logging.getLogger(__name__)
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
_ALLOWED_EXCHANGES = {"NYSE", "Nasdaq", "CBOE", "NYSE American", "NYSE Arca", "OTC", None}


def fetch_sec_tickers() -> list[dict]:
    data = http.get(SEC_TICKERS_URL).json()
    fields = data["fields"]  # ["cik","name","ticker","exchange"]
    out = []
    for row in data["data"]:
        d = dict(zip(fields, row))
        if not d.get("ticker"):
            continue
        out.append({"cik": str(d["cik"]).zfill(10), "name": d["name"], "ticker": d["ticker"].upper(), "exchange": d.get("exchange")})
    return out


def sync_registry(conn: Connection, rows: list[dict] | None = None) -> int:
    rows = rows if rows is not None else fetch_sec_tickers()
    n = 0
    for d in rows:
        cid = conn.execute(
            pg_insert(companies).values(cik=d["cik"], name=d["name"], name_norm=normalize_company(d["name"]))
            .on_conflict_do_update(index_elements=["cik"], set_={"name": d["name"], "name_norm": normalize_company(d["name"])})
            .returning(companies.c.id)
        ).scalar_one()
        conn.execute(
            pg_insert(tickers).values(symbol=d["ticker"], company_id=cid, exchange=d.get("exchange"))
            .on_conflict_do_update(index_elements=["symbol"], set_={"company_id": cid, "exchange": d.get("exchange")})
        )
        n += 1
    return n


class Resolver:
    """In-memory lookup (refreshed periodically) for CIK/ticker/name/CUSIP resolution."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._loaded_at = 0.0
        self.cik_to_ticker: dict[str, str] = {}
        self.cik_to_name: dict[str, str] = {}
        self.ticker_to_cik: dict[str, str] = {}
        self.name_to_ciks: dict[str, set[str]] = {}
        self.cusip_to_ticker: dict[str, str] = {}

    def load(self, conn: Connection, force: bool = False) -> None:
        with self._lock:
            if not force and time.time() - self._loaded_at < 3600 and self.cik_to_ticker:
                return
            c2t: dict[str, str] = {}
            c2n: dict[str, str] = {}
            t2c: dict[str, str] = {}
            n2c: dict[str, set[str]] = {}
            cu: dict[str, str] = {}
            q = select(companies.c.cik, companies.c.name, companies.c.name_norm, tickers.c.symbol, tickers.c.cusip, tickers.c.id) \
                .join(tickers, tickers.c.company_id == companies.c.id).order_by(tickers.c.id)
            for cik, name, norm, sym, cusip, _tid in conn.execute(q):
                # First (lowest id = SEC file order = primary listing) ticker wins for a CIK.
                c2t.setdefault(cik, sym)
                c2n[cik] = name
                t2c[sym] = cik
                if norm:
                    n2c.setdefault(norm, set()).add(cik)
                if cusip:
                    cu[cusip[:8]] = sym
            self.cik_to_ticker, self.cik_to_name, self.ticker_to_cik, self.name_to_ciks, self.cusip_to_ticker = c2t, c2n, t2c, n2c, cu
            self._loaded_at = time.time()

    def ticker_for_cik(self, cik: str | None) -> str | None:
        return self.cik_to_ticker.get((cik or "").zfill(10)) if cik else None

    def ticker_for_cusip(self, cusip: str | None) -> str | None:
        if not cusip or len(cusip) < 8:
            return None
        return self.cusip_to_ticker.get(cusip[:8].upper())

    def ciks_for_name(self, name: str | None) -> set[str]:
        return self.name_to_ciks.get(normalize_company(name), set())

    def ticker_for_name(self, name: str | None) -> str | None:
        """Strict: only an unambiguous exact normalized-name match resolves."""
        ciks = self.ciks_for_name(name)
        if len(ciks) != 1:
            return None
        return self.ticker_for_cik(next(iter(ciks)))


resolver = Resolver()


def remember_cusip(conn: Connection, ticker: str | None, cusip: str | None) -> None:
    if ticker and cusip and len(cusip) >= 8:
        conn.execute(tickers.update().where(tickers.c.symbol == ticker, tickers.c.cusip.is_(None)).values(cusip=cusip[:9].upper()))
        resolver.cusip_to_ticker[cusip[:8].upper()] = ticker
