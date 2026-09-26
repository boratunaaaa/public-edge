"""Corporate-action EXIT resolution (not price recovery).

A position still open at a verified merger's closing is valued at the merger consideration:
  stock-for-stock  share_ratio x successor close on the first trading day AFTER the effective date
  cash             cash_component
  mixed            cash_component + share_ratio x successor close
Only validation_status = CONFIRMED and complexity = SIMPLE are used; elections, proration, collars, CVRs and
floating ratios are COMPLEX_CORPORATE_ACTION and never auto-resolved. The predecessor's own pre-merger prices are
never derived from the successor (A != ratio x B before the deal): a missing entry price stays
HISTORICAL_PRICE_STILL_MISSING.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import and_, select
from sqlalchemy.engine import Connection

from app.db.models import corporate_action_resolution as car

USABLE = ("CONFIRMED",)


@dataclass
class CorporateExit:
    effective_date: date
    exit_date: date  # date the value is observed (first successor session after the close; = effective date for cash)
    value: float  # per predecessor share, in the predecessor's currency
    basis: str  # merger_cash | merger_stock | merger_mixed (event_returns.exit_basis)
    resolution_id: int


def resolution_for(conn: Connection, cik: str | None) -> dict | None:
    if not cik:
        return None
    r = conn.execute(select(car).where(and_(car.c.predecessor_cik == cik.zfill(10), car.c.validation_status.in_(USABLE),
                                             car.c.complexity == "SIMPLE")).order_by(car.c.effective_date.desc()).limit(1)).mappings().first()
    return dict(r) if r else None


def exit_value(res: dict, successor_bars: list | None) -> CorporateExit | None:
    """Value per predecessor share; None when a stock leg cannot be priced (never guessed)."""
    cash = float(res["cash_component"] or 0)
    ratio = float(res["share_ratio"] or 0)
    eff = res["effective_date"]
    if ratio <= 0:
        if cash <= 0:
            return None
        return CorporateExit(eff, eff, cash, "merger_cash", res["id"])
    after = sorted((b for b in (successor_bars or []) if b.date > eff and b.close and b.close > 0), key=lambda b: b.date)
    if not after or (after[0].date - eff).days > 7:
        return None
    x = after[0]
    return CorporateExit(eff, x.date, cash + ratio * x.close, "merger_mixed" if cash > 0 else "merger_stock",
                         res["id"])


def successor_window(res: dict) -> tuple[date, date]:
    return res["effective_date"] - timedelta(days=5), res["effective_date"] + timedelta(days=15)
