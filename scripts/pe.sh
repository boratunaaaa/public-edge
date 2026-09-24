#!/usr/bin/env bash
# PUBLIC EDGE management wrapper.
#   scripts/pe.sh up | restart | status | logs [svc] | down | ps | <any docker compose args>
# Telegram variables are interpolated from $TELEGRAM_ENV_FILE (the existing bot's secret file);
# values are never printed or copied.
set -euo pipefail
cd "$(dirname "$0")/.."
TG_ENV=$(grep -E '^TELEGRAM_ENV_FILE=' .env | cut -d= -f2-)
ARGS=()
if [ -n "${TG_ENV:-}" ] && [ -f "$TG_ENV" ]; then ARGS+=(--env-file "$TG_ENV"); fi
ARGS+=(--env-file .env)
dc() { docker compose "${ARGS[@]}" "$@"; }
case "${1:-status}" in
  up)       dc up -d --build --remove-orphans ;;
  restart)  dc up -d --build --force-recreate api worker backfill ;;
  status)   dc ps; echo; curl -s -m 5 http://127.0.0.1:8090/health | python3 -m json.tool || true ;;
  logs)     shift; dc logs --tail=200 -f "$@" ;;
  down)     dc down ;;
  *)        dc "$@" ;;
esac
