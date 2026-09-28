# DATA SOURCE COST REPORT (PUBLIC EDGE V2)

2026-09-28. Principle: pay the minimum, but never at the cost of signal reliability. A paid source is only worth
buying after a free-data test shows the family it feeds is measurable **and** promising. So far, no family qualifies
(see PUBLIC_EDGE_SIGNAL_REGISTRY.csv), so **no paid source is recommended today**.

Reachability was tested on 2026-09-28 from two networks: the office network (TR) and the Hetzner server (FI).
The full per-dataset matrix is in `PUBLIC_EDGE_DATA_MATRIX.csv`, generated from `app/core/source_registry.py`.

## Current monthly cost: 0 USD for data
All production and V2 sources are free. The Hetzner server is the only running cost; it is outside this report.

## US

| dataset | provider | free tier | paid price | coverage / depth | latency | automation | value to PUBLIC EDGE |
|---|---|---|---|---|---|---|---|
| Form 4 / 13D / 144 / 10-K / 8-K | SEC EDGAR | free, fair access 10 req/s | - | 2001+ | minutes | allowed with a User-Agent | core (existing) |
| 13F holdings | SEC Form 13F data sets | free | - | 2013+ quarterly zips (~100 MB each) | 45 days + data-set release | allowed | candidate families (new position / accumulation) |
| Short-sale volume | FINRA CDN | free | - | 2009+ daily | same evening | allowed | candidate (short pressure) |
| Short interest | FINRA | free | - | twice monthly | ~8 business days | query API | planned |
| Fails-to-deliver | SEC | free | - | 2004+ half-monthly | ~2 weeks | allowed | candidate (FTD spike) |
| Clinical trials | ClinicalTrials.gov v2 | free | - | current snapshot | posting date | allowed | candidate (trial catalysts); **no version history API** |
| FDA approvals / recalls | openFDA | free (1,000 req/day without key; 120k with a free key) | - | decades | Drugs@FDA lag | allowed | candidate (FDA events) |
| Lobbying | lda.gov | free (15 req/min anonymous, 120 with a free key) | - | 1999+ | quarterly | allowed by terms | **BLOCKED**: Akamai 403 from both networks |
| Patents | USPTO ODP / PatentsView | free **API key** | - | 1976+ | weekly | allowed with a key | AUTH_REQUIRED (owner registers) |
| Macro vintages | FRED / ALFRED | free **API key** | - | decades | daily | allowed with a key | AUTH_REQUIRED; regime only |
| COT | CFTC Socrata | free | - | 1986+ | Friday | allowed | regime context |
| Identifiers | OpenFIGI | free (25 req/min) | enterprise | global | on demand | allowed | cross-check only (90 % agreement measured) |
| Historical delisted prices | Tiingo | free: 500 unique symbols per month (475 used in 2026-09) | Power ~30 USD/month (unlimited symbols) | 1962+ | EOD | allowed | **the only paid upgrade worth watching**: it would finish survivorship recovery in weeks instead of months. Buy only if a TRUSTED family is blocked solely by missing delisted prices |
| Quotes (bid/ask) | none configured | - | IBKR market data (with an account) | - | real time | - | needed later for live fill checks, not for research |

## Türkiye (BIST)

| dataset | provider | free tier | paid | notes |
|---|---|---|---|---|
| KAP disclosures (official API) | **MKK API Portal** | **free** (MKK: "uygulama ücretsizdir"): account, MKK approval, API key; 12 KAP data services | - | **The legitimate route.** The owner must register at apiportal.mkk.com.tr. The existing KAP parser (27 real pages) is reused once the key exists |
| KAP pages / list API | kap.org.tr | pages readable; list/JSON API behind a WAF | - | Not used at scale: no WAF bypass |
| BIST company list | kap.org.tr/tr/bist-sirketler | free public page | - | Implemented: MKK member OID = issuer id, 758 listed members |
| SPK bulletins | spk.gov.tr | free (PDF) | - | Implemented: share-issue approvals (bonus / rights) and enforcement codes |
| EOD prices | Borsa İstanbul Datastore / data vendors | - | licensed (quote needed) | Yahoo `.IS` is used for research only; it inserts zero-volume bars on holidays, which are filtered |
| Macro | TCMB EVDS | free **API key** | - | AUTH_REQUIRED |
| Foreign / institutional flow | MKK data products | aggregate reports free | per-stock licensed | planned |
| Public procurement | EKAP / KİK | interactive with captcha | - | BLOCKED (no bulk dataset) |

## Canada

| dataset | provider | free tier | paid | notes |
|---|---|---|---|---|
| Federal contracts | CanadaBuys open data | free (CSV, OGL-Canada) | - | Implemented; uses the publication date |
| Grants & contributions | open.canada.ca | free (2.3 GB CSV) | - | FORWARD_ONLY: records carry no publication date |
| Macro | Bank of Canada Valet | free, no key | - | Implemented |
| Short positions | CIRO | free on website | - | BLOCKED: 403 to automated clients from both networks |
| Insider trades | SEDI | viewing free | licensed SEDI data products (CDS / vendors) | BLOCKED: bot manager. **A paid feed is the only route; worth it only after CA price and identity data are trusted** |
| Filings (NI 43-101, MD&A) | SEDAR+ | viewing free | vendor feeds | BLOCKED: bot manager |
| Lobbying | lobbycanada.gc.ca | free open data | - | BLOCKED: 403 from both networks |
| Listings | TSX / TSXV directories | free | TMX Datalinx (paid) | Implemented (first-sight symbol periods) |

## Recommendation (minimum cost)
1. **Now, free, needs the owner:** register free API keys for the MKK API Portal (the TR primary route), FRED/ALFRED, USPTO ODP, and optionally openFDA and LDA. I did not create any account; credentials go in the root-only secret file, never in the repo.
2. **Paid, conditional:** Tiingo Power (~30 USD/month) only if a family becomes TRUSTED-blocked purely by missing delisted prices. SEDI and Borsa İstanbul licences only after the TR/CA free layers produce a measurable candidate.
