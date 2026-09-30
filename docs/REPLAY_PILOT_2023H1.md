# Historical replay pilot - 2023-H1 (2026-09-30)

Goal: operational validation (coverage status, leakage, proposal / fill / exit, execution costs, risk gates,
determinism, runtime) - NOT an alpha claim. Code: historical-replay-v1 (9604811 + determinism fix).

## Setup
* Window 2023-01-03 .. 2023-06-30 (6 months). The window with its previous annual report (start - 400 d) is inside
  the 10-K backfill's rolling 5-year horizon, so GOING_CONCERN can be COMPLETE.
* Universe: 24 issuers, 12 sectors (2 each), discretionary insider buys in the window, $ADV >= 5 M and price >= 5 $
  (Nov-Dec 2022), clean 10-K history (no failed / unprocessed profile in reach), 10-K backfill completed, and every
  reconstructable risk family COMPLETE per issuer (chosen from 72 candidates, 57 qualified):
  ZD, WBD | KDP, MED | MTDR, OXY | TPL, TCBI | TNDM, GH | HTLD, ENVX | NWL, ALB | INVA, GOSS | UMH, ADC | FIS, RBA |
  VISN (CommScope, COMM in 2023 - resolved point in time) , LDOS | ED, NEE.
* Inputs exported READ ONLY from production (snapshot2.json sha256 a33d6e9a..., risk_src2.jsonl 29aab324...; SEC
  full-text scan, 11 phrase queries, not truncated); replay and reconstruction in a separate local database.

## Results
| item | result |
|---|---|
| status | COMPLETED, 372 steps, 310 events seen |
| leakage | 0 guard failures |
| risk coverage | GOING_CONCERN COMPLETE, FINANCING_TEXT COMPLETE; FINANCING_ACTIONS covered only from 2026-05-08 (SEC submissions "recent" list), DILUTION from 2023-05-05; CASH_RUNWAY UNAVAILABLE and THESIS_INVALIDATION PARTIAL by construction |
| comparability | **LIMITED** (RISK_HISTORY_PARTIAL); gates_not_applied: gate SEVERE_FINANCING_RISK, sizing HIGH_FINANCING_RISK cap, exit D_THESIS thesis invalidation |
| proposals | 29 (all approved, research book); 281 later events rejected as DUPLICATE (same issuer within the duplicate window) |
| orders / fills | 145 BUY + 68 SELL filled, 0 participation-capped, 77 positions open at the end |
| exits | 57 TIME, 11 STOP |
| execution cost | $312.91 on $60,120 traded = 52 bps; LIQUID 144 fills avg 11.9 bps, NORMAL 67 avg 25.6 bps, ILLIQUID 2 avg 100.5 bps |
| risk labels staged | HIGH_FINANCING_RISK 1 (one proposal capped at 1 %), ATM_OFFERING HIGH 2, FREQUENT_CAPITAL_RAISES HIGH 2, DILUTION 3, MATERIAL_WEAKNESS 7, REVERSE_SPLIT 2, CONVERTIBLE_DEBT 3 |
| outcome (not a claim) | 68 closed: mean return -4.2 %, excess vs SPY -8.7 %, win rate 35 %; books -0.05 % .. +1.83 % |
| runtime | 162-184 s wall, ~45-49 s CPU, peak RSS ~77 MB, 16,736 capped price reads |

## Determinism
The first two runs made identical decisions (orders / fills / positions equal to 6-8 decimals) but NOT bit-identical
rows: `check_exits` read open positions without ORDER BY, and PostgreSQL returned them in physical order, which an
UPDATE changes - two same-day exit orders swapped ids, and float sums over positions differed in the 15th digit.
Fixed by an explicit id order on every multi-row position / trade read in the ledger and the replay metrics
(tests/test_ledger_determinism.py, mutation-checked). Two further runs are bit-identical: the full summary (except
wall time) and md5 of paper_orders, paper_trades, paper_positions, proposed_trades, paper_performance and
discovery_risks. The same unordered read exists in production's ledger (exit order ids only; no decision changes).

## Blockers for a FULL (comparable) replay
* CASH_RUNWAY: no filed-date cash / operating-cash-flow facts exist - needs a new XBRL facts ingestion.
* THESIS_INVALIDATION: news thesis_changes exist only since 2026-09-25.
* FINANCING_ACTIONS: SEC submissions "recent" list does not reach back for every issuer - needs the full
  submissions history (files beyond the recent block).
