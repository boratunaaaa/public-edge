"""Point-in-time fundamentals: XBRL facts kept with the date of the filing that disclosed them, so a
historical event only sees revenue / market cap that an investor could have known that day."""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import and_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection

from app import http
from app.db.models import fundamental_facts, processing_jobs

log = logging.getLogger(__name__)
COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
REVENUE_CONCEPTS = ("RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet",
                    "RevenueFromContractWithCustomerIncludingAssessedTax", "RevenuesNetOfInterestExpense")
REFRESH_DAYS = 30


def parse_companyfacts(cik: str, d: dict) -> list[dict]:
    gaap = (d.get("facts") or {}).get("us-gaap") or {}
    out = []
    for concept in REVENUE_CONCEPTS:
        for f in ((gaap.get(concept) or {}).get("units") or {}).get("USD") or []:
            try:
                end = date.fromisoformat(f["end"])
                start = date.fromisoformat(f["start"]) if f.get("start") else None
                filed = date.fromisoformat(f["filed"])
            except (KeyError, ValueError):
                continue
            if start is None:
                continue
            out.append({"cik": cik.zfill(10), "concept": "Revenue", "period_start": start, "period_end": end,
                        "val": float(f["val"]), "filed": filed, "accession": f.get("accn") or "", "form": f.get("form")})
    return out


def ensure_facts(conn: Connection, cik: str) -> None:
    cik = cik.zfill(10)
    key = f"pit_facts:{cik}"
    row = conn.execute(select(processing_jobs.c.last_finished).where(processing_jobs.c.name == key)).first()
    if row and row[0] and datetime.now(timezone.utc) - row[0] < timedelta(days=REFRESH_DAYS):
        return
    try:
        data = http.get(COMPANYFACTS_URL.format(cik=cik), retries=1).json()
        rows = parse_companyfacts(cik, data)
    except http.HttpError as e:
        if e.status != 404:
            raise
        rows = []
    for i in range(0, len(rows), 1000):
        conn.execute(pg_insert(fundamental_facts).values(rows[i:i + 1000]).on_conflict_do_nothing(
            index_elements=["cik", "concept", "period_start", "period_end", "accession"]))
    now = datetime.now(timezone.utc)
    conn.execute(pg_insert(processing_jobs).values(name=key, status="done", state={"facts": len(rows)}, last_finished=now)
                 .on_conflict_do_update(index_elements=["name"], set_={"last_finished": now, "state": {"facts": len(rows)}}))


def ttm_from_facts(facts: list[dict], as_of: date) -> float | None:
    """Trailing-twelve-month revenue from facts FILED on/before as_of.
    Four distinct ~quarterly periods ending within 15 months, else the latest ~annual period."""
    known = [f for f in facts if f["filed"] <= as_of and f["period_end"] <= as_of
             and (as_of - f["period_end"]).days <= 460]
    if not known:
        return None
    latest: dict[tuple, dict] = {}
    for f in known:  # a re-report (later filing, same period) replaces the earlier value
        k = (f["period_start"], f["period_end"])
        if k not in latest or f["filed"] > latest[k]["filed"]:
            latest[k] = f
    quarters = sorted((f for f in latest.values() if 80 <= (f["period_end"] - f["period_start"]).days <= 100),
                      key=lambda f: f["period_end"], reverse=True)
    picked: list[dict] = []
    for q in quarters:
        if all(abs((q["period_end"] - p["period_end"]).days) > 45 for p in picked):
            picked.append(q)
        if len(picked) == 4:
            break
    if len(picked) == 4 and (picked[0]["period_end"] - picked[-1]["period_end"]).days <= 300:
        return float(sum(p["val"] for p in picked))
    annual = [f for f in latest.values() if 350 <= (f["period_end"] - f["period_start"]).days <= 380]
    if annual:
        return float(max(annual, key=lambda f: f["period_end"])["val"])
    return None


def ttm_revenue_as_of(conn: Connection, cik: str | None, as_of: date) -> float | None:
    if not cik:
        return None
    try:
        ensure_facts(conn, cik)
    except Exception as e:  # unavailable facts fail closed (no revenue), never fall back to today's data
        log.info("pit facts %s unavailable: %s", cik, e)
        return None
    rows = conn.execute(select(fundamental_facts).where(and_(
        fundamental_facts.c.cik == cik.zfill(10), fundamental_facts.c.concept == "Revenue",
        fundamental_facts.c.filed <= as_of))).mappings().all()
    return ttm_from_facts([dict(r) for r in rows], as_of)


def market_cap_pit(conn: Connection, cik: str | None, on: date) -> float | None:
    """Shares known on `on` (SEC cover-page facts filed on/before it) x the close of the same CIK's
    price series (security master identity) on/just before `on`."""
    if not cik:
        return None
    from app.marketdata.provider import load_share_facts, shares_as_of
    from app.securitymaster.provider import get_security_master
    got = shares_as_of(load_share_facts(conn, cik), on)
    if got is None:
        return None
    ps = get_security_master(conn).get_historical_price(cik, on - timedelta(days=10), on, on=on)
    closes = [b for b in ps.bars if b.date <= on and b.close]
    if not closes:
        return None
    return float(got[0]) * float(closes[-1].close)
