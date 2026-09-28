"""Matched placebo control: is a family's SPY-beat rate different from what the SAME companies did anyway?

Company-equal SPY-beat rates below 50 % across almost every family point to a universe effect (small / micro caps vs
the cap-weighted S&P 500), not to the signal. For every resolved episode the control is the same issuer, same price
series, entered on the first session >= (event date - 182 days) - a date with no event of this family within 90 days.
Only stored prices of the event's own verified price symbol are used (no new fetch, no synthetic prices). The
statistic is the paired difference: family rate - control rate, company-equal, with a sign test over companies.
Caveat (stated in every report): controls are conditioned on the issuer still trading at the later event date."""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import date, timedelta

from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.backtest.engine import compute_returns
from app.marketdata.provider import load_bars

OFFSET_DAYS = 182


def _excess(conn, sym: str, spy, d: date, horizon: int) -> float | None:
    bars = load_bars(conn, sym, d - timedelta(days=10), d + timedelta(days=horizon + 15))
    res = compute_returns(bars, spy, d, None, horizons=(horizon,))
    return res[0].excess_ret if res else None


def family_vs_placebo(conn: Connection, event_type: str, horizon: int = 90, limit: int | None = None) -> dict:
    rows = conn.execute(text(f"""
        SELECT DISTINCT ON (e.issuer_cik, date_trunc('quarter', e.public_date)) e.id, e.issuer_cik, e.public_date,
               e.price_symbol, r.excess_ret
        FROM events e JOIN event_returns r ON r.event_id = e.id AND r.horizon_days = :h
        WHERE e.event_type = :et AND NOT e.superseded AND e.issuer_cik IS NOT NULL AND e.price_symbol IS NOT NULL
          AND r.excess_ret IS NOT NULL
        ORDER BY e.issuer_cik, date_trunc('quarter', e.public_date), e.public_date
        {'LIMIT :lim' if limit else ''}"""), {"h": horizon, "et": event_type, "lim": limit}).mappings().all()
    spy = load_bars(conn, "SPY", date(2000, 1, 1), date.today())
    own_dates = defaultdict(list)
    for r in rows:
        own_dates[r["issuer_cik"]].append(r["public_date"])
    per_co = defaultdict(lambda: [[], []])  # cik -> ([event wins], [control wins])
    skipped = 0
    for r in rows:
        cd = r["public_date"] - timedelta(days=OFFSET_DAYS)
        if any(abs((cd - d).days) < 90 for d in own_dates[r["issuer_cik"]]):
            skipped += 1
            continue
        cx = _excess(conn, r["price_symbol"], spy, cd, horizon)
        if cx is None:
            skipped += 1
            continue
        per_co[r["issuer_cik"]][0].append(1.0 if r["excess_ret"] > 0 else 0.0)
        per_co[r["issuer_cik"]][1].append(1.0 if cx > 0 else 0.0)
    if not per_co:
        return {"event_type": event_type, "horizon": horizon, "companies": 0, "skipped": skipped}
    ev = [sum(a) / len(a) for a, _ in per_co.values()]
    ct = [sum(b) / len(b) for _, b in per_co.values()]
    diffs = [x - y for x, y in zip(ev, ct)]
    pos, neg = sum(d > 0 for d in diffs), sum(d < 0 for d in diffs)
    n = pos + neg
    z = (pos - n / 2) / math.sqrt(n / 4) if n else 0.0
    return {"event_type": event_type, "horizon": horizon, "companies": len(per_co), "skipped": skipped,
            "event_rate": sum(ev) / len(ev), "placebo_rate": sum(ct) / len(ct), "difference": sum(diffs) / len(diffs),
            "sign_test_p": math.erfc(abs(z) / math.sqrt(2)) if n else None,
            "caveat": "controls conditioned on the issuer still trading at the later event date"}
