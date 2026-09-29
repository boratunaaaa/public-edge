# Return queue repair (migration 0013) — branch `v2-return-repair`

Not deployed. Base: production commit 4bd5e0a.

## Root causes fixed

| # | Symptom (production, 2026-09-29) | Root cause | Fix |
|---|---|---|---|
| A1 | 31,272 partial events re-priced in 20 h; new coverage fell to ~300-600 events/h | `partial` was due again `RECHECK_HOURS` (20 h) after every check, whether or not a new horizon could exist | `events.returns_next_at` = end of the next missing horizon + 1 day (`next_eligible_at`); an elapsed-but-missing horizon (data gap) is re-checked after 7 days |
| A2 | Priced subset = low-CIK issuers | `ORDER BY issuer_cik` | `queue_order()`: tier (1 never attempted, 2 partial due, 3 retry / young pending, 4 no_data re-verification), then `md5(issuer)` (deterministic, issuer-grouped, not CIK-correlated) |
| A3 | Main loop slept on Tiingo (72 s spacing, 50/h; 342 calls and 127 × 429 by 07:10 UTC) | The delisted-price vendor was called inline from `compute_pending` | Main lane never calls the vendor: `VendorDeferred` -> status `vendor_wait`; `compute_vendor_lane` runs in its own thread (`vendor_loop`), paced by the unchanged shared limiter |
| A4 | 1,742 events in `retry` with "string indices must be integers" | Tiingo answers some errors as HTTP 200 + JSON object; the parser iterated the object's keys | `parse_tiingo_prices`: object -> 404 (not found) / 429 (allocation, pushes the shared slot 15 min) / 502 (other) |
| A5 | Same 404 symbols requested every round | A provider 404 was not remembered | `price_absences` (provider, symbol): a 404 is not requested again for 7 days (Yahoo) / 30 days (vendor), return queue only; temporary failures are never recorded |
| B1 | Envista 29415F104 priced as ENvue (FEED) | `LIKE c9[:8] || '%'` matched a registry row with a corrupt check digit | exact 9-character match; prefix-only / name-contradicted matches -> LOW + `REVIEW_REQUIRED` -> `needs_review` (excluded from validation and paper) |
| B2 | Every 13F event had `ticker` NULL | `_ticker` read an in-memory map nobody loaded in the 13F process | `_ticker` loads the resolver (no-op while fresh); ticker is display-only, prices stay CIK-based |
| D | `ca_grants` HEALTHY after a failed, incomplete pass | `ca_grants_weekly` called only `record_run`; `consecutive_failures` stayed 0 | `health_failure` / `health_success` before `record_run` (same order as the worker) -> DEGRADED |

## Production dry run (read-only, 2026-09-29, `scripts/pe_return_repair.py`)

| item | rows | effect of --apply |
|---|---|---|
| partial_next_at | 45,381 | 45,379 deferred to their next horizon, 2 due now |
| vendor_retry | 2,020 (1,742 parser bug, 226 vendor 404, 52 other) - only 11 distinct symbols | -> `vendor_wait` |
| identity_13f | 94,382 checked: 94,089 SAME, 293 REVIEW (187 with returns), 0 CHANGED | REVIEW: returns removed, `needs_review`, parked `no_data IDENTITY_REVIEW` |
| ticker_13f | 94,382 NULL | 84,267 filled, 10,115 no current ticker |
| ca_grants_health | HEALTHY, no run recorded, initial load incomplete | -> DEGRADED |

REVIEW = filer typed a different issuer than the CUSIP's owner (Coursera -> HealthStream, "PROSHARES ULTRAPRO QQQ" on
Republic Services' CUSIP, a manager's own name in the issuer column) or a prefix-only CUSIP (Envista). Conservative:
~0.3 % of 13F events leave the validation population.

## security_id (C) - recommendation

* **Not required before validation.** Returns, research and validation key on `issuer_cik` (security master: CIK ->
  verified symbol); `security_id` is read nowhere in those paths. 0 of 319k events have it.
* **Should it be mandatory?** Yes for the multi-market contract (TR / CA have no CIK), not for US-only validation.
  Make it NOT NULL only for events created after the migration that introduces it, never retroactively in one step.
* **Who fills it:** `create_event` itself (one place) from `issuer_cik` + the event's `country`/`exchange` via
  `app.markets.master_db` (`global_securities`), so every generator gets it without per-generator code; TR/CA
  generators pass it explicitly (they already resolve `security_id` for dataset records).
* **Migration of existing rows:** a batched, idempotent backfill `security_id = global id of (country, issuer_cik)`
  only where the mapping is unique for the event's `available_at` date (period-bound, `security_symbol_periods`);
  ambiguous -> left NULL + counted, never guessed. Dry-run count first, like this repair.

## Safe deployment plan

1. Codex audit of this branch; fix findings.
2. `scripts/pe-backup.sh pre-migration 0013`.
3. rsync code, build, `run --rm migrate` (additive: 1 nullable column + index, 1 table). Worker/backfill keep running
   old code until recreated: the old code ignores the new column (NULL = legacy rule), so the migration is safe first.
4. Recreate `worker` and `backfill` (the return loop is restartable; its state is in `events`).
5. `pe_return_repair.py` dry run again -> compare with the numbers above -> `--apply` item by item
   (`partial_next_at`, `vendor_retry`, `ticker_13f`, `ca_grants_health`, then `identity_13f`).
6. `pe_smoke.py` (expects migration 0013) + watch: `backfill returns batch` logs (candidates / processed /
   vendor_queued), `vendor lane batch`, `rate_limits` for tiingo (<= 50/h), new-event coverage per hour.
7. Rollback: code rollback is enough (column/table unused by old code); `alembic downgrade 0012` only if required.
