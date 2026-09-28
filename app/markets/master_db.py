"""Database-backed global security master (global_securities + security_symbol_periods).

security_id: "<COUNTRY>:<stable issuer id>:<class>" - US: CIK (SEC), CA: exchange directory symbol at first sight
until a SEDAR+/ISIN id is available, TR: ISIN when known else KAP member code. A ticker is an attribute with a
validity period; resolve(symbol, on) returns a security only when exactly one period contains the date."""
from __future__ import annotations

import json
from datetime import date

from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.entities.names import normalize_company


def upsert_security(conn: Connection, *, security_id: str, country: str, issuer_id: str, issuer_name: str,
                    exchange: str | None = None, cik: str | None = None, cusip: str | None = None, isin: str | None = None,
                    figi: str | None = None, local_id: str | None = None, security_type: str | None = None,
                    currency: str | None = None, listing_date: date | None = None, delisting_date: date | None = None,
                    tradeability_status: str = "UNKNOWN", identity_confidence: str = "MEDIUM", evidence: list | None = None) -> None:
    conn.execute(text("""
        INSERT INTO global_securities (security_id, country, exchange, issuer_id, issuer_name, cik, cusip, isin, figi, local_id,
          security_type, currency, listing_date, delisting_date, tradeability_status, identity_confidence, identity_evidence)
        VALUES (:sid, :c, :ex, :iid, :name, :cik, :cusip, :isin, :figi, :lid, :st, :cur, :ld, :dd, :ts, :ic, CAST(:ev AS jsonb))
        ON CONFLICT (security_id) DO UPDATE SET issuer_name = EXCLUDED.issuer_name,
          exchange = COALESCE(EXCLUDED.exchange, global_securities.exchange), cik = COALESCE(EXCLUDED.cik, global_securities.cik),
          cusip = COALESCE(EXCLUDED.cusip, global_securities.cusip), isin = COALESCE(EXCLUDED.isin, global_securities.isin),
          figi = COALESCE(EXCLUDED.figi, global_securities.figi), delisting_date = COALESCE(EXCLUDED.delisting_date, global_securities.delisting_date),
          tradeability_status = EXCLUDED.tradeability_status, updated_at = now()"""),
                 {"sid": security_id, "c": country, "ex": exchange, "iid": issuer_id, "name": issuer_name, "cik": cik, "cusip": cusip,
                  "isin": isin, "figi": figi, "lid": local_id, "st": security_type, "cur": currency, "ld": listing_date,
                  "dd": delisting_date, "ts": tradeability_status, "ic": identity_confidence, "ev": json.dumps(evidence or [])})


def add_symbol_period(conn: Connection, security_id: str, symbol: str, valid_from: date | None, valid_to: date | None,
                      source: str, exchange: str | None = None, confidence: str = "MEDIUM") -> None:
    conn.execute(text("""INSERT INTO security_symbol_periods (security_id, exchange, symbol, valid_from, valid_to, source, confidence)
                         VALUES (:s, :ex, :sym, :f, :t, :src, :c)
                         ON CONFLICT (security_id, symbol, valid_from, source) DO UPDATE SET valid_to = EXCLUDED.valid_to"""),
                 {"s": security_id, "ex": exchange, "sym": symbol.upper(), "f": valid_from, "t": valid_to, "src": source, "c": confidence})


def resolve(conn: Connection, symbol: str, on: date, *, exchange: str | None = None, country: str | None = None) -> str | None:
    """security_id that used `symbol` on `on` (exchange / country narrow the search). Unknown or ambiguous -> None."""
    rows = conn.execute(text("""
        SELECT DISTINCT p.security_id FROM security_symbol_periods p JOIN global_securities g USING (security_id)
        WHERE p.symbol = :sym AND (p.valid_from IS NULL OR p.valid_from <= :d) AND (p.valid_to IS NULL OR :d < p.valid_to)
          AND (CAST(:ex AS text) IS NULL OR p.exchange = :ex) AND (CAST(:c AS text) IS NULL OR g.country = :c)"""),
                        {"sym": symbol.upper(), "d": on, "ex": exchange, "c": country}).scalars().all()
    return rows[0] if len(rows) == 1 else None


def collisions(conn: Connection) -> list[dict]:
    """Symbols claimed by two securities over overlapping periods (ticker reuse or cross-exchange collision)."""
    return [dict(r) for r in conn.execute(text("""
        SELECT a.symbol, a.security_id a_id, b.security_id b_id, a.exchange a_ex, b.exchange b_ex
        FROM security_symbol_periods a JOIN security_symbol_periods b ON a.symbol = b.symbol AND a.security_id < b.security_id
        WHERE COALESCE(a.valid_from, '0001-01-01') < COALESCE(b.valid_to, '9999-12-31')
          AND COALESCE(b.valid_from, '0001-01-01') < COALESCE(a.valid_to, '9999-12-31')""")).mappings()]


def name_index(conn: Connection, country: str) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for sid, name in conn.execute(text("SELECT security_id, issuer_name FROM global_securities WHERE country = :c"), {"c": country}):
        k = normalize_company(name)
        if k:
            out.setdefault(k, set()).add(sid)
    return out


def build_us(conn: Connection) -> dict:
    """US securities from the SEC security master (delisted CIKs included) and point-in-time symbol history."""
    n = conn.execute(text("""
        INSERT INTO global_securities (security_id, country, issuer_id, issuer_name, cik, security_type, delisting_date,
                                       tradeability_status, identity_confidence, identity_evidence)
        SELECT 'US:' || cik || '\:COM', 'US', cik, COALESCE(name, cik), cik, 'COMMON', delisting_date,
               CASE status WHEN 'ACTIVE' THEN 'ACTIVE' WHEN 'DELISTED' THEN 'DELISTED' ELSE 'UNKNOWN' END, 'HIGH',
               '[{"source": "sec_submissions", "field": "cik"}]'::jsonb
        FROM security_master
        ON CONFLICT (security_id) DO UPDATE SET issuer_name = EXCLUDED.issuer_name, delisting_date = EXCLUDED.delisting_date,
          tradeability_status = EXCLUDED.tradeability_status, updated_at = now()""")).rowcount
    p = conn.execute(text("""
        INSERT INTO security_symbol_periods (security_id, exchange, symbol, valid_from, valid_to, source, confidence)
        SELECT 'US:' || s.cik || '\:COM', NULL, s.symbol, s.first_seen, s.last_seen + 1, s.source,
               CASE WHEN s.n_obs >= 3 THEN 'HIGH' ELSE 'MEDIUM' END
        FROM security_symbols s JOIN security_master m ON m.cik = s.cik
        ON CONFLICT (security_id, symbol, valid_from, source) DO UPDATE SET valid_to = EXCLUDED.valid_to""")).rowcount
    return {"securities": n, "symbol_periods": p}


def build_ca_directory(conn: Connection, rows: list[dict], as_of: date) -> int:
    """TSX / TSXV directory rows (app.sources.ca.newswire.parse_directory). First sight opens a symbol period."""
    n = 0
    for r in rows:
        sid = f"CA:{r['exchange']}:{r['ticker']}"
        upsert_security(conn, security_id=sid, country="CA", exchange=r["exchange"], issuer_id=f"{r['exchange']}:{r['ticker']}",
                        issuer_name=r["company_name"], local_id=r["ticker"], security_type="COMMON", currency="CAD",
                        tradeability_status="ACTIVE", identity_confidence="MEDIUM",
                        evidence=[{"source": "tsx_directory", "as_of": as_of.isoformat()}])
        exists = conn.execute(text("SELECT 1 FROM security_symbol_periods WHERE security_id = :s AND symbol = :y"),
                              {"s": sid, "y": r["ticker"]}).first()
        if not exists:
            add_symbol_period(conn, sid, r["ticker"], as_of, None, "tsx_directory", exchange=r["exchange"])
        n += 1
    return n
