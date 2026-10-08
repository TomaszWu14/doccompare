#!/bin/bash
# Nightly backup: pg_dump (custom format) of DATABASE_URL + tar of DATA_DIR.
# Scope is DB + DATA_DIR only (D-05) — never uploads/, never SECRET_KEY.
# DATABASE_URL / DATA_DIR come from the environment (Coolify scheduled task).
set -euo pipefail

url="${DATABASE_URL:-}"
if [ -z "$url" ]; then
  echo "backup_db.sh: DATABASE_URL is not set" >&2
  exit 1
fi
# pg_dump requires the postgresql:// scheme; normalize like check_db.py does.
if [[ "$url" == postgres://* ]]; then
  url="postgresql://${url#postgres://}"
fi

BACKUP_DIR="${BACKUP_DIR:-${DATA_DIR:-/data}/backups}"
mkdir -p "$BACKUP_DIR"

# Single-run lock: a second concurrent invocation exits instead of interleaving
# writes into the same .tmp file (flock ships with util-linux on the VPS).
exec 9>"$BACKUP_DIR/.backup.lock"
if command -v flock >/dev/null 2>&1 && ! flock -n 9; then
  echo "backup_db.sh: another backup run is in progress, exiting" >&2
  exit 0
fi

stamp="$(date +%Y%m%d)"

# --- Database dump (atomic: write to .tmp, mv only on success) ---
dump_tmp="$BACKUP_DIR/db_${stamp}.dump.tmp"
dump_final="$BACKUP_DIR/db_${stamp}.dump"
pg_dump -Fc "$url" -f "$dump_tmp"
mv "$dump_tmp" "$dump_final"

# --- DATA_DIR archive (artwork masters, D-05) ---
if [ -n "${DATA_DIR:-}" ] && [ -d "${DATA_DIR:-}" ]; then
  data_tmp="$BACKUP_DIR/data_${stamp}.tar.gz.tmp"
  data_final="$BACKUP_DIR/data_${stamp}.tar.gz"
  tar -czf "$data_tmp" -C "$(dirname "$DATA_DIR")" "$(basename "$DATA_DIR")"
  mv "$data_tmp" "$data_final"
fi

echo "backup_db.sh: wrote $dump_final"
