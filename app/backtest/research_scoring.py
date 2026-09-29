"""Scoring evidence V2 - read-only research (pre-registered; nothing here is used by the scoring engine until a new
evidence version is committed). It reproduces research_v2.matched_placebo EXACTLY (same event selection, same
prior-return matching, same contamination rule) but keeps the company-level paired differences, so the uncertainty of
the MEAN effect can be measured:

  per company: mean(event beat SPY over 90d) - mean(matched control beat SPY)  ->  d_i
  effect = mean(d_i); se = sd(d_i) / sqrt(n); 95 % normal interval; company bootstrap (B = 2000, fixed seed) percentile
  interval; primary test = paired t (normal approximation, n >= 100); sign test kept as secondary.

Hypothesis families (fixed before any result was looked at):
  A  INSIDER_BUY buckets - the 11 research_v2 buckets + planned 10b5-1 buys + any 10 % owner + >= 3 transactions;
     Benjamini-Hochberg across the family on the paired-t p-values.
  B  Incremental 10-K effect on a LATER insider buy: discretionary insider buys with a 10-K change of the same issuer
     public in the 90 days BEFORE the buy's own public time (point in time: 10-K available_at <= buy available_at)
     versus buys without; primary = NEGATIVE-direction change, secondary = any change; BH across the two.
     Incremental = mean(d | with) - mean(d | without), company-cluster bootstrap.
A weight is proposed only when BH-significant AND the bootstrap interval excludes 0; otherwise none."""
from __future__ import annotations

import bisect
import math
import random
import statistics
from collections import defaultdict
from datetime import timedelta

from sqlalchemy.engine import Connection

from app.backtest.research_v2 import DISCRETIONARY, EXCL, H, INSIDER_BUCKETS, TOL, _events, _exclusion, _ret, _series, sign_p

B = 2000
SEED = 20260930
Z = 1.959964

EXTRA_BUCKETS = {
    "planned_10b5_1": "COALESCE((payload->>'is_10b5_1')::boolean, false)",
    "ten_pct_any": "(payload->>'is_ten_pct')::boolean",
    "n_transactions_ge_3": "(payload->>'n_transactions')::int >= 3",
}


class Matcher:
    """research_v2.matched_placebo's matching, cached per event (one price series per issuer)."""

    def __init__(self, conn: Connection, event_type: str, lookback: int = 60, contamination: str = "family"):
        self.conn, self.lookback = conn, lookback
        self.excl = _exclusion(conn, event_type, contamination)
        self.spy_d, self.spy_p = _series(conn, "SPY")
        self.series: dict[str, tuple] = {}
        self.cache: dict[int, tuple | None] = {}

    def match(self, e: dict) -> tuple[float, float] | None:
        if e["id"] in self.cache:
            return self.cache[e["id"]]
        cik = e["issuer_cik"]
        if cik not in self.series:
            self.series[cik] = _series(self.conn, e["price_symbol"])
        ds, px = self.series[cik]
        out = None
        if len(ds) >= 300:
            out = self._match(e, ds, px, self.excl.get(cik, []))
        self.cache[e["id"]] = out
        return out

    def _match(self, e, ds, px, blocked):
        d0 = e["public_date"]
        pe = _ret(ds, px, d0, back=self.lookback)
        fe, se = _ret(ds, px, d0, fwd=H), _ret(self.spy_d, self.spy_p, d0, fwd=H)
        if pe is None or fe is None or se is None:
            return None
        best, bd = None, None
        for j in range(0, len(ds), 5):
            c = ds[j]
            if abs((c - d0).days) < 180 or c + timedelta(days=H + 7) > ds[-1]:
                continue
            i = bisect.bisect_left(blocked, c - timedelta(days=EXCL))
            if i < len(blocked) and blocked[i] <= c + timedelta(days=EXCL + H):
                continue
            pc = _ret(ds, px, c, back=self.lookback)
            if pc is None:
                continue
            dist = abs(pc - pe)
            if dist <= TOL and (bd is None or dist < bd):
                best, bd = c, dist
        if best is None:
            return None
        fc, sc = _ret(ds, px, best, fwd=H), _ret(self.spy_d, self.spy_p, best, fwd=H)
        if fc is None or sc is None:
            return None
        return (1.0 if fe - se > 0 else 0.0, 1.0 if fc - sc > 0 else 0.0)


def company_diffs(m: Matcher, events: list[dict]) -> dict[str, float]:
    per = defaultdict(lambda: [[], []])
    for e in events:
        r = m.match(e)
        if r is None:
            continue
        per[e["issuer_cik"]][0].append(r[0])
        per[e["issuer_cik"]][1].append(r[1])
    return {cik: statistics.fmean(a) - statistics.fmean(b) for cik, (a, b) in per.items()}


def _norm_p(z: float) -> float:
    return math.erfc(abs(z) / math.sqrt(2))


def summarize(d: dict[str, float], rng: random.Random) -> dict:
    x = list(d.values())
    n = len(x)
    if n < 2:
        return {"companies": n}
    mean, sd = statistics.fmean(x), statistics.stdev(x)
    se = sd / math.sqrt(n)
    boots = sorted(statistics.fmean(rng.choices(x, k=n)) for _ in range(B))
    return {"companies": n, "effect": mean, "sd": sd, "se": se, "ci95": [mean - Z * se, mean + Z * se],
            "boot_ci95": [boots[int(0.025 * B)], boots[int(0.975 * B) - 1]], "t_p": _norm_p(mean / se) if se else None,
            "sign_p": sign_p(x)}


def incremental(with_d: dict[str, float], without_d: dict[str, float], rng: random.Random) -> dict:
    a, b = list(with_d.values()), list(without_d.values())
    if len(a) < 2 or len(b) < 2:
        return {"with_companies": len(a), "without_companies": len(b)}
    inc = statistics.fmean(a) - statistics.fmean(b)
    se = math.sqrt(statistics.variance(a) / len(a) + statistics.variance(b) / len(b))
    ciks = sorted(set(with_d) | set(without_d))
    boots = []
    for _ in range(B):  # company-cluster bootstrap: a resampled company brings its rows in both groups
        pick = rng.choices(ciks, k=len(ciks))
        wa = [with_d[c] for c in pick if c in with_d]
        wb = [without_d[c] for c in pick if c in without_d]
        if wa and wb:
            boots.append(statistics.fmean(wa) - statistics.fmean(wb))
    boots.sort()
    k = len(boots)
    return {"with_companies": len(a), "without_companies": len(b), "with_effect": statistics.fmean(a),
            "without_effect": statistics.fmean(b), "incremental": inc, "se": se, "ci95": [inc - Z * se, inc + Z * se],
            "boot_ci95": [boots[int(0.025 * k)], boots[int(0.975 * k) - 1]] if k else None,
            "welch_p": _norm_p(inc / se) if se else None}


def bh(pvals: dict[str, float], q: float = 0.05) -> dict[str, float]:
    """Benjamini-Hochberg adjusted p-values (step-up, monotone)."""
    items = sorted(((k, p) for k, p in pvals.items() if p is not None), key=lambda kv: kv[1])
    m, out, prev = len(items), {}, 1.0
    for i in range(m, 0, -1):
        k, p = items[i - 1]
        prev = min(prev, p * m / i)
        out[k] = prev
    return out


def run(conn: Connection, lookback: int = 60) -> dict:
    rng = random.Random(SEED)
    et = "INSIDER_OPEN_MARKET_BUY"
    m = Matcher(conn, et, lookback)
    out: dict = {"design": __doc__.split("\n\n")[0], "lookback": lookback, "horizon": H, "bootstrap": B, "seed": SEED}
    primary = company_diffs(m, _events(conn, et, DISCRETIONARY))
    out["primary"] = summarize(primary, rng)
    fam = {}
    for name, cond in INSIDER_BUCKETS.items():
        fam[name] = summarize(company_diffs(m, _events(conn, et, f"({cond}) AND {DISCRETIONARY}")), rng)
    for name, cond in EXTRA_BUCKETS.items():
        where = cond if name == "planned_10b5_1" else f"({cond}) AND {DISCRETIONARY}"
        fam[name] = summarize(company_diffs(m, _events(conn, et, where)), rng)
    adj = bh({k: v.get("t_p") for k, v in fam.items()})
    for k, v in fam.items():
        v["bh_p"] = adj.get(k)
    out["buckets"] = fam
    # B: incremental 10-K effect on a later discretionary buy (point in time on available_at)
    inc = {}
    for label, dir_cond in (("negative_10k", "t.payload->>'direction' = 'NEGATIVE'"), ("any_10k", "TRUE")):
        tagged = f"""EXISTS (SELECT 1 FROM events t WHERE t.issuer_cik = events_e.issuer_cik AND t.event_type = 'TENK_CHANGE'
                     AND NOT t.superseded AND {dir_cond}
                     AND t.available_at <= events_e.available_at AND t.available_at > events_e.available_at - interval '90 days')"""
        w = _events(conn, et, f"{DISCRETIONARY} AND {tagged.replace('events_e', 'e')}")
        wo = _events(conn, et, f"{DISCRETIONARY} AND NOT {tagged.replace('events_e', 'e')}")
        inc[label] = incremental(company_diffs(m, w), company_diffs(m, wo), rng)
    adj = bh({k: v.get("welch_p") for k, v in inc.items()})
    for k, v in inc.items():
        v["bh_p"] = adj.get(k)
    out["tenk_incremental"] = inc
    out["matched_events_cached"] = len(m.cache)
    return out
