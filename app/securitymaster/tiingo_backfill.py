"""Resumable Tiingo backfill for delisted / merged securities + a price-provider coverage report.

Price priority (per series):
  1. trusted point-in-time vendor (Tiingo, only with the operator's TIINGO_API_KEY) for DELISTED
     securities - fetched here in the background at the free-tier rate (50 requests/hour) and cached
     in market_prices, so the returns job finds full coverage and never calls Yahoo for them
  2. validated fallback: the cached series must still pass the security-master identity check
     (series ends around the documented delisting; a symbol trading on belongs to someone else)
  3. Yahoo for ACTIVE securities (current SEC ticker of the same CIK)
No price is used without a CIK <-> ticker identity check (SecSecurityMaster.price_symbol).
Without a key this job does nothing and those events stay visibly unresolved. The key is never
logged, stored or printed.
"""
from __future__ import annotations

import logging
import os
from datetime import date, timedelta

from sqlalchemy import text
from sqlalchemy.engine import Engine


log = logging.getLogger(__name__)
TIINGO_START = date(2005, 1, 1)
MISSING = ("DELISTED_MERGER", "DELISTED_BANKRUPTCY", "DELISTED_COMPLIANCE", "DELISTED_OTHER", "NO_PRICE_DATA")


def configured() -> bool:
    return bool(os.environ.get("TIINGO_API_KEY", "").strip())


def candidates(engine: Engine, limit: int = 5000) -> list[dict]:
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(text("""
            SELECT DISTINCT ON (issuer_cik) issuer_cik cik, ticker, public_date, missing_reason
            FROM events WHERE returns_status = 'no_data' AND NOT superseded AND issuer_cik IS NOT NULL
              AND missing_reason = ANY(:m)
            ORDER BY issuer_cik, public_date DESC LIMIT :n"""), {"m": list(MISSING), "n": limit}).mappings()]


def run(engine: Engine, max_requests: int = 45) -> dict:
    """Superseded by app.pricesources.tiingo (identity-gated, quota-aware, stored per CIK - never spliced
    into the shared symbol cache, where a reused ticker could mix two companies)."""
    if not configured():
        return {"skipped": "TIINGO_NOT_CONFIGURED"}
    from app.pricesources.tiingo import run as gated_run
    return gated_run(engine, max_symbols=max(1, max_requests // 2))


def coverage_report(engine: Engine, sample: int = 20) -> dict:
    """Which free sources can price the currently unresolved (mostly delisted) events? Polite probes
    only: no scraping, no bot-wall bypass; a blocked source is reported as blocked."""
    from app import http
    from app.marketdata.provider import get_provider
    cands = candidates(engine, limit=sample * 5)
    with engine.connect() as conn:
        from app.securitymaster.provider import get_security_master
        sm = get_security_master(conn)
        syms = []
        for c in cands:
            s, _, _ = sm.price_symbol(c["cik"], c["public_date"], c["ticker"])
            if s:
                syms.append((s, c["public_date"]))
            if len(syms) >= sample:
                break
        unresolved = dict(conn.execute(text("""SELECT missing_reason, count(*) FROM events
                                               WHERE returns_status = 'no_data' AND NOT superseded GROUP BY 1""")).all())
    yahoo_hits = 0
    for s, d in syms:
        try:
            yahoo_hits += bool(get_provider().get_historical_prices(s, d - timedelta(days=5), d + timedelta(days=30)))
        except Exception:
            pass
    def probe(url: str) -> str:
        try:
            r = http.get(url, retries=0)
            return f"HTTP {r.status_code}"
        except http.HttpError as e:
            return f"HTTP {e.status}"
        except Exception as e:
            return type(e).__name__
    return {
        "unresolved_events_by_reason": unresolved,
        "sample_symbols": len(syms),
        "providers": {
            "yahoo": {"delisted_sample_priced": f"{yahoo_hits}/{len(syms)}", "note": "current listings only"},
            "tiingo_prices": {"configured": configured(), "note": "delisted EOD; needs the operator's free key"},
            "tiingo_listing_file": {"status": probe("https://apimedia.tiingo.com/docs/tiingo/daily/supported_tickers.zip"),
                                    "note": "listing periods only (no prices)"},
            "stooq": {"status": probe("https://stooq.com/q/d/l/?s=aapl.us&i=d"),
                      "note": "JavaScript bot challenge observed 2026-09-25; not bypassed"},
            "alpha_vantage": {"configured": bool(os.environ.get("ALPHAVANTAGE_API_KEY", "").strip()),
                              "note": "free key needed; LISTING_STATUS gives delisting dates, no delisted prices on free tier"},
            "sec_edgar": {"note": "identity, delisting (Form 25/15), corporate actions - no prices"},
        },
    }
