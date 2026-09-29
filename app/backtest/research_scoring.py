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
        self.series: dict[str, tuple] = {}  # ONE issuer at a time (callers pass events grouped by issuer): bounded memory
        self.cache: dict[int, tuple | None] = {}

    def match(self, e: dict) -> tuple[float, float] | None:
        if e["id"] in self.cache:
            return self.cache[e["id"]]
        cik = e["issuer_cik"]
        if cik not in self.series:
            self.series.clear()
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
    for e in sorted(events, key=lambda e: (e["issuer_cik"], e["public_date"], e["id"])):  # one series in memory
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


# ================================================================== night run 2026-09-30: expansion + V2 families
# (read-only; pre-registered below; no weight is produced here)

from datetime import date as _date  # noqa: E402

from sqlalchemy import text as _text  # noqa: E402

DERIVED = ("repeat_same_issuer_365d", "first_buy_same_issuer_5y", "serial_3plus_issuers_2y", "owner_history_positive",
           "owner_history_negative", "cluster_context_30d", "direct_only", "indirect_only", "value_ge_250k")
V2_TYPES = ("INST_13F_NEW_POSITION", "INST_13F_ACCUMULATION", "INST_13F_EXIT", "INST_13F_DECREASE", "FDA_APPROVAL",
            "FDA_EFFICACY_SUPPLEMENT", "RESULTS_POSTED", "TRIAL_REGISTERED", "FTD_SPIKE", "SHORT_PRESSURE", "GOV_CONTRACT",
            "CA_GOV_CONTRACT_WIN")
MIN_COMPANIES_90D = 100


def _all_rows(conn: Connection, event_type: str) -> list[dict]:
    """Every priced, reviewed-clean event of a type (no per-quarter thinning), with its 90-day excess."""
    return [dict(r) for r in conn.execute(_text("""
        SELECT e.id, e.issuer_cik, e.public_date, e.available_at, e.price_symbol, e.payload, r.excess_ret
        FROM events e JOIN event_returns r ON r.event_id = e.id AND r.horizon_days = :h
        WHERE e.event_type = :et AND NOT e.superseded AND NOT e.needs_review AND e.issuer_cik IS NOT NULL
          AND e.price_symbol IS NOT NULL AND r.excess_ret IS NOT NULL
        ORDER BY e.issuer_cik, e.public_date, e.id"""), {"h": H, "et": event_type}).mappings()]


def per_quarter(rows: list[dict]) -> list[dict]:
    """research_v2._events thinning in Python: the first event per issuer and calendar quarter."""
    seen, out = set(), []
    for r in sorted(rows, key=lambda r: (r["issuer_cik"], r["public_date"], r["id"])):
        k = (r["issuer_cik"], r["public_date"].year, (r["public_date"].month - 1) // 3)
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out


def derive_flags(rows: list[dict]) -> dict[int, set[str]]:
    """Point-in-time flags from EARLIER events only (owner / issuer history strictly before the buy's public date;
    an owner's past outcome only from buys whose 90-day window ended >= 30 days before it: public_date <= d - 120)."""
    by_owner, by_issuer = defaultdict(list), defaultdict(list)
    for r in rows:
        own = (r["payload"] or {}).get("owner_cik")
        if own:
            by_owner[own].append(r)
        by_issuer[r["issuer_cik"]].append(r)
    flags: dict[int, set[str]] = defaultdict(set)
    for r in rows:
        p, d = r["payload"] or {}, r["public_date"]
        own = p.get("owner_cik")
        prior = [x for x in by_owner.get(own, []) if x["public_date"] < d] if own else []
        same = [x for x in prior if x["issuer_cik"] == r["issuer_cik"]]
        if any(x["public_date"] >= d - timedelta(days=365) for x in same):
            flags[r["id"]].add("repeat_same_issuer_365d")
        if own and not any(x["public_date"] >= d - timedelta(days=1826) for x in same):
            flags[r["id"]].add("first_buy_same_issuer_5y")
        if len({x["issuer_cik"] for x in prior if x["public_date"] >= d - timedelta(days=730)}) >= 3:
            flags[r["id"]].add("serial_3plus_issuers_2y")
        hist = [x["excess_ret"] for x in prior if d - timedelta(days=1826) <= x["public_date"] <= d - timedelta(days=120)
                and x["excess_ret"] is not None]
        if len(hist) >= 2:
            flags[r["id"]].add("owner_history_positive" if statistics.fmean(hist) > 0 else "owner_history_negative")
        if any(x["public_date"] < d and x["public_date"] >= d - timedelta(days=30)
               and (x["payload"] or {}).get("owner_cik") != own for x in by_issuer[r["issuer_cik"]]):
            flags[r["id"]].add("cluster_context_30d")
        di = p.get("direct_indirect")
        if di == ["D"]:
            flags[r["id"]].add("direct_only")
        if di == ["I"]:
            flags[r["id"]].add("indirect_only")
        try:
            if float(p.get("value")) >= 250_000:
                flags[r["id"]].add("value_ge_250k")
        except (TypeError, ValueError):
            pass
    return flags


def run_expansion(conn: Connection) -> dict:
    """INSIDER_BUY: primary + every research_v2 / extra / derived bucket, on the per-quarter sample (as before) and on
    the FULL sample (every buy, company-equal); lookback / contamination sensitivity of the primary. BH per sample."""
    from app.scoring.engine import BUCKETS, _discretionary
    rng = random.Random(SEED + 1)
    et = "INSIDER_OPEN_MARKET_BUY"
    rows = _all_rows(conn, et)
    flags = derive_flags(rows)
    disc = [r for r in rows if _discretionary(r["payload"] or {})]
    preds = {k: (lambda r, f=f: f(r["payload"] or {}) is True) for k, f in BUCKETS.items()}
    preds["planned_10b5_1"] = None  # handled on the non-discretionary set
    preds["ten_pct_any"] = lambda r: (r["payload"] or {}).get("is_ten_pct") is True
    for k in DERIVED:
        preds[k] = lambda r, k=k: k in flags.get(r["id"], ())
    out: dict = {"events_priced": len(rows), "discretionary": len(disc)}
    m = Matcher(conn, et, 60)  # a match depends only on the event: shared by both samples
    for sample, thin in (("per_quarter", per_quarter), ("all_events", lambda x: x)):
        res = {"primary": summarize(company_diffs(m, thin(disc)), rng)}
        fam = {}
        for k, pr in preds.items():
            sel = [r for r in rows if not _discretionary(r["payload"] or {})] if pr is None else [r for r in disc if pr(r)]
            fam[k] = summarize(company_diffs(m, thin(sel)), rng)
        adj = bh({k: v.get("t_p") for k, v in fam.items()})
        for k, v in fam.items():
            v["bh_p"] = adj.get(k)
        res["buckets"] = fam
        out[sample] = res
    sens = {}
    for lb, cont in ((20, "family"), (90, "family"), (60, "any")):
        sens[f"lookback{lb}_{cont}"] = summarize(company_diffs(Matcher(conn, et, lb, cont), per_quarter(disc)), rng)
    out["primary_sensitivity"] = sens
    return out


def coverage(conn: Connection) -> dict:
    rows = conn.execute(_text("""
        SELECT event_type, returns_status, count(*) FROM events WHERE NOT superseded GROUP BY 1, 2""")).all()
    cos = dict(conn.execute(_text("""
        SELECT e.event_type, count(DISTINCT e.issuer_cik) FROM events e
        JOIN event_returns r ON r.event_id = e.id AND r.horizon_days = 90
        WHERE NOT e.superseded AND NOT e.needs_review AND r.excess_ret IS NOT NULL GROUP BY 1""")).all())
    out: dict = defaultdict(lambda: {"statuses": {}})
    for et, st, n in rows:
        out[et]["statuses"][st] = n
    for et, v in out.items():
        v["companies_90d"] = cos.get(et, 0)
        v["ready_for_validation"] = cos.get(et, 0) >= MIN_COMPANIES_90D
    tot = defaultdict(int)
    for _et, _st, n in rows:
        tot[_st] += n
    return {"as_of": str(_date.today()), "totals": dict(tot), "by_type": dict(out)}


def run_families(conn: Connection) -> dict:
    """V2 families with enough 90-day coverage: raw company-equal SPY-beat + matched same-stock control (60d lookback,
    same-type contamination), company bootstrap; BH across the families actually tested. Others: reason only."""
    rng = random.Random(SEED + 2)
    cov = coverage(conn)["by_type"]
    out, tested = {}, {}
    for et in V2_TYPES:
        c = cov.get(et, {"companies_90d": 0, "statuses": {}})
        if c["companies_90d"] < MIN_COMPANIES_90D:
            out[et] = {"status": "INSUFFICIENT_COVERAGE", "companies_90d": c["companies_90d"], "statuses": c["statuses"]}
            continue
        rows = _all_rows(conn, et)
        per = defaultdict(list)
        for r in rows:
            per[r["issuer_cik"]].append(1.0 if r["excess_ret"] > 0 else 0.0)
        raw = statistics.fmean(statistics.fmean(v) for v in per.values())
        m = Matcher(conn, et, 60, "family")
        s = summarize(company_diffs(m, per_quarter(rows)), rng)
        out[et] = {"status": "TESTED", "companies_90d": c["companies_90d"], "raw_spy_beat_company_equal": raw, **s}
        tested[et] = s.get("t_p")
    adj = bh(tested)
    for et, p in adj.items():
        out[et]["bh_p"] = p
    return out
