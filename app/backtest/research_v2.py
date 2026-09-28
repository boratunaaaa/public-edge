"""V2 final-validation research (pre-registered, no threshold optimisation). Read-only analyses:

* matched_placebo  - same-issuer control whose PRIOR N-day return (N = 20 / 60 / 90) is closest to the event's
                     (mean-reversion matching); controls excluded within 90 days of any event of the same family
                     (contamination='family') or of ANY event type (contamination='any'). Company-equal paired
                     difference in SPY-beat, sign test.
* breakdown        - SPY-beat by pre-declared buckets (role, value, ownership increase, repeat buyer, cluster size).
* tenk_negative    - NEGATIVE 10-K changes: forward drawdown, delisting / bankruptcy, dilution filings, going concern.
Buckets are fixed before looking at results; nothing here selects a threshold."""
from __future__ import annotations

import bisect
import math
import statistics
from collections import defaultdict
from datetime import date, timedelta

from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.marketdata.provider import load_bars

H = 90
EXCL = 90
TOL = 0.05  # |prior-return difference| accepted for a match


def _series(conn, sym: str) -> tuple[list[date], list[float]]:
    bars = [b for b in load_bars(conn, sym, date(2015, 1, 1), date.today()) if b.close and b.close > 0]
    return [b.date for b in bars], [(b.adj_close or b.close) for b in bars]


def _ret(ds, px, d: date, back: int = 0, fwd: int = 0) -> float | None:
    """Return from the first session >= d-back to the first session >= d+fwd (calendar days)."""
    i0 = bisect.bisect_left(ds, d - timedelta(days=back))
    i1 = bisect.bisect_left(ds, d + timedelta(days=fwd))
    if i0 >= len(ds) or i1 >= len(ds) or i1 <= i0 or (ds[i0] - (d - timedelta(days=back))).days > 7:
        return None
    return px[i1] / px[i0] - 1


def sign_p(diffs: list[float]) -> float | None:
    pos, neg = sum(d > 0 for d in diffs), sum(d < 0 for d in diffs)
    n = pos + neg
    return math.erfc(abs(pos - n / 2) / math.sqrt(n / 4) / math.sqrt(2)) if n else None


def _events(conn, event_type: str, where: str = "TRUE") -> list[dict]:
    return [dict(r) for r in conn.execute(text(f"""
        SELECT DISTINCT ON (e.issuer_cik, date_trunc('quarter', e.public_date)) e.id, e.issuer_cik, e.public_date, e.price_symbol, e.payload
        FROM events e JOIN event_returns r ON r.event_id = e.id AND r.horizon_days = :h
        WHERE e.event_type = :et AND NOT e.superseded AND NOT e.needs_review AND e.issuer_cik IS NOT NULL
          AND e.price_symbol IS NOT NULL AND r.excess_ret IS NOT NULL AND ({where})
        ORDER BY e.issuer_cik, date_trunc('quarter', e.public_date), e.public_date"""), {"h": H, "et": event_type}).mappings()]


def _exclusion(conn, event_type: str, contamination: str) -> dict[str, list[date]]:
    cond = "event_type = :et" if contamination == "family" else "TRUE"
    out = defaultdict(list)
    for cik, d in conn.execute(text(f"SELECT issuer_cik, public_date FROM events WHERE {cond} AND NOT superseded AND issuer_cik IS NOT NULL"),
                               {"et": event_type}):
        out[cik].append(d)
    for v in out.values():
        v.sort()
    return out


def matched_placebo(conn: Connection, event_type: str, *, lookback: int = 60, contamination: str = "family",
                    where: str = "TRUE", limit_issuers: int | None = None) -> dict:
    evs = _events(conn, event_type, where)
    excl = _exclusion(conn, event_type, contamination)
    spy_d, spy_p = _series(conn, "SPY")
    by_issuer = defaultdict(list)
    for e in evs:
        by_issuer[e["issuer_cik"]].append(e)
    per = defaultdict(lambda: [[], []])
    skipped = 0
    for k, (cik, items) in enumerate(by_issuer.items()):
        if limit_issuers and k >= limit_issuers:
            break
        ds, px = _series(conn, items[0]["price_symbol"])
        if len(ds) < 300:
            skipped += len(items)
            continue
        blocked = excl.get(cik, [])
        for e in items:
            d0 = e["public_date"]
            pe = _ret(ds, px, d0, back=lookback)
            fe, se = _ret(ds, px, d0, fwd=H), _ret(spy_d, spy_p, d0, fwd=H)
            if pe is None or fe is None or se is None:
                skipped += 1
                continue
            best, bd = None, None
            for j in range(0, len(ds), 5):  # weekly grid of candidate control dates
                c = ds[j]
                if abs((c - d0).days) < 180 or c + timedelta(days=H + 7) > ds[-1]:
                    continue
                i = bisect.bisect_left(blocked, c - timedelta(days=EXCL))
                if i < len(blocked) and blocked[i] <= c + timedelta(days=EXCL + H):
                    continue  # an own event near / inside the control window
                pc = _ret(ds, px, c, back=lookback)
                if pc is None:
                    continue
                dist = abs(pc - pe)
                if dist <= TOL and (bd is None or dist < bd):
                    best, bd = c, dist
            if best is None:
                skipped += 1
                continue
            fc, sc = _ret(ds, px, best, fwd=H), _ret(spy_d, spy_p, best, fwd=H)
            if fc is None or sc is None:
                skipped += 1
                continue
            per[cik][0].append(1.0 if fe - se > 0 else 0.0)
            per[cik][1].append(1.0 if fc - sc > 0 else 0.0)
    if not per:
        return {"event_type": event_type, "lookback": lookback, "contamination": contamination, "companies": 0, "skipped": skipped}
    ev = [sum(a) / len(a) for a, _ in per.values()]
    ct = [sum(b) / len(b) for _, b in per.values()]
    diffs = [x - y for x, y in zip(ev, ct)]
    return {"event_type": event_type, "where": where, "lookback": lookback, "contamination": contamination, "companies": len(per),
            "skipped": skipped, "event_rate": statistics.fmean(ev), "control_rate": statistics.fmean(ct),
            "difference": statistics.fmean(diffs), "sign_test_p": sign_p(diffs)}


INSIDER_BUCKETS = {
    "role_ceo": "(payload->>'is_ceo')::boolean",
    "role_cfo": "(payload->>'is_cfo')::boolean AND NOT (payload->>'is_ceo')::boolean",
    "role_director_only": "(payload->>'is_director')::boolean AND NOT (payload->>'is_ceo')::boolean AND NOT (payload->>'is_cfo')::boolean",
    "role_10pct_only": "(payload->>'is_ten_pct')::boolean AND NOT (payload->>'is_director')::boolean AND NOT (payload->>'is_ceo')::boolean AND NOT (payload->>'is_cfo')::boolean",
    "role_other_officer": "payload->>'officer_title' IS NOT NULL AND NOT (payload->>'is_ceo')::boolean AND NOT (payload->>'is_cfo')::boolean",
    "value_lt_100k": "(payload->>'value')::float < 100000",
    "value_100k_1m": "(payload->>'value')::float >= 100000 AND (payload->>'value')::float < 1000000",
    "value_ge_1m": "(payload->>'value')::float >= 1000000",
    "incr_lt_10pct": "(payload->>'holdings_increase')::float < 0.10",
    "incr_10_50pct": "(payload->>'holdings_increase')::float >= 0.10 AND (payload->>'holdings_increase')::float < 0.50",
    "incr_ge_50pct": "(payload->>'holdings_increase')::float >= 0.50",
}
DISCRETIONARY = "NOT COALESCE((payload->>'is_10b5_1')::boolean, false)"


def spy_beat(conn: Connection, event_type: str, where: str = "TRUE") -> dict:
    rows = conn.execute(text(f"""
        SELECT e.issuer_cik, r.excess_ret FROM events e JOIN event_returns r ON r.event_id = e.id AND r.horizon_days = :h
        WHERE e.event_type = :et AND NOT e.superseded AND NOT e.needs_review AND r.excess_ret IS NOT NULL AND ({where})"""),
                        {"h": H, "et": event_type}).all()
    per = defaultdict(list)
    for cik, x in rows:
        per[cik].append(1.0 if x > 0 else 0.0)
    comp = [sum(v) / len(v) for v in per.values()]
    return {"events": len(rows), "companies": len(per), "event_weighted": statistics.fmean([1.0 if x > 0 else 0.0 for _, x in rows]) if rows else None,
            "company_equal": statistics.fmean(comp) if comp else None,
            "median_excess": statistics.median([x for _, x in rows]) if rows else None}


def insider_breakdown(conn: Connection, lookback: int = 60) -> dict:
    out = {}
    for name, cond in INSIDER_BUCKETS.items():
        w = f"({cond}) AND {DISCRETIONARY}"
        out[name] = {"spy": spy_beat(conn, "INSIDER_OPEN_MARKET_BUY", w),
                     "placebo": matched_placebo(conn, "INSIDER_OPEN_MARKET_BUY", lookback=lookback, where=w)}
    return out


def tenk_negative(conn: Connection) -> dict:
    out = {}
    for label, cond in (("NEGATIVE", "payload->>'direction' = 'NEGATIVE'"), ("POSITIVE", "payload->>'direction' = 'POSITIVE'"),
                        ("NEW_GOING_CONCERN", "payload->'change_types' ? 'NEW_GOING_CONCERN'"),
                        ("LIQUIDITY_DETERIORATION", "payload->'change_types' ? 'LIQUIDITY_DETERIORATION'")):
        base = spy_beat(conn, "TENK_CHANGE", cond)
        r = conn.execute(text(f"""
            WITH ev AS (SELECT DISTINCT ON (e.issuer_cik, e.public_date) e.id, e.issuer_cik, e.public_date FROM events e
                        WHERE e.event_type = 'TENK_CHANGE' AND NOT e.superseded AND NOT e.needs_review AND e.issuer_cik IS NOT NULL
                          AND e.public_date <= CURRENT_DATE - 190 AND ({cond}))
            SELECT count(*) n,
              percentile_cont(0.5) WITHIN GROUP (ORDER BY r.mae) median_mae_180,
              avg((sm.delisting_date BETWEEN ev.public_date AND ev.public_date + 180)::int) delist_180,
              avg((sm.delisting_reason = 'BANKRUPTCY' AND sm.delisting_date BETWEEN ev.public_date AND ev.public_date + 365)::int) bankrupt_365,
              avg((EXISTS (SELECT 1 FROM corporate_actions ca WHERE ca.cik = ev.issuer_cik AND ca.action IN
                   ('PROSPECTUS_424B','SHELF_REGISTRATION','UNREGISTERED_SALE','REGISTRATION_S1')
                   AND ca.action_date BETWEEN ev.public_date AND ev.public_date + 180))::int) dilution_filing_180,
              avg(r.sector_excess) mean_sector_excess_180
            FROM ev LEFT JOIN event_returns r ON r.event_id = ev.id AND r.horizon_days = 180
            LEFT JOIN security_master sm ON sm.cik = ev.issuer_cik""")).mappings().one()
        out[label] = {"spy_90d": base, **{k: (float(v) if v is not None else None) for k, v in r.items()}}
    out["placebo_negative"] = matched_placebo(conn, "TENK_CHANGE", where="payload->>'direction' = 'NEGATIVE'")
    return out


def _surname(name: str) -> str:
    toks = [t for t in (name or "").replace(",", " ").upper().split() if len(t) > 1 and t not in ("JR", "SR", "II", "III", "IV", "MD")]
    return toks[-1] if toks else ""


def cluster_actor_audit(conn: Connection) -> dict:
    """Clusters are already deduped on the beneficial-owner graph (joint filers, trusts, LLCs). Stricter sensitivity:
    individuals sharing a surname count as ONE family actor. Reports how many clusters survive and their results."""
    rows = conn.execute(text("""SELECT id, payload FROM events WHERE event_type = 'CLUSTER_INSIDER_BUY' AND NOT superseded
                                AND NOT needs_review""")).all()
    strict, dropped, inflated = [], 0, 0
    for eid, p in rows:
        buyers = p.get("buyers") or []
        if (p.get("reporting_owners_in_window") or 0) > (p.get("independent_beneficial_owners") or 0):
            inflated += 1
        actors = {_surname(b.get("name")) or f"#{i}" for i, b in enumerate(buyers)}
        if len(actors) >= 2:
            strict.append(eid)
        else:
            dropped += 1
    ids = "ARRAY[" + ",".join(map(str, strict)) + "]::bigint[]" if strict else "ARRAY[]::bigint[]"
    w = f"e.id = ANY({ids})"
    return {"clusters": len(rows), "graph_dedupe_reduced": inflated, "family_surname_dropped": dropped,
            "strict_clusters": len(strict), "strict_spy": spy_beat(conn, "CLUSTER_INSIDER_BUY", w),
            "strict_placebo_60": matched_placebo(conn, "CLUSTER_INSIDER_BUY", lookback=60, where=w),
            "all_spy": spy_beat(conn, "CLUSTER_INSIDER_BUY")}
