"""Forward market snapshot at (or right after) available_at (V2 #73): the survivorship-safe dataset of the future.

For each live, non-backfill event with a security, store the last traded price, 20-day average volume, point-in-time
market cap / shares, SPY and the sector ETF, with the provider and the capture lag. Bid/ask are stored only when a
quote source provides them (the free daily provider does not -> NULL, never invented)."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

log = logging.getLogger(__name__)


def capture(conn: Connection, ev: dict, now: datetime | None = None) -> str:
    from app.backtest.sector import etf_for_sector
    from app.discovery.pit_facts import market_cap_pit
    from app.marketdata.provider import ensure_prices
    from app.securitymaster.provider import get_security_master
    now = now or datetime.now(timezone.utc)
    avail = ev["available_at"] or now
    sym = None
    if ev.get("issuer_cik"):
        sym, _basis, _ = get_security_master(conn).price_symbol(ev["issuer_cik"], avail.date(), ev.get("ticker"))
    sym = sym or (ev.get("ticker") if ev.get("country", "US") == "US" and not ev.get("issuer_cik") else sym)
    status, bars = "IDENTITY_UNRESOLVED", []
    if sym:
        try:
            bars = ensure_prices(conn, sym, avail.date() - timedelta(days=40), now.date())
        except Exception as e:  # noqa: BLE001
            log.info("snapshot %s: no prices (%s)", sym, e)
        status = "CAPTURED" if bars else "NO_QUOTE"
    last = bars[-1] if bars else None
    vols = [b.volume for b in bars[-21:-1] if b.volume]
    sector = conn.execute(text("SELECT sector FROM security_master WHERE cik = :c"), {"c": ev.get("issuer_cik")}).scalar() \
        if ev.get("issuer_cik") else None
    etf = etf_for_sector(sector)

    def px(s):
        try:
            b = ensure_prices(conn, s, now.date() - timedelta(days=7), now.date())
            return b[-1].close if b else None
        except Exception:  # noqa: BLE001
            return None
    mcap = market_cap_pit(conn, ev.get("issuer_cik"), avail.date()) if ev.get("issuer_cik") else None
    conn.execute(text("""
        INSERT INTO event_market_snapshots (event_id, captured_at, symbol, price, price_time, volume, avg_volume_20d, market_cap,
          shares_outstanding, benchmark_symbol, benchmark_price, sector_symbol, sector_price, provider, lag_seconds, status)
        VALUES (:e, :now, :sym, :p, :pt, :v, :av, :mc, :so, 'SPY', :spy, :etf, :etfp, :prov, :lag, :st)
        ON CONFLICT (event_id) DO NOTHING"""),
                 {"e": ev["id"], "now": now, "sym": sym, "p": last.close if last else None,
                  "pt": datetime.combine(last.date, datetime.min.time(), timezone.utc) if last else None,
                  "v": last.volume if last else None, "av": (sum(vols) / len(vols)) if vols else None, "mc": mcap,
                  "so": (mcap / last.close) if (mcap and last and last.close) else None, "spy": px("SPY"),
                  "etf": etf, "etfp": px(etf) if etf else None, "prov": "yahoo" if bars else None,
                  "lag": (now - avail).total_seconds(), "st": status})
    return status


def capture_pending(engine: Engine, max_age_hours: int = 36, limit: int = 200) -> dict:
    with engine.connect() as c:
        evs = [dict(r) for r in c.execute(text("""
            SELECT e.id, e.ticker, e.issuer_cik, e.country, e.available_at FROM events e
            LEFT JOIN event_market_snapshots s ON s.event_id = e.id
            WHERE s.event_id IS NULL AND NOT e.is_backfill AND NOT e.superseded AND e.available_at IS NOT NULL
              AND e.available_at > now() - make_interval(hours => :h) AND e.available_at <= now()
            ORDER BY e.available_at LIMIT :n"""), {"h": max_age_hours, "n": limit}).mappings()]
    out: dict[str, int] = {}
    for ev in evs:
        with engine.begin() as c:
            st = capture(c, ev)
        out[st] = out.get(st, 0) + 1
    return out
