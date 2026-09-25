# Alpha validation (2026-09-25)

Goal: measure honestly which signal families carry investment value, which do not, and which cannot
be measured yet — instead of adding features. **No real order is ever sent**: there is no broker
client, no order API and no broker credential anywhere in the code base.

## 10-K text changes (`app/tenk/`)
* Items 1, 1A, 1C, 3, 7 are cut at line-start headings (the table of contents and inline
  "see Item 7" references never cut a section). Sentences are normalized (numbers, years, dates, $ →
  tokens) before hashing, so a yearly roll-forward is not a change.
* Topic-tagged sentences carry a MinHash signature; a reworded sentence is an edit, not a new risk.
  A change needs a NET shift (added − removed) that is large relative to what the report already said.
* Types: RISK_INCREASE/DECREASE, LIQUIDITY_DETERIORATION/IMPROVEMENT, NEW_CUSTOMER_CONCENTRATION,
  NEW_SUPPLY/LEGAL/REGULATORY_RISK, NEW/REMOVED_GOING_CONCERN (≥ 2 unhedged "substantial doubt"
  sentences; "management evaluates whether…" is boilerplate), NEW_CAPACITY_EXPANSION,
  NEW_COMMERCIALIZATION_LANGUAGE, NEW_MAJOR_CUSTOMER, OTHER_MATERIAL_CHANGE.
* Timing: `public_at` = SEC acceptance timestamp (EDGAR's "Z" is Eastern time; read as Eastern, which
  can only delay the entry). Entry = first session opening after it. Period end / document date unused.
* History: survivorship-free — every 10-K filer of each EDGAR full-index quarter that had an exchange
  ticker AT the filing date (delisted and merged companies included). Resumable, LOW priority thread
  in the backfill process, stops below 10 GB free disk. Events have priority 2 (recorded, not alerted).
* Classifier changes never delete: `python -m app.cli tenk-reclassify --apply` supersedes old rows
  (audited in `data_cleanup_audit`) and the backfill re-creates them.

## Insider person profiles (`app/signals/insider_profile.py`)
Completed open-market buys only (90-day horizon matured before the evaluation date); same person +
same company within 90 days = one episode; Wilson 95% interval; label only with ≥ 8 (configurable:
`INSIDER_PROFILE_MIN_EPISODES`) resolved episodes, ≤ 10% unresolved and an interval ≤ 45 pp wide.
Telegram shows a line only for HISTORICALLY_STRONG / MIXED / HISTORICALLY_WEAK, computed as of the
event day. Snapshot table `insider_profiles` refreshed daily.

## Alpha validation (`app/paper/alpha.py`, dashboard `/alpha`)
Per family and horizon (30/90/180): quality status, companies, episodes, unresolved share, rates only
when TRUSTED, bounds when PARTIAL, walk-forward result. Verdict at 90 days: OUTPERFORMING /
UNDERPERFORMING / INCONCLUSIVE / BLOCKED (see module docstring). UNDERPERFORMING, BLOCKED and
unproven families never create automatic proposals (`PAPER_RESEARCH_MODE=1` overrides, clearly labelled).

## Paper trading (`app/paper/`, dashboard `/paper`)
* Gate: data quality, verified same-CIK price identity, no duplicate (30 days / open position),
  30-day $ volume ≥ 1M, no GOING_CONCERN / CASH_RUNWAY HIGH, long-only (NEGATIVE/MIXED signals never
  become buys), family evidence.
* Sizing (paper risk limits): max 5%, microcap (or unknown size) 2%, HIGH_FINANCING_RISK 1%.
* Human approval only: dashboard buttons (POST needs the page's custom header) or
  `python -m app.cli paper-approve CODE`. Telegram only announces ("Bu sanal işlem önerisidir.").
  Proposals expire after 72 h.
* Execution: next session open + slippage (50 / 20 / 10 bps by size) + commission ($1 default);
  split/dividend-safe valuation via adj_close. Five separate books: A 30 days, B 90 days
  ("PUBLIC EDGE PAPER"), C 180 days, D thesis invalidation, E stop-loss 15% + 90 days.
* Metrics: NAV, cash, SPY same-capital NAV, realized/unrealized P/L, max drawdown, win rate,
  average holding days, average sector-ETF-adjusted return.

## Prices
Priority: Tiingo (operator key, delisted) cached by a resumable background job → identity-validated
cache → Yahoo for active symbols. Without `TIINGO_API_KEY` the job is skipped and delisted events stay
visibly unresolved. `python -m app.cli provider-coverage` reports free-source coverage.

## Resources
cpu_shares: worker 1024 (live), intelligence 512 (paper, discovery, 10-K live), backfill 256
(history, 10-K backfill). OGE PDF text extraction runs in a memory-capped child process.
