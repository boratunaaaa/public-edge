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

    @property
    def available(self) -> bool:
        return self.probability is not None


def shrink(wins: int, n: int, prior: float = PRIOR) -> float:
    return (wins + prior) / (n + 2 * prior)


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
        SELECT count(*) n, count(*) FILTER (WHERE r.excess_ret > 0) wins,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY r.excess_ret) med_ex,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY r.ret) med_ret,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY r.mae) med_mae
        FROM event_returns r JOIN events e ON e.id = r.event_id
        WHERE e.event_type = :et AND r.horizon_days = :h AND r.excess_ret IS NOT NULL
          AND e.date_quality <> 'estimated' AND {cond}"""), {**params, "h": horizon}).mappings().first()
    return dict(row)


def success_rate(conn: Connection, event_type: str, payload: dict, horizon: int = DEFAULT_HORIZON,
                 exclude_event_id: int | None = None) -> SuccessRate:
    for label, cond, params in peer_groups(event_type, payload):
        if exclude_event_id is not None:
            cond = f"({cond}) AND e.id <> :xid"
            params = {**params, "xid": exclude_event_id}
        r = _query(conn, cond, params, horizon)
        if r["n"] >= MIN_SAMPLE:
            best, best_val = None, None
            for h in (30, 90, 180):
                x = _query(conn, cond, params, h)
                if x["n"] >= MIN_SAMPLE and x["med_ex"] is not None and (best_val is None or x["med_ex"] > best_val):
                    best, best_val = h, x["med_ex"]
            return SuccessRate(shrink(r["wins"], r["n"]), r["wins"] / r["n"], r["n"], horizon, label,
                               r["med_ex"], r["med_ret"], r["med_mae"], best)
    total = _query(conn, "TRUE", {"et": event_type}, horizon)
    return SuccessRate(None, None, int(total["n"] or 0), horizon, "yetersiz veri", None, None, None, None)
