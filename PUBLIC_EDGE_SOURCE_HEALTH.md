# PUBLIC EDGE SOURCE HEALTH

Snapshot from production (`source_health`, read-only) on **2026-09-28 00:25 UTC**. Weekend: SEC and newswire
"last event" times date from Friday 2026-09-25/26, which is expected.

Production still runs code `c79960b`. V2 health fields (`health_status`, watermark, schema fingerprint, latency,
day status that separates source failure from true zero, shared quotas) arrive with migration 0012, which is not
deployed yet (see the MASTER REPORT, "Blockers"). Until then, `status` is the legacy value (WORKING / DEGRADED /
ERROR / DISABLED).

## Live sources (production)

| source | status | last success (UTC) | last event (UTC) | failures | watermark | quota | schema |
|---|---|---|---|---|---|---|---|
| sec_form4 | WORKING | 09-28 00:24 | 09-26 00:05 | 0 | EDGAR index position (job state) | SEC 3+3+2 req/s across containers (< 10) | canary: parse failures 7 / 64,522 in 7 days (0.0 %) |
| sec_13d | WORKING | 09-28 00:23 | 09-26 01:24 | 0 | idem | idem | canary OK |
| sec_form144 | WORKING | 09-28 00:23 | 09-25 23:27 | 0 | idem | idem | - |
| news_sec_8k | WORKING | 09-28 00:23 | 09-25 21:32 | 0 | - | SEC | canary OK |
| news_prnewswire | WORKING | 09-28 00:23 | 09-25 22:12 | 0 | - | - | - |
| news_businesswire | WORKING | 09-28 00:23 | - | 0 | - | - | - |
| news_gov | WORKING | 09-27 23:56 | - | 0 | - | - | - |
| news_openfda | WORKING | 09-27 20:56 | 09-24 22:55 | 0 | - | openFDA 1,000/day (no key) | - |
| news_yahoo_watchlist | WORKING | 09-27 22:59 | 09-27 11:40 | 0 | - | Yahoo (unofficial) | - |
| congress_house | WORKING | 09-27 23:55 | 09-24 21:15 | 0 | - | - | canary: newest filing 09-22 (3 business days) |
| usaspending | WORKING | 09-27 20:56 | 09-27 03:17 | 0 | 3-day window (V2: 5-day) | - | the last error is a disconnect; V2 adds minute-scale backoff and page checkpoints |
| trump_family | WORKING | 09-27 23:56 | - | 0 | - | - | - |
| trump_oge | **DEGRADED** | 09-27 22:56 | 09-25 18:06 | 1 | - | - | ConnectTimeout (transient) |
| telegram | WORKING | 09-27 20:55 | - | 0 | - | 1 msg/s | - |
| discovery_engine | WORKING | 09-27 10:04 | - | 0 | - | - | the stored `last_error` (CursorResult.distinct) is **stale**: it is not in the last 48 h of logs, and success does not clear the text |
| alpha:paper / tenk / insider_profiles | WORKING | 09-27 | - | 0 | - | - | - |
| Tiingo (price recovery) | quota | - | - | - | 475 / 500 unique symbols used in 2026-09 | 50/h, 1,000/day, 500 symbols/month | no requests on 09-27 (quota nearly exhausted for the month) |

## Disabled / blocked (known, not alerts)

| source | reason |
|---|---|
| congress_senate | efdsearch.senate.gov: 403 (Akamai) |
| news_dod_contracts, news_fda_press | 403 (Akamai); covered by USAspending / openFDA |
| news_globenewswire | connection timeout from the server |
| news_tier2_media | no licensed feed |
| senate_lda (V2) | lda.gov: 403 from both tested networks |
| ciro_short, ca_lobbying (V2) | 403 from both tested networks |
| sedi, SEDAR+ (V2) | bot manager |
| kap_live (V2) | KAP list API behind a WAF; official route = MKK API Portal (key) |
| uspto_patents, fred_alfred (V2) | AUTH_REQUIRED (free API key) |

## V2 sources ready (tested on real samples; start after deploy)
finra_short_volume, sec_ftd, us_clinical_trials, us_openfda, us_cftc_cot, ca_canadabuys, ca_boc_valet,
tr_spk_bulletin (+ TR company list), 13F data sets (backfill container). Their reachability from the production
server was checked on 2026-09-28: FINRA 200, openFDA 200, SPK 200, KAP company list 200, CanadaBuys 206.
