"""Conservative identity resolution for V2 datasets (never fuzzy).

name   -> CIK : exact normalized match over the SEC security master (current AND former names, delisted CIKs
                included -> no survivorship), unique match only (MEDIUM), else unresolved.
CUSIP  -> CIK : issuer CUSIPs stated on 13D/13G cover pages (the filer names the issuer) or the SEC registry's
                ticker CUSIP, full 9 characters (HIGH when two sources agree, else MEDIUM); prefix-only or
                name-contradicted matches are LOW + REVIEW_REQUIRED (see cusip_to_cik).
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


# generic words that carry no issuer identity in a 13F "name of issuer" column
_NAME_STOP = {"INC", "CORP", "CO", "CORPORATION", "COMPANY", "LTD", "PLC", "THE", "HOLDINGS", "HLDGS", "HLDG", "GROUP",
              "GRP", "NEW", "CL", "CLASS", "COM", "COMMON", "SHS", "LP", "LLC", "SA", "AG", "NV", "TRUST", "TR", "FUND",
              "ETF", "INTL", "INTERNATIONAL", "AND", "OF", "DE", "DEL", "MD", "ADR", "SPONSORED", "ORD", "STK", "CAP", "USD",
              "NPV", "REIT", "BANCORP", "FINANCIAL", "TECHNOLOGIES", "THERAPEUTICS", "PHARMACEUTICALS", "SYSTEMS",
              "ENERGY", "CAPITAL", "INDUSTRIES", "SERVICES", "SOLUTIONS", "PARTNERS", "BANK", "HOLDING", "AMERICA"}
REVIEW = "REVIEW_REQUIRED"


def _name_words(name: str | None) -> list[str]:
    import re
    s = re.sub(r"[^A-Z0-9 ]", " ", (name or "").upper().replace("&", " "))
    return [t for t in s.split() if len(t) >= 2 and t not in _NAME_STOP]


def _abbrev_of(short: str, long: str) -> bool:
    """"MTN" / "MOUNTAIN", "CMNTY" / "COMMUNITY", "FIN" / "FINANCE": same first letter, letters in order."""
    if len(short) < 3 or len(short) >= len(long) or short[0] != long[0]:
        return False
    it = iter(long)
    return all(ch in it for ch in short)


def names_agree(a: str | None, names: list[str]) -> bool:
    """Loose, deliberately permissive agreement between a filer-typed issuer name and the CIK's SEC names (current +
    former) and tickers: one shared distinctive word (a ticker typed as the issuer name counts), a shared 4-letter
    word prefix, an in-order abbreviation ("NEW MTN FIN" / "NEW MOUNTAIN FINANCE"), or one name's letters contained in
    the other's ("WAL MART" / "WALMART"). Only a clear disagreement is flagged."""
    wa = _name_words(a)
    if not wa:
        return True  # nothing distinctive to compare
    ta, ca = set(wa), "".join(wa)
    for n in names:
        wb = _name_words(n)
        tb, cb = set(wb), "".join(wb)
        if ta & tb or any(x[:4] == y[:4] for x in ta for y in tb if len(x) >= 4 and len(y) >= 4):
            return True
        if any(_abbrev_of(x, y) or _abbrev_of(y, x) for x in ta for y in tb):
            return True
        if len(ca) >= 5 and len(cb) >= 5 and (ca in cb or cb in ca):
            return True
    return False


def cik_names(conn: Connection, cik: str) -> list[str]:
    """Every SEC name we know for a CIK: security master current + former names, and the registry name."""
    out: list[str] = []
    for name, former in conn.execute(text("SELECT name, former_names FROM security_master WHERE cik = :c"), {"c": cik}):
        out += [name] + [f.get("name") if isinstance(f, dict) else f for f in (former or [])]
    out += list(conn.execute(text("SELECT name FROM companies WHERE cik = :c"), {"c": cik}).scalars())
    # tickers too: 13F filers often type the symbol ("CTRI", "VRNA US", "MSM 04/17/2025 PUT 75") as the issuer name
    out += list(conn.execute(text("""SELECT t.symbol FROM tickers t JOIN companies c ON c.id = t.company_id WHERE c.cik = :c
                                     UNION SELECT symbol FROM security_symbols WHERE cik = :c"""), {"c": cik}).scalars())
    return [n for n in out if n]


def cusip_to_cik(conn: Connection, cusip: str | None, issuer_name: str | None = None) -> tuple[str | None, str, str]:
    """(cik, confidence, reason). Matching is on the FULL 9-character CUSIP only:
      * 13D/13G cover pages (the filer states the issuer's CUSIP) and the SEC registry's ticker CUSIP;
        HIGH when both agree, else MEDIUM; two different issuers -> CONFLICT (unresolved).
      * No exact match anywhere: an 8-character prefix hit (e.g. a registry CUSIP with a wrong check digit - Envista
        29415F104 vs a corrupt 29415F100 row of another issuer) is only a candidate -> LOW + REVIEW_REQUIRED, never a
        silent attribution. Registry placeholders (000000000) and invalid check digits never match.
      * issuer_name given: when the filer-typed issuer name clearly disagrees with every SEC name of the matched CIK
        (current and former), the match is downgraded to LOW + REVIEW_REQUIRED (e.g. two sources carrying the same
        wrong CUSIP). Callers mark LOW events needs_review, which keeps them out of validation and paper trading."""
    if not cusip or len(cusip) < 8:
        return None, "NONE", "BAD_CUSIP"
    c9 = cusip[:9].upper()
    thirteen_d = conn.execute(text("""SELECT issuer_cik, count(*) n FROM activist_filings WHERE upper(cusip) = :c
                                        AND issuer_cik IS NOT NULL GROUP BY 1 ORDER BY 2 DESC"""), {"c": c9}).all()
    reg_rows = conn.execute(text("""SELECT DISTINCT c.cik, upper(t.cusip) FROM tickers t JOIN companies c ON c.id = t.company_id
                                     WHERE upper(t.cusip) LIKE :p"""), {"p": c9[:8] + "%"}).all()
    usable = [(cik, cu) for cik, cu in reg_rows if cu and cu != "000000000" and cusip_check_digit_ok(cu)]
    a = {r[0] for r in thirteen_d}
    b = {cik for cik, cu in usable if cu == c9}
    if len(a) == 1 and (not b or b == a):
        cik, conf, why = next(iter(a)), "HIGH" if b == a else "MEDIUM", "13D_COVER" + ("+SEC_REGISTRY" if b == a else "")
    elif not a and len(b) == 1:
        cik, conf, why = next(iter(b)), "MEDIUM", "SEC_REGISTRY"
    elif a or b:
        return None, "NONE", "CONFLICT"
    else:
        prefix = {cik for cik, cu in reg_rows if cik and cu and cu != "000000000"}
        if len(prefix) == 1:
            return next(iter(prefix)), "LOW", f"{REVIEW}:CUSIP_PREFIX_ONLY"
        return None, "NONE", "CONFLICT" if prefix else "NO_MATCH"
    if issuer_name:
        names = cik_names(conn, cik)
        if names and not names_agree(issuer_name, names):
            return cik, "LOW", f"{REVIEW}:NAME_MISMATCH:{why}"
    return cik, conf, why


def symbol_to_cik(conn: Connection, symbol: str | None, on: date) -> str | None:
    if not symbol:
        return None
    from app.securitymaster.provider import get_security_master
    return get_security_master(conn).resolve_security_at_date(symbol.upper(), on)
