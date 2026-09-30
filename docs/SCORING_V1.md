# Investment scoring (research-only)

`app/scoring/engine.py` gives every scanned live event an evidence-weighted estimate. It is **kept apart from the
hard gate** (`app.paper.proposals.evaluate_gate`) and changes nothing in it: the gate still decides alone whether a
proposal exists, its size and its book. The score is stored in `signal_scores` (migration 0015) together with the
gate's failed rules (`vetoes`). No MAIN book, no real money, no sizing change.

## Unit and formula (model `score.v2-evidence`, evidence `scoring_evidence_v3_night_2026-09-30`)
Probability points of beating SPY over 90 days, the event minus a matched control of the **same stock**
(company-level paired differences, `app.backtest.research_scoring`).

    score  = weight of the ONE validated cell the event falls into (never added across cells)
    vetoes = the gate's failed rules + scoring vetoes; any veto means "no action", the score is never lowered or raised

The score and the vetoes are stored side by side in `signal_scores`; neither changes the other. The score is never
read by the gate, the sizing or the ledger; the 10-K vetoes come from the same policy the gate enforces.

## Which evidence counts (`app/scoring/evidence.py`, `app/scoring/evidence_2026-09-30.json`)
The evidence file is rebuilt deterministically by `scripts/build_scoring_evidence_v3.py` from committed research
outputs only (`docs/scoring_evidence_v2_2026-09-30.json` = the pre-registered 14-hypothesis insider family, and the
2026-09-30 night run in `docs/night_2026-09-30/`). A test checks that the committed file equals the rebuild.
Significance is judged in the **pre-registered** family; a subgroup significant only in the larger exploratory night
family, or discovered in it, gets no weight until a pre-registered holdout replicates it. An `ACTIVE` label is
re-checked in code (>= 100 companies, BH p <= 0.05, bootstrap interval above 0) - a label never overrides the data.

| component | measured | role | weight |
|---|---|---|---|
| INSIDER_BUY: any 10 % owner (`ten_pct_any`) | +5.02 pp, 1,050 cos, BH 0.023, boot [+1.78, +8.42] | ACTIVE | **+5.02 pp** |
| INSIDER_BUY: 10 % owner only (`role_10pct_only`) | +7.52 pp, 642 cos, BH 0.011 | EVIDENCE_ONLY (subset of ten_pct_any, never stacked) | 0 |
| INSIDER_BUY (all discretionary buys) | +2.01 pp at 60-day lookback, but +0.50 pp (p 0.56) at 90 days and p 0.055 with any-type contamination | NOT_ROBUST | 0 |
| value 100k-1M $, holdings +10-50 % | +2.98 / +3.19 pp, pre-registered BH 0.057 / 0.051 | HOLDOUT_REQUIRED | 0 |
| serial buyer (3+ issuers in 2y), owner history negative, indirect only, first buy in 5y | +12.8 / +6.1 / +8.2 / +2.7 pp, night family only | HOLDOUT_REQUIRED | 0 |
| CEO / CFO / director / other officer / other value and increase buckets, 10b5-1, >= 3 transactions | not significant | NOT_SIGNIFICANT | 0 |
| INSIDER_CLUSTER (matched control) | -0.48 pp, p 0.82 | NOT_SIGNIFICANT | 0 |
| 13F new / accumulation / exit / decrease, trial results / registration | BH >= 0.052 | NOT_SIGNIFICANT | 0 |
| FDA, GOV_CONTRACT, CA contracts, FTD, short pressure | < 100 companies with 90-day returns | NOT_MEASURED | 0 |
| 13D, HOUSE_BUY, MULTI_SIGNAL / NEWS, DISCOVERY, ACTIVIST_CROWDING | not significant / no matched-control result | - | 0 |

## Vetoes (one policy: `app/core/tenk_policy.py`)
| veto | rule | evidence |
|---|---|---|
| `TENK_NEGATIVE_RECENT` | a 10-K change of the issuer with direction NEGATIVE | -6.71 pp vs matched control, 2,863 cos, sign p 5e-9 |
| `TENK_NEW_GOING_CONCERN` | a 10-K change of the issuer listing NEW_GOING_CONCERN, whatever its direction (measured by change type) | 28 % beat SPY, 27 % delisted in 180 d, 12 % bankrupt in 365 d |
| other gate rules | every failed rule of `evaluate_gate` (SEVERE_FINANCING_RISK, LOW_LIQUIDITY, NOT_A_LONG_SIGNAL ...) | unchanged |

Both 10-K vetoes are **hard vetoes in the paper gate** (`evaluate_gate` rule 7: no proposal, research mode never
overrides them) and the same `tenk_risk()` result is stored as the research score's vetoes, so the two can never
disagree. Point in time: only changes public **strictly before** the decision moment
(`coalesce(available_at, public_at) < decision`) and within the 90 days before it; a live decision ignores superseded
changes, a retry at an earlier moment counts them (its supersession state is not reconstructable). A 10-K change is
never a weight: a vetoed event keeps its evidence score, and any veto means no action.

The veto is re-checked after creation with the same `tenk_risk()`: at approval (`decide`: the proposal EXPIRES,
`decided_by = system:tenk_...`, no order is queued), in the expiry sweep (PENDING proposals), and at every BUY fill
at that fill's session open (the order is CANCELLED with the veto as note). SELL / exit orders are never checked.
Every TENK_CHANGE write takes the issuer's exclusive advisory lock; approvals and fills read under its shared lock
until they commit, so a 10-K write and a decision never interleave.

## Missing scores
A score is written after the gate. If that write fails (or the process dies in between), the next scan retries it for
events whose proposal already exists, at the proposal's creation time and with no vetoes (the gate passed then). A
retry does not rebuild the 10-K veto (supersession carries no timestamp, so the decision's state is not
reconstructable); its component records that instead of today's view.

## Point in time
Only events with `available_at <= scoring moment` count; the 10-K veto uses NEGATIVE changes public in the 90 days
before the scoring moment. Evidence is a versioned file: a new measurement is a new version,
never an in-place edit.

## What would change a weight
Only a new measured result (final validation, forward evidence) committed as a new evidence version. The gate, the
family POLICY and the MAIN books stay as they are until that validation says otherwise.
