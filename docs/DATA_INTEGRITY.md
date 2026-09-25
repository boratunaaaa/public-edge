# PUBLIC EDGE — data integrity design (2026-09-25)

Rule: a metric that could overstate performance is not shown. Missing data is counted and explained,
never silently dropped.

## 1. Point-in-time security master (`app/securitymaster`)

`SecurityMasterProvider` → `SecSecurityMaster`:

| function | source |
|---|---|
| `resolve_security_at_date(symbol, d)` | symbol intervals from the issuer's own Form 3/4/5 filings (`security_symbols`) |
| `resolve_ticker_at_date(cik, d)` | same, never today's ticker for a past date |
| `get_listing_period(cik)` | first filing-symbol date / listing file start → SEC delisting date |
| `get_delisting_date(cik)` | Form 25 / 25-NSE / 15 from the EDGAR quarterly full index |
| `get_corporate_actions(cik)` | full index + submissions JSON: 8-K 1.03 bankruptcy, 5.01 change in control, 2.01, 3.01 listing deficiency, 3.02 unregistered sale; S-3, 424B, DEFM14A, SC 14D9 |
| `get_historical_price(cik, …)` | price series chosen **by CIK** (see below) |

Price identity: the same CIK's current exchange ticker (vendors keep a renamed security's history under
its current symbol); a historical symbol only when no other CIK owns it today, and its series must end
around the documented delisting (a series that keeps trading belongs to someone else → `TICKER_REUSED`).
Every computed return stores `price_symbol` + `price_symbol_basis`; returns computed before this change
were re-verified (kept only when the security master resolves the same symbol).

Delisted securities: returns exit at the last traded price when the horizon runs past the SEC delisting
date (`exit_basis = delisting_terminal`, CRSP-style). Without prices, performance delistings (bankruptcy,
exchange compliance) inside the horizon count as **losses** (Shumway 1997 delisting bias); mergers and
unknown cases stay **unresolved**.

### Provider evaluation (tested from the production server)

| provider | delisted | ticker history | adjusted / splits / dividends | limits / cost | verdict |
|---|---|---|---|---|---|
| SEC EDGAR (full index, submissions, Form 3/4/5 datasets) | delisting dates + reasons (Form 25, 8-K) | filing-time symbols | – (no prices) | free, 10 req/s | **backbone** |
| Yahoo chart | **no** (SIVB, TWTR, ATVI, FRC → 404) | current symbol keeps renamed history | split + dividend adjusted | unofficial, free | current listings only |
| Tiingo | listing file (no key): start/end incl. delisted (TWTR, ATVI; not SIVB/FRC/BBBY); prices need a key | via listing periods | adjusted | free key: 50 req/h, 1000/day | listing file used; prices **optional** (`TIINGO_API_KEY`) |
| Alpha Vantage LISTING_STATUS | delisted list + dates | no | – | free key needed (demo key = documented date only) | not wired (needs an account) |
| Stooq | – | – | – | JavaScript bot challenge | **not bypassed** |
| Polygon / EODHD / FMP | delisted with paid tiers | yes | yes | paid / key | not selected (cost) |
| Nasdaq Data Link (Sharadar SEP/TICKERS) | yes | yes | yes | paid | best paid option if ever needed |

## 2. Historical hit rate (`app/backtest/quality.py`, `probability.py`)

* Every MATURE comparable event is WIN, LOSS or UNRESOLVED (LEFT JOIN — never only priced rows).
* Episodes: same issuer + event family inside a configurable window (`EPISODE_WINDOWS`, default 90 days;
  news 30, discovery 180). The first event's outcome is the episode's outcome.
* Weighting: company-equal (default display), event-level and sector-equal (SIC sector) reported.
* Counts: unique events / episodes / companies / quarters.
* `metrics_quality_status`: **TRUSTED** (unresolved ≤ 2 %, ≥ 98 % verified price identity, benchmark
  complete, ≥ 30 companies and episodes), **PARTIAL** (unresolved ≤ 10 %, dashboard only with
  worst/best-case bounds), **BLOCKED** (everything else). Percentages only when TRUSTED — Telegram,
  dashboard, API and event pages are all gated. Events whose returns are not computed yet count as
  unresolved, so a half-finished backtest can never look good.
* Walk-forward (`walkforward.py`): 3y train → 1y validation → 1y test, rolled yearly; thresholds chosen
  on realized train outcomes only; reported numbers are TEST windows only. Discovery cutoffs are
  NOT_EVALUABLE (historical discovery features were not stored point in time).

## 3. Insider identity (`app/signals/owners.py`)

* Form 4 rows carry no owner reference; a joint filing's rows belong to all filers. A row is attributed
  to one owner only when its ownership text / footnotes name exactly that owner, otherwise `joint`
  (owner_cik NULL) — never the first-listed owner. Direct/indirect and nature of ownership are stored.
* Beneficial-owner graph (`beneficial_owner_links`): joint filers (HIGH when one natural person + vehicles),
  trusts carrying an insider's full name (HIGH), family vehicles with a unique surname (MEDIUM).
* One buy event per beneficial owner per filing; clusters count **independent beneficial owners**.
* CMP-inspired heuristic (routine vs opportunistic) uses only trades FILED before the classified filing.

## 4. Legacy cleanup (`app/integrity/cleanup.py`)

Superseded + `canonical_id`, never deleted; every change in `data_cleanup_audit`; dry run first, then
apply, counts compared. Rules: 13D/A is a new episode only for ≥ 1 pp ownership move or a new activist
purpose; multi-signal = one canonical per ticker + source-group combination + rolling episode (chain
while occurrences are ≤ 30 days apart); multi-signals whose component for a source group was superseded
lose independence; joint Form 4 events re-derived; clusters re-counted by beneficial owner.

## 5. News

Exact URL → canonical URL → headline overlap → lead-paragraph MinHash → same ticker + 72 h → source
lineage. Story keeps canonical_story, first_published_at, first_seen_at, sources, syndicated_count.
Timestamp confidence HIGH/MEDIUM/LOW; LOW never enters a hit rate. Historical 8-K backfill uses the
point-in-time listed universe (filing-symbol history), point-in-time revenue (XBRL facts filed on/before
the story) and point-in-time market cap; the old current-universe load is excluded / rebuilt.

## 6. Other

* Government awards: mapping confidence VERIFIED / HIGH_CONFIDENCE / MEDIUM / LOW with source; MEDIUM
  and LOW never alert above priority 3 and never join a multi-signal.
* Discovery: HIGH requires real-world evidence (commercial revenue, binding contract, qualification,
  production ramp, government award, major customer, shipment, strong insider buying); sub-$1B
  financing-risk score (ATM, S-3, 424B, PIPE, convertibles, warrants, dilution, runway, going concern,
  reverse split, exchange compliance) with a visible HIGH_FINANCING_RISK label; validation dataset
  (`discovery_validation`, CLI `validation-*`).
* Senate: SENATE_UNAVAILABLE (official eFD 403; the public GitHub mirror stops in 2020 and is not a primary
  source). Congress data is labelled HOUSE ONLY everywhere.
* OGE: official REST index + PDFs (sha256, OCR text layer, per-row parse confidence, explicit tickers only).
