"""Microcap financing-risk score (companies below $1B).

Primary sources: SEC submissions (S-3 shelves, 424B prospectus supplements, 8-K 3.02 unregistered
sales = PIPEs, 8-K 3.01 listing deficiencies), EDGAR full-text risk phrases (ATM, going concern,
reverse split, convertible notes, warrants) and XBRL (share-count growth, cash burn, runway).
A strong discovery signal never hides this: HIGH_FINANCING_RISK is stored as a HIGH risk and shown
in Telegram and on the dashboard.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from sqlalchemy import and_, select
from sqlalchemy.engine import Connection

from app.core import clock as _clock
from app.db.models import corporate_actions
from app.discovery.risks import upsert_risk

SUB_BILLION = 1e9
HIGH_THRESHOLD = 5.0
WEIGHTS = {"GOING_CONCERN": 4.0, "RUNWAY_UNDER_9M": 3.0, "RUNWAY_UNDER_18M": 1.5, "SHARE_GROWTH_50PCT": 2.5,
           "SHARE_GROWTH_20PCT": 1.0, "ATM_PROGRAM": 1.5, "SHELF_S3": 0.5, "PROSPECTUS_424B_3PLUS": 1.5,
           "PIPE_3_02": 1.5, "CONVERTIBLE_NOTES": 1.0, "WARRANTS": 0.5, "REVERSE_SPLIT": 2.0,
           "EXCHANGE_COMPLIANCE_3_01": 2.0, "MIN_BID_DEFICIENCY": 1.5, "FREQUENT_RAISES": 1.5}


def _count(conn: Connection, cik: str, action: str, days: int) -> int:
    return len(conn.execute(select(corporate_actions.c.accession).where(and_(
        corporate_actions.c.cik == cik, corporate_actions.c.action == action,
        corporate_actions.c.action_date >= _clock.today() - timedelta(days=days)))).all())


def financing_flags(conn: Connection, cik: str, metrics: dict, risks: list[dict]) -> list[str]:
    have = {r["risk"]: r["severity"] for r in risks}
    flags = []
    if "GOING_CONCERN" in have:
        flags.append("GOING_CONCERN")
    rw = metrics.get("runway_months")
    if rw is not None and rw < 9:
        flags.append("RUNWAY_UNDER_9M")
    elif rw is not None and rw < 18:
        flags.append("RUNWAY_UNDER_18M")
    g = metrics.get("share_growth_yoy")
    if g is not None and g >= 0.5:
        flags.append("SHARE_GROWTH_50PCT")
    elif g is not None and g >= 0.2:
        flags.append("SHARE_GROWTH_20PCT")
    if "ATM_OFFERING" in have:
        flags.append("ATM_PROGRAM")
    if _count(conn, cik, "SHELF_REGISTRATION", 730):
        flags.append("SHELF_S3")
    if _count(conn, cik, "PROSPECTUS_424B", 365) >= 3:
        flags.append("PROSPECTUS_424B_3PLUS")
    if _count(conn, cik, "UNREGISTERED_SALE", 365):
        flags.append("PIPE_3_02")
    if "CONVERTIBLE_DEBT" in have:
        flags.append("CONVERTIBLE_NOTES")
    if "WARRANTS" in have:
        flags.append("WARRANTS")
    if "REVERSE_SPLIT" in have:
        flags.append("REVERSE_SPLIT")
    if _count(conn, cik, "LISTING_DEFICIENCY", 365):
        flags.append("EXCHANGE_COMPLIANCE_3_01")
    if "MIN_BID_DEFICIENCY" in have or "NASDAQ_MIN_BID_RISK" in have:
        flags.append("MIN_BID_DEFICIENCY")
    if "FREQUENT_CAPITAL_RAISES" in have:
        flags.append("FREQUENT_RAISES")
    return flags


def financing_score(flags: list[str]) -> float:
    return round(min(10.0, sum(WEIGHTS.get(f, 0.0) for f in flags)), 2)


def is_high(flags: list[str], score: float) -> bool:
    return score >= HIGH_THRESHOLD or "GOING_CONCERN" in flags or {"RUNWAY_UNDER_9M", "SHARE_GROWTH_50PCT"} <= set(flags)


def assess(conn: Connection, cik: str, market_cap: float | None, metrics: dict, risks: list[dict]) -> tuple[float | None, list[str]]:
    """(score, flags) for sub-$1B companies; stores/clears the HIGH_FINANCING_RISK risk row."""
    from app.db.models import discovery_risks
    if market_cap is not None and market_cap >= SUB_BILLION:
        conn.execute(discovery_risks.delete().where(and_(discovery_risks.c.cik == cik, discovery_risks.c.risk == "HIGH_FINANCING_RISK")))
        return None, []
    flags = financing_flags(conn, cik, metrics, risks)
    score = financing_score(flags)
    if is_high(flags, score):
        upsert_risk(conn, cik, "HIGH_FINANCING_RISK", "HIGH", f"score {score}/10: {', '.join(flags)}", None,
                    datetime.now(timezone.utc))
    else:
        conn.execute(discovery_risks.delete().where(and_(discovery_risks.c.cik == cik, discovery_risks.c.risk == "HIGH_FINANCING_RISK")))
    return score, flags
