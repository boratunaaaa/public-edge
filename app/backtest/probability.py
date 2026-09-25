"""Historical success rate ("tarihsel başarı oranı") for an event.

Not a promise and not a model guess: it is the measured share of comparable PAST events whose
stock beat the S&P 500 (SPY) over the horizon, taken from this system's own no-look-ahead
backtest. To stay honest:
  * the most specific peer group with enough samples is used (hierarchical fallback);
  * small samples are shrunk toward 50% (Beta(5,5) prior) so 3/3 wins never shows as 100%;
  * with fewer than MIN_SAMPLE comparable events no percentage is shown at all.
"""
from __future__ import annotations

from dataclasses import dataclass
import math

from sqlalchemy import text
from sqlalchemy.engine import Connection

MIN_SAMPLE = 30
PRIOR = 5.0  # pseudo-wins and pseudo-losses (pulls toward 50%)
DEFAULT_HORIZON = 90


@dataclass
class SuccessRate:
    probability: float | None  # shrunk probability of beating SPY over the horizon
    raw_rate: float | None
    n: int
    horizon_days: int
    group: str  # human-readable peer group used
    median_excess: float | None
    median_return: float | None
    typical_drawdown: float | None  # median worst dip (MAE) inside the window
    best_horizon: int | None  # horizon with the highest median excess among 30/90/180
    company_n: int = 0  # independent-ish issuer clusters (events from one issuer share one weight)
    ci_low: float | None = None
    ci_high: float | None = None
    affected_samples: int = 0  # mature events omitted because price/benchmark data is unavailable
    blocked_reason: str | None = None

    @property
    def available(self) -> bool:
        return self.probability is not None


def shrink(wins: float, n: int, prior: float = PRIOR) -> float:
    return (wins + prior) / (n + 2 * prior)


def wilson_interval(wins: float, n: int, z: float = 1.96) -> tuple[float | None, float | None]:
    """Approximate 95% interval using issuer clusters as the effective sample size.

    ``wins`` may be fractional because every issuer contributes its mean outcome, rather than
    letting a prolific filer contribute dozens of nominally independent observations.
    """
    if n <= 0:
        return None, None
    p = wins / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n) / den
    return max(0.0, centre - half), min(1.0, centre + half)


# peer groups from most specific to broadest: (label, SQL condition, needs)
def peer_groups(event_type: str, payload: dict) -> list[tuple[str, str, dict]]:
    g: list[tuple[str, str, dict]] = []
    et = {"et": event_type}
    if event_type == "INSIDER_OPEN_MARKET_BUY":
        role = "ceo" if payload.get("is_ceo") else "cfo" if payload.get("is_cfo") else "director" if payload.get("is_director") else None
        opp = payload.get("insider_type") == "opportunistic"
        big = (payload.get("value") or 0) >= 500_000
        if role and opp:
            g.append((f"{role}+fırsatçı", f"(e.payload->>'is_{role}')::boolean AND e.payload->>'insider_type' = 'opportunistic'", et))
        if role and big:
            g.append((f"{role}+500k$ üstü", f"(e.payload->>'is_{role}')::boolean AND (e.payload->>'value')::float >= 500000", et))
        if role:
            g.append((role, f"(e.payload->>'is_{role}')::boolean", et))
        if opp:
            g.append(("fırsatçı alım", "e.payload->>'insider_type' = 'opportunistic'", et))
    elif event_type == "CLUSTER_INSIDER_BUY":
        if (payload.get("number_of_buyers") or 0) >= 3:
            g.append(("3+ alıcı", "(e.payload->>'number_of_buyers')::int >= 3", et))
        if payload.get("CEO_present"):
            g.append(("CEO dahil", "(e.payload->>'CEO_present')::boolean", et))
    elif event_type == "MATERIAL_NEWS":
        b = payload.get("mcap_bucket")
        for cls in [c for c in (payload.get("classes") or []) if c != "OTHER"][:2]:
            if b:
                g.append((f"{cls}+{b}", "e.payload->'classes' ? :cls AND e.payload->>'mcap_bucket' = :b", {**et, "cls": cls, "b": b}))
            g.append((cls, "e.payload->'classes' ? :cls", {**et, "cls": cls}))
    elif event_type == "ACTIVIST_13D":
        if payload.get("item4_class") not in (None, "PASSIVE_OR_UNCLEAR", "MANAGEMENT_DISCUSSION"):
            g.append(("aktivist amaçlı 13D", "e.payload->>'item4_class' NOT IN ('PASSIVE_OR_UNCLEAR','MANAGEMENT_DISCUSSION')", et))
    elif event_type == "GOV_CONTRACT":
        if (payload.get("value_to_mcap") or 0) >= 0.05:
            g.append(("şirket değerinin %5+'ı kadar kontrat", "(e.payload->>'value_to_mcap')::float >= 0.05", et))
    g.append(("tüm benzer olaylar", "TRUE", et))
    return g


def _query(conn: Connection, cond: str, params: dict, horizon: int) -> dict:
    row = conn.execute(text(f"""
        WITH sample AS (
          SELECT r.*, COALESCE(e.issuer_cik, e.ticker, 'EVENT:' || e.id::text) issuer_key
          FROM event_returns r JOIN events e ON e.id = r.event_id
          WHERE e.event_type = :et AND r.horizon_days = :h AND r.excess_ret IS NOT NULL
            AND e.date_quality <> 'estimated'
            AND COALESCE(e.payload->>'universe_quality', '') <> 'current_universe_survivorship_biased'
            AND {cond}
        ), issuer_results AS (
          SELECT issuer_key, avg((excess_ret > 0)::int)::float8 issuer_hit
          FROM sample GROUP BY issuer_key
        )
        SELECT (SELECT count(*) FROM sample) n,
               (SELECT count(*) FROM issuer_results) company_n,
               (SELECT coalesce(sum(issuer_hit), 0) FROM issuer_results) weighted_wins,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY r.excess_ret) med_ex,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY r.ret) med_ret,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY r.mae) med_mae
        FROM sample r"""), {**params, "h": horizon}).mappings().first()
    return dict(row)


def _affected(conn: Connection, cond: str, params: dict, horizon: int) -> int:
    """Mature comparable events silently absent from the SPY-relative sample.

    Any such omission can be survivorship/data-availability bias.  The displayed hit rate is
    therefore disabled instead of assuming the missing securities were neutral.
    """
    row = conn.execute(text(f"""
        SELECT count(*)
        FROM events e
        LEFT JOIN event_returns r ON r.event_id=e.id AND r.horizon_days=:h
        WHERE e.event_type=:et AND e.date_quality <> 'estimated'
          AND COALESCE(e.payload->>'universe_quality', '') <> 'current_universe_survivorship_biased' AND {cond}
          AND e.public_date <= CURRENT_DATE - (:h * INTERVAL '1 day')
          AND (e.returns_status = 'no_data' OR (r.event_id IS NOT NULL AND r.excess_ret IS NULL))
    """), {**params, "h": horizon}).scalar()
    return int(row or 0)


def success_rate(conn: Connection, event_type: str, payload: dict, horizon: int = DEFAULT_HORIZON,
                 exclude_event_id: int | None = None) -> SuccessRate:
    for label, cond, params in peer_groups(event_type, payload):
        if event_type == "ACTIVIST_13D":
            # Historical low-information amendments were emitted as fresh events.  Do not let
            # them multiply the apparent evidence until the stored history can be rebuilt.
            cond = (f"({cond}) AND (COALESCE((e.payload->>'is_amendment')::boolean, false) = false "
                    "OR abs(COALESCE((e.payload->>'change')::float, 0)) >= 1)")
        if exclude_event_id is not None:
            cond = f"({cond}) AND e.id <> :xid"
            params = {**params, "xid": exclude_event_id}
        r = _query(conn, cond, params, horizon)
        affected = _affected(conn, cond, params, horizon)
        # MIN_SAMPLE applies to issuer clusters, not correlated event rows.
        if r["company_n"] >= MIN_SAMPLE and affected == 0:
            best, best_val = None, None
            for h in (30, 90, 180):
                x = _query(conn, cond, params, h)
                if x["n"] >= MIN_SAMPLE and x["med_ex"] is not None and (best_val is None or x["med_ex"] > best_val):
                    best, best_val = h, x["med_ex"]
            lo, hi = wilson_interval(r["weighted_wins"], r["company_n"])
            return SuccessRate(shrink(r["weighted_wins"], r["company_n"]),
                               r["weighted_wins"] / r["company_n"], r["n"], horizon, label,
                               r["med_ex"], r["med_ret"], r["med_mae"], best,
                               company_n=r["company_n"], ci_low=lo, ci_high=hi)
        if r["company_n"] >= MIN_SAMPLE and affected:
            return SuccessRate(None, None, r["n"], horizon, label, r["med_ex"], r["med_ret"],
                               r["med_mae"], None, company_n=r["company_n"], affected_samples=affected,
                               blocked_reason="missing_price_or_benchmark_data")
    total = _query(conn, "TRUE", {"et": event_type}, horizon)
    affected = _affected(conn, "TRUE", {"et": event_type}, horizon)
    return SuccessRate(None, None, int(total["n"] or 0), horizon, "yetersiz veri", None, None, None, None,
                       company_n=int(total["company_n"] or 0), affected_samples=affected,
                       blocked_reason="missing_price_or_benchmark_data" if affected else None)
