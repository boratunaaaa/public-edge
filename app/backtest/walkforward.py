"""Walk-forward evaluation: thresholds are chosen on the past only and scored on the future only.

For each fold (default: 3y train -> 1y validation -> 1y test, rolled forward one year):
  1. TRAIN   - every candidate threshold is scored on train-window episodes whose outcome was
               already REALIZED before the train window closed (public date + horizon + lag);
               the best candidate with enough independent issuers is chosen;
  2. VALIDATE- the choice must not lose to the baseline (no threshold) on the validation year,
               otherwise the baseline is kept;
  3. TEST    - only now is the chosen rule scored, on a year it has never seen.
Reported performance = TEST-window results only. Nothing is fitted on the full history, and the
live system's peer groups are fixed a-priori definitions (never selected by outcome).

Discovery cutoffs cannot be walked forward: historical discovery features were not stored point
in time, so that study reports NOT_EVALUABLE instead of an in-sample number.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date

from sqlalchemy.engine import Connection, Engine

from app.backtest import quality as Q
from app.db.models import walkforward_results


@dataclass(frozen=True)
class Candidate:
    label: str
    cond: str
    params: dict


STUDIES: dict[str, tuple[str, list[Candidate]]] = {
    "insider_buy_value": ("INSIDER_OPEN_MARKET_BUY", [
        Candidate("all", "TRUE", {}),
        Candidate(">=100k", "(e.payload->>'value')::float >= 100000", {}),
        Candidate(">=250k", "(e.payload->>'value')::float >= 250000", {}),
        Candidate(">=500k", "(e.payload->>'value')::float >= 500000", {}),
        Candidate(">=1M", "(e.payload->>'value')::float >= 1000000", {})]),
    "insider_role": ("INSIDER_OPEN_MARKET_BUY", [
        Candidate("all", "TRUE", {}),
        Candidate("ceo", "(e.payload->>'is_ceo')::boolean", {}),
        Candidate("cfo", "(e.payload->>'is_cfo')::boolean", {}),
        Candidate("opportunistic", "e.payload->>'insider_type' = 'opportunistic'", {})]),
    "cluster_size": ("CLUSTER_INSIDER_BUY", [
        Candidate(">=2", "TRUE", {}),
        Candidate(">=3", "(e.payload->>'number_of_buyers')::int >= 3", {}),
        Candidate(">=4", "(e.payload->>'number_of_buyers')::int >= 4", {})]),
    "activist_purpose": ("ACTIVIST_13D", [
        Candidate("all", "TRUE", {}),
        Candidate("activist_purpose", "e.payload->>'item4_class' NOT IN ('PASSIVE_OR_UNCLEAR','MANAGEMENT_DISCUSSION')", {})]),
    "news_materiality": ("MATERIAL_NEWS", [
        Candidate("HIGH+CRITICAL", "TRUE", {}),
        Candidate("CRITICAL", "e.payload->>'materiality' = 'CRITICAL'", {})]),
    "tenk_change": ("TENK_CHANGE", [
        Candidate("all", "TRUE", {}),
        Candidate("positive", "e.payload->>'direction' = 'POSITIVE'", {}),
        Candidate("high", "(e.payload->>'high')::int >= 1", {})]),
    "contract_ratio": ("GOV_CONTRACT", [
        Candidate(">=0.5%", "TRUE", {}),
        Candidate(">=2%", "(e.payload->>'value_to_mcap')::float >= 0.02", {}),
        Candidate(">=5%", "(e.payload->>'value_to_mcap')::float >= 0.05", {})]),
}
NOT_EVALUABLE = {"discovery_cutoff": "historical discovery features were not stored point in time"}


@dataclass(frozen=True)
class Fold:
    n: int
    train_start: date
    train_end: date
    validation_end: date
    test_end: date


def folds(first: date, last: date, train_years: int = 3, val_years: int = 1, test_years: int = 1) -> list[Fold]:
    out, k = [], 0
    start = date(first.year, 1, 1)
    while True:
        tr_end = date(start.year + train_years, 1, 1)
        va_end = date(tr_end.year + val_years, 1, 1)
        te_end = date(va_end.year + test_years, 1, 1)
        if va_end > last:
            break
        out.append(Fold(k, start, tr_end, va_end, min(te_end, last)))
        k += 1
        start = date(start.year + 1, 1, 1)
    return out


def score_window(conn: Connection, event_type: str, cand: Candidate, lo: date, hi: date, horizon: int) -> dict:
    """Company-equal hit rate on episodes that START in [lo, hi) and are REALIZED by hi."""
    rows = [r for r in Q.load_rows(conn, event_type, cand.cond, cand.params, horizon, as_of=hi) if r.public_date >= lo]
    st = Q.summarize(rows, horizon, Q.episode_windows().get(Q.family_of(event_type), horizon))
    ok = st.company_equal_rate is not None and st.company_n >= Q.MIN_COMPANIES and st.unresolved_share <= Q.PARTIAL_MAX_UNRESOLVED
    # a window that fails the data gate reports NO rate (its resolved subset could be survivorship-biased)
    return {"rate": st.company_equal_rate if ok else None, "rate_withheld": not ok, "companies": st.company_n,
            "episodes": st.resolved_episodes, "unresolved_share": round(st.unresolved_share, 4), "status": st.status}


ALPHA = 0.05


def beats_baseline(chosen: dict, baseline: dict, n_trials: int, alpha: float = ALPHA) -> tuple[bool, float | None, float]:
    """Multiple-testing guard (Bonferroni over the candidates tried, cf. the Deflated Sharpe idea in
    bagoldbe/form4lab): the validation improvement must be significant after correcting for the number
    of thresholds searched; otherwise the plain baseline is kept. One-sided z-test on company-equal rates."""
    from statistics import NormalDist
    trials = max(1, n_trials - 1)
    required = NormalDist().inv_cdf(1 - alpha / trials)
    pc, pb, n = chosen.get("rate"), baseline.get("rate"), chosen.get("companies") or 0
    if pc is None or pb is None or n <= 0 or pb <= 0 or pb >= 1:
        return False, None, required
    z = (pc - pb) / ((pb * (1 - pb) / n) ** 0.5)
    return z > required, round(z, 3), round(required, 3)


def usable(s: dict) -> bool:
    return s["rate"] is not None and s["companies"] >= Q.MIN_COMPANIES and s["unresolved_share"] <= Q.PARTIAL_MAX_UNRESOLVED


def run_study(conn: Connection, study: str, horizon: int = 90, first: date | None = None, last: date | None = None) -> list[dict]:
    event_type, cands = STUDIES[study]
    last = last or date.today()
    first = first or date(last.year - 6, 1, 1)
    results = []
    for f in folds(first, last):
        train = {c.label: score_window(conn, event_type, c, f.train_start, f.train_end, horizon) for c in cands}
        ok = {k: v for k, v in train.items() if usable(v)}
        baseline = cands[0]
        chosen = max(ok, key=lambda k: ok[k]["rate"]) if ok else None
        val_c = score_window(conn, event_type, next(c for c in cands if c.label == chosen), f.train_end, f.validation_end, horizon) \
            if chosen else None
        val_b = score_window(conn, event_type, baseline, f.train_end, f.validation_end, horizon)
        z = req = None
        if chosen and chosen != baseline.label and val_c and usable(val_c) and usable(val_b):
            ok, z, req = beats_baseline(val_c, val_b, len(cands))
            if not ok:
                chosen = baseline.label  # improvement not significant after correcting for the thresholds tried
        elif chosen and (not val_c or not usable(val_c)):
            chosen = baseline.label  # validation did not confirm the train-window choice
        test_c = score_window(conn, event_type, next(c for c in cands if c.label == chosen), f.validation_end, f.test_end, horizon) \
            if chosen else None
        test_b = score_window(conn, event_type, baseline, f.validation_end, f.test_end, horizon)
        results.append({"fold": f.n, "train": [str(f.train_start), str(f.train_end)], "validation_end": str(f.validation_end),
                        "test_end": str(f.test_end), "chosen": chosen, "trials": len(cands), "validation_z": z,
                        "required_z": req, "train_scores": train, "validation": val_c,
                        "validation_baseline": val_b, "test": test_c, "test_baseline": test_b,
                        "status": "OK" if (test_c and usable(test_c)) else "INSUFFICIENT_OUT_OF_SAMPLE_DATA"})
    return results


def run_all(engine: Engine, horizon: int = 90) -> dict:
    run_id = f"wf-{date.today().isoformat()}-{uuid.uuid4().hex[:6]}"
    summary = {"run_id": run_id, "studies": {}, "not_evaluable": NOT_EVALUABLE}
    for study in STUDIES:
        with engine.begin() as conn:
            res = run_study(conn, study, horizon)
            for r in res:
                conn.execute(walkforward_results.insert().values(
                    run_id=run_id, study=study, fold=r["fold"], train_start=date.fromisoformat(r["train"][0]),
                    train_end=date.fromisoformat(r["train"][1]), validation_end=date.fromisoformat(r["validation_end"]),
                    test_end=date.fromisoformat(r["test_end"]), chosen={"label": r["chosen"]}, metrics=_j(r)))
        summary["studies"][study] = [{"fold": r["fold"], "chosen": r["chosen"], "status": r["status"],
                                      "test_rate": (r["test"] or {}).get("rate"),
                                      "baseline_test_rate": (r["test_baseline"] or {}).get("rate")} for r in res]
    return summary


def _j(d):
    import json
    return json.loads(json.dumps(d, default=str))
