"""Build app/scoring/evidence_2026-09-30.json (scoring evidence v3) from committed research outputs only - no database.

Sources (all read-only production runs, committed next to this script):
  docs/scoring_evidence_v2_2026-09-30.json   pre-registered insider family (14 hypotheses, BH) - decides significance
  docs/night_2026-09-30/expansion.json       night expansion: same numbers + derived subgroups + primary sensitivity
  docs/night_2026-09-30/research_v2_refresh.json   cluster placebo, 10-K outcome study
  docs/night_2026-09-30/families.json        V2 families (13F, trials, FDA ...)

Conservative policy (user decision 2026-09-30, see docs/SCORING_V1.md):
  * ACTIVE only: ten_pct_any (pre-registered, BH-significant, bootstrap interval excludes 0).
  * role_10pct_only: EVIDENCE_ONLY - a subset of ten_pct_any; kept as evidence, never stacked on the same event.
  * Every subgroup that reached significance only in the (larger, exploratory) night family, and every derived
    subgroup, is HOLDOUT_REQUIRED: weight 0 until a pre-registered holdout replicates it.
  * The primary INSIDER_BUY effect is judged by the engine against its sensitivity runs (not labelled here).

Usage: python scripts/build_scoring_evidence_v3.py   (rewrites the evidence file deterministically)"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NIGHT = ROOT / "docs" / "night_2026-09-30"
OUT = ROOT / "app" / "scoring" / "evidence_2026-09-30.json"

ACTIVE = {"ten_pct_any"}
EVIDENCE_ONLY = {"role_10pct_only": "subset of ten_pct_any (a 10 % owner with no officer/director role): kept as evidence, "
                                    "not stacked on an event that already carries the ten_pct_any weight"}


def _cell(c: dict, family: str) -> dict:
    keys = ("companies", "effect", "se", "ci95", "boot_ci95", "t_p", "sign_p", "bh_p")
    return {**{k: c.get(k) for k in keys}, "bh_family": family}


def build() -> dict:
    v2 = json.loads((ROOT / "docs" / "scoring_evidence_v2_2026-09-30.json").read_text())
    exp = json.loads((NIGHT / "expansion.json").read_text())["result"]
    rv2 = json.loads((NIGHT / "research_v2_refresh.json").read_text())["result"]
    fam = json.loads((NIGHT / "families.json").read_text())["result"]
    old = json.loads((ROOT / "app" / "scoring" / "evidence_2026-09-28.json").read_text())

    pre = v2["buckets"]
    night = exp["per_quarter"]["buckets"]
    buckets = {}
    for name in sorted(set(pre) | set(night)):
        if name in pre:  # significance from the pre-registered family; night numbers are identical where both exist
            cell = _cell(pre[name], "prereg_insider_14")
            if name in ACTIVE:
                role, why = "ACTIVE", ""
            elif name in EVIDENCE_ONLY:
                role, why = "EVIDENCE_ONLY", EVIDENCE_ONLY[name]
            elif (night.get(name, {}).get("bh_p") or 1) <= 0.05 and (cell["bh_p"] or 1) > 0.05:
                role, why = "HOLDOUT_REQUIRED", (f"BH {cell['bh_p']:.3f} in the pre-registered family; significant only in "
                                                 f"the larger exploratory night family (BH {night[name]['bh_p']:.3f})")
            else:
                role, why = "NOT_SIGNIFICANT", f"BH {cell['bh_p']:.3f} in the pre-registered family"
        else:  # derived night subgroup: discovered, not pre-registered
            cell = _cell(night[name], "night_expansion_28")
            sig = (cell["bh_p"] or 1) <= 0.05
            role = "HOLDOUT_REQUIRED" if sig else "NOT_SIGNIFICANT"
            why = ("discovered in the 2026-09-30 night expansion (not pre-registered): weight 0 until the pre-registered "
                   "holdout replicates it" if sig else f"BH {cell['bh_p']:.3f} in the night expansion family")
        buckets[name] = {**cell, "role": role, "why": why}

    tk = rv2["tenk_negative"]
    return {
        "evidence_version": "scoring_evidence_v3_night_2026-09-30",
        "unit": "probability points of beating SPY over 90 days, event minus matched same-stock control (company-equal)",
        "sources": ["docs/scoring_evidence_v2_2026-09-30.json", "docs/night_2026-09-30/expansion.json",
                    "docs/night_2026-09-30/research_v2_refresh.json", "docs/night_2026-09-30/families.json"],
        "insider_buy": {
            "primary": _cell(exp["per_quarter"]["primary"], "single"),
            "primary_sensitivity": {k: _cell(v, "single") for k, v in exp["primary_sensitivity"].items()},
            "buckets": buckets,
        },
        "insider_cluster": {k: v for k, v in rv2.items() if k.startswith("cluster_placebo")},
        "tenk": {
            "outcomes_from_10k_date": {k: v for k, v in tk.items() if k != "placebo_negative"},
            "placebo_negative": tk["placebo_negative"],
            "incremental_on_later_buy": v2["tenk_incremental"],
            "veto_rule": "a NEGATIVE 10-K change of the issuer public in the 90 days before the scoring moment is a veto "
                         "(score 0, never a positive weight); NEW_GOING_CONCERN among its change types is named separately",
        },
        "v2_families": fam,
        "not_significant_or_unmeasured": old["not_significant_or_unmeasured"],
    }


if __name__ == "__main__":
    OUT.write_text(json.dumps(build(), indent=1, sort_keys=True, default=str) + "\n")
    print("wrote", OUT.relative_to(ROOT))
