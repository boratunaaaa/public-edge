#!/usr/bin/env bash
# PUBLIC EDGE database backups with retention.
#   scripts/pe-backup.sh daily                 # re-derivable caches excluded (10-K text, raw vendor responses)
#   scripts/pe-backup.sh weekly                # full
#   scripts/pe-backup.sh pre-migration <label> # full, before any migration / bulk data change
# Retention (per class, by age): daily 2, weekly 2, pre-migration 2. Only files this script created
# (<class>/pe_<class>_*.dump) are ever removed. Every dump is validated with pg_restore --list; an invalid or
# partial dump is deleted and the older ones are kept. A dump is skipped when free space < 2x the last dump.
set -euo pipefail
cd "$(dirname "$0")/.."
CLASS=${1:?daily|weekly|pre-migration}
LABEL=${2:-}
ROOT=${PE_BACKUP_ROOT:-/root/backups/public-edge}
declare -A KEEP=([daily]=2 [weekly]=2 [pre-migration]=2)
[ -n "${KEEP[$CLASS]:-}" ] || { echo "unknown class $CLASS" >&2; exit 2; }
DIR="$ROOT/$CLASS"; mkdir -p "$DIR"; chmod 700 "$ROOT" "$DIR"
TS=$(date -u +%Y%m%dT%H%MZ)
OUT="$DIR/pe_${CLASS}_${TS}${LABEL:+_$LABEL}.dump"
last=$(ls -1t "$ROOT"/*/pe_*.dump 2>/dev/null | head -1 || true)
need=$(( ${last:+$(stat -c %s "$last")} + 0 ))
need=$(( need > 0 ? need * 2 : 2000000000 ))
free=$(df --output=avail -B1 "$ROOT" | tail -1)
if [ "$free" -lt "$need" ]; then
  echo "$(date -u +%FT%TZ) SKIP $CLASS: free $free < needed $need" | tee -a "$ROOT/backup.log"; exit 3
fi
EXCL=""
if [ "$CLASS" = daily ]; then EXCL="--exclude-table-data=tenk_filings --exclude-table-data=provider_raw"; fi
TG_ENV=${TELEGRAM_ENV_FILE:-/etc/public-edge/telegram.env}
if ! docker compose --env-file "$TG_ENV" --env-file .env exec -T db sh -c \
     "pg_dump -U \$POSTGRES_USER -d \$POSTGRES_DB -Fc -Z6 $EXCL" > "$OUT.part"; then
  rm -f "$OUT.part"; echo "$(date -u +%FT%TZ) FAIL $CLASS dump" | tee -a "$ROOT/backup.log"; exit 4
fi
if ! docker compose --env-file "$TG_ENV" --env-file .env exec -T db pg_restore --list < "$OUT.part" > /dev/null; then
  rm -f "$OUT.part"; echo "$(date -u +%FT%TZ) FAIL $CLASS validation" | tee -a "$ROOT/backup.log"; exit 5
fi
mv "$OUT.part" "$OUT"; chmod 600 "$OUT"
ls -1t "$DIR"/pe_"$CLASS"_*.dump | tail -n +$(( ${KEEP[$CLASS]} + 1 )) | while read -r old; do
  rm -f -- "$old"; echo "$(date -u +%FT%TZ) RETENTION removed $old" >> "$ROOT/backup.log"
done
echo "$(date -u +%FT%TZ) OK $CLASS $OUT $(stat -c %s "$OUT")" | tee -a "$ROOT/backup.log"
