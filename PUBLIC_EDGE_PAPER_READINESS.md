# PUBLIC EDGE PAPER READINESS

2026-09-28. Only virtual money. No real broker order exists anywhere in the code: `IbkrBroker.place_order` raises
`RealMoneyDisabled` for live accounts, and there is no flag to turn it on.

## Can internal paper trading run unattended for signal capture?

**YES**, once V2 is deployed. The deploy is prepared but not run, because production writes need the owner's
permission (see "Activation" below).

What runs unattended:
* **Signal capture.** Every live event gets `available_at`, source evidence and a market snapshot at or right after
  `available_at` (price, 20-day volume, point-in-time market cap, SPY, sector ETF). This builds the survivorship-safe
  forward dataset (V2 #72-73).
* **The ledger.** Daily at 22:00 UTC: pending fills at the first session after approval, exits (30/90/180 days,
  15 % stop, thesis invalidation), NAV vs SPY, and the daily PUBLIC EDGE PAPER Telegram report (only on days with
  activity).
* **Proposals.** Every 30 minutes, from fresh live events. **Human approval stays mandatory**: the Telegram code
  decides, and nothing fills without it.

## Current production state (read-only check)

| item | state |
|---|---|
| Accounts | 5 MAIN books (A 30d, B 90d, C 180d, D thesis, E 15 % stop + 90d), 10,000 USD each; alpha:paper last ran 2026-09-27 22:00 |
| Proposals ever | 2 (both research: 1 approved, 1 rejected) |
| Open positions | 0 |
| Research mode | off (`PAPER_RESEARCH_MODE=0`) |

**Why MAIN books stay empty:** a MAIN proposal requires a family that is TRUSTED **and** OUTPERFORMING. No family is
(see the signal registry). This is by design, not a failure.

## V2 changes

| area | before | V2 |
|---|---|---|
| Fill price | next open + flat slippage by market cap | next open + max(market-cap floor, half-spread + 10·√(participation/1 %)) bps |
| Liquidity | not modelled | LIQUID (≥ 20 M USD/day median) 5 bps; NORMAL (≥ 1 M) 25 bps; ILLIQUID 100 bps |
| Size | target % of NAV | capped at 5 % of the fill day's volume (ILLIQUID: 2 %), so large orders are partially filled |
| Zero-volume day | filled at open | never fills; the order waits |
| Research proposals | shared the MAIN books | separate RESEARCH books (same 5 strategies), reported separately and never aggregated |
| Forward timeline | proposal / decision times | + `signal_time` (event available_at) and `eligible_market_at` (first open after approval); fill, slippage, participation and ADV per order |

Tests: `tests/test_v2_paper.py` (liquidity classes, cap, zero-volume, separate books, report) plus the existing
split / dividend / stop tests updated to the fill model.

## Exact blockers

1. **Production deploy of V2 (migration 0012) needs the owner's permission.** The auto-mode classifier refused the
   production write. No other blocker exists for unattended signal capture.
2. **MAIN book activity needs a validated family.** None exists yet. The first candidates are INSIDER_BUY and
   INSIDER_CLUSTER: the placebo control shows a positive difference against the same companies' own history (see the
   MASTER REPORT). They are still BLOCKED on data quality (unresolved share 11-12 %).
3. Optional, the owner's decision: `PAPER_RESEARCH_MODE=1` would route not-yet-proven families into the RESEARCH
   books only. This is useful to start forward-validating insider-cluster signals now.

## Activation (after permission)
`scripts/pe-backup.sh pre-migration 0012` → rsync → build → `run --rm migrate` → recreate worker, backfill,
intelligence, api (runbook: docs/V2_DEPLOY_RUNBOOK.md).
