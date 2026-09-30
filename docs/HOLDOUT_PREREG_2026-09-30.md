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
