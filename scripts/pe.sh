#!/usr/bin/env bash
# PUBLIC EDGE management wrapper.
#   scripts/pe.sh up | restart | status | logs [svc] | down | ps | <any docker compose args>
# Telegram variables are interpolated from $TELEGRAM_ENV_FILE (default /etc/public-edge/telegram.env,
# root-only 600, outside the repo so deploys can never delete it). Values are never printed.
set -euo pipefail
cd "$(dirname "$0")/.."
TG_ENV=$(grep -E '^TELEGRAM_ENV_FILE=' .env | cut -d= -f2- || true)
TG_ENV=${TG_ENV:-/etc/public-edge/telegram.env}
if [ ! -r "$TG_ENV" ]; then
  echo "ERROR: Telegram secret file $TG_ENV missing/unreadable - refusing to start without alerts" >&2
  exit 1
fi
ARGS=(--env-file "$TG_ENV" --env-file .env)
dc() { docker compose "${ARGS[@]}" "$@"; }
case "${1:-status}" in
  up)       dc up -d --build --remove-orphans ;;
  restart)  dc up -d --build --force-recreate api worker backfill intelligence ;;
  status)   dc ps; echo; curl -s -m 5 http://127.0.0.1:8090/health | python3 -m json.tool || true ;;
  logs)     shift; dc logs --tail=200 -f "$@" ;;
  down)     dc down ;;
  *)        dc "$@" ;;
esac
