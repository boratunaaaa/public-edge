# PUBLIC EDGE GLOBAL MARKET EXPANSION REPORT

Night of 2026-09-25/26. Branch `global-markets` (git worktree `~/ClaudeWork/public-edge-global`), isolated dev
environment `/opt/public-edge-global-dev` + dev database `pe_global_dev` in the test Postgres container.
**No production deploy, no production migration, no production restart, no Telegram, no paper/live trade.**

## Verdict

| market | status | ready to deploy |
|---|---|---|
| AU (ASX) | production-ready at **headline level** (bodies blocked by ASX terms) | **YES** (headline adapter) |
| TR (BIST / KAP) | strong **beta**: robust parser on 27 real disclosures, access is the blocker | **NO** |
| CA (TSX/TSXV/CSE) | **blocked for insiders** (SEDI/SEDAR+ bot wall); news-release beta | **NO** |

## TR - Türkiye / BIST
* **Source access** (from the production server, no bypass):
  * KAP disclosure pages `kap.org.tr/tr/Bildirim/<id>`: served, server-side rendered, structured.
  * KAP `robots.txt`, JSON list API and some paths: WAF block (HTTP 666 "150207-PPE13") or timeouts -> **PARTIAL**.
  * No official list feed is reachable, so discovery = sequential disclosure ids (~1,600/day) -> fragile, WAF risk at scale.
  * SPK bulletins reachable (parser not built); MKK has no per-company feed (licensed); TEFAS rejects the server.
* **Working parsers** (`app/sources/tr/kap.py`): header (company, all BIST codes, Istanbul timestamp, bildirim tipi, form,
  summary, related companies, update/correction flags), explanation text, structured **pay geri alım** table (Turkish number
  format), **pay alım/satım** by major holders/insiders (price, nominal, amount, ownership %), **VBTS** (short-sale / margin
  ban, gross settlement, single price, order restrictions, dates), **devre kesici**, bedelli / tahsisli / bedelsiz
  sermaye artırımı (+DILUTION), birleşme (incl. intra-group and "görüşmeler sonlandı"), pay devri / satın alma / zorunlu
  pay alım teklifi, divestment, management change, dividend / no-dividend, related-party transactions, credit ratings,
  contracts (government / export), capacity / factory / production line, incentives, financial reports.
  Skipped as administrative: debt instruments, funds, market making, articles of association, general-meeting admin.
* **Blocked**: KAP list API / robots (WAF), official "Pay Alım Satım Bildirimi" form values (only in the PDF attachment -
  left empty, never invented), SEDI-like insider feed does not exist publicly.
* **Sample events**: 276 real disclosures (1 full day + a 4-month spread) -> **96 canonical events** (dev DB + `data/global_samples/tr_canonical_events.jsonl`).
* **Fixtures**: 27 real pages. **Tests**: 36 KAP + TR parts of calendar/risk/integration.
* **Market-specific risk features** (`app/markets/tr_risk.py`, no score): VBTS active/count, gross settlement, credit ban,
  short-sale ban, order restrictions, circuit breakers, ceiling/floor runs, capital-raise frequency, large dilution,
  related-party disclosures, major holder/insider selling, low free float, low liquidity - all point-in-time with evidence.
* **Data providers**: Yahoo `.IS` works (2000+) but inserts **zero-volume flat bars on BIST holidays** (Kurban Bayramı 2026)
  -> filter `drop_synthetic_bars`; licensed Borsa İstanbul EOD / vendor adapters prepared (`KeyedProvider`, skip without key).

## CA - Canada
* **Source access**: SEDI and SEDAR+ answer a **Radware bot-manager** page to automated clients -> **BLOCKED**
  (not bypassed). Canada NewsWire news sitemap (title, timestamp with offset, keywords, stock tickers) and the TSX/TSXV
  company directories (2,281 + 1,426 issuers, robots crawl-delay 6 s honoured) -> **VERIFIED**. GlobeNewswire Canada RSS:
  20 items, company name only.
* **Working parsers**: sitemap (Canadian listings mapped to XTSE/XTSX/XCNQ, foreign listings kept apart), release-title
  classifier with critical-minerals taxonomy (discovery, resource, PEA/PFS/DFS, offtake, permit, production start,
  government funding, strategic investment, flow-through placements, shares-for-debt dilution), directory security master.
* **Insider coverage: NONE.** `sedi.py` holds the contract (open-market code 10 -> INSIDER_BUY/SELL) but no real SEDI record
  could be collected, so no insider parser is claimed as tested.
* **Sample events**: 377 releases (the sitemap covers ~2 days) -> **30 canonical events**. Fixtures: 5 sitemap pages +
  2 directories + RSS. Tests: 29.

## AU - Australia
* **Source access**: ASX daily announcement lists, ASX research API (markitdigital) and the listed-companies CSV ->
  **VERIFIED** (robots allows). Announcement **bodies (PDF)** sit behind an ASX page: "freely available for investors'
  private and personal use; commercial use requires the express written authority of ASX" -> not clicked through
  automatically; body parsing (Appendix 3Y amounts, Form 603/604 %) is **blocked pending the owner's decision**.
* **Working parsers**: list page (code, Sydney time -> UTC across AEST/AEDT, **price-sensitive flag**, idsId, pages),
  markit JSON, headline classifier: director interest 3Y/3X/3Z (direction honestly UNKNOWN at headline level),
  becoming / change / ceasing substantial holder (holder vs subject resolved - "for X" notices are lodged under the
  HOLDER's code), placement / entitlement / SPP / Appendix 3B (LOW confidence), drilling / resource / feasibility,
  offtake, production start, contracts (government), trading halt / suspension, schemes / takeovers, buy-backs,
  management changes, dividends, reports; follow-ups (cleansing notices, Appendix 4G) and negations are not events.
  Multi-ETF documents -> one event per security; notices lodged under two codes -> one event (fields merged).
* **Price-sensitive support**: carried into `CanonicalEvent.price_sensitive` and the core payload.
* **Sample events**: one full trading day (851 announcements) + 18 per-company feeds -> **551 canonical events**.
  Fixtures: 20 files. Tests: 31.

## Common core changes (minimal, US behaviour unchanged)
* `app/backtest/engine.py`: `entry_bar` / `compute_returns` accept an optional `calendar` (default NYSE).
* `app/http.py`: polite per-host rates for KAP, ASX, markitdigital, TSX (6 s), newswire, GlobeNewswire.
* New shared layer `app/markets/`: `schema.py` (CanonicalEvent + 38 families, raw payload kept), `calendars.py`
  (XNYS delegate, XIST, XTSE/XTSX/XCNQ, XASX), `security_master.py` (symbol periods, rename/reuse safe, ambiguous -> None),
  `prices.py`, `fingerprint.py` (schema fingerprints + source health), `collector.py`, `tr_risk.py`, `discovery_vocab.py`
  (keywords never reach HIGH), `news_sources.py`. Adapters only in `app/sources/{tr,ca,au}`; no US engine copied.
* Calendars verified against real trading days 2021-2026: TSX and ASX exact; BIST exact after adding the 2024-04-09
  arife closure and the Feb-2023 earthquake halt.

## Schema changes proposed (NOT applied)
`docs/proposed_migrations/0012_global_markets.py`: `events.country/exchange/security_id` + index (exchange, ticker, date),
`global_securities`, `global_symbol_periods`, `source_schema_state`. **Required before any second market goes live**:
several core queries key by ticker only (multi-signal, related events, dashboard) and tickers collide across exchanges
(BHP on ASX and as a NYSE ADR) - today protected only because foreign event types never map to US signal families
(test `test_us_multi_signal_is_not_affected_by_a_same_ticker_foreign_listing`).

## Tests
* Before: **357** (main). After: **488** in the dev environment (all passing, ruff clean) - **131 new**: calendars 21,
  ASX 31, KAP 36, Canada 29, core 25 (incl. source failure, WAF stop, rate limits, schema compatibility), integration 8.

## Server load estimate (US + TR + CA + AU live)
* CPU: parsing one day of all three markets = **~15 CPU-seconds/day** (measured, 1 vCPU cap). Negligible.
* RAM: collectors add ~50 MB to a process; measured peak 130 MB when holding a day of KAP pages in memory.
* Requests/hour: KAP ~70 avg (1 page per disclosure + ~10% id probes; capped at 0.5 req/s), ASX 4 (list every 15 min),
  newswire 6, TSX directory 2/day -> ~80/h added to today's US load.
* Network: KAP ~300 MB/day (171 KB pages), ASX ~115 MB/day (1.9 MB list page x 60) -> ~12 GB/month inbound.
* DB growth: TR ~550 events/day x ~4 KB (raw text) + AU ~550 x 0.65 KB + CA ~15 -> ~2.7 MB/day, **x2 because
  signal_features duplicates the payload** -> ~5.5 MB/day, ~170 MB/month (+indexes). Proposal: keep only features.
* Heavy backfill must stay out of live ingestion (see blockers: the 10-K backfill incident below).

## Production deployment risk
* **Found tonight, pre-existing (my 2026-09-25 10-K backfill): the production backfill container was OOM-killed
  ~7 times between 18:50 and 22:55 UTC** (1 GB limit; multi-MB 10-K HTML parsed in-process after the classifier-v3
  re-walk). Live ingestion (worker) was not affected; the backfill restarts itself and resumes. **Not fixed in
  production tonight (no restarts allowed).** Fix prepared on branch `fix-tenk-memory` (10-K extraction in a
  memory-capped child process, 358/358 tests) - deploy it first in the morning.
* Also seen in production logs (pre-existing, not touched): 13D backfill `StringDataRightTruncation` on a few filings,
  USAspending server disconnects.
* Global adapters themselves: low risk (isolated modules, no alerts: priority 1 < ALERT_MIN_PRIORITY, verified by test).

## Blockers
1. TR: KAP list/API behind WAF; production needs the licensed KAP data service (MKK) or an agreed access path.
2. AU: announcement bodies behind ASX terms (personal vs commercial use) - owner decision (and possibly ASX authority).
3. CA: SEDI / SEDAR+ bot manager - insider data needs an authorised feed (e.g. a licensed SEDI data product).
4. Core: exchange-aware keys (proposed migration 0012) before more than one market is live.

## Recommended deployment order
1. `fix-tenk-memory` (production stability) - then migration 0012 on a restored copy (dry-run) and deploy.
2. **AU headline adapter** (live list every 15 min; events stored, not alerted; backtest with the ASX calendar + `.AX` prices).
3. TR once the KAP access path is agreed (licensed feed), reusing the parser unchanged; CA news beta only after that.

**Ready to deploy: TR NO · CA NO · AU YES (headline level)**

## Most important finding
Access, not parsing, decides these markets: none of the three gives free, machine-readable **insider amounts**
(TR official form values sit in PDF attachments and KAP's list API is WAF-protected; Canada's SEDI is behind a bot
wall; ASX bodies are under personal-use terms). What is freely and reliably available - ASX headline + price-sensitive
flags, KAP disclosure pages, Canadian newswire sitemaps - is enough for event/risk features, but a TR insider signal
comparable to US Form 4 needs a licensed KAP feed. Second finding: free price data hides market traps (Yahoo's
zero-volume BIST holiday bars; cross-exchange ticker collisions like BHP) that would silently corrupt a backtest.

## Files
* Code: `app/markets/*`, `app/sources/{tr,ca,au}/*`, `scripts/global_sample_ingest.py`, `scripts/global_load_sim.py`
* Fixtures: `tests/fixtures/global/{tr,ca,au,prices}` (real public data only)
* Samples: `data/global_samples/{tr,ca,au}_canonical_events.jsonl` (source URL, publication time, raw + normalized)
* Proposal: `docs/proposed_migrations/0012_global_markets.py` (not applied)
