#!/usr/bin/env bash
set -euo pipefail

# Safe cleanup script for Grok2API SQLite database.
# Prevents orphaned request_audit_attempts and avoids database locking.
# Run periodically via cron (e.g. hourly or daily).

DB_PATH="${1:-${BACKEND_DB:-./data/backend.db}}"
RETENTION_DAYS="${RETENTION_DAYS:-3}"

if [ ! -f "$DB_PATH" ]; then
    echo "[WARN] Database file not found at: $DB_PATH"
    exit 0
fi

echo "[INFO] Cleaning audit records older than ${RETENTION_DAYS} days in: $DB_PATH"

sqlite3 "$DB_PATH" <<EOF
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 10000;

-- 1. Cascade delete request audits older than retention cutoff
DELETE FROM request_audits WHERE created_at < datetime('now', '-${RETENTION_DAYS} days');

-- 2. Clean any orphaned audit attempts in case of historic un-enforced foreign keys
DELETE FROM request_audit_attempts WHERE audit_id NOT IN (SELECT id FROM request_audits);

-- 3. Reclaim unused disk space
VACUUM;
EOF

echo "[INFO] Grok2API database cleanup completed successfully."
