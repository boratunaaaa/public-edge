# Investment scoring V1 (research-only)

`app/scoring/engine.py` gives every scanned live event an evidence-weighted estimate. It is **kept apart from the
hard gate** (`app.paper.proposals.evaluate_gate`) and changes nothing in it: the gate still decides alone whether a
proposal exists, its size and its book. The score is stored in `signal_scores` (migration 0015) together with the
gate's failed rules (`vetoes`). No MAIN book, no real money, no sizing change.

## Unit and formula
Probability points of beating SPY over 90 days, the event minus a matched control of the **same stock**
(`app.backtest.research_v2` matched placebo, `docs/v2_placebo_results_2026-09-28.jsonl`).

    score  = edge of the most specific VALIDATED cell the event falls into
           + VALIDATED risk adjustments that apply at the scoring moment
    ci_low = score - 1.96 * sqrt(sum of the components' squared standard errors)

When several validated cells match, the smallest effect is used. Components are added and their errors combined as
if independent (a V1 assumption, stated: the edge cell and the 10-K adjustment were measured on different events).

Standard errors are those IMPLIED by each measured test: the evidence is a paired, company-equal comparison tested
with a two-sided sign test, so se = |difference| / z(p) under its normal approximation (a component's lower bound
touches 0 exactly at p = 0.05). Marginal event / control rates are never used for the variance. Bucket membership
follows the research SQL exactly, including NULL semantics: a missing role flag never qualifies for a bucket. An
event that is not public at the scoring moment (or has no public time) scores 0.

## Which evidence counts (`app/scoring/evidence.py`, `evidence_2026-09-28.json`; sources
`docs/v2_research_2026-09-28_insider.json`, `docs/v2_placebo_results_2026-09-28.jsonl`)
A component gets a weight only if it has >= 100 companies and its p-value survives Benjamini-Hochberg (q 0.05) across
its test family (the 11 insider buckets are one family). Everything else weighs 0 and is shown with the reason.

| component | measured | weight |
|---|---|---|
| INSIDER_BUY (discretionary, 60-day prior-return matched control) | +1.89 pp, p 0.005, 3,277 cos | **+1.89 pp** |
| INSIDER_BUY: 10 % owner only (bucket) | +8.25 pp, p 8e-5, 620 cos (only bucket surviving BH) | **+8.25 pp cell** |
| INSIDER_BUY: CEO / CFO / director / other officer / value / holdings increase buckets | p >= 0.03, none survives BH | 0 |
| 10b5-1 pre-planned buys | outside the evidence (discretionary only) | 0 |
| INSIDER_CLUSTER (matched control) | p 0.27 | 0 |
| TENK_CHANGE in the 90 days before (risk adjustment) | -3.60 / -3.74 pp, p 0.0005 / 0.004 | **-3.60 pp** (smaller magnitude) |
| 13D, HOUSE_BUY, MULTI_SIGNAL | not significant | 0 |
| NEWS, GOV_CONTRACT, DISCOVERY, ACTIVIST_CROWDING | no matched-control result | 0 |
| insider history profiles | not validated as a predictor | 0 (context only) |
| all 21+ V2 families (13F, FDA, trials, FTD, short volume, TR, CA ...) | returns still filling, not measured | 0 |

`family_status()` lists every declared family with its status - the audit view of what is wired.

## Point in time
Only events with `available_at <= scoring moment` count; the 10-K adjustment uses changes public in the 90 days
before (the window the effect was measured over). Evidence is a versioned file: a new measurement is a new version,
never an in-place edit.

## What would change a weight
Only a new measured result (final validation, forward evidence) committed as a new evidence version. The gate, the
family POLICY and the MAIN books stay as they are until that validation says otherwise.
