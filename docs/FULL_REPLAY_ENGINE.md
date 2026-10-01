# FULL replay engine on the point-in-time tables (branch full-replay-engine)

Not deployed. No replay campaign was started. Inputs: production tables pit_facts / sec_filings /
pit_backfill_progress (migration 0016, backfilled 2026-10-01, docs/PIT_BACKFILL_RUNBOOK.md).

## What changed

| Family | Before | Now |
|---|---|---|
| CASH_RUNWAY | UNAVAILABLE (no filed-date cash / OCF) | `detect_cash_runway`: production formula (`metrics.compute`) on the production concepts at every moment a runway fact becomes public; production `derived_risks` severity (< 9 months HIGH, < 18 MEDIUM). Feeds `discovery_risks` (gate SEVERE) and the financing rule |
| FINANCING_ACTIONS | production corporate_actions (recent submissions block only; complete from 2026-05) | `app.replay.actions`: production `parse_submissions` + full-index mapping over the COMPLETE SEC history; every action public from its exact acceptance time, staged per step |
| DILUTION | shares_outstanding_facts (lazily filled), filing-date visibility | dei cover-page facts from pit_facts (same concept, every issuer) public from the filings' acceptance times |
| THESIS_INVALIDATION | PARTIAL (news thesis_changes since 2026-09-25) | SEC-only proxy (8-K items 1.03 / 3.01): coverage status `PROXY_VALIDATED` |

Insider forms 3/4/5 are excluded in SQL on every read (`app.pit.readers`): filings by their form, XBRL facts
by their own form AND by the form of the filing they cite. So the mixed rows of the first 1,468 companies
never reach the replay, and a fact citing an insider filing can never replace a real same-period share
count in DILUTION (ALT review #1 finding, fixed; regression tests in tests/test_full_replay_engine.py).

## Coverage rules (risk_history._pit_coverage)

COMPLETE only when the backfill verifiably holds EVERY issuer of the scope (pit_backfill_progress:
submissions `done`; XBRL `done` or `not_found` - the issuer never filed XBRL, production has no value
either) and the window lies inside the period the SEC source is complete for all filers:

- FINANCING_ACTIONS from 2003-01-01 (complete electronic history from 2001 + the 730-day shelf lookback),
  and only when the snapshot's actions come from sec_filings (`actions_source`; the engine downgrades a
  snapshot that stages production's table to PARTIAL);
- CASH_RUNWAY from 2012-07-01 (all filers tag XBRL for periods after 2011-06-15);
- DILUTION from 2013-09-04 (+ the 430-day comparison base);
- the `to` bound is the backfill date, never past the export horizon.

Why the progress table is trusted: `pit_backfill.store` writes an issuer's rows and its `done` / `not_found`
progress row in ONE transaction, so a status can never exist without its data (a failed issuer is `failed`).

An issuer whose rule is undecidable at some moment (no 300-430-day share pair yet, a non-negative OCF) is
not a gap: production has no value then either, and the replay reproduces that.

## THESIS proxy semantics

- Status `PROXY_VALIDATED` is never COMPLETE. Without `ReplayConfig.thesis_proxy` the family is not
  complete (comparability LIMITED, D_THESIS listed in `gates_not_applied`).
- With `thesis_proxy=True` the proxy 8-Ks are staged as thesis_changes THESIS_INVALIDATION rows at their
  acceptance time (production `thesis_invalidated` reads them). The best comparability is then
  `FULL_WITH_PROXY` (flag `RISK_HISTORY_COMPLETE_WITH_PROXY`, `gates_on_proxy` lists the exits that ran on
  the proxy). `campaign.holdout_verdict_inputs` reports these runs separately and never pools them with FULL.
- Proxy labels are never discovery risks (`active_risks` skips them).
- Pairs: two runs that differ only in `thesis_proxy` share `comparison_key()`; `campaign.proxy_pairs` lists
  completed pairs side by side. A default config keeps its old `config_hash`.
- Validation (production, read-only, 2026-10-01): vs clusters labelled BANKRUPTCY_RISK since 2025-11-03
  precision 0.869 (1,085 / 1,248 proxy 8-Ks), recall 0.888 (1,084 / 1,221); vs thesis_changes
  THESIS_INVALIDATION by cluster time recall 0.80 (192 / 240). Weaker than the news-text proxy of the
  2026-10-01 audit (F1 0.998), which needs 8-K text the database only holds since 2025-11.

## Runway: the two production mismatches (362 CASH_RUNWAY_RISK events)

- 0001482541: the annual OCF is a fiscal-year-change report (2025-06-07..2026-04-30, 327 days); the SEC CY
  frame production reads counts it, the old 335-day bound did not. `ANNUAL_DAYS` is now 300-395 (a 10-Q's
  nine-month year-to-date, ~273 days, stays out): 361 / 362 match within 1 %, no previous match lost.
  Conscious trade-off: any other 300-335-day annual report (stub years, fiscal-year changes) is now
  accepted as annual too - the same as the SEC CY frames production reads.
- 0001549084: production used the CY2025 frame (-11.8 M) because the CY2026 frame did not exist yet; the
  company changed its fiscal year and its latest annual OCF (Jun 2025 - May 2026) is +39 M. Same formula,
  production on stale data; the point-in-time value is the one that was public. Not changed.
- Faithful quirk: like production (`cash[-1]`, no age limit), a company that stopped tagging the
  production cash concept keeps its last value (seen on 0001860657).

## ADR market cap

Root cause: price of the listed security x dei:EntityCommonStockSharesOutstanding. For a depositary
programme the price is the ADS's and the count is the ORDINARY shares (1 ADS = N shares: TSM 5, LTM 2,000).
Not a currency or unit problem. One rule (`app.marketdata.market_cap.market_cap`) for live
`build_universe`, `market_cap_pit`, `get_market_cap` and the replay `build_universe`: a depositary
programme (Form F-6 family under the issuer's CIK, public by the moment) -> market cap unknown (NULL row,
so the gate never reads an older inflated row; unknown is already sized as the risky class). Live
freshness: F-6 forms map to a `DEPOSITARY_PROGRAM` corporate action in the submissions refresh. Production
impact (read-only): 200 issuers become unknown, including all 7 foreign issuers above 1 T$; the 9
remaining issuers above 1 T$ are US mega caps; 366 foreign filers without a programme are unchanged.
Separate anomaly, not addressed: BRRN (10-K, no programme) at 945 B$.
Follow-up (pre-existing, live only): `provider.shares_as_of` behind `get_market_cap` / `market_cap_pit` sees
share facts by filing DATE (a same-day filing counts at any hour); the replay's universe uses exact acceptance
times (`visibility.share_fact_visible`).

## Real-data check (read-only export of 8 issuers, 2023-01-01..2023-06-30)

413 corporate actions (all with an acceptance time), 311 share facts (all with one), 129 runway / 81
dilution / 50 proxy labels. Coverage: FINANCING_ACTIONS, DILUTION, CASH_RUNWAY COMPLETE; THESIS
PROXY_VALIDATED; GOING_CONCERN PARTIAL (foreign 20-F filers have no 10-K profile); FINANCING_TEXT
UNAVAILABLE (no EDGAR full-text scan in this export).

## Sprint 2 (from 1f34726): FINANCING_TEXT, GOING_CONCERN, ADR ratio

### EDGAR full-text store (app.jobs.fulltext_backfill, app.pit.fulltext)

- Queries: the 11 production TEXT_RISKS phrases on their production forms (one query per phrase and year covers
  every company, like `scan_text_risks`) + FOREIGN_GC_0..2: the going-concern wording ("our" / "the Company's" /
  "the Group's") on 20-F / 40-F. 2001-01-01..2026-09-30.
- Stored as FILES on the server (`/opt/pe-crosscheck/fulltext`: gz JSONL per key and year + a manifest): no
  production database change, no deploy. Read by `risk_source_export --efts-store`, whose scan status for
  [start - 365 days, end] becomes the FINANCING_TEXT / GOING_CONCERN text coverage.
- Exactness: EFTS page order is NOT stable (pilot: 21 of 5,886 documents shifted across pages of one query).
  Periods are split until one page (100) holds them; a single busy day is split by form, then paged until the
  union equals EFTS's total (DAY_PASSES). A year is stored only when its documents equal the total; otherwise
  `failed` (retried next run, MAX_ATTEMPTS 3), a day beyond EFTS's 10,000 window `truncated` - both make the
  coverage incomplete. Writes are atomic; re-runs skip done years; shared SEC budget mandatory; disk guard 8 GB.
- Text labels are public from the filing's exact acceptance time (sec_filings) when known (detector v2).

### GOING_CONCERN coverage (risk_history._gc_from_annual)

Production has two going-concern inputs: the full-text phrases (discovery risk, `scan_text_risks`) and the 10-K
classifier (`app.tenk`, forms 10-K / 10-K405 / 10-KT only; profiles exist since 2020-03, 31,302 ok, all current
classifier v3). The export now lists every annual report from sec_filings (the ground truth of what was filed):

- COMPLETE: the text scan covers the window AND every 10-K-family report of [start - 400 days, end] has a
  current-classifier profile. 20-F / 40-F reports are not a gap: production never classifies them.
- PROXY_VALIDATED: some 10-K-family report has no current profile (before 2020-03, `no_sections`, failed) - only
  the full-text proxy can decide it. Never COMPLETE.
- PARTIAL: the text scan or the annual-report scan is incomplete.

Equivalent path for 20-F / 40-F and unprofiled 10-Ks (`detect_annual_gc_proxy`): a report whose full text holds the
going-concern wording warns from its acceptance time until the next annual report. Staged only with
`ReplayConfig.gc_proxy` (as GOING_CONCERN risks); such a run is FULL_WITH_PROXY at best.

Validation (read-only, every current-classifier 10-K profile 2020-03..2026-09, 31,302 reports): precision 0.6125,
recall 0.8488, F1 0.7115, agreement 0.9219 (TP 3,014 / FP 1,907 / FN 537 / TN 25,844), stable by year (F1
0.65-0.74). Conservative bias: the wording also appears in auditor reports / notes the classifier does not read,
and hedged mentions are not removed. Much weaker than the THESIS proxy; 20-F / 40-F cannot be validated at all
(no production classifier) - the 10-K measurement is assumed to transfer.

Rule change: a run that STAGES a family's proxy reports that family under proxy even when it is COMPLETE (its
labels change decisions production could not have made). `ReplayConfig.proxies`, `comparison_key()` ignores both
proxy options; `campaign.proxy_pairs` groups the without / with-proxy variants of one input.

### ADR ratio (research only; production unchanged: NULL market cap stays)

Pilot (read-only, 12 ADR issuers, every F-6-family document, regex "each ADS represents N shares"):
TSM 5 (implied 2.3 T$), BSAC 400, BCH 200, AMX 20, SAP 1, EC 20 parse and look plausible. Not reliable:
- TM, HSBC, NVO: no ratio in any F-6 document text (0 of 4-5 parsed);
- LTM: the 2024 ratio change (F-6EF) does not parse - the last parsed ratio (1) gives 30 T$ again;
- BABA: ratio 8 parses (2022), but its dei share count already looks ADS-denominated -> 25.7 B$ (clearly wrong);
- AKTX: 2 -> 100 -> 2,000 -> 80,000 over time (ratio changes are real and frequent).
Design if pursued: ratio(issuer, t) = the ratio of the latest F-6-family filing public at t that states one,
used ONLY when (a) every F-6-family filing since the programme start parsed, (b) the dei count's unit is
confirmed ordinary shares (e.g. cross-checked against XBRL `dei:EntityCommonStockSharesOutstanding` vs the ADS
count in the 20-F cover), else NULL. Neither (a) nor (b) holds reliably in the pilot: not implemented.

## Remaining blockers for FULL

1. FINANCING_TEXT needs the untruncated EDGAR full-text scan (`--efts`, network) for the scope.
2. GOING_CONCERN needs current-classifier 10-K profiles for every issuer over the window (and has no
   answer for issuers that file 20-F / 40-F).
3. THESIS_INVALIDATION has no real history before 2026-09-25: FULL is impossible for earlier windows;
   FULL_WITH_PROXY is the ceiling.
