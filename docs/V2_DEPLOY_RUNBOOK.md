# V2 deploy runbook (needs explicit owner permission: production write)

Pre-checks (all done on 2026-09-28):
* Tests: 591+ passing locally (Colima, Postgres 16), including a migration-drift test (0001..0012 == models).
* Migration 0012 was rehearsed on a schema-only copy of production (stamp 0011 → upgrade → downgrade → upgrade).
  It is additive only: new nullable columns and new tables. The only data change is an UPDATE that backfills
  events.available_at, first_seen_at and accepted_at with the rule the backtest already used.
* Disk: 11 GB free. The backup (~1 GB) fits; the V2 history steps self-limit (`PE_V2_MIN_FREE_GB=9`).

```bash
# on the server, from /opt/public-edge
scripts/pe-backup.sh pre-migration 0012_v2_core                     # validated with pg_restore --list
rsync (laptop -> /opt/public-edge, excluding .env, .git, audit-final-sync)
mkdir -p /opt/public-edge-data/raw /opt/public-edge-data/exports && chown -R 10050 /opt/public-edge-data
docker compose --env-file /etc/public-edge/telegram.env --env-file .env build
docker compose --env-file /etc/public-edge/telegram.env --env-file .env run --rm migrate
docker compose --env-file /etc/public-edge/telegram.env --env-file .env up -d --no-deps --force-recreate worker backfill intelligence api
```

Validation after deploy:
* `curl -s 127.0.0.1:8090/api/v2/health` shows health_status, today_status and quotas.
* `SELECT count(*) FROM events WHERE available_at IS NULL` returns 0.
* V2 sources show `health_status` HEALTHY within 12 h.
* The backfill log shows `v2_short_data`, `v2_health_gov` and `v2_13f` steps (or LOW_DISK skips).

Rollback: `alembic downgrade 0011` is safe (drops only V2 tables and columns). Then redeploy c79960b.

## Backup retention (needs owner permission: deletes files)
Inventory on 2026-09-28: 13 PUBLIC EDGE dumps, 5.9 GB.
* `/root/backups/public-edge`: pre_13d_class (0.99 GB), pre_reporting_person (0.99 GB), pre_13d_repair,
  pre_migration0009, pre_recovery_import, pre_tenkfix.
* `/opt/archive/public-edge-db`: pre-integrity, pre-deploy, pre-migrate-0004, pre-0005, pre-alpha, pre-0006, pre-0008.

Proposal:
* **Keep** pre_13d_class and pre_reporting_person (newest two; validated with pg_restore --list). Keep
  pre-integrity-20260925 and pre-deploy-20260925 (0.3 GB): the baseline from before any cleanup.
* **Delete** the other 9 (~3.8 GB). Every later data change is audited row by row in data_cleanup_audit
  (supersede, never delete), so these files are no longer the only record.
* From then on, `scripts/pe-backup.sh` keeps: daily 2 (excluding 10-K text and raw vendor responses),
  weekly 2, pre-migration 2. Cron proposal: daily 03:30 UTC, weekly Sunday 04:00 UTC.

Other large directories (not PUBLIC EDGE, owner decision): `/root/crypto-bot/data` 16 GB (bot disabled 2026-09-24),
`/opt/ai-stack` 18 GB (.ollama 5.3 GB, .local 3.6 GB), `/root/.cache/ms-playwright` 1.2 GB,
`/root/out` 0.6 GB (video renders), Docker build cache 0.8 GB.
