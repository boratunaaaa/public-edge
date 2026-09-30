# Holdout pre-registration - insider subgroups (2026-09-30)

Written and committed BEFORE the holdout run (code: `app.backtest.research_scoring.run_holdout`). Read-only research;
no weight changes from this run alone.

## Hypotheses (one BH family, q 0.05)
Subgroups with weight 0 / `HOLDOUT_REQUIRED` in evidence v3:
`serial_3plus_issuers_2y`, `owner_history_negative`, `indirect_only`, `first_buy_same_issuer_5y`, `value_100k_1m`,
`incr_10_50pct`. H1 for each: effect > 0.

Reference cells (reported, not in the family, no decision): all discretionary buys (`primary`), `ten_pct_any`,
`role_10pct_only`.

## Design (identical to the night expansion)
- Events: INSIDER_OPEN_MARKET_BUY, discretionary (not 10b5-1), not superseded, not needs_review, priced 90-day return.
- Holdout: event public date 2024-07-01 .. 2026-06-30 (the last 2 years with a complete 90-day window).
  Also reported: the pre-holdout period (< 2024-07-01).
- Sample: first buy per issuer and calendar quarter; company-equal.
- Outcome: beat SPY over 90 calendar days, minus a matched control of the same stock (60-day prior-return match,
  >= 180 days away, same-family contamination excluded); company-level paired differences.
- Flags derived point in time from history strictly before each buy (all periods).
- Statistics: paired t (normal), company bootstrap B = 2000 (seed 20260933), BH across the 6 subgroups.

## Pass rule (per subgroup, holdout period)
REPLICATED only if >= 100 companies AND effect > 0 AND BH p <= 0.05 AND bootstrap 95 % lower bound > 0.
Fewer than 100 companies: INSUFFICIENT_SAMPLE. Anything else: NOT_REPLICATED. A REPLICATED subgroup still needs a new
evidence version and a user decision before any weight.

## Known limitation
The subgroups were found in the 2026-09-30 night run on the FULL history, which includes the holdout years, so this is
a temporal replication, not a clean out-of-sample test. The pre-holdout column is the discovery-period estimate.

## Result (run 2026-09-30, code 59cd781, `docs/holdout_2026-09-30.json`; run 1 on a37dcbe identical)
Holdout 2024-07-01..2026-06-29: 7,396 per-quarter events. No weight was changed.

| subgroup | holdout cos | effect | bootstrap 95 % | BH p | verdict | pre-holdout effect |
|---|---|---|---|---|---|---|
| serial_3plus_issuers_2y | 137 | +21.37 pp | [+11.33, +31.17] | 0.0001 | REPLICATED | +14.71 pp (128 cos) |
| owner_history_negative | 785 | +8.93 pp | [+4.60, +13.32] | 0.0002 | REPLICATED | +5.07 pp |
| indirect_only | 142 | +10.92 pp | [+1.06, +21.01] | 0.070 | NOT_REPLICATED | +3.59 pp (n.s.) |
| value_100k_1m | 1,096 | +3.84 pp | [+0.19, +7.45] | 0.070 | NOT_REPLICATED | +4.75 pp |
| incr_10_50pct | 887 | +3.99 pp | [-0.18, +8.30] | 0.074 | NOT_REPLICATED | +3.80 pp |
| first_buy_same_issuer_5y | 1,517 | +2.98 pp | [-0.28, +6.23] | 0.074 | NOT_REPLICATED | +3.26 pp |

Reference: all discretionary buys +3.91 pp in the holdout but +0.97 pp (p 0.34) before it - the base effect is
period-dependent, consistent with NOT_ROBUST. `ten_pct_any` +7.39 pp holdout (526 cos, [+2.15, +12.64]) and +4.37 pp
before (p 0.051): the active weight holds in both periods. `role_10pct_only` +5.98 / +6.32 pp.

REPLICATED is a necessary, not a sufficient, condition: serial buyer and owner-history-negative stay weight 0 until a
new evidence version and a user decision (limitation above: temporal replication, not a clean out-of-sample test).
