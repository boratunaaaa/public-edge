# PUBLIC EDGE V2 MASTER REPORT

2026-09-28. Branch `v2` (worktree `~/ClaudeWork/public-edge-v2`, 18 local commits on top of `main` c79960b, never
pushed). **Production still runs c79960b.** The V2 production deploy (backup → migration 0012 → restart) was refused
by the Claude Code auto-mode classifier as a production write, so it waits for the owner's explicit permission.
Everything else in this report is built, tested (592 tests) and partly measured on production data (read-only).

## CURRENT SYSTEM (before V2)
US-only production on Hetzner (4 vCPU, 7.7 GB RAM, 75 GB disk):
* Sources: SEC Form 4, 13D (reporting persons repaired, classified ACTIVIST/INSIDER/CONTROL/PASSIVE/AMBIGUOUS),
  Form 144, 10-K change engine, House PTR, USAspending, Trump/OGE tracker, official news (8-K, PR Newswire,
  Business Wire, openFDA, gov).
* Analysis: Early Discovery, multi-signal, historical validation (TRUSTED/PARTIAL/BLOCKED, company-equal episodes,
  walk-forward), a security master with point-in-time symbols and delistings, free historical-price recovery
  (identity-gated), the corporate-action exit resolver (0009).
* Paper: 5 books, proposals with human approval.
* Size: 170,774 events; DB 3.67 GB; migrations through 0011.
* Global markets (TR/CA/AU) existed only on the `global-markets` development branch: parsers on real samples,
  never deployed.

## ARCHITECTURE CHANGES
* **One core, country adapters.** `app/core/` holds provenance, source_state, ratelimit, rawlake, manifest, records,
  bulk, snapshots, signal_registry and source_registry. `app/markets/` holds identity, master_db, calendars and the
  global security master. `app/sources/{us,tr,ca}` only fetch and parse. `global-markets` is merged into `v2`
  (1 conflict, resolved).
* **Global event contract (migration 0012).** Events gain country, exchange, security_id, issuer_id, event_family,
  accepted_at, first_seen_at, **available_at**, source_document_id, source_accession, parser_version,
  schema_version, identity/event confidence, needs_review, tradeability, supersedes_event_id, episode_id,
  feature_source_event_ids, updated_at.
  * `available_at` rule (`app/core/provenance.py`): exact timestamp; else the end of the public date in the market's
    time zone; live first-seen = observation time. transaction_date is never used.
  * The backtest now anchors entry on `available_at`. Existing rows are backfilled with the identical rule, so there
    is no behaviour change for them.
* **Provenance chain.** Derived events store `feature_source_event_ids` (multi-signal).
  `/api/v2/provenance/{id}` returns event → components → source URLs → market snapshot, and Telegram messages
  already carry a "Kaynak belge" link.
* **Source health v2.** `health_status` (HEALTHY / STALE / DEGRADED / BLOCKED / SCHEMA_CHANGED / AUTH_REQUIRED),
  watermark, response status, latency, schema fingerprint (a changed baseline is not trusted until accepted),
  parser version, license, plus a **per-day status** that separates SOURCE_UNAVAILABLE / API_QUOTA / AUTH_REQUIRED /
  LICENSE_BLOCK from TRUE_NO_EVENT.
* **Shared rate limits** (`rate_limits` table). One atomic Postgres reservation per request, shared by
  worker / backfill / intelligence, with daily and hourly quotas. A 429 pushes everyone's next slot out, and quota
  exhaustion raises `QuotaExhausted` (logged as API_QUOTA). Visible in `/api/v2/health`.
* **Raw lake.** `/data/raw/<country>/<source>/yyyy/mm/dd/<sha>.gz`: content-addressed (sha256 dedupe), atomic
  writes, retention classes HOT / COLD / EVIDENCE. HOT files expire; the row stays with an explicit empty path.
* **Manifests.** `dataset_manifests` records row count, first/last event, watermark, source health and a content
  hash; optional zstd Parquet export with atomic rename.
* **Queues.** LIVE_HIGH (worker fast loop, unchanged); DAILY_MEDIUM (worker slow thread: the new V2 sources);
  HISTORICAL_LOW (backfill container: V2 history, disk-guarded, idempotent, checkpointed). The 13F data sets are
  streamed from disk in two passes, and holdings are flushed in 20k chunks (peak RSS measured locally before the
  fix: 590 MB).
* **Other.** Entity graph (`entity_edges`, timestamped; a reviewed SUBSIDIARY_OF edge is required, never inferred);
  point-in-time generic records (`dataset_records`: new version = new row); hypothesis ledger (`hypothesis_log`).
* **USAspending stability.** Minute-scale backoff beyond the HTTP client's retries, a page checkpoint per month in
  the backfill, and a live window widened from 3 to 5 days.

## GITHUB IDEAS ADOPTED
| repo | concept | PUBLIC EDGE implementation |
|---|---|---|
| LuxAlgo/market-trackers | per-row provenance (URL, retrieved_at, parser, confidence), needsReview | events provenance columns; `dataset_records.parser_version / license_status / needs_review`; raw lake retrieved_at |
| LuxAlgo/market-trackers | manifest (row counts, freshness, per-source health, watermarks) | `app/core/manifest.py` + `dataset_manifests` + `/api/v2/manifests` |
| LuxAlgo/market-trackers | natural-key upserts, per-source watermarks, resumable sync | `app/core/bulk.upsert_rows`, `RunResult.watermark` → `source_health.watermark`, job-state checkpoints |
| LuxAlgo/market-trackers | canaries + format fingerprints | existing canaries + V2 `schema_fingerprint` / `expected_fingerprint` → SCHEMA_CHANGED |
| LuxAlgo/market-trackers | JSON + Parquet dumps, atomic writes | Parquet export with `.tmp` + rename; raw lake atomic writes |
| dgunning/edgartools | structured 13F / filing objects | 13F from SEC structured data sets (not HTML scraping); CUSIP identity via issuer covers |
| form4-style trackers | transaction-code filtering, filing (not trade) date | kept: open-market code P, `available_at` = acceptance |
| event-driven backtesters | entry strictly after information time | `check_entry` guard + `first_tradable_session(available_at)` |
| security-master projects | symbol validity intervals, reuse detection | `security_symbol_periods` + `resolve(symbol, date)` (unique or None) + `collisions()` |

(No code was copied. market-trackers was reviewed from its public README on 2026-09-28; the other entries reflect
the concepts from the earlier GITHUB_BENCHMARK work, not a fresh re-review.)

## GLOBAL SECURITY MASTER
* Tables `global_securities` and `security_symbol_periods`. A security id is never a ticker: `US:<CIK>:COM`,
  `TR:XIST:<code>` with the MKK member OID as issuer id, `CA:<MIC>:<symbol>`.
* Builders:
  * US: from the SEC security master (delisted CIKs included) and point-in-time symbol history.
  * TR: from KAP's public BIST company list (758 listed members, MKK OID).
  * CA: from the TSX / TSXV directories (first-sight periods; history before first sight is unknown and never
    assumed).
* Ticker reuse and cross-exchange collisions are tested: resolution between two listings returns None, and an
  overlapping vendor period makes the symbol ambiguous.
* **OpenFIGI measured** on 200 random CUSIPs from the SEC FTD file (2026-08b): 90.0 % agree with the SEC symbol,
  6.5 % no mapping, 3.0 % disagree (mostly preferred-share symbol conventions and one renamed issuer). So it serves
  as a cross-check, **never an identity authority**.
* Status: code and tests ready; populated at deploy.

## US DATA
| source | implemented | historical | live | events | blockers |
|---|---|---|---|---|---|
| 13F (SEC data sets) | yes (streamed parse, focused managers ≤150 positions, per-CUSIP aggregates, position changes) | 2 real quarters parsed (9,470 filings, 5,941 focused, 353k holdings, 38k CUSIP-quarters); 9 quarters planned | quarterly | INST_13F_NEW_POSITION, INST_13F_ACCUMULATION (tested on real Q1→Q2 2026) | deploy |
| FINRA short-sale volume | yes | 2024+ planned | daily | SHORT_PRESSURE (volume ≠ short interest, kept apart) | deploy |
| FINRA short interest | table ready | - | - | - | adapter not yet written |
| SEC FTD | yes (Last-Modified = available_at) | 2024+ planned | twice monthly | FTD_SPIKE | deploy |
| ClinicalTrials.gov | yes | current snapshot only; registration + results dates honest | 6-hourly diff | TRIAL_REGISTERED, RESULTS_POSTED, TRIAL_* (status forward-only) | no version-history API |
| openFDA | approvals (ORIG + efficacy supplements); recall parser | 2018+ planned | 12-hourly | FDA_APPROVAL, FDA_EFFICACY_SUPPLEMENT | CRLs not reliable |
| CFTC COT | yes (sector regime) | 2015+ planned | weekly | context only | - |
| Lobbying (LDA) | no | - | - | - | **403 (Akamai) from both tested networks** |
| Patents | no | - | - | - | **API key required** |
| FRED / ALFRED | no | - | - | - | **API key required** |
| Grants (USAspending) | registry slot | - | - | - | adapter extension pending |

## BIST DATA
| source | implemented | historical | live | events | blockers | licensing |
|---|---|---|---|---|---|---|
| KAP disclosure parser (global branch) | yes, 27 real pages, 36 tests | page archive | - | buyback, insider, VBTS, capital, contracts, capacity ... | list/JSON API behind a WAF (no bypass) | pages public; automation restricted |
| **MKK API Portal** (12 KAP data services) | not yet (the key is needed to read the schema) | per service | yes | reuse the KAP parser | **AUTH_REQUIRED: the owner registers; MKK approves** | free (MKK: "uygulama ücretsizdir") |
| BIST company list | **yes** | current | daily | security master | - | public page |
| SPK bulletins | **yes** (share-issue approvals with bonus/rights %, enforcement codes; company-level only, person data redacted) | 2026 list reachable | 6-hourly | TR_BONUS_ISSUE_APPROVAL, TR_RIGHTS_ISSUE_APPROVAL, TR_SPK_ENFORCEMENT | - | public |
| EOD prices | research only (Yahoo `.IS`, zero-volume holiday bars filtered) | - | - | - | licence for production | paid |
| TCMB EVDS | no | - | - | - | API key | free |
| EKAP, teşvik, TÜRKPATENT, EPDK, MAPEG | no | - | - | - | captcha / no bulk / unreachable | - |

## CANADA DATA
| source | implemented | historical | live | events | blockers | licensing |
|---|---|---|---|---|---|---|
| TSX / TSXV directory | yes | current | daily | security master | - | public |
| Newswire (mining taxonomy) | yes (global branch) | ~2 days | continuous | resource / PEA / PFS / DFS / offtake / permit ... | shallow history | public |
| CanadaBuys contract history | **yes** (publication date; contact data stripped) | 2009+ | monthly refresh | CA_GOV_CONTRACT_WIN / AMENDMENT | subsidiaries need reviewed edges (real example: NMG Matawinie → Nouveau Monde Graphite, a 470 M CAD graphite contract) | OGL |
| Grants & contributions | **yes, forward-only** (for-profit recipients only) | no publication date per record | quarterly | context | not point-in-time historically | OGL |
| Bank of Canada Valet | **yes** | decades | daily | macro context | - | BoC terms |
| CIRO short data | no | - | - | - | **403 from both networks** | public |
| SEDI / SEDAR+ | contract only | - | - | - | **bot manager: an authorised or paid feed is needed** | restricted |
| Federal lobbying | no | - | - | - | **403 from both networks** | OGL |

## DATA QUALITY
* **Point in time.**
  * One `available_at` rule for every event (backfilled for existing rows with the identical rule).
  * New datasets use publication stamps: HTTP Last-Modified for FINRA and FTD, the filing date for 13F, the posting
    date for trials, the publication date for CanadaBuys, the bulletin date for SPK, Friday release for COT.
  * Sources without a publication date (CA grants) are FORWARD_ONLY.
* **Identity.** Never fuzzy: exact normalized names over current **and former** names, CUSIPs from 13D covers
  cross-checked with the SEC registry, symbol + date through symbol periods. Unresolved records are kept with
  `needs_review` and produce no event.
* **Survivorship.** Delisted CIKs stay in the master, and the 13F / FTD / FINRA universes are the files' own, not
  today's list.
* **Delisting and corporate actions.** The 0009 resolver is unchanged. The extended taxonomy
  (CVR / collars / elections) stays COMPLEX_REVIEW_REQUIRED; the broader engine extension is not done in this build.
* **Existing families (production, read-only).** Every family has a company-equal SPY-beat rate below 50 %. This
  prompted the placebo control below.

## NEW SIGNAL FAMILIES
All 21 new families are declared contracts (`app/core/signal_registry.py`: entry, exit, benchmark, quality gate,
minimum sample, CI). Historical results are **not run**: the history ingests only after deploy. Each run will go
through `validation_v2` (events, companies, episodes, raw / median / mean excess, SPY and sector beat, Wilson CI,
missing-price and delisting rates, 60/40 walk-forward, p-value, BH over the whole ledger, regime split with ≥ 30
companies per bucket) plus the placebo control. Status per family: `PUBLIC_EDGE_SIGNAL_REGISTRY.csv`.

## EXISTING SIGNAL FAMILIES (revalidated on production, read-only)
90-day horizon, company-equal share of episodes beating SPY. The placebo is the same issuer's own stock entered
182 days **before** (pre) or **after** (post) the event, with no own event within 90 days.

| family | data quality | SPY beat 90d | median excess | vs own placebo (pre / post) | verdict |
|---|---|---|---|---|---|
| INSIDER_BUY | BLOCKED (12 % unresolved) | 41.9 % (4,460 cos) | -3.0 % | **+6.9 pp (p 9e-13) / +2.7 pp (p 0.002)** | below SPY; small positive residual vs same stock |
| INSIDER_CLUSTER | BLOCKED (11 %) | 41.6 % | -3.1 % | +10.2 pp (p 4e-13) / +2.0 pp (p 0.049) | residual does not survive multiple testing |
| MULTI_SIGNAL | PARTIAL | 36.9 % | -8.0 % | +8.1 pp (p 0.01) / +1.5 pp (ns) | timing effect only |
| HOUSE_BUY | PARTIAL | 42.4 % | -1.9 % | -4.9 pp / +2.8 pp (ns) | no edge |
| 10K_CHANGE | BLOCKED (23 %) | 34.7 % | -7.5 % | **-3.8 pp (p 0.004) / -3.6 pp (p 0.0005)** | consistently negative → **risk / avoid feature candidate** |
| 13D | BLOCKED (28 %) | 36.3 % | -6.7 % | +1.9 pp (ns) | context only (decision kept) |
| ACTIVIST_CROWDING | PARTIAL | 27.2 % | -19.7 % | - | all 858 packs superseded |
| NEWS | BLOCKED (21 %) | 33.0 % | -8.3 % | not run | underperforming |

## PAPER TRADING
* Production: 5 MAIN books, 0 open positions, 2 research proposals ever. MAIN stays empty by design (no TRUSTED +
  OUTPERFORMING family).
* V2: fill model (liquidity class, spread, √-impact, participation cap 5 % / 2 %, zero-volume waits), separate
  RESEARCH books, forward timeline (signal_time, eligible_market_at, fill, slippage, participation), daily
  PUBLIC EDGE PAPER report, MORNING BRIEF sections.
* Unattended signal capture: **YES after deploy** (`PUBLIC_EDGE_PAPER_READINESS.md`).

## IBKR
* Adapter readiness: interface complete (account, cash, positions, quote, preview, place, cancel, status) and a
  router (US/CA → IBKR, BIST → manual). Signal engines never import a broker (tested).
* Paper readiness: not connected (no credentials read or needed).
* **Real execution disabled:** `place_order` on a live account raises unconditionally, and every order needs an
  approval code.

## SERVER
| item | value |
|---|---|
| CPU | 4 vCPU; PUBLIC EDGE containers idle at < 1 % except backfill runs |
| RAM | 7.7 GB total, 2.8 GB available; containers: db 196 MB, backfill 289 MB, worker 76 MB, intelligence 65 MB, api 62 MB |
| Disk | **86 % used, 11 GB free.** The 10-K backfill already skips on LOW_DISK (< 10 GB). |
| DB | 3.67 GB (tenk_filings 920 MB, market_prices 863 MB, insider_transactions 474 MB) |
| V2 growth estimate | FINRA ~275 MB/yr; FTD ~115 MB/yr; 13F ~40 MB/quarter (focused) + ~5 MB aggregates; trials / FDA / CanadaBuys / SPK < 100 MB. The history steps self-stop below 9 GB free. |
| Network | SEC shared budget 8 req/s across containers (was 3+3+2 uncoordinated) |

## COST
* Current monthly data cost: **0 USD.**
* Optional: Tiingo Power (~30 USD/month), only if a TRUSTED family is blocked solely by delisted prices. SEDI and
  Borsa İstanbul licences only after the free layers show a measurable candidate.
* Incremental value of paid sources today: unproven. Details: `DATA_SOURCE_COST_REPORT.md`.

## BLOCKERS (genuine only)
1. **Production deploy of V2 needs the owner's permission** (the auto-mode classifier refused the production write
   and the backup-retention deletes). Runbook: `docs/V2_DEPLOY_RUNBOOK.md`.
2. **Disk:** 11 GB free. Freeing space needs owner decisions: 9 redundant PUBLIC EDGE dumps (~3.8 GB);
   `/root/crypto-bot/data` 16 GB, a disabled bot and not a PUBLIC EDGE asset.
3. **Free API keys only the owner can create:** MKK API Portal (the TR primary route), FRED/ALFRED, USPTO ODP
   (optional: openFDA, LDA).
4. **Access walls (no bypass):** lda.gov, CIRO, lobbycanada (403 from both networks); SEDI / SEDAR+ (bot manager);
   KAP list API (WAF); EKAP (captcha).

## SOURCE HEALTH
`PUBLIC_EDGE_SOURCE_HEALTH.md`: all production sources WORKING except trump_oge DEGRADED (1 transient timeout).
The discovery_engine error text is stale. Tiingo is at 475/500 symbols this month.

## TESTS
Before: 402 (main c79960b). After: **592 passing** (local Colima, Postgres 16).
* Additions: +131 from the global branch and +59 V2 tests (core, sources on real samples, families, paper,
  brokers, API, TR, migration drift).
* Adjusted: two paper tests updated to the fill model; two environment-dependent disk-guard tests made host
  independent.
* ruff is not in the image and was not run this session.

## MIGRATIONS
* Applied in production: 0009, 0010, 0011.
* Prepared: **0012_v2_global_core**. Rehearsed up / down / up on a production schema copy, and the drift test passes.
  **Not applied.**
* The global-markets proposal is absorbed into 0012 (its separate proposal file was removed).

## NEXT 30 DAYS (forward validation)
1. Deploy V2 (with permission). History ingests (FINRA / FTD 2024+, 9 quarters of 13F, openFDA 2018+, trials, COT);
   then `validation_v2` + placebo for the 8 new US families, every test logged with BH.
2. Pre-register the insider hypotheses before looking again:
   * a price-momentum-matched placebo, to separate buy-after-decline from information;
   * a size-matched benchmark (IWM / equal-weight) instead of SPY.
3. Turn 10K_CHANGE into a risk feature (position-size haircut) only if it holds out-of-sample.
4. Owner: register the MKK API Portal key → the TR KAP live adapter reuses the tested parser.
5. Forward snapshots accumulate from day 1. After 30 days, report signal-to-fill delay and slippage from the new
   order fields.

## READY NOW (after the single deploy step)
The V2 core (available_at, health, quotas, raw lake, manifests, APIs); the US FINRA / FTD / 13F / trials / openFDA /
COT adapters; the CA CanadaBuys / Valet / grants (forward); TR SPK + company list; the paper fill model, research
books, reports and snapshots; IBKR interface (disabled).

## NOT READY
* Live TR KAP (key).
* SEDI / SEDAR+ / CIRO / lobbying (access).
* Patents / FRED (keys).
* FINRA short interest and US grants (adapters not written).
* Extended corporate-action taxonomy.
* Validated alpha: none. **No family is paper-enabled for MAIN books.**

## MOST IMPORTANT FINDINGS
1. **The "below 50 % vs SPY" picture was mostly a universe effect.** The same small and micro caps beat SPY only
   ~31-45 % of the time in their own placebo windows. Measured against their own stock:
   * insider buys still keep a small, statistically solid edge: +2.7 pp in the conservative post-event control,
     3,676 companies;
   * most of the +6.9 pp pre-event gap is buy-after-decline timing;
   * cluster buys and multi-signal lose significance after the correction.
2. **10-K material changes are consistently negative** against the same stock (-3.6 to -3.8 pp, both directions
   significant). This is a risk-feature candidate, not a buy signal.
3. **Access, not parsing, limits TR and CA.** The legitimate TR route is free: the MKK API Portal (account + key).
   CA insider data needs a licensed feed. Four official sources return 403 to automated clients from both networks.
4. **Free vendor identifiers are not authorities.** OpenFIGI disagrees with SEC symbols for 3 % of CUSIPs and has
   no mapping for 6.5 %.
