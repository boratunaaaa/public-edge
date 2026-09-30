# FULL replay: data gaps, audit, plan, estimates (2026-09-30)

A replay is FULL only when all six risk families are COMPLETE over its window. The 2023-H1 pilot was LIMITED because
of three data gaps. Audit = READ ONLY on production + a READ ONLY network pilot on the 24 pilot issuers. Nothing
heavy was started.

## 1. CASH_RUNWAY (+ debt, shares) - point-in-time XBRL
**Today:** `fundamental_facts` (filed date + accession) holds only Revenue (617 k facts, 4,142 issuers, since 2009).
Cash / OCF live only in `fundamentals_quarterly` (since 2024, no filed date) -> not point in time.
**Pilot (24 issuers):** SEC companyfacts has cash 24/24, OCF 24/24, debt 23/24, dei shares 24/24, all with filed date
+ accession, back to 2009. ~3.3 MB JSON per large-cap issuer (79 MB for 24).
**Code started:** `app/replay/pit_sources.py` - `parse_companyfacts` (CASH, DEBT, OCF, SHARES_DEI, SHARES_GAAP with
the disclosing filing), `visible` (exact acceptance time, else the day after the filing date), `runway_months`
(production formula `cash / (-annual OCF / 12)` over facts visible at the moment). Tests: tests/test_pit_sources.py.
**Night job (not started):** fetch companyfacts for the 8,988 security-master issuers, store the five families in a
new PIT facts table keyed (cik, family, concept, end, accession).
* requests: 8,988 at the shared SEC limit (~3 req/s) -> **~50-60 min**
* transfer: ~12-30 GB streamed, not stored (average issuer much smaller than the pilot's large caps)
* stored: ~600 facts / issuer -> ~5 M rows, **~0.8-1.0 GB** with indexes (free disk today ~12 GB)
**Then:** CASH_RUNWAY family in the risk reconstruction (coverage COMPLETE per issuer when both a cash and an annual
OCF fact are known for the window), SHARES_GAAP as the dilution fallback.

## 2. FINANCING_ACTIONS / DILUTION - complete SEC submissions history
**Today:** submissions fetched for all 8,988 issuers, but only the "recent" block (last ~1,000 filings); for active
filers that reaches back only 1-3 years (pilot: 22/24 issuers have older pages, 23,374 older filings; recent blocks
start 2020-2023). corporate_actions therefore thin out before ~2021 for large filers, so FINANCING_ACTIONS coverage
starts late (pilot: covered only from 2026-05-08 for one issuer).
**Pilot:** acceptanceDateTime present on 23,327 / 23,327 recent filings -> exact acceptance times for the XBRL facts.
**Code started:** `merge_submission_pages` (recent + filings.files pages, de-duplicated) and `acceptance_map`.
**Night job (not started):** re-fetch submissions (8,988) + every older page, re-derive corporate actions with the
production `parse_submissions`, store accession -> acceptance for the forms the replay uses.
* requests: 8,988 + ~3-5 k older pages -> **~1.0-1.5 h** at ~3 req/s
* transfer: ~2-4 GB, not stored
* stored: +~0.2-0.4 M corporate_actions rows (~60 MB), acceptance map ~3-4 M rows (**~0.5 GB** with indexes)
DILUTION: the remaining partial coverage comes from share-fact gaps; SHARES_GAAP from job 1 fills most of them.

## 3. THESIS_INVALIDATION - historical reconstruction (audit)
**Production rule:** `thesis.classify_change` -> THESIS_INVALIDATION iff the news classes contain BANKRUPTCY_RISK;
`news/classify.py` BANKRUPTCY_RISK = regex on headline + lead (chapter 11 | chapter 7 | bankruptcy | going concern |
forbearance | default notice | delist*), plus the 10-K going-concern path.
**Stored history:** thesis_changes since 2026-09-25; news_items sec_8k since 2025-11-03, Yahoo since 2025-09-29,
press wires since 2026-09-24. 8-K Item 1.03 (bankruptcy) in corporate_actions since 2004 (699 filings, 357 issuers).
**Reconstructable (point in time, SEC-sourced):** SEC full-text search finds 8-K / EX-99 documents with those phrases
back to 2001 (pilot: 2016-2026 counts >= 10,000 for most phrases market-wide -> must run PER REPLAY SCOPE, like the
existing FINANCING_TEXT scan). Plan: per scope, EFTS candidates -> fetch each hit's EX-99 -> apply the production
headline + lead extraction and `classify` -> label at the filing's acceptance time. Scope of 24-100 issuers x a
6-12 month window: ~10-30 EFTS queries + tens to low hundreds of documents -> minutes.
**NOT reconstructable:** non-SEC news (Yahoo, PR wires) before 2025-09 - no archive exists in the database; a
historical headline feed would be a new (paid / licensed) source. Therefore THESIS_INVALIDATION can only be COMPLETE
under a policy that accepts SEC-sourced news as the definition for historical windows - **a user decision**; without
it the family stays PARTIAL by construction.
**Leakage rule:** only documents accepted before the replay moment; no later classifier output, no later news.

## Summary
| gap | status | next step | night-job estimate |
|---|---|---|---|
| CASH_RUNWAY | parser + formula ready (tested) | PIT facts table + backfill job + risk family | ~1 h, ~1 GB |
| FINANCING_ACTIONS / DILUTION | page merge + acceptance map ready (tested) | submissions history job | ~1-1.5 h, ~0.5 GB |
| THESIS_INVALIDATION | reconstructable only SEC-sourced | policy decision, then per-scope EFTS reconstruction | minutes per scope |
