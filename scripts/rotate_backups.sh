#!/bin/bash
# Retention rotation for backup_db.sh output (D-04): keep the 14 most recent
# daily dumps, plus up to 3 monthly (1st-of-month) copies; delete the rest.
# Matches db_<stamp>.dump / data_<stamp>.tar.gz naming from backup_db.sh.
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-${DATA_DIR:-/data}/backups}"
DAILY_KEEP=14
MONTHLY_KEEP=3

[ -d "$BACKUP_DIR" ] || { echo "rotate_backups.sh: no backup dir at $BACKUP_DIR, nothing to do"; exit 0; }

cd "$BACKUP_DIR"

# All daily dump stamps (YYYYMMDD), newest first. Guard every pipeline that can
# legitimately match nothing with `|| true` — under `set -euo pipefail` an empty
# match otherwise kills the script silently (empty dir, or no 1st-of-month dump).
stamps=$(ls -1 db_*.dump 2>/dev/null | sed -E 's/^db_([0-9]{8})\.dump$/\1/' | sort -r || true)
[ -n "$stamps" ] || { echo "rotate_backups.sh: no dumps in $BACKUP_DIR, nothing to do"; exit 0; }

# Monthly copies = stamps whose day-of-month is 01, most recent MONTHLY_KEEP kept.
monthly_keep_stamps=$(echo "$stamps" | { grep -E '^[0-9]{6}01$' || true; } | head -n "$MONTHLY_KEEP")

# Daily copies = most recent DAILY_KEEP stamps overall.
daily_keep_stamps=$(echo "$stamps" | head -n "$DAILY_KEEP")

keep_stamps=$(printf '%s\n%s\n' "$daily_keep_stamps" "$monthly_keep_stamps" | sort -u)

for stamp in $stamps; do
  if ! grep -qx "$stamp" <<< "$keep_stamps"; then
    rm -f "db_${stamp}.dump" "data_${stamp}.tar.gz"
    echo "rotate_backups.sh: removed backup $stamp"
  fi
done

# Clean up orphaned .tmp files from failed backup runs older than 1 day.
find . -maxdepth 1 -name '*.tmp' -mtime +0 -exec rm -f {} + 2>/dev/null || true
