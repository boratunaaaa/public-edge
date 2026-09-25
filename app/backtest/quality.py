"""Honest historical hit rates: independence controls + data-quality gate.

A hit rate is only shown when it is TRUSTED. It is computed on EPISODES, not raw rows:
  * episode clustering - events of one issuer and one event family inside a rolling window are one
    historical episode (1 Jan, 10 Jan, 20 Jan buys do not count the same 90-day move three times);
  * company-equal weighting (default display) - each issuer contributes its mean episode outcome;
    event-level and sector-equal rates are reported next to it;
  * no silent exclusion - every mature comparable event is WIN, LOSS or UNRESOLVED:
      - SPY-relative return known                           -> win / loss
      - performance delisting (bankruptcy / exchange
        compliance, SEC Form 25 + 8-K) inside the horizon   -> loss (Shumway 1997 delisting bias)
      - anything else without a verified return             -> unresolved (never dropped)
Status:
  TRUSTED  unresolved <= 2%, >= 98% of resolved outcomes on a verified point-in-time price identity,
           benchmark complete, >= MIN_COMPANIES issuers and >= MIN_EPISODES episodes
  PARTIAL  unresolved <= 10% (dashboard only, shown with worst/best-case bounds)
  BLOCKED  everything else - no percentage anywhere (fail closed)
"""
from __future__ import annotations

import json
import os
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection

from app.db.models import metrics_quality

MIN_COMPANIES = 30
MIN_EPISODES = 30
TRUSTED_MAX_UNRESOLVED = 0.02
PARTIAL_MAX_UNRESOLVED = 0.10
TRUSTED_MIN_VERIFIED = 0.98
ENTRY_LAG_DAYS = 7  # a horizon is mature only after entry + horizon (+ lag for the next-session entry)
CACHE_TTL = timedelta(hours=6)
VERIFIED_BASES = ("current_ticker_same_cik", "historical_symbol_verified")

# event family (user-facing taxonomy) and default episode windows in days - configurable through
# the EPISODE_WINDOWS environment variable, e.g. EPISODE_WINDOWS='{"NEWS": 45}'
FAMILY = {
    "INSIDER_OPEN_MARKET_BUY": "INSIDER_BUY", "CLUSTER_INSIDER_BUY": "INSIDER_CLUSTER", "ACTIVIST_13D": "ACTIVIST",
    "GOV_CONTRACT": "GOVERNMENT_CONTRACT", "CONGRESS_BUY": "CONGRESS", "CONGRESS_CLUSTER_BUY": "CONGRESS",
    "MATERIAL_NEWS": "NEWS", "EARLY_DISCOVERY": "DISCOVERY", "MULTI_SIGNAL_EVENT": "MULTI_SIGNAL",
}
DEFAULT_EPISODE_WINDOWS = {"INSIDER_BUY": 90, "INSIDER_CLUSTER": 90, "ACTIVIST": 90, "GOVERNMENT_CONTRACT": 90,
                           "CONGRESS": 90, "NEWS": 30, "DISCOVERY": 180, "MULTI_SIGNAL": 90}


def episode_windows() -> dict[str, int]:
    w = dict(DEFAULT_EPISODE_WINDOWS)
    raw = os.environ.get("EPISODE_WINDOWS", "").strip()
    if raw:
        try:
            w.update({k: int(v) for k, v in json.loads(raw).items()})
        except (ValueError, TypeError, AttributeError):
            pass  # a malformed override never disables the control
    return w


def family_of(event_type: str) -> str:
    return FAMILY.get(event_type, event_type)


@dataclass
class Row:
    event_id: int
    issuer: str
    sector: str | None
    public_date: date
    outcome: str  # win | loss | unresolved
    reason: str | None  # why unresolved / how resolved
    verified: bool
    excess: float | None = None
    ret: float | None = None
    mae: float | None = None


@dataclass
class Episode:
    issuer: str
    sector: str | None
    start: date
    first: Row
    n_events: int = 1


@dataclass
class QualityStats:
    horizon: int
    unique_events: int = 0
    unique_episodes: int = 0
    unique_companies: int = 0
    unique_periods: int = 0  # distinct calendar quarters of episode starts
    resolved_episodes: int = 0
    unresolved_episodes: int = 0
    unresolved_by_reason: dict = field(default_factory=dict)
    verified_share: float | None = None
    event_level_rate: float | None = None
    company_equal_rate: float | None = None
    sector_equal_rate: float | None = None
    company_n: int = 0  # issuers with >= 1 resolved episode
    sectors_n: int = 0
    worst_case: float | None = None  # company-equal if every unresolved episode were a loss
    best_case: float | None = None
    median_excess: float | None = None
    median_return: float | None = None
    median_mae: float | None = None
    status: str = "BLOCKED"
    reasons: list = field(default_factory=list)

    @property
    def unresolved_share(self) -> float:
        tot = self.resolved_episodes + self.unresolved_episodes
        return (self.unresolved_episodes / tot) if tot else 1.0

    def as_dict(self) -> dict:
        d = dict(self.__dict__)
        d["unresolved_share"] = self.unresolved_share
        return d


def outcome_for(excess: float | None, has_return: bool, missing_reason: str | None, delisting_reason: str | None,
                delisting_date: date | None, public_date: date, horizon: int) -> tuple[str, str | None]:
    if has_return and excess is not None:
        return ("win" if excess > 0 else "loss"), None
    if has_return and excess is None:
        return "unresolved", "BENCHMARK_MISSING"
    if delisting_reason in ("BANKRUPTCY", "EXCHANGE_COMPLIANCE") and delisting_date is not None \
            and public_date <= delisting_date <= public_date + timedelta(days=horizon + ENTRY_LAG_DAYS):
        return "loss", "DELISTED_PERFORMANCE_RULE"
    return "unresolved", missing_reason or "NO_RETURN_COMPUTED"


def load_rows(conn: Connection, event_type: str, cond: str, params: dict, horizon: int, as_of: date | None = None) -> list[Row]:
    """Every MATURE comparable event (never only the ones that happen to have prices)."""
    as_of = as_of or date.today()
    q = text(f"""
        SELECT e.id, COALESCE(e.issuer_cik, 'T:' || e.ticker, 'E:' || e.id::text) issuer, sm.sector,
               e.public_date, r.excess_ret, r.ret, r.mae, (r.event_id IS NOT NULL) has_ret, e.missing_reason,
               sm.delisting_reason, sm.delisting_date,
               (e.price_symbol_basis IN ('current_ticker_same_cik','historical_symbol_verified')) verified
        FROM events e
        LEFT JOIN event_returns r ON r.event_id = e.id AND r.horizon_days = :h
        LEFT JOIN security_master sm ON sm.cik = e.issuer_cik
        WHERE e.event_type = :et AND NOT e.superseded AND e.date_quality <> 'estimated'
          AND COALESCE(e.payload->>'universe_quality', '') <> 'current_universe_survivorship_biased'
          AND COALESCE(e.payload->>'timestamp_confidence', '') <> 'LOW'
          AND e.public_date <= :mature AND ({cond})
        ORDER BY 2, e.public_date, e.id""")
    mature = as_of - timedelta(days=horizon + ENTRY_LAG_DAYS)
    out = []
    for r in conn.execute(q, {**params, "et": event_type, "h": horizon, "mature": mature}).mappings():
        oc, why = outcome_for(r["excess_ret"], r["has_ret"], r["missing_reason"], r["delisting_reason"], r["delisting_date"],
                              r["public_date"], horizon)
        out.append(Row(r["id"], r["issuer"], r["sector"], r["public_date"], oc, why, bool(r["verified"]),
                       r["excess_ret"], r["ret"], r["mae"]))
    return out


def episodes(rows: list[Row], window_days: int) -> list[Episode]:
    """Same issuer, same family (the caller passes one family): a new episode starts only when an event
    is more than `window_days` after the START of the current episode. The episode's outcome is its
    first event's outcome (entry at the first public information)."""
    by_issuer: dict[str, list[Row]] = defaultdict(list)
    for r in rows:
        by_issuer[r.issuer].append(r)
    out: list[Episode] = []
    for issuer, rs in by_issuer.items():
        rs.sort(key=lambda r: (r.public_date, r.event_id))
        cur: Episode | None = None
        for r in rs:
            if cur is None or (r.public_date - cur.start).days > window_days:
                cur = Episode(issuer, r.sector, r.public_date, r)
                out.append(cur)
            else:
                cur.n_events += 1
    return out


def _company_equal(eps: list[Episode], unresolved_as: str | None = None) -> tuple[float | None, int, dict[str, float]]:
    per: dict[str, list[float]] = defaultdict(list)
    for ep in eps:
        oc = ep.first.outcome
        if oc == "unresolved":
            if unresolved_as is None:
                continue
            oc = unresolved_as
        per[ep.issuer].append(1.0 if oc == "win" else 0.0)
    if not per:
        return None, 0, {}
    comp = {k: sum(v) / len(v) for k, v in per.items()}
    return sum(comp.values()) / len(comp), len(comp), comp


def summarize(rows: list[Row], horizon: int, window_days: int) -> QualityStats:
    st = QualityStats(horizon=horizon, unique_events=len(rows))
    eps = episodes(rows, window_days)
    st.unique_episodes = len(eps)
    st.unique_companies = len({e.issuer for e in eps})
    st.unique_periods = len({(e.start.year, (e.start.month - 1) // 3) for e in eps})
    resolved = [e for e in eps if e.first.outcome != "unresolved"]
    st.resolved_episodes = len(resolved)
    st.unresolved_episodes = len(eps) - len(resolved)
    reasons: dict[str, int] = defaultdict(int)
    for e in eps:
        if e.first.outcome == "unresolved":
            reasons[e.first.reason or "UNKNOWN"] += 1
    st.unresolved_by_reason = dict(reasons)
    if resolved:
        st.verified_share = sum(1 for e in resolved if e.first.verified or e.first.reason == "DELISTED_PERFORMANCE_RULE") / len(resolved)
        st.event_level_rate = sum(1 for r in rows if r.outcome == "win") / max(1, sum(1 for r in rows if r.outcome != "unresolved"))
        st.company_equal_rate, st.company_n, comp = _company_equal(eps)
        sectors: dict[str, list[float]] = defaultdict(list)
        sector_of = {e.issuer: (e.sector or "UNKNOWN") for e in eps}
        for issuer, v in comp.items():
            sectors[sector_of[issuer]].append(v)
        st.sectors_n = len(sectors)
        st.sector_equal_rate = sum(sum(v) / len(v) for v in sectors.values()) / len(sectors)
        st.worst_case = _company_equal(eps, "loss")[0]
        st.best_case = _company_equal(eps, "win")[0]
        ex = [e.first.excess for e in resolved if e.first.excess is not None]
        rt = [e.first.ret for e in resolved if e.first.ret is not None]
        mae = [e.first.mae for e in resolved if e.first.mae is not None]
        st.median_excess = statistics.median(ex) if ex else None
        st.median_return = statistics.median(rt) if rt else None
        st.median_mae = statistics.median(mae) if mae else None
    st.status, st.reasons = grade(st)
    return st


def grade(st: QualityStats) -> tuple[str, list[dict]]:
    reasons: list[dict] = []
    if st.company_n < MIN_COMPANIES or st.resolved_episodes < MIN_EPISODES:
        reasons.append({"code": "INSUFFICIENT_SAMPLE", "companies": st.company_n, "episodes": st.resolved_episodes})
    un = st.unresolved_share
    if st.unresolved_episodes:
        reasons.append({"code": "UNRESOLVED_SAMPLES", "count": st.unresolved_episodes, "share": round(un, 4),
                        "by_reason": st.unresolved_by_reason})
    if st.unresolved_by_reason.get("BENCHMARK_MISSING"):
        reasons.append({"code": "BENCHMARK_INCOMPLETE", "count": st.unresolved_by_reason["BENCHMARK_MISSING"]})
    if st.verified_share is not None and st.verified_share < TRUSTED_MIN_VERIFIED:
        reasons.append({"code": "PRICE_IDENTITY_UNVERIFIED", "verified_share": round(st.verified_share, 4)})
    if any(r["code"] == "INSUFFICIENT_SAMPLE" for r in reasons) or un > PARTIAL_MAX_UNRESOLVED:
        return "BLOCKED", reasons
    if un <= TRUSTED_MAX_UNRESOLVED and (st.verified_share or 0) >= TRUSTED_MIN_VERIFIED \
            and not st.unresolved_by_reason.get("BENCHMARK_MISSING"):
        return "TRUSTED", reasons
    return "PARTIAL", reasons


def reason_text_tr(st: QualityStats | dict) -> str:
    """One short Turkish sentence for Telegram / dashboard."""
    d = st.as_dict() if isinstance(st, QualityStats) else st
    by = d.get("unresolved_by_reason") or {}
    if d.get("company_n", 0) < MIN_COMPANIES or d.get("resolved_episodes", 0) < MIN_EPISODES:
        return (f"yeterli bağımsız örnek yok ({d.get('company_n', 0)} şirket, {d.get('resolved_episodes', 0)} bağımsız dönem; "
                f"en az {MIN_COMPANIES} gerekir).")
    if d.get("unresolved_episodes"):
        pending = by.get("NO_RETURN_COMPUTED", 0)
        price = d["unresolved_episodes"] - pending
        parts = []
        if price:
            parts.append(f"{price} geçmiş örneğin fiyat verisi eksik (çoğu borsadan çıkmış şirket)")
        if pending:
            parts.append(f"{pending} örneğin getirisi henüz hesaplanmadı")
        return "; ".join(parts) + "."
    if (d.get("verified_share") or 0) < TRUSTED_MIN_VERIFIED:
        return "geçmiş fiyat serilerinin bir kısmının şirket kimliği henüz doğrulanmadı."
    return "veri kalitesi eşiği sağlanmadı."


# ------------------------------------------------------------------ cache

def cached(conn: Connection, key: str) -> dict | None:
    r = conn.execute(text("SELECT status, reasons, stats, computed_at FROM metrics_quality WHERE metric_key = :k"),
                     {"k": key}).mappings().first()
    if r and r["computed_at"] and datetime.now(timezone.utc) - r["computed_at"] < CACHE_TTL:
        return dict(r["stats"])
    return None


def store(conn: Connection, key: str, st: QualityStats) -> None:
    vals = {"status": st.status, "reasons": st.reasons, "stats": _json(st.as_dict()), "computed_at": datetime.now(timezone.utc)}
    conn.execute(pg_insert(metrics_quality).values(metric_key=key[:160], **vals)
                 .on_conflict_do_update(index_elements=["metric_key"], set_=vals))


def _json(d: dict) -> dict:
    return json.loads(json.dumps(d, default=str))


def compute(conn: Connection, event_type: str, label: str, cond: str, params: dict, horizon: int,
            use_cache: bool = True, persist: bool = False) -> dict:
    """persist=True only inside engine.begin() (the cache row is committed with the caller)."""
    key = f"{event_type}|{label}|{horizon}"
    if use_cache:
        c = cached(conn, key)
        if c is not None:
            return c
    rows = load_rows(conn, event_type, cond, params, horizon)
    st = summarize(rows, horizon, episode_windows().get(family_of(event_type), horizon))
    if persist:
        store(conn, key, st)
    return _json(st.as_dict())
