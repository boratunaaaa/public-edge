"""Microcap risk filter (stored in discovery_risks). Official SEC text + XBRL + market data."""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import and_, delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection, Engine

from app.db.models import discovery_risks, insider_transactions, news_items
from app.discovery.sec_data import doc_url, efts_search

log = logging.getLogger(__name__)

# risk -> (exact phrase, forms, severity)
TEXT_RISKS: dict[str, tuple[str, list[str], str]] = {
    "GOING_CONCERN": ("substantial doubt about our ability to continue as a going concern", ["10-K", "10-Q"], "HIGH"),
    "GOING_CONCERN_ALT": ("substantial doubt about the Company's ability to continue as a going concern", ["10-K", "10-Q"], "HIGH"),
    "ATM_OFFERING": ("at-the-market offering", ["8-K", "424B5", "S-3"], "MEDIUM"),
    "ATM_OFFERING_ALT": ("at the market offering", ["8-K", "424B5", "S-3"], "MEDIUM"),
    "REGISTERED_DIRECT": ("registered direct offering", ["8-K", "424B5"], "MEDIUM"),
    "REVERSE_SPLIT": ("reverse stock split", ["8-K"], "MEDIUM"),
    "MIN_BID_DEFICIENCY": ("minimum bid price", ["8-K"], "HIGH"),
    "CONVERTIBLE_DEBT": ("convertible notes", ["8-K"], "LOW"),
    "WARRANTS": ("warrants to purchase up to", ["8-K", "424B5"], "LOW"),
    "MATERIAL_WEAKNESS": ("material weakness in our internal control", ["10-K", "10-Q"], "MEDIUM"),
    "LATE_FILING": ("could not be eliminated without unreasonable effort or expense", ["NT 10-K", "NT 10-Q"], "MEDIUM"),
}
CANON = {"GOING_CONCERN_ALT": "GOING_CONCERN", "ATM_OFFERING_ALT": "ATM_OFFERING"}


def scan_text_risks(engine: Engine, universe_ciks: set[str], days: int = 365) -> dict:
    """One EDGAR full-text query per phrase covers every company at once."""
    end, start = date.today(), date.today() - timedelta(days=days)
    counts: dict[str, dict[str, list[dict]]] = {}
    for key, (phrase, forms, _sev) in TEXT_RISKS.items():
        try:
            hits = efts_search(phrase, forms, start, end, max_hits=2000)
        except Exception as e:
            log.warning("risk scan %s failed: %s", key, e)
            continue
        risk = CANON.get(key, key)
        for h in hits:
            if h["cik"] in universe_ciks:
                counts.setdefault(risk, {}).setdefault(h["cik"], []).append(h)
    now = datetime.now(timezone.utc)
    n = 0
    with engine.begin() as conn:
        for risk, per in counts.items():
            sev = next(v[2] for k, v in TEXT_RISKS.items() if CANON.get(k, k) == risk)
            for cik, hits in per.items():
                raises = len({h["adsh"] for h in hits})
                s = sev
                if risk in ("ATM_OFFERING", "REGISTERED_DIRECT") and raises >= 3:
                    s = "HIGH"
                latest = max(hits, key=lambda h: h["file_date"] or "")
                upsert_risk(conn, cik, risk, s, f"{raises} filing(s) in {days}d, latest {latest['file_date']} {latest['form']}",
                            doc_url(cik, latest["adsh"], latest["filename"]), now)
                n += 1
        # frequent capital raises across offering types
        for cik in set(counts.get("ATM_OFFERING", {})) | set(counts.get("REGISTERED_DIRECT", {})):
            total = len({h["adsh"] for r in ("ATM_OFFERING", "REGISTERED_DIRECT") for h in counts.get(r, {}).get(cik, [])})
            if total >= 3:
                upsert_risk(conn, cik, "FREQUENT_CAPITAL_RAISES", "HIGH", f"{total} offering filings in {days}d", None, now)
    return {"risk_rows": n, "by_risk": {k: len(v) for k, v in counts.items()}}


def upsert_risk(conn: Connection, cik: str, risk: str, severity: str, value: str | None, url: str | None, at: datetime) -> None:
    st = pg_insert(discovery_risks).values(cik=cik, risk=risk, severity=severity, value=value, source_url=url, detected_at=at)
    conn.execute(st.on_conflict_do_update(index_elements=["cik", "risk"],
                                          set_={"severity": severity, "value": value, "source_url": url, "detected_at": at}))


def derived_risks(conn: Connection, cik: str, ticker: str, *, metrics: dict, price: float | None, market_cap: float | None,
                  adv: float | None) -> None:
    """Risks computed from numbers (replaced on every scan)."""
    now = datetime.now(timezone.utc)
    derived = {"DILUTION", "CASH_RUNWAY", "NEGATIVE_WORKING_CAPITAL", "HIGH_SBC", "LOW_LIQUIDITY", "NASDAQ_MIN_BID_RISK",
               "INSIDER_SELLING", "PROMOTIONAL_PR", "EXTREME_VALUATION"}
    conn.execute(delete(discovery_risks).where(and_(discovery_risks.c.cik == cik, discovery_risks.c.risk.in_(derived))))
    g = metrics.get("share_growth_yoy")
    if g is not None and g >= 0.2:
        upsert_risk(conn, cik, "DILUTION", "HIGH" if g >= 0.5 else "MEDIUM", f"shares outstanding +{g:.0%} YoY", None, now)
    rw = metrics.get("runway_months")
    if rw is not None and rw < 18:
        upsert_risk(conn, cik, "CASH_RUNWAY", "HIGH" if rw < 9 else "MEDIUM",
                    f"~{rw:.0f} months (cash ${(metrics.get('cash') or 0) / 1e6:.0f}M / OCF burn)", None, now)
    wc = metrics.get("working_capital")
    if wc is not None and wc < 0:
        upsert_risk(conn, cik, "NEGATIVE_WORKING_CAPITAL", "MEDIUM", f"${wc / 1e6:.1f}M", None, now)
    sbc = metrics.get("sbc_to_revenue")
    if sbc is not None and sbc >= 0.3:
        upsert_risk(conn, cik, "HIGH_SBC", "MEDIUM", f"SBC = {sbc:.0%} of revenue", None, now)
    if adv is not None and adv < 5e6:
        upsert_risk(conn, cik, "LOW_LIQUIDITY", "HIGH" if adv < 1e6 else "MEDIUM", f"avg daily $ volume ${adv / 1e6:.2f}M", None, now)
    if price is not None and price < 1.0:
        upsert_risk(conn, cik, "NASDAQ_MIN_BID_RISK", "HIGH", f"price ${price:.2f} < $1.00", None, now)
    rev = metrics.get("ttm_revenue")
    if market_cap and rev and rev > 0 and market_cap / rev > 50:
        upsert_risk(conn, cik, "EXTREME_VALUATION", "MEDIUM", f"market cap / TTM revenue = {market_cap / rev:.0f}x", None, now)
    since = date.today() - timedelta(days=90)
    sold = conn.execute(select(func.coalesce(func.sum(insider_transactions.c.value), 0)).where(and_(
        insider_transactions.c.issuer_cik == cik, insider_transactions.c.category == "OPEN_MARKET_SALE",
        insider_transactions.c.is_10b5_1.is_(False), insider_transactions.c.filed_date >= since))).scalar()
    if sold and market_cap and float(sold) >= max(1e6, 0.01 * market_cap):
        upsert_risk(conn, cik, "INSIDER_SELLING", "MEDIUM", f"${float(sold) / 1e6:.1f}M discretionary insider sales in 90d", None, now)
    prs = conn.execute(select(func.count()).select_from(news_items).where(and_(
        news_items.c.ciks.contains([cik]), news_items.c.source_tier == 1, news_items.c.source != "sec_8k",
        news_items.c.first_seen_at >= now - timedelta(days=30)))).scalar()
    if prs and prs >= 12:
        upsert_risk(conn, cik, "PROMOTIONAL_PR", "MEDIUM", f"{prs} press releases in 30 days", None, now)


def risks_for(conn: Connection, cik: str) -> list[dict]:
    return [dict(r) for r in conn.execute(select(discovery_risks).where(discovery_risks.c.cik == cik)).mappings().all()]
