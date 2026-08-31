#!/usr/bin/env sh
set -eu

umask 077

database_path="${AUDIT_DB_PATH:-/var/lib/stonetree-it-automation/operations.sqlite3}"
backup_directory="${BACKUP_DIRECTORY:-/var/backups/stonetree-it-automation}"
retention_days="${BACKUP_RETENTION_DAYS:-14}"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup_path="${backup_directory}/operations-${timestamp}.sqlite3"

mkdir -p "${backup_directory}"
sqlite3 "${database_path}" ".backup '${backup_path}'"

integrity_result="$(sqlite3 "${backup_path}" 'PRAGMA integrity_check;')"
if [ "${integrity_result}" != "ok" ]; then
    echo "Backup integrity check failed" >&2
    exit 1
fi

find "${backup_directory}" -type f -name 'operations-*.sqlite3' -mtime "+${retention_days}" -delete
echo "Backup created and verified: ${backup_path}"
