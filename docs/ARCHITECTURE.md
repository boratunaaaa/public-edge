# PUBLIC EDGE — architecture & operations

Public US financial disclosures → normalized rows → events → historical returns → Telegram.
**No trading.** Every event is "something to research", never investment advice.

```
PUBLIC DATA → NORMALIZATION → EVENT DETECTION → TICKER MATCHING → HISTORICAL ANALYSIS → MULTI-SIGNAL → TELEGRAM
```

## Processes (docker compose, `restart: unless-stopped`)

| service  | role |
|----------|------|
| db       | PostgreSQL 16 (no published port) |
| migrate  | `alembic upgrade head`, one-shot |
| worker   | LIVE INGESTION — every source on its interval, incremental backtest (30 min), Telegram dispatch |
| backfill | HISTORICAL BACKFILL — resumable/checkpointed, pauses below 3 GB free disk, re-runs daily |
| api      | FastAPI JSON API + dashboard, bound to 127.0.0.1:8090 and the Tailscale IP :8091 only |

## Sources (official first)

| source | live | history | notes |
|---|---|---|---|
| SEC Form 4 | EDGAR latest-filings feed, 60 s | SEC Insider Transactions Data Sets (quarterly; P and S rows only, disk budget) | acceptance timestamp known live; datasets have date only |
| SEC 13D / 13D-A | feed, 120 s | EDGAR quarterly full-index → complete submission | structured XML (Dec-2024+) and legacy HTML/text parsers; 13G never mixed in |
| SEC Form 144 | feed, 5 min | – | info signal; `FORM144_MAJOR_SALE` ≥ $5M or ≥ 1% of shares |
| House PTR | Clerk yearly index + e-filed PDFs, 3 h | same, previous years | scanned PDFs are stored as `no_text` |
| Senate | **disabled** | – | efdsearch.senate.gov returns 403 (Akamai) to automated clients |
| USAspending | `last_modified_date` last 3 days, award signed ≤150 days, 6 h | `new_awards_only` by month | contract/market-cap ratio levels >2/5/10/25/50% |
| Trump family | data.sec.gov submissions for CIK 947033 / 2016181 / 2057754, 30 min; plus name match in all feeds | all listed filings | documented facts only |
| Phase 2 slots | FDA, ClinicalTrials, DOJ antitrust, merger arb, buyback execution, 13F | – | `PlannedSource` placeholders |

Market data: `MarketDataProvider` abstraction; V1 implementation = Yahoo chart endpoint
(split/dividend adjusted). Market cap = SEC XBRL `EntityCommonStockSharesOutstanding` × close.

## No look-ahead (backtest)

* Entry = first **open** strictly after the information became public
  (exact SEC acceptance time when known; date-only ⇒ treated as after the close ⇒ next session).
* The transaction date is not a parameter of any backtest function.
* Historical USAspending awards: public date = award date + 90 days (DoD) / + 5 days (civilian).
* Congress clusters only count reports already filed on the evaluation date.
* Horizons 1/7/30/60/90/180/365 calendar days, only once elapsed; excess vs SPY on the same dates.
* Stats with n < `MIN_SAMPLE_SIZE` (30) are labelled insufficient and not shown as signals.

## Alert policy

Priority is a temporary heuristic for notifications only (party never used). Raw features are
stored in `signal_features`. Only live events with priority ≥ `ALERT_MIN_PRIORITY` (5), public in
the last 2 days, max `MAX_ALERTS_PER_HOUR` (8); related events of one ticker in the same run are
merged into one message. Multi-signal requires two independent source groups
(insider / activist / contract / congress).

## Secrets

`.env` (DB password, SEC user agent, flags) is chmod 600 and git-ignored. Telegram variables live in
`/etc/public-edge/telegram.env` (root-only, 600, outside the repo so rsync deploys cannot remove it) and
are interpolated at `docker compose` time by `scripts/pe.sh`, which refuses to start if the file is
missing. PUBLIC EDGE no longer depends on `/root/crypto-bot`. The Telegram client redacts the token
from errors; nothing is logged or committed.

## Commands (on the server, /opt/public-edge)

```
scripts/pe.sh status            # containers + /health
scripts/pe.sh logs worker       # follow logs (worker|backfill|api|db)
scripts/pe.sh restart           # rebuild + recreate app containers
scripts/pe.sh up                # full stack
```

## V2 — Early Discovery + News Intelligence

Extra process `intelligence` (daily discovery at 10:00 UTC, full refresh weekly; daily Telegram
report 12:30 UTC Mon–Fri = 08:30 New York; weekly report Sunday 16:00 UTC).

**News** (`app/news`): SEC 8-K (exact acceptance time, Exhibit 99 press release), PR Newswire,
Business Wire, DOE/NASA, openFDA (original + new-indication approvals only), watchlist headlines.
Only the headline and lead paragraph are classified — forward-looking-statement boilerplate,
"About X" sections, legal exhibits and inline-XBRL headers are cut first (`app/news/lead.py`).
Copies of the same story are clustered; the story's public time is the earliest *original*
publication time (updated/future-dated timestamps are ignored). Hype filter: MOU / LOI / pilot /
collaboration can never be HIGH on wording alone. Materiality scales with contract / market cap
and contract / annual revenue. Historical 8-Ks (backfill) feed the news backtest.

**Discovery** (`app/discovery`): point-in-time universe snapshots (>= $50M; buckets MICRO,
EARLY_SMALL, SMALL, GROWTH, ALREADY_SCALED), XBRL frames fundamentals, EDGAR full-text evidence
(sentence-level, source-linked), counterparty relation quality (customer vs competitor vs potential,
binding vs MOU), commercialization stage, inflection flags that need two confirming quarters,
microcap risk filter, research-priority score (no price-momentum feature), watchlist with reasons
and manual override (`python -m app.cli watchlist TICKER CATEGORY "reason"`), evidence-linked thesis.

**Insiders**: routine vs opportunistic (Cohen, Malloy & Pomorski 2012) and holdings increase.

## Telegram messages (Turkish, for non-experts)

Every alert: company + size + price context, "Ne oldu?", "Neden önemli?", "Geçmişte ne oldu?",
"Alım puanı", overall verdict, "Dikkat" (plain-language risks), source link, and a 6-month price
chart. **Alım puanı** = share of comparable past events (most specific peer group with >= 30
samples) whose stock beat the S&P 500 over 3 months in this system's no-look-ahead backtest,
shrunk toward 50% (Beta(5,5)); below 30 samples no percentage is shown. The weekly report carries an
alert scorecard (how the alerts actually sent 1–4 months ago performed).
