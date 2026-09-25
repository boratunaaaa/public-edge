# PUBLIC EDGE INDEPENDENT AUDIT

Date: 2026-09-25. Scope: repository, production database samples, Docker deployment and live
health/log verification. This is an adversarial audit, not an endorsement.

## CRITICAL

- Historical hit rate was statistically overstated: event rows were treated as independent even
  though 77,227 insider-buy events came from only 5,852 issuers, and the available 90-day return
  sample covered only about 326 issuers. Individual issuers contributed dozens of overlapping
  observations. Fixed with issuer-equal weighting, issuer-based minimum sample and a 95% interval.
- Missing/delisted prices were silently removed. Production currently has 567 mature comparable
  insider events with missing price/benchmark data. The live metric now fails closed and displays
  no percentage; production verification returned `available=False`.
- Historical news backfill selected today's universe and used latest stored XBRL revenue, creating
  survivorship and look-ahead leakage. Backfilled current-universe news is now excluded from hit
  rates; historical revenue fails closed until filing-effective fundamentals exist.

## HIGH

- Historical SEC backfill could replace filing-time symbols with today's ticker. Filing symbol is
  now preferred. Complete delisted/ticker-history coverage still requires a point-in-time vendor.
- 71 of 92 sampled 13D events were amendments with no/under-1pp ownership change. Future
  immaterial amendments are suppressed; legacy immaterial amendments are excluded from hit rate.
- Multi-signal rolling windows repeatedly emitted the same evidence episode (TSM had eight).
  Source-group episode dedupe and one representative component per group were added.
- Discovery treated supplier-dependency risk prose as positive scarcity evidence. Directional
  matching was corrected. SCALED companies and candidates with multiple severe financing risks
  can no longer be HIGH.
- Congress UI implied all Congress although Senate is disabled. UI/reports/Telegram now state
  `HOUSE DATA ONLY / SENATE UNAVAILABLE`.
- Routine/opportunistic implementation is a point-in-time CMP-inspired heuristic, not a full paper
  replication. It is now labelled explicitly and no longer makes an exaggerated academic claim.

## MEDIUM

- Form 4 codes and derivative/non-derivative separation are generally correct, but a multi-owner
  filing can still attach the primary reporting owner to all transaction rows. Related trusts and
  related-person identity dedupe are not sufficiently robust.
- News title-token clustering may miss semantically identical syndication with rewritten titles.
- Only ten material live news clusters existed; a requested 50-item material validation was not
  statistically possible. One hundred total clusters and all material clusters were reviewed.
- Government contract amount handling uses award amount and amendment dedupe, but public-parent /
  private-subsidiary attribution remains dependent on strict name mappings and needs human review.
- Discovery precision is not outcome-validated. Thirty random HIGH/MEDIUM and all 50 HIGH records
  were reviewed; mature/scaled and financing-risk false positives were found and capped.

## LOW

- Docker Compose warns that Buildx is absent; builds complete correctly but without Bake.
- A harmless `/opt/public-edge/audit-final-sync/` staging directory was created during transfer.
  It was not removed because deletion was not authorized.

## BACKTEST

- look-ahead: first-tradeable entry logic is correct for pre-market, session and after-hours SEC
  acceptance times. Historical-news financial leakage was fixed.
- survivorship: not solved at the data-source level. Metrics now block on missing mature samples
  and exclude current-universe historical news.
- delisted handling: Yahoo is insufficient; affected count is exposed and percentage suppressed.
- timestamp integrity: SEC acceptance is interpreted in America/New_York; date-only disclosures
  enter on the next session. News retains publication/update/first-seen fields.
- benchmark: SPY and security use matching entry sessions; a missing benchmark blocks the sample.

## MODULE FINDINGS

- FORM 4: P/S/A/M/F/G/D/C classification and table separation passed review; multi-owner identity
  resolution remains open.
- CONGRESS: backtest uses filing/public date, not transaction date; House-only labelling fixed.
- CONTRACTS: obligated/award value is not intentionally presented as potential ceiling; mapping
  ambiguity remains an operational risk.
- NEWS: leakage fixed; semantic syndication and limited labelled sample remain open.
- DISCOVERY: 2,817 companies, current 50 HIGH/327 MEDIUM/2,440 LOW. MP, ABAT, FLY, MGTX, LPTH,
  EOSE, UUUU and AXTI were individually inspected. New caps apply on the next scheduled rescore.
- MICROCAP: severe dilution/going-concern risk now prevents HIGH discovery status and remains
  visible in Telegram. Full cash-runway accuracy depends on timely filings.
- MULTI-SIGNAL: derived signals no longer count as independent source groups; rolling duplicates
  fixed prospectively. Stored legacy duplicates were not deleted.
- TELEGRAM: “Alım puanı” and rate-derived GÜÇLÜ/ORTA/ZAYIF verdicts removed. Historical hit rate is
  descriptive, issuer-weighted, interval-labelled and blocked when biased.
- DEVOPS: five services healthy; DB 713 MB; disk 72% used; log rotation 20 MB x3; secrets mode 600;
  DB not publicly exposed; API bound to localhost/Tailscale; no active legacy crypto process.

## TESTS

- BEFORE: 183 tests.
- AFTER: 189 tests locally (25 DB tests skipped); 188/188 passed against isolated Postgres before
  the final additional predicate unit test. Production import/query and health verification passed.

## BUGS FIXED

Issuer correlation, confidence interval, missing-data fail-closed behavior, historical-news leakage,
historical ticker replacement, 13D amendment duplication, multi-signal episode duplication,
directionless supplier keywords, discovery risk/stage caps, House-only labelling and misleading
Telegram score terminology.

## UNRESOLVED

Point-in-time delisted/security-master data; multi-owner Form 4 identity resolution; robust trust /
related-person dedupe; semantic news-copy clustering; outcome-labelled discovery precision; a large
enough material-news validation set; legacy duplicate-row cleanup; full walk-forward model selection.

## MOST IMPORTANT FINDING

The former historical hit rate was not an investable performance statistic. Correlated events and
silent missing-price exclusion could materially inflate it. The production UI now refuses to show
the rate in the currently affected insider sample, but the underlying history is not repaired.

## DO NOT TRUST YET

Historical hit-rate percentages until point-in-time delisted coverage is complete; historical news
performance; discovery “precision”; stored legacy 13D/multi-signal event counts; any claim that the
system proves investment alpha.

## SAFE TO USE

The system is suitable as a monitored research-event feed for raw SEC, official House and official
contract/news records, with source links and visible risk context. It is not yet safe as an
autonomous investment-ranking or proven-alpha system.
