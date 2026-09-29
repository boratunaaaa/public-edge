# Historical replay / walk-forward simulation - V1 (branch `historical-replay-v1`)

Not deployed. Base: production commit 4bd5e0a. US only.

## Architecture

The production paper pipeline runs **unchanged** at simulated time:

`event -> identity check (point in time) -> create_proposal (production gate) -> decide (research auto-approval) ->
queue_buy -> fill_orders (production fill model) -> check_exits / time exits -> snapshot`

Only four things are swapped for the duration of a run:

| swapped | how | module |
|---|---|---|
| clock | `app.core.clock` (`SystemClock` in production, `ReplayClock` via `use_clock`) | `app/core/clock.py` |
| data | an isolated Postgres schema `replay_<id>` in a replay database; data is **staged** into it only once it was knowable | `app/replay/store.py`, `visibility.py` |
| prices | `ReplayPriceProvider` over the input snapshot, capped at the visible session | `visibility.py` |
| proposal code | deterministic per seed (production uses `secrets`) | `engine._patched` |

Step model `SESSION_3STEP`, per NYSE session D:

| step | time (ET) | phase | bars visible | what runs |
|---|---|---|---|---|
| PRE_OPEN | 09:00 | signal | up to D-1 | stage events `available_at <= now`, point-in-time reference data + universe, identity check, gate, approval |
| OPEN | 09:30 | execution | D (fill engine only) | `fill_orders(D)` |
| CLOSE | 16:00 + 2 h | signal | up to D | events of the session / after hours (enter at D+1), `expire`, `check_exits(D)`, time-exit fills, `snapshot(D)` |

Point-in-time reference data (re-staged every step): symbol periods observed up to the date (last_seen clipped), a
**registry built from those periods** (each issuer's most recently used symbol - never today's SEC ticker file),
security master delisting fields only once dated, corporate actions by action date, and a universe snapshot built
with the production formulas (`shares_as_of` on facts filed by then, `mcap_bucket`, 30-day $ volume for MICRO /
EARLY_SMALL like `build_universe`).

Isolation: the run's engine has `search_path = replay_<id>` only (the `public` schema is not reachable by name),
`ReplayStore` refuses the production database (host + database of `DATABASE_URL` / settings) and any non-`replay_`
schema before writing anything; every run has its own schema; metadata in `replay_meta.replay_runs`
(config, config hash, code commit, data snapshot hash, result, leakage log, `sealed` for holdout).

Campaigns (`app/replay/campaign.py`): `windows()` builds 3 / 6 month windows over many start dates, `Split` assigns
development / validation / holdout chronologically with a purge gap (windows straddling a boundary are not scheduled);
holdout runs are sealed and `tuning_results()` never returns them. `ReplayCampaign` is sequential in V1
(concurrency 1, max 2, parallel not implemented) and starts a run only when load <= 0.6 / CPU and >= 1.5 GB is free.

## Leakage guards (all FAIL the run - status `FAILED_LEAKAGE` - never silently clipped)

| guard | where |
|---|---|
| event with `available_at > now` in the schema | `check_state` after every step |
| price request past the simulated date | `ReplayPriceProvider` |
| price bar after the visible session stored | `check_state` |
| outcome rows (`event_returns`) during the run | `check_state` |
| future delisting / corporate action / universe row / symbol period | `check_state` |
| future regulatory / thesis label (`discovery_risks`, `thesis_changes`) | `check_state` |
| later review / supersession label on an event | `check_state` + snapshot refuses such fields |
| proposal before its event's `available_at` | `check_state` |
| fill before `not_before` or after the simulated date | `check_state` |
| event identity outside its symbol period | `identity_check` -> REVIEW (not traded, recorded) |

## Correctness fixes (commit 9fbc72e)

| finding | fix |
|---|---|
| zero-volume first bar -> order stuck forever | `fill_orders`: a BUY tries every session inside its validity (`PAPER_ORDER_VALID_SESSIONS`, default 5) and fills on the first with an opening trade; after that it is CANCELLED (`expired: ...`). Exits never expire but also skip sessions without an opening trade |
| liquidity gate only knew MICRO / EARLY_SMALL | root cause: `build_universe` fills `adv_dollar_30d` only for those buckets and the gate read NULL as LOW_LIQUIDITY. The gate now measures the same 30-session mean close x volume from PRIOR sessions of the resolved series when the snapshot has none (`adv_source`); the $1M policy (`PAPER_MIN_ADV_USD`, all proposals) is unchanged |
| fill used the fill day's total volume (close-time knowledge) | `fills.v3-prior-session-adv`: liquidity class, participation cap and impact use only the median volume of PRIOR sessions; the only same-session fact is whether an opening trade printed. No prior volume -> a BUY is not sized blind |
| renamed securities (QXO / SSNT) | `PriceIndex`: price identity = issuer + symbol period + date. A symbol resolves to its point-in-time owner, served from ONE stored series of that issuer (catalog `series`: the vendor's current-symbol series is unbounded, other stored symbols only for their period), restricted to that symbol period - reused tickers never splice issuers |
| no historical risk labels | `risk_history` per run: `RISK_HISTORY_COMPLETE / PARTIAL / UNAVAILABLE` from the source's label history start dates, the list of production decisions that could not be applied, and `comparability` FULL / LIMITED (stored in `replay_meta.replay_runs`); `holdout_verdict_inputs()` returns LIMITED holdout runs only as `limited_excluded` |
| reference data rebuilt every step | incremental: re-staged only when the date changes, only changed rows written; tested equal to a full rebuild |

## Historical risk reconstruction (design only - nothing is invented)

`discovery_risks` are derived from SEC filings whose acceptance time is known (S-3 / 424B / ATM programs, convertible
and warrant exhibits, 8-K 3.01 deficiency notices, 10-K going-concern / material-weakness language, reverse splits).
A reconstruction would re-run the SAME detectors over the historical filing index with `detected_at` = the filing's
acceptance timestamp (never today's run time), store the rows with provenance (accession, detector version) in a
separate history table, and publish `risk_coverage` = the first date from which every detector's inputs are complete.
Until that exists, pre-2026-09-24 replays are `RISK_HISTORY_UNAVAILABLE` / LIMITED.

## Point-in-time risk reconstruction (V1, `app/replay/risk_history.py`)

Production risk inputs and what the replay can rebuild (production database, 2026-09-29):

| family (production consumer) | production detector | its time anchor | reconstructable source | coverage (pilot scope) |
|---|---|---|---|---|
| GOING_CONCERN (gate SEVERE, financing score, D_THESIS) | `scan_text_risks`: EDGAR full-text phrases on 10-K/10-Q, `today-365..today`, `detected_at = run time` | run time (not PIT) | EDGAR full-text over a historical window (network) + 10-K profiles (`tenk_filings`, SEC acceptance, 2021+ complete, 3.2k older-classifier profiles skipped) | COMPLETE 2021-03-01..2021-06-30 |
| FINANCING_TEXT (ATM, registered direct, reverse split, min bid, convertible, warrants, weakness, late filing) | same scan | run time | EDGAR full-text, filing date, 365-day validity like production | COMPLETE same window |
| FINANCING_ACTIONS (shelf 730d, 424B x3 365d, 3.02, 3.01) | `financing._count` on `corporate_actions` | `date.today()` -> now the replay clock | `corporate_actions` filing dates (2004+) | COMPLETE from 2006-08-25 |
| DILUTION (share growth >= 20 %) | `derived_risks` on frames `fundamentals_quarterly` (no filed date) | run time | `shares_outstanding_facts` (filed, 2009+) with the production formula | COMPLETE from 2014-07-18 |
| CASH_RUNWAY (gate SEVERE when HIGH) | `derived_risks` from frames cash / OCF | run time | none in the database (no filed-date cash / OCF facts) | UNAVAILABLE |
| THESIS_INVALIDATION (D_THESIS) | `thesis.on_material_news` (tracked companies' news) + 10-K going concern events | news first-seen | news only since 2026-09-25; 10-K part from 2022 | PARTIAL |
| HIGH_FINANCING_RISK (size cap) | `financing.assess` | run time | recomputed in the replay with the production flags / weights / `is_high` on the point-in-time inputs above | follows its inputs |
| LOW_LIQUIDITY / NASDAQ_MIN_BID labels | `derived_risks` from price / ADV | run time | the gate already measures ADV point in time (prior sessions) | covered by the gate |

Every label: risk type, issuer, source document (accession) + URL, source kind, acceptance time when known,
`available_at` (acceptance, else end of the filing day in New York), `valid_from >= available_at`, `valid_to` (next
report / production 365-day window), detector + version, rule version (hash of the production rule table or the
10-K classifier version), confidence, `reconstructed_at`, run id. Stored in `risk_hist` of the REPLAY database
(production refused), idempotent (unique label key), restartable (per issuer checkpoint in the same transaction).

**FULL policy**: every family in `REQUIRED_FOR_FULL` (all six) COMPLETE over the replayed sessions. With CASH_RUNWAY
unavailable and THESIS_INVALIDATION partial, no historical replay can be FULL yet - it is recorded as LIMITED with
the production decisions that ran on missing history (`gates_not_applied`).

## Findings about the production pipeline at 4193e20 (items 1-3 and 5 fixed in 9fbc72e, 4 flagged per run)

1. **Zero-volume first bar -> order stuck forever**: `fill_orders` always prices the first bar on/after `not_before`;
   if that session had no volume the order stays PENDING and never fills on a later session.
2. **Liquidity gate only knows MICRO / EARLY_SMALL**: `build_universe` computes `adv_dollar_30d` only for those buckets,
   so every larger company fails `LOW_LIQUIDITY` in the live gate as well.
3. **Fill model uses the fill day's total volume** (known only at the close) for the participation cap - a mild
   intra-day look-ahead inside the execution model (not the decision layer).
4. **No history for risk labels**: `discovery_risks` / `universe_snapshots` exist only since 2026-09-24, so a replay
   of 2020-2021 sees no financing / dilution risk flags - the historical gate is more permissive than today's.
5. **Renamed securities**: price bars are cached under the vendor's current symbol (QXO) while the point-in-time
   registry says SSNT in 2021 -> the replay refuses (no universe row / reference price) instead of using today's ticker.
