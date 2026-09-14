#!/usr/bin/env bash
set -Eeuo pipefail
ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
: "${RAG_BACKUP_ARCHIVE:?Set RAG_BACKUP_ARCHIVE to a backup archive}"
: "${RAG_RESTORE_DIR:?Set RAG_RESTORE_DIR to a new, non-existing directory}"
exec python3 "$ROOT_DIR/deploy/backup.py" restore --archive "$RAG_BACKUP_ARCHIVE" --destination "$RAG_RESTORE_DIR"
