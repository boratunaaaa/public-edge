"""Conservative identity resolution for V2 datasets (never fuzzy).

name   -> CIK : exact normalized match over the SEC security master (current AND former names, delisted CIKs
                included -> no survivorship), unique match only (MEDIUM), else unresolved.
CUSIP  -> CIK : issuer CUSIPs stated on 13D/13G cover pages (the filer names the issuer) or the SEC registry's
                ticker CUSIP; the 6-char issuer prefix must agree (HIGH when two sources agree, else MEDIUM).
symbol -> CIK : point in time (symbol + date) through the SEC security master; a symbol alone is never an identity.
"""
from __future__ import annotations

import threading
import time
from datetime import date

from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.entities.names import normalize_company

_lock = threading.Lock()
_names: dict[str, set[str]] = {}
_names_at = 0.0


def cusip_check_digit_ok(cusip: str) -> bool:
    if not cusip or len(cusip) != 9:
        return False
    total = 0
    for i, ch in enumerate(cusip[:8].upper()):
        if ch.isdigit():
            v = int(ch)
        elif ch.isalpha():
            v = ord(ch) - 55
        elif ch in "*@#":
            v = {"*": 36, "@": 37, "#": 38}[ch]
        else:
            return False
        if i % 2:
            v *= 2
        total += v // 10 + v % 10
    return str((10 - total % 10) % 10) == cusip[8]


def _load_names(conn: Connection) -> dict[str, set[str]]:
    global _names, _names_at
    with _lock:
        if _names and time.time() - _names_at < 3600:
            return _names
        out: dict[str, set[str]] = {}
        for cik, name, former in conn.execute(text("SELECT cik, name, former_names FROM security_master")):
            for n in [name] + [f.get("name") if isinstance(f, dict) else f for f in (former or [])]:
                k = normalize_company(n)
                if k:
                    out.setdefault(k, set()).add(cik)
        for cik, name in conn.execute(text("SELECT cik, name FROM companies")):
            k = normalize_company(name)
            if k:
                out.setdefault(k, set()).add(cik)
        _names, _names_at = out, time.time()
        return out


def reset_cache() -> None:
    global _names_at
    _names_at = 0.0


def name_to_cik(conn: Connection, name: str | None) -> tuple[str | None, str, str]:
    """(cik, confidence, reason). Only a unique exact normalized match resolves."""
    k = normalize_company(name)
    if not k or len(k) < 3:
        return None, "NONE", "EMPTY_NAME"
    ciks = _load_names(conn).get(k, set())
    if len(ciks) == 1:
        return next(iter(ciks)), "MEDIUM", "EXACT_NORMALIZED_NAME"
    return None, "NONE", "AMBIGUOUS_NAME" if ciks else "NO_MATCH"


def cusip_to_cik(conn: Connection, cusip: str | None) -> tuple[str | None, str, str]:
    if not cusip or len(cusip) < 8:
        return None, "NONE", "BAD_CUSIP"
    c9 = cusip[:9].upper()
    thirteen_d = conn.execute(text("""SELECT issuer_cik, count(*) n FROM activist_filings WHERE upper(cusip) = :c
                                        AND issuer_cik IS NOT NULL GROUP BY 1 ORDER BY 2 DESC"""), {"c": c9}).all()
    reg = conn.execute(text("""SELECT DISTINCT c.cik FROM tickers t JOIN companies c ON c.id = t.company_id
                                WHERE upper(t.cusip) LIKE :p"""), {"p": c9[:8] + "%"}).scalars().all()
    a = {r[0] for r in thirteen_d}
    b = set(reg)
    if len(a) == 1 and (not b or b == a):
        return next(iter(a)), "HIGH" if b == a else "MEDIUM", "13D_COVER" + ("+SEC_REGISTRY" if b == a else "")
    if not a and len(b) == 1:
        return next(iter(b)), "MEDIUM", "SEC_REGISTRY"
    if a or b:
        return None, "NONE", "CONFLICT"
    return None, "NONE", "NO_MATCH"


def symbol_to_cik(conn: Connection, symbol: str | None, on: date) -> str | None:
    if not symbol:
        return None
    from app.securitymaster.provider import get_security_master
    return get_security_master(conn).resolve_security_at_date(symbol.upper(), on)
