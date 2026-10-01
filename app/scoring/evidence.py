"""Evidence registry for scoring (v3, 2026-09-30): every weight is a MEASURED effect, never a chosen number.

Unit: probability points of beating SPY over 90 days, event minus a matched control of the SAME stock (company-level
paired differences, app.backtest.research_scoring). The evidence file is built by scripts/build_scoring_evidence_v3.py
from committed research outputs; it labels each insider subgroup with a role, and this module re-checks the numbers:

  ACTIVE            gets its measured weight only if >= MIN_COMPANIES companies, BH-adjusted p <= Q in its family AND
                    the company-bootstrap interval excludes 0 - otherwise weight 0 (a label never overrides the data)
  EVIDENCE_ONLY     measured and significant, kept as evidence, never stacked (role_10pct_only is a subset of ten_pct_any)
  HOLDOUT_REQUIRED  significant only in an exploratory family or discovered afterwards: weight 0 until a pre-registered
                    holdout replicates it
  NOT_SIGNIFICANT   tested, not significant

The primary INSIDER_BUY effect is VALIDATED only when it is significant in the main design AND in every sensitivity run
(lookback / contamination); otherwise NOT_ROBUST with weight 0. A NEGATIVE 10-K change is never a weight: it is a veto
(app.scoring.engine)."""
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

EVIDENCE_FILE = Path(__file__).with_name("evidence_2026-09-30.json")
MIN_COMPANIES = 100
Q = 0.05

VALIDATED, NOT_SIGNIFICANT, NOT_MEASURED = "VALIDATED", "NOT_SIGNIFICANT", "NOT_MEASURED"
NOT_ROBUST, EVIDENCE_ONLY, HOLDOUT_REQUIRED, VETO = "NOT_ROBUST", "EVIDENCE_ONLY", "HOLDOUT_REQUIRED", "VETO"


@dataclass(frozen=True)
class Effect:
    name: str
    status: str
    effect: float  # the WEIGHT in probability points (0.05 = +5 pp); 0 unless VALIDATED
    companies: int | None
    p: float | None
    source: str
    why: str = ""
    measured: float | None = None  # the measured effect, kept for audit even when the weight is 0

    @property
    def usable(self) -> bool:
        return self.status == VALIDATED


def bh_survivors(pvals: dict[str, float], q: float = Q) -> set[str]:
    """Benjamini-Hochberg step-up: the names whose p-value is rejected at FDR q."""
    ranked = sorted(pvals.items(), key=lambda kv: kv[1])
    m, cut = len(ranked), 0
    for i, (_n, p) in enumerate(ranked, 1):
        if p <= q * i / m:
            cut = i
    return {n for n, _p in ranked[:cut]}


def _bucket(name: str, c: dict, src: str) -> Effect:
    n, p, d, lo = c.get("companies"), c.get("bh_p"), c.get("effect"), (c.get("boot_ci95") or [None])[0]
    if n is None or p is None or d is None:
        return Effect(name, NOT_MEASURED, 0.0, n, p, src, "no measured result")
    role = c.get("role")
    if role == "ACTIVE":
        fail = [w for w, bad in ((f"sample {n} < {MIN_COMPANIES} companies", n < MIN_COMPANIES),
                                 (f"BH p={p:.3g} > {Q}", p > Q), ("bootstrap interval includes 0", lo is None or lo <= 0))
                if bad]
        if fail:
            return Effect(name, NOT_SIGNIFICANT, 0.0, n, p, src, "; ".join(fail), d)
        return Effect(name, VALIDATED, float(d), n, p, src, "", d)
    status = {"EVIDENCE_ONLY": EVIDENCE_ONLY, "HOLDOUT_REQUIRED": HOLDOUT_REQUIRED}.get(role, NOT_SIGNIFICANT)
    return Effect(name, status, 0.0, n, p, src, c.get("why") or "", d)


def _primary(c: dict, sens: dict, src: str) -> Effect:
    n, p, d = c.get("companies"), c.get("t_p"), c.get("effect")
    if n is None or p is None or d is None:
        return Effect("INSIDER_BUY", NOT_MEASURED, 0.0, n, p, src, "no measured result")
    if p > Q:
        return Effect("INSIDER_BUY", NOT_SIGNIFICANT, 0.0, n, p, src, f"p={p:.3g}", d)
    weak = {k: v for k, v in sens.items() if (v.get("t_p") or 1.0) > Q}
    if weak:
        why = "; ".join(f"{k}: {100 * v['effect']:+.2f} pp, p={v['t_p']:.2g}" for k, v in sorted(weak.items()))
        return Effect("INSIDER_BUY", NOT_ROBUST, 0.0, n, p, src, "not robust to the sensitivity runs - " + why, d)
    return Effect("INSIDER_BUY", VALIDATED, float(d), n, p, src, "", d)


@lru_cache(maxsize=1)
def registry() -> dict:
    raw = json.loads(EVIDENCE_FILE.read_text())
    src = raw["evidence_version"]
    ins = raw["insider_buy"]
    cl = raw["insider_cluster"]["cluster_placebo_pre60_family"]
    tk = raw["tenk"]["placebo_negative"]
    return {
        "INSIDER_BUY": _primary(ins["primary"], ins["primary_sensitivity"], src),
        "INSIDER_BUY_BUCKETS": {k: _bucket(f"INSIDER_BUY:{k}", v, src) for k, v in ins["buckets"].items()},
        # matched placebo, one pre-registered test (60-day lookback, same-family contamination)
        "INSIDER_CLUSTER": Effect("INSIDER_CLUSTER", NOT_SIGNIFICANT, 0.0, cl["companies"], cl["sign_test_p"], src,
                                  f"{100 * cl['difference']:+.2f} pp, sign p={cl['sign_test_p']:.2g}", cl["difference"]),
        # measured from the 10-K date: a risk, never a weight -> veto in the engine
        "TENK_NEGATIVE_RECENT": Effect("TENK_NEGATIVE_RECENT", VETO, 0.0, tk["companies"], tk["sign_test_p"], src,
                                       f"NEGATIVE 10-K change: {100 * tk['difference']:+.2f} pp vs matched control "
                                       f"(p={tk['sign_test_p']:.1g}); veto, never a positive weight", tk["difference"]),
        "v2_families": raw["v2_families"],
        "unmeasured": raw["not_significant_or_unmeasured"],
        "version": src,
    }
