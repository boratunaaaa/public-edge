# Free historical price recovery (2026-09-25)

Goal: price as many of the ~26,700 unresolved historical events as possible **without a paid provider**,
never at the cost of correctness. A wrong price is worse than no price.

## Provider evaluation (real requests from the production server, 2026-09-25)

| Provider | Delisted coverage | Depth | OHLCV | Adjusted | Identity | Limits | ToS / licence | Verdict |
|---|---|---|---|---|---|---|---|---|
| SEC (EDGAR) | identity, Form 25/15, 8-K items, symbols | 1996+ | no prices | – | CIK (authoritative) | 10 req/s | public | backbone of the identity gate, no prices |
| Yahoo chart | **none** (SIVB, ATVI, TWTR, FRC → 404) | long | full | raw + adj | symbol only | informal | informal | active securities only |
| Tiingo free (Starter) | many delisted/merged symbols (listing file covers ~14k of our unresolved events) | 2015+ used | full | raw + adj | vendor name (checked vs SEC) | 500 unique symbols/month, 50 req/h, 1000/day | free tier | **main recovery source**, quota-managed |
| FINSABER V2 (HF) | S&P 500 members incl. delisted, 2000-2025 | daily | full | raw + adj | **own CIK column** | download ~5 MB/year | "other" (users responsible for underlying terms) → internal research only | small but strong; 1 wrong CIK found (INFO) and rejected |
| Alpha Vantage free | LISTING_STATUS gives delisting dates; free tier serves **no delisted prices** | – | – | – | symbol + name | 25 req/day | free key | delisting-date cross-check only (key optional) |
| Wayback (Yahoo archive) | only sporadic intraday/5-day chart snapshots; no archived daily CSV for our symbols | – | – | – | none | polite CDX | archive terms | **rejected**: no reproducible daily series |
| WIKI Prices (Quandl) | frozen April 2018; needs a key | ≤ 2018 | full | yes | symbol | key | Nasdaq terms | **not applicable**: 99.8% of unresolved events are 2021-2026 |
| Eulerpool free | needs an account + key (401 without) | ? | ? | ? | ? | ? | ? | not tested beyond auth: account creation is the user's decision |
| Massive (ex-Polygon) free | needs an account + key (401 without); free tier = 2 years history, 5 req/min | 2 y | full | yes | ticker + reference API | 5/min | free tier | candidate for 2024-2026 events once the user creates a key |
| Stooq | JavaScript bot wall | – | – | – | – | – | – | not bypassed |

## Identity gate (app/pricesources/gate.py)
A series is used only when:
* identity evidence: dataset CIK, or vendor company name matching the SEC name (token overlap ≥ 0.5, < 0.2 = REJECTED)
* the symbol belongs to this CIK (SEC Form 3/4/5 symbols or its own events) and no other CIK used it inside the range
* it ends around the SEC delisting (trading > 30 days after = REJECTED, ending > 15 days before = LOW_CONFIDENCE,
  except when the SOURCE snapshot ends first: then no cash-out is assumed beyond the last bar)
* ≥ 90% trading-day completeness, no non-positive prices
* a second overlapping series (other provider or cached Yahoo) agrees on daily ADJUSTED RETURNS (median |Δ| ≤ 0.5%,
  ≤ 5% outlier days) - levels are never compared, so raw vs split-adjusted is not a false disagreement

Verdicts: TRUSTED, VALIDATED_FALLBACK (used) · LOW_CONFIDENCE, REJECTED (stored with reasons, never used).

## Corporate actions
* Ticker change (same CIK, contiguous, price-continuous): spliced into one series (e.g. COG → CTRA).
* Cash / stock merger: horizons past the documented delisting exit at the **final exchange trading price**
  (observed data, not assumed deal terms); the benchmark keeps running (cash earns nothing).
* Bankruptcy: exits at the last exchange price; OTC quotes after the SEC delisting are cut off (never spliced
  into a "continuous" series); a new equity issued later is never treated as the same security. Where the last
  exchange trade precedes the Form 25 by more than 15 days (trading halt, e.g. SIVB) the series stays
  LOW_CONFIDENCE and the hit-rate's Shumway rule (performance delisting = loss) applies instead.
* Merger terms are not parsed from proxy statements (not needed: the final trading price is observed).

## Provenance and cache
`price_sources` (provider, source_ref without credentials, retrieved_at, sha256, gzip raw response, verdict,
reasons, checks), `vendor_prices` (per source), `provider_raw` (Alpha Vantage CSV), `provider_usage` /
`provider_requests` (free-tier quota ledger). Recovered series are stored per CIK and never written into the
shared symbol cache (a reused ticker could otherwise mix two companies).

## Recompute
Accepting a series re-queues only that company's unresolved events (`returns_status = retry`).
