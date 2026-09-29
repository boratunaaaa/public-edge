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

## Codex audit fixes (on top of d860fe2)

| # | Finding | Fix |
|---|---|---|
| 1 | REVIEW events could still be priced; a batch selected before a repair could persist a return | `needs_review = false` in every lane's selection (`_conds`: main, vendor claim, retry, partial); `_fence`: `SELECT ... FOR UPDATE` + re-check (needs_review, superseded, issuer, ticker, lane status) right before pricing, in the same transaction that writes the returns |
| 2 | `event_type_stats`, `stats_overview`, `family_vs_placebo` counted REVIEW events | `NOT e.needs_review` there and in `insider_profile` (per-insider statistics). Already filtered: `research_v2`, `quality`. Not statistics consumers (unchanged): per-event API view, raw manifest export, sent-alert scorecard, the unrouted `_legacy_news_stats` |
| 3 | `(provider, symbol)` absence key; later intervals covered; recorded interval wider than the one checked | key = provider + identity + market + symbol + [window_start, window_end]. Identity: `SEC:<security_id>` when the event has one, else `CIK:<cik>\|SYM:<period>` (`CUR` for the issuer's current ticker, else the first sighting of the (cik, symbol) period covering the event date); no issuer or no covering period -> no negative cache. The recorded interval is exactly the one asked of the provider (`[fetch_from, need_end]`, narrower than the request when bars are partly cached); a hit requires the next provider interval to lie INSIDE it (earlier, later or overlapping requests are asked again) |
| 4 | strict tiers / per-batch schedule could starve partial / retry | aging key = moment the event became due minus its tier's head start (24 h / 12 h / 6 h / 0), a pure function of the row, so batch size and interrupted batches cannot reset it: a partial never waits more than 12 h behind new arrivals, a retry 18 h, a re-verification 24 h. The time budget is checked only after progress (>= 1 event per batch). Inside an hour of keys: issuer hash |
| 5 | calendar-day eligibility | `horizon_ready_at`: NYSE sessions (`app.marketdata.calendar`): 1d = next session's close; otherwise the last session on/before entry + h, never before target 00:00 UTC (engine's `as_of` rule); + 2 h EOD lag |
| 6 | two vendor workers could spend quota on one event; stale claims | `claim_vendor`: `FOR UPDATE SKIP LOCKED` + 20 min lease (`returns_next_at`) + unique token (`returns_lease`), chunks of 5; `_fence` re-validates token AND expiry under the row lock before any provider call; crash -> lease expires -> reclaimed with a new token, the stale worker is refused |
| 7 | malformed Tiingo list == no data | a non-empty list without one valid bar -> HttpError 502 (temporary, never cached); `[]` stays "no prices" |
| 8 | CUSIP normalization | `normalize_cusip`: trim, upper, CUSIP characters, exactly 9, check digit, not the 000000000 placeholder; only a valid value can match exactly (13D cover and registry compared on `upper(trim())`); invalid -> at most LOW + REVIEW (prefix) |
| 9 | blocking CREATE INDEX; restart after a failed build | `CREATE INDEX CONCURRENTLY` in `autocommit_block`; `lock_timeout = 5s` for the catalog-only DDL. Every step is idempotent (`ADD COLUMN IF NOT EXISTS`, `CREATE TABLE IF NOT EXISTS`, `DROP INDEX CONCURRENTLY IF EXISTS` before the build), so re-running `upgrade head` completes from any partial state (failure-injection test: INVALID index + committed column/table at 0012) |
| 10 | CHANGED kept the old ticker | repair sets `ticker` from the corrected CIK (NULL when it has none) |
| 11 | repair deleted returns before taking the row lock | repair locks the event row (`FOR UPDATE`, same order as the worker's fence) before deleting returns / changing identity: an in-flight worker's returns are committed first and then removed |
| 12 | overlength CUSIP truncated to a valid one | the complete normalized input must be a valid CUSIP for an exact match; otherwise prefix path only (LOW + REVIEW) |
| 13 | Tiingo bars with a date but no usable price | a bar needs a finite, positive numeric close; other fields sanitized to finite positive or NULL; a non-empty payload with no usable bar -> 502 (temporary) |

## Return eligibility vs validation eligibility (app/core/event_policy.py)

* **Return blocker** (`identity_unsafe_clause` / `is_identity_unsafe`, used by `_conds` for every lane and by `_fence`):
  `identity_confidence IN ('LOW','NONE')` OR `payload ? 'identity_review'` OR `missing_reason = 'IDENTITY_REVIEW'`.
  `needs_review` alone never blocks raw returns.
* **Validation blocker** (unchanged policy): `needs_review` for any reason - `event_type_stats`, `stats_overview`,
  `family_vs_placebo`, `insider_profile`, `research_v2`, `quality`, and the paper gate (`validation_excluded`).
* The 1,137 production events flagged by the 2026-09-28 V2 identity audit (`data_cleanup_audit` reason
  `TICKER_OWNED_BY_OTHER_ISSUER`: the event ticker belonged to another CIK at the event date) carry
  `identity_confidence = 'LOW'`, i.e. they are identity flags and stay return-excluded under this rule.

## Production dry run (read-only, 2026-09-29, `scripts/pe_return_repair.py`)

| item | rows | effect of --apply |
|---|---|---|
| partial_next_at | 45,381 | 45,379 deferred to their next horizon, 2 due now |
| vendor_retry | 2,020 (1,742 parser bug, 226 vendor 404, 52 other) - only 11 distinct symbols | -> `vendor_wait` |
| identity_13f | 94,382 checked: 94,089 SAME, 293 REVIEW (187 with returns), 0 CHANGED (before the audit fixes; re-run the dry run: CUSIP normalization can move a few more to REVIEW) | REVIEW: returns removed, `needs_review`, parked `no_data IDENTITY_REVIEW` |
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
3. rsync code, build, `run --rm migrate` (additive: 2 nullable columns, 1 table, 1 index built CONCURRENTLY). Worker /
   backfill keep running old code until recreated: the old code ignores the new column (NULL = legacy rule), so the
   migration is safe first. If the 5 s lock_timeout fires (a long transaction holds events), simply re-run.
   CONCURRENTLY waits for older transactions (it never blocks writers); a failed build leaves an INVALID index that
   the next run drops first.
4. Recreate `worker` and `backfill` (the return loop is restartable; its state is in `events`).
5. `pe_return_repair.py` dry run again -> compare with the numbers above -> `--apply` item by item
   (`partial_next_at`, `vendor_retry`, `ticker_13f`, `ca_grants_health`, then `identity_13f`).
6. `pe_smoke.py` (expects migration 0013) + watch: `backfill returns batch` logs (candidates / processed /
   vendor_queued), `vendor lane batch`, `rate_limits` for tiingo (<= 50/h), new-event coverage per hour.
7. Rollback: code rollback is enough (column/table unused by old code); `alembic downgrade 0012` only if required.
