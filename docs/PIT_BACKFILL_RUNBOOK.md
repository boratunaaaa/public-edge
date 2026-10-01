# PIT backfill runbook (replay / research data only)

Tables (migration 0016): `pit_facts`, `sec_filings`, `pit_backfill_progress`. No live decision reads them.

## Invocation

Always through compose so `PE_SHARED_RATELIMIT=1` (one SEC budget shared with the live workers) and the
container memory limit are inherited; `--apply` refuses to start without it.

    cd /opt/public-edge
    docker compose --env-file /etc/public-edge/telegram.env --env-file .env run --rm --no-deps -T --name pe-pit-xbrl \
        worker nice -n 10 python -m app.jobs.pit_backfill xbrl --apply --max-minutes 240

Dry run (default) writes nothing. Re-runs skip done / not_found companies. At most two heavy jobs at once.
The job stops cleanly under 8 GB free disk.

Post-run checks:

    SELECT job, status, count(*) FROM pit_backfill_progress GROUP BY 1, 2;
    SELECT count(*) FROM pit_backfill_progress WHERE status = 'failed' AND attempts >= 3;  -- abandoned companies
    SELECT count(*) FROM corporate_actions;  -- must not change (the job never writes it)

## Known data properties

- **Mixed form coverage in `sec_filings`.** The first submissions run (`pit-submissions-20261001T0148-*`,
  1,468 companies, lowest CIKs) stored every form. From commit 38469c4 (2026-10-01) insider ownership forms
  3 / 4 / 5 (+ amendments) are not stored (46% of rows; production ingests Form 4 separately; no XBRL fact
  links to them - verified 0 of 881,597 linked facts). Consumers must filter by form; never infer
  "no insider filings" from this table.
- XBRL companyfacts: 8,137 companies with facts, 851 without (404). `value_conflicts` = 0.
- CASH_RUNWAY reconstruction vs production CASH_RUNWAY_RISK events: 360 / 362 within 1%. The two misses are
  fiscal-year changes (annual OCF period outside the 335-395 day window).
