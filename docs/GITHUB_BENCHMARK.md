# PUBLIC EDGE — GitHub benchmark & gap analysis (2026-09-25)

Method: 78 GitHub searches (the requested queries plus synonyms) → 603 candidate repositories →
45 relevant repositories shallow-cloned and read (code, tests, licences); two large ones
(dgunning/edgartools, kadoa-org/congress-trading-monitor) read through the GitHub API. Nothing was
executed from a third-party repository except a FinBERT resource/quality measurement in a
throw-away, resource-capped container. Claims were checked against PUBLIC EDGE's production data
where possible (10b5-1 regex variants, House parse rate, 13D pack counts, Senate / KAP access).

Scores are technical 1–5 (not investment scores). NA = component absent.

| repo | ★ | last commit | lic | purpose | data | backtest | no-lookahead | arch | prod | tests | reuse | unique idea |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| LuxAlgo/market-trackers | 30 | 2026-09-24 | MIT (data CC0) | 18 primary-source US datasets, MCP | 5 | NA | 4 | 5 | 4 | 5 | 4 | per-source canaries (fingerprint, parse rate, freshness); provenance on every row |
| dgunning/edgartools | 2747 | 2026-09-22 | MIT | EDGAR library | 5 | NA | NA | 5 | 5 | 5 | 4 | 10b5-1 checkbox + typographic-variant regex with regression tests |
| bagoldbe/form4lab | 7 | 2026-07-09 | Apache-2.0 | Form 4 research + backtest + Alpaca paper | 3 | 3 | 2 | 4 | 3 | 4 | 3 | train/validate/locked-test harness, Deflated Sharpe; insider skill tiers |
| Leo-Y-Zhang/FilingsLab | 0 | 2026-09-22 | proprietary (no reuse) | disclosure-delay backtester | 2 | 3 | 3 | 3 | 3 | 4 | 1 | alpha decay vs execution delay; Monte-Carlo CIs |
| TanushReddy111/Defense_contracts_event_study | 1 | 2026-05-26 | MIT | contract event study | 3 | 4 | 2 | 2 | 1 | 1 | 2 | sector-ETF benchmark erased the SPY "alpha" |
| vvvarshith/government_contracts_alpha | 0 | 2026-05-06 | none | contract event study | 2 | 3 | 1 | 2 | 1 | 1 | 1 | market-model CAR, Bonferroni |
| nncl12345/investor-holdings | 1 | 2026-04-22 | none | 13D/13F activist tracker | 3 | NA | 3 | 3 | 2 | 2 | 1 | crowded activists (wolf pack) |
| dokson/hedge-fund-tracker | 69 | 2026-09-24 | NOASSERTION (CC-BY docs) | 13F/13D tracker + AI | 3 | NA | NA | 3 | 3 | 2 | 1 | fund-level portfolio diffs |
| wescules/insider-trading-analyzer | 8 | 2025-06-02 | MIT | Form 4/144 filters | 2 | NA | 1 | 1 | 1 | 1 | 1 | none (see "hazardous") |
| SahanThemiya/insider-trading-signal-classifier | 0 | 2026-08-05 | MIT | routine/noise/opportunistic ML | 3 | 2 | 1 | 3 | 1 | 3 | 1 | labels defined by future CAR |
| vcsniper7/sec_bot | 3 | 2026-08-23 | MIT | cluster-buy Telegram bot | 2 | NA | NA | 2 | 2 | 1 | 1 | — |
| sd3v/openinsiderData | 84 | 2026-08-30 | none | openinsider scraper | 2 | NA | NA | 2 | 2 | 1 | 1 | secondary source only |
| authorturker/sec-analyzer-ai | 8 | 2026-06-27 | MIT | SEC Telegram bot (EN/TR) | 2 | NA | NA | 3 | 3 | 4 | 1 | Turkish UI |
| jadchaar/sec-edgar-downloader | 716 | 2026-02-02 | MIT | filing downloader | 4 | NA | NA | 4 | 4 | 4 | 2 | — |
| alphanome-ai/sec-parser | 294 | 2026-06-25 | MIT | 10-K/10-Q section tree | 4 | NA | NA | 4 | 4 | 4 | 3 | semantic section parsing |
| GeoCodeCrafter/dd-to-signal | 8 | 2026-09-08 | MIT | 10-K year-over-year diff | 4 | 2 | 4 | 4 | 3 | 4 | 3 | "Lazy Prices" text-change signal |
| mecdesigns/finbert-signals | 1 | 2026-08-23 | none | FinBERT + TA alerts | 2 | 1 | 2 | 2 | 1 | 1 | 1 | — |
| ProsusAI/finBERT | 2237 | 2022-02-01 | Apache-2.0 | sentiment model | — | NA | NA | 3 | 3 | 2 | 2 | domain model (measured below) |
| HemantBK/Algorithmic-Trading-AI | 18 | 2026-03-06 | GPL-3.0 | FinBERT + TimeGPT | 2 | 1 | 1 | 2 | 1 | 1 | 0 (GPL) | — |
| flash131307/multi-agent-investment | 207 | 2026-03-26 | MIT | 3-agent research, deterministic hub | 2 | 1 | NA | 4 | 3 | 4 | 2 | deterministic fusion, graceful degradation |
| zhound420/swarm-trader | 58 | 2026-03-22 | none | 20 LLM agents + Alpaca | 2 | 1 | 1 | 3 | 2 | 2 | 0 | code-enforced risk manager |
| TheWillMundy/CapitolGains | 6 | 2025-11-29 | MIT | Senate/House access | 3 | NA | NA | 3 | 2 | 3 | 0 | uses Playwright against eFD (rejected) |
| DMulajkar/Quantgress | 3 | 2026-09-10 | MIT | Senate + House PTR scraper | 3 | NA | NA | 2 | 2 | 1 | 1 | official agreement handshake |
| jeremiak/us-senate-financial-disclosure-scraper | 23 | 2022-07-04 | none | Senate scraper | 2 | NA | NA | 2 | 1 | 1 | 0 | Puppeteer (rejected) |
| timothycarambat/senate-stock-watcher-data | 100 | 2021-03-16 | none | Senate data mirror | 2 | NA | NA | 1 | 1 | 1 | 0 | ends 2020 |
| crnicholson/capitol-api | 9 | 2026-07-05 | none | Congress trades API | 2 | NA | NA | 2 | 2 | 1 | 0 | — |
| QuantConnect/Lean.DataSource.QuiverQuantCongressTrading | 7 | 2026-06-18 | none | paid-data adapter | 3 | NA | NA | 3 | 3 | 2 | 0 | — |
| joelcedric2/congress-stock-trades-blueprint | 0 | 2026-06-14 | none | committee-overlap notebooks | 1 | NA | 1 | 1 | 1 | 1 | 0 | committee jurisdiction overlap |
| kdricha0602/microcap-momentum-scanner | 1 | 2026-09-22 | MIT | microcap momentum scanner | 2 | NA | NA | 2 | 2 | 1 | 1 | dilution-vehicle reject list |
| cemsinano/pykap | 63 | 2026-03-12 | MIT | KAP company wrapper | 3 | NA | NA | 3 | 2 | 2 | 2 | KAP company metadata |
| saidsurucu/borsaci | 283 | 2026-07-13 | MIT | BIST AI agent via Borsa MCP | 2 | 2 | NA | 3 | 2 | 1 | 1 | — |
| cahitihac/kap-notifier | 7 | 2025-03-04 | none | KAP new-disclosure notifier | 3 | NA | NA | 2 | 2 | 1 | 0 | `afterDisclosureIndex` incremental polling |
| enciyo/kap-tr-sdk | 3 | 2025-06-14 | none | KAP scraping SDK | 3 | NA | NA | 2 | 2 | 1 | 0 | KAP member / disclosure list endpoints |
| urazakgul/isyatirimhisse | 160 | 2026-02-21 | MIT | İş Yatırım prices / statements | 3 | NA | NA | 3 | 3 | 2 | 2 | BIST price + statement history |
| muhammedaksam/borsats | 6 | 2026-06-28 | MIT | TS lib for BIST/KAP | 3 | NA | NA | 3 | 2 | 2 | 1 | — |
| atillayurtseven/AlgoLab | 89 | 2026-01-23 | none | AlgoLab broker API client | 3 | NA | NA | 2 | 2 | 1 | 0 | broker integration |
| ajtgjmdjp/tdnet-disclosure-mcp | 5 | 2026-08-23 | Apache-2.0 | TDnet (Japan) disclosures | 3 | NA | NA | 3 | 3 | 2 | 1 | disclosure auto-categorisation |
| desiguel/asx-announce-analysis | 10 | 2016-12-15 | none | ASX announcement prediction | 2 | 2 | 2 | 1 | 1 | 1 | 0 | price-sensitive flag as label |
| leoliu0/cik-cusip-mapping | 181 | 2025-04-02 | none | CIK↔CUSIP from 13D/G | 4 | NA | NA | 2 | 2 | 1 | 1 | CUSIP history (PUBLIC EDGE already stores CUSIPs from 13D) |

Only the rows above were judged relevant. Discarded search hits: security-vulnerability trackers
("SEC-Tracker"), audio "UrbanSound8K", Matrix chat clients ("matriks"), ETH "Algolab" coursework,
and similar name collisions. Named families: FilingsLab = Leo-Y-Zhang/FilingsLab;
"SEC-Tracker" / "Government Contract Arbitrage" / "Swarm Trader" have no single canonical repo, the
closest real projects are listed.

## Verified findings (with evidence)

**Backtests (PUBLIC EDGE is more correct in every reviewed project):**
- form4lab `outcome_calculator`: base price = close on the *transaction* date (±5 days, searching
  forward and backward) — a follower cannot trade before the filing; missing prices silently skipped.
- form4lab `temporal_scorer`: insider skill uses outcomes of trades before `as_of`, but a 120-day
  outcome is not known until 120 days later → unmatured outcomes leak into the score.
- FilingsLab: executes at the close on/before `disclosure_date + delay` — a Form 4 accepted at 21:00
  gets that afternoon's close; synthetic GBM prices as fallback.
- insider-signal-classifier: "opportunistic" is defined by the trade's future 30-day CAR.
- insider-trading-analyzer: clusters by transaction date; self-join sums inflate values.
- contract studies: event date = award signing date (DoD awards are published ~90 days later).
- No reviewed project handles delisted securities, ticker reuse, episode overlap or same-company
  dominance. PUBLIC EDGE: next session open after acceptance, security master, episodes,
  company-equal weighting, TRUSTED gate.

**10b5-1:** edgartools' typographic-variant regex was compared on production data: 2,959 stored buy
Form 4 XMLs and the 2021Q2 dataset (3,300 buy filings): **0 disagreements**. Its "checkbox is
authoritative" rule would be *worse*: 5 filings have `aff10b5One=false` while the footnote states the
trades were made under a spouse's Rule 10b5-1 plan — PUBLIC EDGE's OR rule classifies them
correctly. → not adopted.

**FinBERT (ProsusAI/finbert, measured on this server, 2 CPU cap):** 1.22 GB RSS, 39 ms/headline,
13 s load, ~1.5 GB image layers. Feasible, but quality on filing headlines is poor: "Nasdaq minimum
bid price deficiency notice" → 76 % neutral; "FDA approves new drug application" → 66 % neutral.
PUBLIC EDGE's event-type classifier distinguishes both. → not adopted as default.

**Senate:** official eFD answers this server with Akamai "Access Denied" (403) even for a plain
declared-User-Agent GET. CapitolGains (Playwright) and jeremiak (Puppeteer) drive headless browsers
past bot protection → rejected (bot-detection circumvention). Quantgress / LuxAlgo use the official
agreement handshake (acceptable) but it cannot reach eFD from our IP. LuxAlgo's CC0 dump holds 97
Senate rows (2026-08-17 … 09-03, not updated since 09-07) → too sparse to call "Senate coverage".
Status stays SENATE_UNAVAILABLE.

**KAP / BIST:** `www.kap.org.tr/tr/api/disclosures?afterDisclosureIndex=` (incremental index, used by
kap-notifier) and `/tr/api/member/filter/`, `/tr/bist-sirketler` (company ↔ ticker) are the public
site endpoints the community uses; from this server KAP answers with its WAF error page (HTTP 666,
"erişim problemi"). Official machine access is MKK's licensed KAP data distribution. Prices /
statements: İş Yatırım endpoints (isyatirimhisse, MIT). Corporate actions: KAP "Sermaye Artırımı"
and dividend disclosures. → BIST adapter prepared as a plan only (see report); no integration.

## Adopted (own implementations; idea sources credited, no code copied)

| change | idea source (licence) | files |
|---|---|---|
| sector-ETF benchmark next to SPY (hit rate vs sector) | TanushReddy111/Defense_contracts_event_study (MIT) | `app/backtest/sector.py`, `jobs.py`, `quality.py`, migration 0005 |
| activist crowding ("wolf pack") event | nncl12345/investor-holdings (no licence → idea only); Brav, Dasgupta & Mathews 2022 | `app/signals/crowding.py` |
| multiple-testing guard in walk-forward (Bonferroni over thresholds tried) | bagoldbe/form4lab Deflated-Sharpe harness (Apache-2.0); vvvarshith (none) | `app/backtest/walkforward.py` |
| disclosure-delay decay table (gated) | Leo-Y-Zhang/FilingsLab (proprietary → idea only) | `app/api/main.py` |
| source canaries: stored-data freshness + SEC parse-failure rate | LuxAlgo/market-trackers (MIT) | `app/integrity/canary.py` |

## Rejected

10b5-1 regex/precedence (no measured gain, precedence worse) · FinBERT default (quality) · LLM agent
swarms / autonomous strategy evolution (overfitting, no audit trail) · Playwright/Puppeteer Senate
scrapers (bot-detection circumvention) · Senate / House Stock Watcher mirrors (stale, secondary) ·
openinsider scraping (secondary source) · transaction-date entry models · per-insider skill scores
from unmatured outcomes · synthetic price fallbacks · market-model CAR (needs a 120-day estimation
window per event; SPY + sector excess already cover the benchmark question) · KAP scraping from this
server (blocked; licensed channel required).

## Paper trading (design only — not activated)

SIGNAL → PROPOSED TRADE (event id, entry rule "next session open after approval", size, horizon,
exit rule; stored, never auto-sent) → HUMAN APPROVAL (dashboard on tailnet or Telegram reply with a
one-time code; approval timestamp is the public time for entry, so approval delay is part of the
measured result) → PAPER TRADE (internal ledger first; Alpaca paper adapter optional behind
`ALPACA_ENABLED=false`, `ALPACA_PAPER=true`, real-money path absent from the code). Reviewed
implementations: form4lab (single Alpaca adapter, paper default), FilingsLab (paper broker +
auto-trader behind a token), Swarm Trader (autonomous — rejected).
