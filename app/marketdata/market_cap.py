"""Market capitalisation: ONE rule for live sizing / materiality and for the point-in-time replay.

Root cause of the inflated ADR values (audit 2026-10-01): every path multiplied the listed security's price by
dei:EntityCommonStockSharesOutstanding. For a depositary (ADR) programme the price is the depositary share's while the
cover-page count is the ORDINARY shares, and one ADS represents N of them (TSM 5, LTM 2,000), so the product was N times
the real value - LTM 30.1T $, TSM 11.6T $, BSAC 6.5T $, BCH 4.2T $, AKTX 0.9T $ in universe_snapshots. Not a currency or
unit problem: the share count is right, it counts a different security. The ratio is in no SEC dataset we hold, so a
company with a depositary programme public at the moment gets an UNKNOWN market cap - never a wrong one (the gate and
the sizing already treat unknown as the risky case). A company without one (every domestic issuer, ASML's New York
registry shares, Canadian 40-F direct listings) is computed exactly as before.

Detection: a depositary programme is registered on Form F-6 (F-6EF, F-6 POS, F-6/A) under the issuer's CIK: present
for all 12 ADRs checked (TSM, LTM, BSAC, BCH, TM, HSBC, AMX, AKTX, EC, BABA, SAP, NVO), absent for ASML, NVDA, SHOP."""
from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import text
from sqlalchemy.engine import Connection

DEPOSITARY_ACTION = "DEPOSITARY_PROGRAM"  # corporate_actions row of an F-6 filing (app.securitymaster.build)
DEPOSITARY_RATIO_UNKNOWN = "DEPOSITARY_RATIO_UNKNOWN"


def market_cap(price: float | None, shares: float | None, depositary_since: datetime | None,
               at: datetime) -> tuple[float | None, str | None]:
    """(market cap, why it is unknown). `depositary_since` = first public moment of the issuer's depositary programme
    (None = none known); `at` = the decision moment (a programme registered later does not reach back)."""
    if at.tzinfo is None:
        raise ValueError("a market-cap moment must be timezone-aware")
    if price is None or shares is None:
        return None, "NO_PRICE_OR_SHARES"
    if depositary_since is not None and depositary_since <= at:
        return None, DEPOSITARY_RATIO_UNKNOWN
    return float(price) * float(shares), None


def depositary_since(conn: Connection, ciks: list[str]) -> dict[str, datetime]:
    """Live: the full SEC filing history (sec_filings, app.pit.readers) plus the F-6 rows the live submissions refresh
    records in corporate_actions after that backfill (a date-only row counts from the end of its filing day)."""
    from app.pit.readers import depositary_since as from_history
    from app.pit.sources import filing_day_end
    if not ciks:
        return {}
    out = dict(from_history(conn, ciks))
    for cik, d in conn.execute(text("""SELECT cik, min(action_date) FROM corporate_actions WHERE cik = ANY(:c)
                                       AND action = :a GROUP BY cik ORDER BY cik"""), {"c": ciks, "a": DEPOSITARY_ACTION}):
        t = filing_day_end(d)
        out[cik] = min(out.get(cik, t), t)
    return out


def live_moment(on: date | None = None) -> datetime:
    """The decision moment of a live computation for day `on` (end of that day: everything public by then counts)."""
    from app.pit.sources import filing_day_end
    return filing_day_end(on) if on else datetime.now(timezone.utc)
