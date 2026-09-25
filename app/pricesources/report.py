"""FREE DATA RECOVERY coverage report + hard test set (source-by-source comparison)."""
from __future__ import annotations

import time
from datetime import timedelta

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from app.db.store import job_state, save_job_state

CAUSE_SQL = """CASE
  WHEN e.missing_reason = 'DELISTED_MERGER' OR (e.missing_reason IS NULL AND sm.delisting_reason = 'MERGER') THEN 'MERGER'
  WHEN e.missing_reason = 'DELISTED_BANKRUPTCY' OR (e.missing_reason IS NULL AND sm.delisting_reason = 'BANKRUPTCY') THEN 'BANKRUPTCY'
  WHEN e.missing_reason = 'DELISTED_COMPLIANCE' OR (e.missing_reason IS NULL AND sm.delisting_reason = 'EXCHANGE_COMPLIANCE') THEN 'DELIST_RULE'
  WHEN e.missing_reason IN ('TICKER_REUSED','TICKER_UNRESOLVED','SYMBOL_UNVERIFIED') THEN 'TICKER_CHANGE'
  ELSE 'UNKNOWN' END"""


def unresolved_by_cause(conn: Connection) -> dict:
    rows = conn.execute(text(f"""SELECT {CAUSE_SQL} cause, count(*) events, count(DISTINCT e.issuer_cik) ciks
                                 FROM events e LEFT JOIN security_master sm ON sm.cik = e.issuer_cik
                                 WHERE e.returns_status = 'no_data' AND NOT e.superseded GROUP BY 1""")).mappings().all()
    out = {r["cause"]: {"events": r["events"], "companies": r["ciks"]} for r in rows}
    out["TOTAL"] = {"events": sum(v["events"] for v in out.values()), "companies": conn.execute(text(
        "SELECT count(DISTINCT issuer_cik) FROM events WHERE returns_status = 'no_data' AND NOT superseded")).scalar()}
    return out


def baseline(engine: Engine) -> dict:
    """The starting snapshot is taken once and never overwritten."""
    with engine.begin() as conn:
        st = job_state(conn, "free_prices_baseline")
        if not st.get("snapshot"):
            st = {"snapshot": unresolved_by_cause(conn), "taken_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            save_job_state(conn, "free_prices_baseline", st, status="done")
    return st


def provider_results(conn: Connection) -> dict:
    src = conn.execute(text("""SELECT provider, validation_status, count(*) n, count(DISTINCT cik) ciks
                               FROM price_sources GROUP BY 1, 2 ORDER BY 1, 2""")).mappings().all()
    reasons = conn.execute(text("""SELECT provider, r reason, count(*) n FROM price_sources, jsonb_array_elements_text(reasons) r
                                   WHERE validation_status IN ('REJECTED','LOW_CONFIDENCE') GROUP BY 1, 2 ORDER BY 3 DESC""")).mappings().all()
    resolved = conn.execute(text(f"""
        SELECT p.provider, {CAUSE_SQL} cause, count(DISTINCT e.id) events, count(DISTINCT e.issuer_cik) companies
        FROM events e JOIN price_sources p ON p.cik = e.issuer_cik AND p.symbol = e.price_symbol
             AND p.validation_status IN ('TRUSTED','VALIDATED_FALLBACK')
        LEFT JOIN security_master sm ON sm.cik = e.issuer_cik
        WHERE e.price_symbol_basis IN ('vendor_trusted','vendor_validated') AND e.returns_status IN ('complete','partial')
          AND NOT e.superseded GROUP BY 1, 2""")).mappings().all()
    out: dict = {}
    for r in src:
        out.setdefault(r["provider"], {"sources": {}, "rejected_reasons": {}, "resolved": {}})["sources"][r["validation_status"]] = \
            {"series": r["n"], "companies": r["ciks"]}
    for r in reasons:
        out.setdefault(r["provider"], {"sources": {}, "rejected_reasons": {}, "resolved": {}})["rejected_reasons"][r["reason"]] = r["n"]
    for r in resolved:
        out.setdefault(r["provider"], {"sources": {}, "rejected_reasons": {}, "resolved": {}})["resolved"][r["cause"]] = \
            {"events": r["events"], "companies": r["companies"]}
    return out


NAMED = ["SIVB", "FRC", "TWTR", "ATVI", "CODQL"]


def hard_set(conn: Connection, n: int = 100) -> list[dict]:
    """>= n hard securities: the named ones + the most event-heavy unresolved companies of each cause."""
    quotas = {"MERGER": 40, "BANKRUPTCY": 20, "TICKER_CHANGE": 20, "DELIST_RULE": 10, "UNKNOWN": 15}
    picked: dict[str, dict] = {}
    for sym in NAMED:
        r = conn.execute(text("""SELECT DISTINCT cik FROM security_symbols WHERE symbol = :s
                                 UNION SELECT DISTINCT issuer_cik FROM events WHERE ticker = :s LIMIT 3"""), {"s": sym}).all()
        for (cik,) in r:
            picked[cik] = {"cik": cik, "symbol": sym, "cause": "NAMED"}
    for cause, k in quotas.items():
        rows = conn.execute(text(f"""SELECT e.issuer_cik cik, max(e.ticker) symbol, count(*) events FROM events e
                                     LEFT JOIN security_master sm ON sm.cik = e.issuer_cik
                                     WHERE e.returns_status = 'no_data' AND NOT e.superseded AND {CAUSE_SQL} = :c
                                     GROUP BY 1 ORDER BY 3 DESC LIMIT :k"""), {"c": cause, "k": k}).mappings().all()
        for r in rows:
            picked.setdefault(r["cik"], {"cik": r["cik"], "symbol": r["symbol"], "cause": cause})
    out = []
    for p in list(picked.values())[: max(n, len(picked))]:
        srcs = conn.execute(text("""SELECT provider, symbol, validation_status, reasons, n_bars, first_date, last_date
                                    FROM price_sources WHERE cik = :c"""), {"c": p["cik"]}).mappings().all()
        sm = conn.execute(text("SELECT name, delisting_reason, delisting_date, status FROM security_master WHERE cik = :c"),
                          {"c": p["cik"]}).mappings().first()
        ev = conn.execute(text("""SELECT min(public_date) FILTER (WHERE returns_status = 'no_data') first_unresolved,
                                  count(*) FILTER (WHERE returns_status = 'no_data') unresolved,
                                  count(*) FILTER (WHERE price_symbol_basis LIKE 'vendor%%') vendor_resolved
                                  FROM events WHERE issuer_cik = :c AND NOT superseded"""), {"c": p["cik"]}).mappings().first()
        out.append({**p, "sec": dict(sm) if sm else None, "events": dict(ev),
                    "sources": {s["provider"]: {"symbol": s["symbol"], "status": s["validation_status"], "reasons": s["reasons"],
                                                "bars": s["n_bars"], "range": [str(s["first_date"]), str(s["last_date"])]}
                                for s in srcs}})
    return out


def probe_yahoo(symbols: list[tuple[str, object]]) -> dict[str, bool]:
    from app.marketdata.provider import get_provider
    out = {}
    for sym, d in symbols:
        try:
            out[sym] = bool(get_provider().get_historical_prices(sym, d - timedelta(days=5), d + timedelta(days=30)))
        except Exception:
            out[sym] = False
    return out


def probe_wayback(symbols: list[str], pause_s: float = 1.0) -> dict[str, dict]:
    """Internet Archive CDX lookups only (no page downloads): is there any archived Yahoo DAILY history
    (the retired ichart CSV, the v7 download CSV, or a v8 chart with a multi-year range)?"""
    import httpx
    out = {}
    for s in symbols:
        res = {}
        for label, url in (("ichart_csv", f"ichart.finance.yahoo.com/table.csv?s={s}*"),
                           ("v7_download", f"query1.finance.yahoo.com/v7/finance/download/{s}*"),
                           ("v8_chart", f"query1.finance.yahoo.com/v8/finance/chart/{s}*")):
            try:
                r = httpx.get("http://web.archive.org/cdx/search/cdx", params={"url": url, "output": "json", "limit": 200},
                              timeout=30, headers={"User-Agent": "PublicEdge research"})
                rows = r.json()[1:] if r.status_code == 200 and r.text.strip() else []
            except Exception:
                rows = None
            if rows is None:
                res[label] = "error"
            else:
                daily = [x for x in rows if "range=1y" in x[2] or "range=5y" in x[2] or "range=max" in x[2] or "period1" in x[2]
                         or label != "v8_chart"]
                res[label] = {"snapshots": len(rows), "daily_history_candidates": len(daily)}
            time.sleep(pause_s)
        out[s] = res
    return out
