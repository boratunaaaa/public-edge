"""Point-in-time issuer identity of a House PTR stock line.

The ticker in a PTR comes from the member's disclosure text ("Deere & Company (DE) [ST]"). It is resolved to the
issuer that used that symbol ON THE TRADE DATE (security_symbols periods observed in SEC filings), never from today's
ticker map alone - a reused symbol would otherwise attach an old trade to a different company.

    resolve(conn, ticker, asset_name, on) -> Identity(cik, confidence, reason)

  HIGH     exactly one issuer used the symbol around `on` and its SEC names agree with the disclosed asset name
  MEDIUM   exactly one issuer, nothing distinctive to compare; or no symbol history at all and today's map points to
           an issuer listed by then whose names agree with the asset name
  LOW      REVIEW_REQUIRED - several issuers around `on` (AMBIGUOUS_SYMBOL), a clear name contradiction
           (NAME_MISMATCH), a parser record that merged several disclosure lines (PARSER_MERGED_RECORDS), no issuer
           at the date (NO_ISSUER_AT_DATE), no ticker (NO_TICKER)

A LOW identity is never attributed silently: callers mark the event needs_review (kept for audit, excluded from
returns, validation and paper trading)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.markets.identity import REVIEW, cik_names, names_agree
from app.securitymaster.provider import SYMBOL_GRACE_DAYS

# the Clerk's PDFs render check boxes and some glyphs as letters ("gfedc", "FIlINg STATuS: New"); none is a name
_JUNK = re.compile(r"\bgfedc?b?\b|FILING\s+STATUS:?\s*\w*|SUBHOLDING\s+OF:.*|DESCRIPTION:.*|COMMENTS?:.*", re.I)
# the PTR table header, repeated at page breaks, can land right before a "(TICKER)": never an issuer name
_HEADER = re.compile(r"\b(ID|Owner|Asset|Transaction|Type|Date|Notification|Amount|Cap\.?|Gains|JT|SP|DC)\b|[>?$]", re.I)
_ANY_TICKER = re.compile(r"\(([A-Za-z][A-Za-z0-9.\-]{0,7})\)")
_TYPE_TAG = re.compile(r"\[[A-Z]{2}\]")
FRONT_GRACE_DAYS = 30  # same as SecSecurityMaster.resolve_security_at_date


@dataclass(frozen=True)
class Identity:
    cik: str | None
    confidence: str  # HIGH | MEDIUM | LOW | NONE
    reason: str

    @property
    def review(self) -> bool:
        return self.confidence in ("LOW", "NONE")


def merged_record(asset_name: str | None) -> bool:
    """Line-shift protection: one parsed PTR record that carries several asset-type tags or several parenthesised
    tickers (any case - the PDF text mangles case) is several disclosure lines run together; its ticker may belong to
    another line's transaction."""
    a = asset_name or ""
    return len(_TYPE_TAG.findall(a)) > 1 or len({t.upper() for t in _ANY_TICKER.findall(a)}) > 1


_SHARE_CLASS = re.compile(r"\b(common|ordinary|stock|shares?|class\s+[a-z]\b|cl\s+[a-z]\b|units?|representing|limited|"
                          r"partner(ship)?\s+interests?|sponsored|american\s+depositary|depository|receipts?|adrs?|ads|"
                          r"new|par\s+value|\$?\d+(\.\d+)?)\b", re.I)
NAME_WORDS = 8  # the issuer name sits right before "(TICKER)"; earlier text can be a page header or a comment


def clean_asset_name(asset_name: str | None, ticker: str | None = None) -> str:
    """The issuer name as disclosed: the words just before "(TICKER)" (case-insensitive - the PDF text mangles case),
    without check-box glyphs, filing-status boilerplate or share-class words ("Common Stock", "Class A", "Units")."""
    a = asset_name or ""
    m = re.search(r"\(\s*" + re.escape(ticker) + r"\s*\)", a, re.I) if ticker else None
    a = a[: m.start()] if m else a.split("(")[0]
    a = _HEADER.sub(" ", _SHARE_CLASS.sub(" ", _JUNK.sub(" ", a)))
    return " ".join(a.split()[-NAME_WORDS:]).strip(" -,.")


def sec_names(conn: Connection, cik: str) -> list[str]:
    """The CIK's SEC names without its bare tickers (cik_names also lists symbols; a symbol is not a name to compare)."""
    return [n for n in cik_names(conn, cik) if n and not re.fullmatch(r"[A-Z0-9.\-]{1,6}", n.strip())]


def symbol_issuers(conn: Connection, ticker: str, on: date) -> set[str]:
    """Issuers observed (in SEC filings) using the symbol around `on` (production grace windows)."""
    rows = conn.execute(text("""SELECT DISTINCT cik FROM security_symbols WHERE upper(symbol) = :s AND source <> 'sec_registry'
                                AND first_seen - make_interval(days => :f) <= :on AND last_seen + make_interval(days => :g) >= :on"""),
                        {"s": ticker.upper(), "on": on, "f": FRONT_GRACE_DAYS, "g": SYMBOL_GRACE_DAYS}).scalars()
    return set(rows)


def _issuers_ever(conn: Connection, ticker: str) -> set[str]:
    return set(conn.execute(text("SELECT DISTINCT cik FROM security_symbols WHERE upper(symbol) = :s AND source <> 'sec_registry'"),
                            {"s": ticker.upper()}).scalars())


def resolve(conn: Connection, ticker: str | None, asset_name: str | None, on: date | None) -> Identity:
    if merged_record(asset_name):
        return Identity(None, "LOW", f"{REVIEW}:PARSER_MERGED_RECORDS")
    if not ticker:
        return Identity(None, "NONE", f"{REVIEW}:NO_TICKER")
    if on is None:
        return Identity(None, "NONE", f"{REVIEW}:NO_TRADE_DATE")
    name = clean_asset_name(asset_name, ticker)
    ciks = symbol_issuers(conn, ticker, on)
    if len(ciks) > 1:
        return Identity(None, "LOW", f"{REVIEW}:AMBIGUOUS_SYMBOL:{','.join(sorted(ciks))}")
    if len(ciks) == 1:
        cik = next(iter(ciks))
        names = sec_names(conn, cik)
        if name and names and not names_agree(name, names):
            return Identity(cik, "LOW", f"{REVIEW}:NAME_MISMATCH")
        return Identity(cik, "HIGH" if (name and names) else "MEDIUM", "SYMBOL_PERIOD")
    # no issuer observed with the symbol around the trade date. Our filing-based symbol history starts late for many
    # issuers, so a symbol that only ONE issuer ever used (no reuse) is that issuer when its names agree; a symbol used
    # by several issuers over time, none around this date, stays ambiguous.
    ever = _issuers_ever(conn, ticker)
    if len(ever) == 1:
        cik = next(iter(ever))
        names = sec_names(conn, cik)
        if name and names and names_agree(name, names):
            return Identity(cik, "MEDIUM", "SINGLE_ISSUER_SYMBOL_NAME_MATCH")
        return Identity(cik, "LOW", f"{REVIEW}:SINGLE_ISSUER_SYMBOL_UNCONFIRMED")
    if ever:
        return Identity(None, "LOW", f"{REVIEW}:NO_ISSUER_AT_DATE")
    from app.entities.registry import resolver
    resolver.load(conn)  # no-op when loaded; the map must never be silently empty
    cur = resolver.ticker_to_cik.get(ticker.upper())
    if cur:
        from app.securitymaster.provider import get_security_master
        lp = get_security_master(conn, allow_vendor=False).get_listing_period(cur)
        listed = lp is None or lp[0] is None or lp[0] - timedelta(days=FRONT_GRACE_DAYS) <= on
        names = sec_names(conn, cur)
        if listed and name and names and names_agree(name, names):
            return Identity(cur, "MEDIUM", "CURRENT_MAP_NAME_MATCH")
        return Identity(cur, "LOW", f"{REVIEW}:CURRENT_MAP_UNCONFIRMED")
    return Identity(None, "NONE", f"{REVIEW}:NO_SEC_ISSUER")
