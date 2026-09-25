"""Security identity gate + series quality checks for vendor / open-dataset price series.

Checks (all must hold for an accepted series):
  identity     the provider's identity evidence: the dataset's own CIK column ('cik'), the vendor's
               company name matching SEC's ('name'), or only the symbol ('symbol', weakest)
  ownership    the symbol is recorded for THIS CIK in SEC Form 3/4/5 symbols or its own events, and no
               other CIK used the symbol inside the series' date range (ticker reuse)
  delisting    the series ends around the SEC-documented delisting (a series that keeps trading more
               than 30 days after delisting belongs to someone else / a successor security)
  completeness >= 90% of trading days present, no non-positive prices
  cross-check  where a second series overlaps, daily adjusted RETURNS agree (levels are not compared,
               so raw vs split-adjusted is never mistaken for a disagreement)
Verdict: REJECTED (conflict) | LOW_CONFIDENCE (a check failed or cannot be verified) |
VALIDATED_FALLBACK (symbol-level identity, everything else consistent) | TRUSTED (CIK or name identity,
everything consistent, and cross-validated when a second series exists).
"""
from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.marketdata.calendar import is_trading_day
from app.marketdata.provider import Bar

DELIST_AFTER_DAYS = 30
DELIST_BEFORE_DAYS = 15
MIN_COMPLETENESS = 0.90
XV_MIN_OVERLAP = 20
XV_MEDIAN_TOL = 0.005
XV_OUTLIER_TOL = 0.02
XV_MAX_OUTLIER_SHARE = 0.05
STOP = {"inc", "corp", "corporation", "co", "company", "ltd", "plc", "holdings", "holding", "group", "the", "class",
        "common", "stock", "shares", "sa", "nv", "llc", "lp", "trust", "de", "new", "a", "b", "incorporated", "limited"}


def name_tokens(s: str | None) -> set[str]:
    return {w for w in re.sub(r"[^a-z0-9 ]", " ", (s or "").lower()).split() if w not in STOP and len(w) > 1}


def name_similarity(a: str | None, b: str | None) -> float | None:
    ta, tb = name_tokens(a), name_tokens(b)
    if not ta or not tb:
        return None
    return len(ta & tb) / min(len(ta), len(tb))


def completeness(bars: list[Bar]) -> float:
    if not bars:
        return 0.0
    d, n = bars[0].date, 0
    while d <= bars[-1].date:
        n += is_trading_day(d)
        d += timedelta(days=1)
    return min(1.0, len({b.date for b in bars}) / max(1, n))


def returns_agree(a: list[Bar], b: list[Bar]) -> dict:
    """Compare daily adjusted-close returns on common dates (basis-independent)."""
    def rets(bars):
        s = sorted(bars, key=lambda x: x.date)
        out = {}
        for p, c in zip(s, s[1:]):
            pa, ca = (p.adj_close or p.close), (c.adj_close or c.close)
            if pa and ca and pa > 0:
                out[c.date] = ca / pa - 1
        return out
    ra, rb = rets(a), rets(b)
    common = sorted(set(ra) & set(rb))
    if len(common) < XV_MIN_OVERLAP:
        return {"overlap": len(common), "agree": None}
    diffs = [abs(ra[d] - rb[d]) for d in common]
    med = statistics.median(diffs)
    out_share = sum(x > XV_OUTLIER_TOL for x in diffs) / len(diffs)
    return {"overlap": len(common), "median_abs_diff": round(med, 6), "outlier_share": round(out_share, 4),
            "agree": med <= XV_MEDIAN_TOL and out_share <= XV_MAX_OUTLIER_SHARE}


@dataclass
class Verdict:
    status: str
    confidence: float
    reasons: list[str] = field(default_factory=list)
    checks: dict = field(default_factory=dict)


def classify(conn: Connection, cik: str, symbol: str, bars: list[Bar], *, identity: str, vendor_name: str | None = None,
             delisting_date: date | None = None, other_series: list[list[Bar]] | None = None,
             today: date | None = None, source_end: date | None = None, truncated_at_delisting: bool = False) -> Verdict:
    """source_end: last date the SOURCE covers (a dataset snapshot); a series ending there is cut by the
    source, not by the company. truncated_at_delisting: exchange part of a series that continued past the
    SEC delisting (OTC / successor quotes removed) - never better than VALIDATED_FALLBACK."""
    today = today or date.today()
    bars = sorted((b for b in bars if b.close is not None), key=lambda b: b.date)
    v = Verdict("LOW_CONFIDENCE", 0.0)
    ck = v.checks
    if not bars:
        v.status, v.reasons = "REJECTED", ["EMPTY_SERIES"]
        return v
    first, last = bars[0].date, bars[-1].date
    ck["range"] = [first.isoformat(), last.isoformat()]
    bad = sum(1 for b in bars if (b.close or 0) <= 0 or (b.adj_close is not None and b.adj_close <= 0))
    ck["completeness"] = round(completeness(bars), 4)
    ck["non_positive"] = bad
    conflict, low = [], []
    # --- ownership / ticker reuse
    owners = conn.execute(text("""SELECT cik, min(first_seen) f, max(last_seen) l FROM security_symbols
                                  WHERE symbol = :s GROUP BY cik"""), {"s": symbol}).mappings().all()
    own = [o for o in owners if o["cik"] == cik]
    others = [o for o in owners if o["cik"] != cik and o["l"] and o["f"] and o["l"] >= first and o["f"] <= last]
    ev_sym = conn.execute(text("SELECT 1 FROM events WHERE issuer_cik = :c AND ticker = :s LIMIT 1"),
                          {"c": cik, "s": symbol}).first()
    ck["symbol_owned_by_cik"] = bool(own or ev_sym)
    ck["other_owner_in_range"] = [o["cik"] for o in others]
    if others and identity != "cik":
        conflict.append("TICKER_REUSED_IN_RANGE")
    if not (own or ev_sym) and identity == "symbol":
        low.append("SYMBOL_NOT_LINKED_TO_CIK")
    # --- vendor name vs SEC name
    if identity == "name":
        sec_names = [r[0] for r in conn.execute(text("""SELECT name FROM companies WHERE cik = :c UNION
                                                        SELECT DISTINCT company_name FROM events WHERE issuer_cik = :c
                                                        AND company_name IS NOT NULL LIMIT 20"""), {"c": cik}).all()]
        sims = [s for s in (name_similarity(vendor_name, n) for n in sec_names) if s is not None]
        ck["name_similarity"] = round(max(sims), 3) if sims else None
        if not sims:
            low.append("NO_SEC_NAME_TO_COMPARE")
            identity = "symbol"
        elif max(sims) < 0.2:
            conflict.append("VENDOR_NAME_MISMATCH")
        elif max(sims) < 0.5:
            low.append("VENDOR_NAME_WEAK_MATCH")
            identity = "symbol"
    # --- delisting consistency
    cut_by_source = source_end is not None and last >= source_end - timedelta(days=4)
    ck["cut_by_source_end"] = cut_by_source
    if delisting_date:
        ck["delisting_date"] = delisting_date.isoformat()
        if last > delisting_date + timedelta(days=DELIST_AFTER_DAYS):
            conflict.append("TRADES_AFTER_DELISTING")
        elif last < delisting_date - timedelta(days=DELIST_BEFORE_DAYS) and not cut_by_source:
            low.append("ENDS_BEFORE_DELISTING")
    elif last < today - timedelta(days=DELIST_AFTER_DAYS) and not cut_by_source:
        if identity == "symbol":
            low.append("ENDED_WITHOUT_SEC_DELISTING")
        else:
            ck["ended_without_sec_delisting"] = True
    # --- quality
    if bad:
        low.append("NON_POSITIVE_PRICES")
    if ck["completeness"] < MIN_COMPLETENESS:
        low.append("INCOMPLETE_SERIES")
    # --- cross-validation
    xv = [returns_agree(bars, o) for o in (other_series or []) if o]
    ck["cross_validation"] = xv
    if any(x.get("agree") is False for x in xv):
        low.append("CROSS_PROVIDER_DISAGREEMENT")
    cross_ok = any(x.get("agree") for x in xv)
    # --- verdict
    if conflict:
        v.status, v.confidence, v.reasons = "REJECTED", 0.0, conflict + low
        return v
    if low:
        v.status, v.confidence, v.reasons = "LOW_CONFIDENCE", 0.3, low
        return v
    if truncated_at_delisting:
        ck["truncated_at_delisting"] = True
        v.status, v.confidence = "VALIDATED_FALLBACK", 0.6
        v.reasons.append("TRUNCATED_AT_SEC_DELISTING")
    elif identity in ("cik", "name"):
        v.status = "TRUSTED"
        v.confidence = 0.95 if cross_ok else 0.85
        if not xv:
            v.reasons.append("NO_SECOND_SOURCE")
    else:
        v.status, v.confidence = "VALIDATED_FALLBACK", 0.7 if cross_ok else 0.6
    ck["identity"] = identity
    return v
