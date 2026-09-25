"""Tiingo free tier: delisted / merged daily prices for the highest-value unresolved securities.

Never beyond the free tier: 500 unique symbols per calendar month, 50 requests / hour, 1000 / day
(a 5% safety margin is kept). Order of use:
  1. symbols that unlock the most unresolved historical events,
  2. weighted x2 when the events are INSIDER_BUY / INSIDER_CLUSTER / 13D (the blocked metrics),
  3. only companies no other free source has already resolved.
Candidates come from Tiingo's public listing file: the symbol must have been listed on the event date.
Every series goes through the identity gate (vendor name vs SEC name, ownership, delisting, cross-check).
The key is read from the environment and is never logged, stored or put into a URL.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app import http
from app.marketdata.provider import Bar
from app.pricesources import quota
from app.pricesources.store import save_source

log = logging.getLogger(__name__)
META_URL = "https://api.tiingo.com/tiingo/daily/{s}"
PRICES_URL = "https://api.tiingo.com/tiingo/daily/{s}/prices"
HISTORY_START = date(2015, 1, 1)
PRIORITY_TYPES = ("INSIDER_OPEN_MARKET_BUY", "CLUSTER_INSIDER_BUY", "ACTIVIST_13D")


def configured() -> bool:
    return bool(os.environ.get("TIINGO_API_KEY", "").strip())


def _headers() -> dict:
    return {"Authorization": f"Token {os.environ['TIINGO_API_KEY'].strip()}", "Content-Type": "application/json"}


CANDIDATES_SQL = """
WITH ev AS (
  SELECT e.id, e.issuer_cik cik, e.ticker, e.public_date, e.event_type FROM events e
  WHERE e.returns_status = 'no_data' AND NOT e.superseded AND e.issuer_cik IS NOT NULL),
cand AS (
  SELECT ev.id, ev.cik, ev.public_date, ev.event_type, s.symbol FROM ev JOIN security_symbols s ON s.cik = ev.cik
  UNION SELECT ev.id, ev.cik, ev.public_date, ev.event_type, ev.ticker FROM ev WHERE ev.ticker IS NOT NULL),
hit AS (
  SELECT DISTINCT c.id, c.cik, c.symbol, c.event_type FROM cand c JOIN listing_periods lp ON lp.symbol = c.symbol
   AND lp.start_date <= c.public_date AND COALESCE(lp.end_date, current_date) >= c.public_date + 5)
SELECT cik, symbol, count(DISTINCT id) events,
       sum(CASE WHEN event_type = ANY(:pt) THEN 2 ELSE 1 END) score
FROM hit h
WHERE NOT EXISTS (SELECT 1 FROM price_sources p WHERE p.cik = h.cik AND p.provider = 'tiingo' AND p.symbol = h.symbol)
  AND NOT EXISTS (SELECT 1 FROM price_sources p WHERE p.cik = h.cik
                  AND p.validation_status IN ('TRUSTED','VALIDATED_FALLBACK'))
GROUP BY cik, symbol ORDER BY score DESC, events DESC LIMIT :n
"""


def candidates(engine: Engine, n: int = 600) -> list[dict]:
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(text(CANDIDATES_SQL), {"pt": list(PRIORITY_TYPES), "n": n}).mappings()]


def _get(url: str, **kw):
    try:
        r = http.get(url, retries=1, headers=_headers(), **kw)
        return r.status_code, r.content
    except http.HttpError as e:
        return e.status, None
    except Exception as e:  # network: never echo the request (it carries the key header)
        log.info("tiingo request failed: %s", type(e).__name__)
        return None, None


def parse_prices(body: bytes) -> list[Bar]:
    out = []
    for x in json.loads(body or b"[]") or []:
        try:
            d = date.fromisoformat(x["date"][:10])
        except (KeyError, ValueError, TypeError):
            continue
        out.append(Bar(d, x.get("open"), x.get("high"), x.get("low"), x.get("close"), x.get("adjClose"), x.get("volume")))
    return out


def fetch_symbol(engine: Engine, cik: str, symbol: str) -> dict:
    s = symbol.lower()
    with engine.begin() as conn:  # both requests of this symbol are reserved atomically before any call
        ok, why, ids = quota.reserve(conn, "tiingo", symbol, "symbol", n=2)
        if not ok:
            return {"skipped": why}
    st_meta, meta_body = _get(META_URL.format(s=s))
    with engine.begin() as conn:
        quota.settle(conn, ids[0], st_meta)
    if st_meta != 200 or not meta_body:
        with engine.begin() as conn:
            quota.settle(conn, ids[1], 0)  # the prices request is not sent: release its reservation
            save_source(conn, cik=cik, symbol=symbol, provider="tiingo", bars=[], identity="name",
                        source_ref=META_URL.format(s=s), raw=meta_body)
        return {"status": "NOT_FOUND", "http": st_meta}
    meta = json.loads(meta_body)
    st_px, px_body = _get(PRICES_URL.format(s=s), params={"startDate": HISTORY_START.isoformat(),
                                                           "endDate": date.today().isoformat()})
    with engine.begin() as conn:
        quota.settle(conn, ids[1], st_px)
    bars = parse_prices(px_body) if st_px == 200 else []
    raw = json.dumps({"meta": meta, "prices": json.loads(px_body) if (st_px == 200 and px_body) else None}).encode()
    with engine.begin() as conn:
        sid, v = save_source(conn, cik=cik, symbol=symbol, provider="tiingo", bars=bars, identity="name",
                             vendor_name=meta.get("name"), source_ref=PRICES_URL.format(s=s), raw=raw)
    return {"status": v.status, "reasons": v.reasons, "bars": len(bars)}


def run(engine: Engine, max_symbols: int = 22) -> dict:
    """One resumable batch (default sized to stay under 50 requests/hour: 2 requests per symbol)."""
    if not configured():
        return {"skipped": "TIINGO_NOT_CONFIGURED"}
    out = {"symbols": 0, "accepted": 0, "rejected": 0, "low_confidence": 0, "not_found": 0, "stopped": None}
    for c in candidates(engine, n=max_symbols * 3):
        if out["symbols"] >= max_symbols:
            break
        r = fetch_symbol(engine, c["cik"], c["symbol"])
        if "skipped" in r:
            out["stopped"] = r["skipped"]  # quota reached: other free providers keep working
            break
        out["symbols"] += 1
        st = r["status"]
        out["accepted"] += st in ("TRUSTED", "VALIDATED_FALLBACK")
        out["rejected"] += st == "REJECTED"
        out["low_confidence"] += st == "LOW_CONFIDENCE"
        out["not_found"] += st == "NOT_FOUND"
    with engine.connect() as conn:
        out["quota"] = quota.summary(conn, "tiingo")
    return out


def connection_test(engine: Engine) -> dict:
    """Exactly one request (meta of a long-listed symbol) to verify the key; returns only the status."""
    if not configured():
        return {"configured": False}
    with engine.begin() as conn:
        ok, why, ids = quota.reserve(conn, "tiingo", "SPY", "test")
        if not ok:
            return {"configured": True, "skipped": why}
    st, body = _get(META_URL.format(s="spy"))
    with engine.begin() as conn:
        quota.settle(conn, ids[0], st)
        q = quota.summary(conn, "tiingo")
    return {"configured": True, "http_status": st, "ok": st == 200, "quota": q}
