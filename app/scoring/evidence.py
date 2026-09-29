"""Evidence registry for scoring V1: every weight is a MEASURED effect, never a chosen number.

Unit: probability points of beating SPY over 90 days, event minus a matched control of the SAME stock (the placebo /
matched-placebo designs of app.backtest.research_v2). A component is VALIDATED only when

  * its sample has >= MIN_COMPANIES companies (the signal-contract minimum sample), and
  * its p-value survives Benjamini-Hochberg at Q across every hypothesis of its test family (the 11 insider buckets are
    one family; single pre-registered tests are their own family).

Anything else - not significant, not measured, or a V2 family whose history is still being priced - has weight 0
and is reported with the reason. Nothing is fitted or tuned here: the numbers come from the versioned evidence file."""
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

EVIDENCE_FILE = Path(__file__).with_name("evidence_2026-09-28.json")
MIN_COMPANIES = 100
Q = 0.05

VALIDATED, NOT_SIGNIFICANT, NOT_MEASURED = "VALIDATED", "NOT_SIGNIFICANT", "NOT_MEASURED"


@dataclass(frozen=True)
class Effect:
    name: str
    status: str
    effect: float  # probability points (0.019 = +1.9 pp); 0 unless VALIDATED
    companies: int | None
    p: float | None
    source: str
    why: str = ""

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


# No confidence interval in V1: the evidence file carries the company-equal mean difference and a SIGN-test p-value,
# which says nothing about the mean's variance. A valid bound needs the company-level paired differences (next
# evidence version); until then no interval is computed or stored.


def _effect(name: str, cell: dict, source: str, survives: bool) -> Effect:
    n, p, d = cell.get("companies"), cell.get("sign_test_p"), cell.get("difference")
    if n is None or p is None or d is None:
        return Effect(name, NOT_MEASURED, 0.0, n, p, source, "no measured result")
    if n < MIN_COMPANIES:
        return Effect(name, NOT_SIGNIFICANT, 0.0, n, p, source, f"sample {n} < {MIN_COMPANIES} companies")
    if not survives:
        return Effect(name, NOT_SIGNIFICANT, 0.0, n, p, source, f"p={p:.3g} does not survive BH q={Q}")
    return Effect(name, VALIDATED, float(d), n, p, source)


@lru_cache(maxsize=1)
def registry() -> dict:
    raw = json.loads(EVIDENCE_FILE.read_text())
    fam = raw["families"]
    src = raw["evidence_version"]
    ins = fam["INSIDER_BUY"]
    buckets = ins["buckets"]
    surv = bh_survivors({k: v["sign_test_p"] for k, v in buckets.items() if v.get("sign_test_p") is not None})
    single = lambda c: (c.get("sign_test_p") or 1.0) <= Q  # noqa: E731 - a one-hypothesis family
    out = {
        "INSIDER_BUY": _effect("INSIDER_BUY", ins["primary"], src, single(ins["primary"])),
        "INSIDER_BUY_BUCKETS": {k: _effect(f"INSIDER_BUY:{k}", v, src, k in surv) for k, v in buckets.items()},
        "INSIDER_CLUSTER": _effect("INSIDER_CLUSTER", fam["INSIDER_CLUSTER"]["primary"], src, single(fam["INSIDER_CLUSTER"]["primary"])),
    }
    # 10-K material change: measured NEGATIVE (both placebo directions), but on the 90 days starting AT the 10-K event.
    # Its incremental effect on a later signal's 90-day window (delayed entry, alongside an insider cell) was never
    # measured, so it is context with weight 0 until that design is measured.
    tk = fam["10K_NEGATIVE"]["primary"]
    out["TENK_CHANGE_RECENT"] = Effect("TENK_CHANGE_RECENT", NOT_MEASURED, 0.0, tk.get("companies"), tk.get("sign_test_p"),
                                       src, f"measured {100 * tk['difference']:+.2f} pp from the 10-K date; the incremental "
                                            "effect on a later signal's window is not measured")
    out["unmeasured"] = raw["not_significant_or_unmeasured"]
    out["version"] = src
    return out
