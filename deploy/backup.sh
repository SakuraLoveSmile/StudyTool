#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
COMPOSE_FILE=${COMPOSE_FILE:-"$ROOT_DIR/compose.yaml"}
ENV_FILE=${ENV_FILE:-"$ROOT_DIR/deploy/.env"}
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # deploy/.env is an administrator-controlled shell-compatible env file.
  source "$ENV_FILE"
  set +a
fi
DATA_DIR=${RAG_DATA_DIR:?Set RAG_DATA_DIR to the dedicated production data directory}
BACKUP_DIR=${RAG_BACKUP_DIR:?Set RAG_BACKUP_DIR outside the source tree}
compose=(docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE")
was_running=0
if "${compose[@]}" ps --status running -q backend | grep -q .; then was_running=1; fi
restart() { if [[ "$was_running" == 1 ]]; then "${compose[@]}" start backend >/dev/null; fi; }
trap restart EXIT
"${compose[@]}" stop backend
python3 "$ROOT_DIR/deploy/backup.py" create --source "$DATA_DIR" --output "$BACKUP_DIR" --retain 7
