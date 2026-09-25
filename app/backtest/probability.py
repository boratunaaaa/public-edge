"""Historical success rate ("tarihsel başarı oranı") for an event.

Not a promise and not a model guess: it is the measured share of comparable PAST episodes whose
stock beat the S&P 500 (SPY) over the horizon, taken from this system's own no-look-ahead
backtest. To stay honest:
  * the most specific peer group with enough independent issuers is used (hierarchical fallback);
  * events are clustered into episodes and weighted company-equal (app.backtest.quality);
  * missing / delisted samples are never dropped silently; the quality gate withholds the number
    unless it is TRUSTED;
  * small samples are shrunk toward 50% (Beta(5,5) prior) so 3/3 wins never shows as 100%.
"""
from __future__ import annotations

from dataclasses import dataclass
import math

from sqlalchemy.engine import Connection

from app.backtest import quality as Q

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
    affected_samples: int = 0  # unresolved mature episodes (missing price / benchmark / not yet computed)
    blocked_reason: str | None = None  # short Turkish reason when the percentage is withheld
    status: str = "BLOCKED"  # metrics_quality_status: TRUSTED | PARTIAL | BLOCKED
    episodes: int = 0
    unique_periods: int = 0
    event_level_rate: float | None = None
    sector_equal_rate: float | None = None
    worst_case: float | None = None
    best_case: float | None = None
    sector_benchmark_rate: float | None = None  # shown only when TRUSTED and sector coverage >= 90%

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


def success_rate(conn: Connection, event_type: str, payload: dict, horizon: int = DEFAULT_HORIZON,
                 exclude_event_id: int | None = None, persist: bool = False) -> SuccessRate:
    """Most specific peer group whose sample is large enough; its verdict comes from the data-quality
    gate (app.backtest.quality). A percentage is filled in ONLY when the gate says TRUSTED.

    exclude_event_id is kept for callers; the gated sample only contains MATURE events, so a new
    event is never part of its own statistic anyway."""
    chosen = None
    for label, cond, params in peer_groups(event_type, payload):
        st = Q.compute(conn, event_type, label, cond, params, horizon, persist=persist)
        if st["company_n"] >= MIN_SAMPLE or chosen is None:
            chosen = (label, st)
        if st["company_n"] >= MIN_SAMPLE:
            break  # the most specific group with enough independent issuers
    label, st = chosen
    trusted = st["status"] == "TRUSTED"
    p_hat = st["company_equal_rate"]
    lo = hi = None
    if trusted and p_hat is not None:
        lo, hi = wilson_interval(p_hat * st["company_n"], st["company_n"])
    return SuccessRate(
        probability=shrink(p_hat * st["company_n"], st["company_n"]) if (trusted and p_hat is not None) else None,
        raw_rate=p_hat if trusted else None, n=st["unique_events"], horizon_days=horizon, group=label,
        median_excess=st["median_excess"] if trusted else None, median_return=st["median_return"] if trusted else None,
        typical_drawdown=st["median_mae"] if trusted else None, best_horizon=None,
        company_n=st["company_n"], ci_low=lo, ci_high=hi,
        affected_samples=st["unresolved_episodes"], blocked_reason=None if trusted else Q.reason_text_tr(st),
        status=st["status"], episodes=st["unique_episodes"], unique_periods=st["unique_periods"],
        event_level_rate=st["event_level_rate"] if trusted else None,
        sector_equal_rate=st["sector_equal_rate"] if trusted else None,
        worst_case=st["worst_case"], best_case=st["best_case"],
        sector_benchmark_rate=st.get("sector_benchmark_rate") if (trusted and (st.get("sector_coverage") or 0) >= 0.9) else None)
