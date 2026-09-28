"""V2 historical validation standard (#46-#49): one report per family x horizon, every hypothesis logged.

Reported: events, companies, episodes, median raw / median + mean excess (company-equal, first event of each
episode), SPY- and sector-beat rates, Wilson 95 % CI, missing-price (unresolved) rate, delisting rate, walk-forward
(first 60 % of episodes by time = in-sample, last 40 % = out-of-sample), a two-sided binomial p-value vs 50 %,
Benjamini-Hochberg adjustment across the WHOLE hypothesis ledger, and a coarse regime split (SPY above/below its
200-day mean, realised-volatility high/low) only when each bucket keeps >= 30 companies.
Data-quality status (TRUSTED/PARTIAL/BLOCKED) and alpha verdict are kept separate."""
from __future__ import annotations

import math
import statistics
from collections import defaultdict
from datetime import date, timedelta

from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.backtest import quality as Q
from app.backtest.probability import wilson_interval
from app.core.signal_registry import BY_ID

HORIZONS = (30, 90, 180)
MIN_BUCKET = 30


def _company_mean(eps, key) -> tuple[float | None, int]:
    per = defaultdict(list)
    for e in eps:
        v = key(e.first)
        if v is not None:
            per[e.issuer].append(v)
    if not per:
        return None, 0
    return statistics.fmean(statistics.fmean(v) for v in per.values()), len(per)


def binom_p(rate: float | None, n: int) -> float | None:
    if rate is None or n < 10:
        return None
    z = abs(rate - 0.5) / math.sqrt(0.25 / n)
    return math.erfc(z / math.sqrt(2))


def _spy_regimes(conn: Connection) -> dict[date, tuple[str, str]]:
    rows = conn.execute(text("SELECT date, close FROM market_prices WHERE symbol = 'SPY' ORDER BY date")).all()
    out, closes = {}, []
    rets = []
    for i, (d, c) in enumerate(rows):
        closes.append(c)
        if i:
            rets.append(math.log(c / rows[i - 1][1]) if rows[i - 1][1] else 0.0)
        if len(closes) >= 200 and len(rets) >= 20:
            trend = "bull" if c >= statistics.fmean(closes[-200:]) else "bear"
            vol = statistics.pstdev(rets[-20:]) * math.sqrt(252)
            out[d] = (trend, "high_vol" if vol >= 0.20 else "low_vol")
    return out


def family_report(conn: Connection, family: str, *, horizons=HORIZONS, log: bool = True, as_of: date | None = None) -> dict:
    fam = BY_ID[family]
    rep = {"family": family, "country": fam.country, "version": fam.version, "context_only": fam.context_only, "horizons": {}}
    regimes = None
    for h in horizons:
        rows = []
        for et in fam.source_event_types:
            rows += Q.load_rows(conn, et, "TRUE", {}, h, as_of=as_of)
        win = Q.episode_windows().get(Q.family_of(fam.source_event_types[0]) if fam.source_event_types else family, h)
        st = Q.summarize(rows, h, win)
        eps = [e for e in Q.episodes(rows, win) if e.first.outcome != "unresolved"]
        mean_ex, _ = _company_mean(eps, lambda r: r.excess)
        med_raw = statistics.median([e.first.ret for e in eps if e.first.ret is not None]) if eps else None
        rate, n = st.company_equal_rate, st.company_n
        lo, hi = wilson_interval(rate * n, n) if (rate is not None and n) else (None, None)
        eps_sorted = sorted(eps, key=lambda e: e.start)
        cut = int(len(eps_sorted) * 0.6)
        ins, oos = eps_sorted[:cut], eps_sorted[cut:]
        r_in = Q._company_equal(ins)[0] if ins else None
        r_oos = Q._company_equal(oos)[0] if oos else None
        all_eps = Q.episodes(rows, win)
        delisted = sum(1 for e in all_eps if e.first.reason == "DELISTED_PERFORMANCE_RULE")
        reg = {}
        if eps:
            if regimes is None:
                regimes = _spy_regimes(conn)
            buckets = defaultdict(list)
            for e in eps:
                g = regimes.get(e.start) or regimes.get(e.start - timedelta(days=1)) or regimes.get(e.start - timedelta(days=3))
                if g:
                    buckets[g[0]].append(e)
                    buckets[g[1]].append(e)
            for k, v in buckets.items():
                r, nk = Q._company_equal(v)[:2]
                reg[k] = {"companies": nk, "rate": r} if nk >= MIN_BUCKET else {"companies": nk, "rate": None, "note": "too small"}
        p = binom_p(rate, n)
        ep_wins = [e for e in eps if e.first.outcome == "win"]
        out = {"status": st.status, "events": st.unique_events, "episodes": st.unique_episodes, "companies": st.unique_companies,
               "event_weighted": st.event_level_rate, "episode_equal": (len(ep_wins) / len(eps)) if eps else None,
               "company_equal": rate,
               "resolved_companies": n, "median_raw": med_raw, "median_excess": st.median_excess, "mean_excess": mean_ex,
               "spy_beat": rate, "sector_beat": st.sector_benchmark_rate, "ci95": [lo, hi], "missing_price_rate": st.unresolved_share,
               "delist_rate": (delisted / len(all_eps)) if all_eps else None, "worst_case": st.worst_case, "best_case": st.best_case,
               "in_sample": r_in, "out_of_sample": r_oos, "p_value": p, "regimes": reg}
        out["verdict"] = verdict(out, fam.minimum_sample)
        rep["horizons"][h] = out
        if log:
            conn.execute(text("""INSERT INTO hypothesis_log (family, variant, horizon, n_events, n_companies, in_sample_rate,
                                   out_of_sample_rate, p_value, verdict, params)
                                 VALUES (:f, 'base', :h, :ne, :nc, :i, :o, :p, :v, CAST(:pr AS jsonb))"""),
                         {"f": family, "h": h, "ne": st.unique_events, "nc": n, "i": r_in, "o": r_oos, "p": p, "v": out["verdict"],
                          "pr": '{"version": "%s"}' % fam.version})
    if log:
        adjust_ledger(conn)
    return rep


def verdict(o: dict, min_sample: int) -> str:
    if o["status"] == "BLOCKED":
        return "BLOCKED"
    if (o["resolved_companies"] or 0) < min_sample:
        return "INSUFFICIENT_SAMPLE"
    lo, hi = o["ci95"]
    if hi is not None and hi < 0.5:
        return "UNDERPERFORMING"
    if lo is not None and lo > 0.5:
        if o["out_of_sample"] is not None and o["out_of_sample"] <= 0.5:
            return "IN_SAMPLE_ONLY"  # walk-forward did not confirm
        if o["status"] != "TRUSTED":
            return "PROMISING_PARTIAL"
        return "OUTPERFORMING"
    return "INCONCLUSIVE"


def adjust_ledger(conn: Connection) -> int:
    """Benjamini-Hochberg over the latest p-value of every (family, variant, horizon) ever tested."""
    rows = conn.execute(text("""SELECT DISTINCT ON (family, variant, horizon) id, p_value FROM hypothesis_log
                                WHERE p_value IS NOT NULL ORDER BY family, variant, horizon, run_at DESC""")).all()
    m = len(rows)
    if not m:
        return 0
    ranked = sorted(rows, key=lambda r: r[1])
    adj, prev = {}, 1.0
    for i in range(m - 1, -1, -1):
        rid, p = ranked[i]
        prev = min(prev, p * m / (i + 1))
        adj[rid] = prev
    for rid, a in adj.items():
        conn.execute(text("UPDATE hypothesis_log SET adjusted_p = :a WHERE id = :i"), {"a": a, "i": rid})
    return m


def ledger_summary(conn: Connection) -> dict:
    r = conn.execute(text("""SELECT count(DISTINCT (family, variant, horizon)) n, count(*) runs,
                                    count(*) FILTER (WHERE adjusted_p < 0.05) sig FROM hypothesis_log""")).mappings().one()
    return dict(r)
