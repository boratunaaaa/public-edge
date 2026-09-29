"""Insider person-level track record (point-in-time).

Only COMPLETED open-market purchases count: an INSIDER_OPEN_MARKET_BUY whose 90-day horizon (plus
the entry lag) had fully elapsed BEFORE the evaluation date. When a new trade is evaluated the
profile is recomputed as of that trade's public date, so no later outcome leaks into it.

Episodes: the same person buying the same company repeatedly within EPISODE_DAYS of the episode's
first buy is ONE episode (its outcome is the first buy's outcome) - ten top-ups are not ten wins.

Label (a description of history, NOT an investment score):
  INSUFFICIENT_HISTORY  < MIN_EPISODES resolved episodes, or > MAX_UNRESOLVED of them unresolved
  HISTORICALLY_STRONG   95% interval of the episode SPY-beat rate entirely above 50%
  HISTORICALLY_WEAK     95% interval entirely below 50%
  MIXED                 otherwise (also whenever the interval is wider than MAX_CI_WIDTH)
"""
from __future__ import annotations

import os
import re
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection, Engine

from app.backtest.probability import wilson_interval
from app.backtest.quality import ENTRY_LAG_DAYS, outcome_for

EPISODE_DAYS = 90
MIN_EPISODES = int(os.environ.get("INSIDER_PROFILE_MIN_EPISODES", "8") or 8)
MAX_UNRESOLVED = 0.10
MAX_CI_WIDTH = 0.45
HORIZON = 90


def person_id(owner_cik: str | None, owner_name: str | None) -> str | None:
    if owner_cik:
        return owner_cik
    n = re.sub(r"[^a-z ]", "", (owner_name or "").lower()).split()
    return ("N:" + " ".join(sorted(n)))[:64] if n else None


@dataclass
class Buy:
    person: str
    name: str
    issuer: str
    public_date: date
    outcome: str  # win | loss | unresolved (90d vs SPY)
    ex30: float | None = None
    ex90: float | None = None
    ex180: float | None = None
    sector_ex90: float | None = None


@dataclass
class Profile:
    person_id: str
    name: str
    issuer_count: int = 0
    completed_buy_count: int = 0
    episode_count: int = 0
    unique_company_count: int = 0
    median_30d_excess: float | None = None
    median_90d_excess: float | None = None
    median_180d_excess: float | None = None
    spy_beat_rate_90d: float | None = None
    sector_beat_rate_90d: float | None = None
    ci_low: float | None = None
    ci_high: float | None = None
    unresolved_share: float | None = None
    data_quality: str = "BLOCKED"
    label: str = "INSUFFICIENT_HISTORY"
    reasons: list = field(default_factory=list)


def episodes(buys: list[Buy], window: int = EPISODE_DAYS) -> list[Buy]:
    """First buy of each (person, issuer) episode."""
    by: dict[str, list[Buy]] = defaultdict(list)
    for b in buys:
        by[b.issuer].append(b)
    out = []
    for rs in by.values():
        rs.sort(key=lambda b: b.public_date)
        start = None
        for b in rs:
            if start is None or (b.public_date - start).days > window:
                start = b.public_date
                out.append(b)
    return out


def _med(xs: list[float | None]) -> float | None:
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def build_profile(pid: str, buys: list[Buy], min_episodes: int = MIN_EPISODES) -> Profile:
    p = Profile(pid, buys[0].name if buys else pid)
    p.completed_buy_count = len(buys)
    p.issuer_count = len({b.issuer for b in buys})
    eps = episodes(buys)
    p.episode_count = len(eps)
    resolved = [b for b in eps if b.outcome != "unresolved"]
    p.unique_company_count = len({b.issuer for b in resolved})
    p.unresolved_share = (1 - len(resolved) / len(eps)) if eps else None
    p.median_30d_excess = _med([b.ex30 for b in resolved])
    p.median_90d_excess = _med([b.ex90 for b in resolved])
    p.median_180d_excess = _med([b.ex180 for b in resolved])
    if resolved:
        wins = sum(b.outcome == "win" for b in resolved)
        p.spy_beat_rate_90d = wins / len(resolved)
        p.ci_low, p.ci_high = wilson_interval(wins, len(resolved))
    sec = [b for b in resolved if b.sector_ex90 is not None]
    p.sector_beat_rate_90d = (sum(b.sector_ex90 > 0 for b in sec) / len(sec)) if sec else None
    if len(resolved) < min_episodes:
        p.reasons.append(f"only {len(resolved)} completed episodes (< {min_episodes})")
        p.data_quality = "PARTIAL" if resolved else "BLOCKED"
        return p
    if (p.unresolved_share or 0) > MAX_UNRESOLVED:
        p.reasons.append(f"unresolved share {p.unresolved_share:.0%} > {MAX_UNRESOLVED:.0%}")
        p.data_quality = "PARTIAL"
        return p
    p.data_quality = "OK"
    if p.ci_high - p.ci_low > MAX_CI_WIDTH:
        p.label = "MIXED"
        p.reasons.append("confidence interval too wide for a strong/weak label")
    elif p.ci_low > 0.5:
        p.label = "HISTORICALLY_STRONG"
    elif p.ci_high < 0.5:
        p.label = "HISTORICALLY_WEAK"
    else:
        p.label = "MIXED"
    return p


SQL = """
SELECT e.id, e.issuer_cik, e.ticker, e.public_date, e.payload->>'owner_cik' owner_cik, e.payload->>'owner' owner,
       r30.excess_ret ex30, r90.excess_ret ex90, r180.excess_ret ex180, r90.sector_excess sx90, (r90.event_id IS NOT NULL) has90,
       e.missing_reason, sm.delisting_reason, sm.delisting_date
FROM events e
LEFT JOIN event_returns r30 ON r30.event_id = e.id AND r30.horizon_days = 30
LEFT JOIN event_returns r90 ON r90.event_id = e.id AND r90.horizon_days = 90
LEFT JOIN event_returns r180 ON r180.event_id = e.id AND r180.horizon_days = 180
                             AND e.public_date <= :mature180
LEFT JOIN security_master sm ON sm.cik = e.issuer_cik
WHERE e.event_type = 'INSIDER_OPEN_MARKET_BUY' AND NOT e.superseded AND NOT e.needs_review AND e.date_quality <> 'estimated'
  AND COALESCE(e.payload->>'owner_attribution', '') <> 'joint'
  AND e.public_date <= :mature90 {extra}
"""


def load_buys(conn: Connection, as_of: date, person: str | None = None) -> dict[str, list[Buy]]:
    """Completed buys as known on `as_of` (90d horizon matured before it), grouped by person."""
    params = {"mature90": as_of - timedelta(days=HORIZON + ENTRY_LAG_DAYS),
              "mature180": as_of - timedelta(days=180 + ENTRY_LAG_DAYS)}
    extra = ""
    if person and not person.startswith("N:"):
        extra, params["pc"] = "AND e.payload->>'owner_cik' = :pc", person
    out: dict[str, list[Buy]] = defaultdict(list)
    for r in conn.execute(text(SQL.format(extra=extra)), params).mappings():
        pid = person_id(r["owner_cik"], r["owner"])
        if not pid or (person and pid != person):
            continue
        oc, _ = outcome_for(r["ex90"], r["has90"], r["missing_reason"], r["delisting_reason"], r["delisting_date"],
                            r["public_date"], HORIZON)
        out[pid].append(Buy(pid, r["owner"] or pid, r["issuer_cik"] or f"T:{r['ticker']}", r["public_date"], oc,
                            r["ex30"], r["ex90"], r["ex180"], r["sx90"]))
    return out


def profile_at(conn: Connection, pid: str, as_of: date) -> Profile:
    """Point-in-time profile: what was knowable about this person on `as_of`."""
    buys = load_buys(conn, as_of, pid).get(pid, [])
    return build_profile(pid, buys) if buys else Profile(pid, pid)


def refresh_all(engine: Engine) -> dict:
    today = date.today()
    now = datetime.now(timezone.utc)
    with engine.connect() as conn:
        by = load_buys(conn, today)
    rows = []
    for pid, buys in by.items():
        p = build_profile(pid, buys)
        rows.append({k: getattr(p, k) for k in (
            "person_id", "name", "issuer_count", "completed_buy_count", "episode_count", "unique_company_count",
            "median_30d_excess", "median_90d_excess", "median_180d_excess", "spy_beat_rate_90d", "sector_beat_rate_90d",
            "ci_low", "ci_high", "unresolved_share", "data_quality", "label")} | {"last_updated": now})
    from app.db.models import insider_profiles
    with engine.begin() as conn:
        for i in range(0, len(rows), 1000):
            st = pg_insert(insider_profiles).values(rows[i:i + 1000])
            conn.execute(st.on_conflict_do_update(index_elements=["person_id"],
                                                  set_={c: st.excluded[c] for c in rows[0] if c != "person_id"}))
    labels: dict[str, int] = defaultdict(int)
    for r in rows:
        labels[r["label"]] += 1
    return {"persons": len(rows), "labels": dict(labels)}


LABEL_TR = {"HISTORICALLY_STRONG": "geçmişte güçlü", "HISTORICALLY_WEAK": "geçmişte zayıf", "MIXED": "karışık"}


def telegram_line(p: Profile | dict | None) -> str | None:
    """One Turkish line, only when the history is reliable enough to say anything."""
    if p is None:
        return None
    g = (lambda k: p.get(k)) if isinstance(p, dict) else (lambda k: getattr(p, k))
    if g("label") not in LABEL_TR:
        return None
    return (f"👤 Bu kişinin geçmiş alımları: {g('unique_company_count')} şirkette {g('episode_count')} dönem, "
            f"90 günde S&P 500'ü geçme oranı %{round(100 * g('spy_beat_rate_90d'))} "
            f"(%{round(100 * g('ci_low'))}–%{round(100 * g('ci_high'))} aralığı) → {LABEL_TR[g('label')]}")
