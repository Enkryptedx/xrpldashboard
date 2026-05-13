#!/bin/bash
# Wrapper invoked by com.charliebruce.xrpldashboard.mpt_holders_refresh.plist.
#
# Why this exists: mpt_holders_refresh.py writes per-MPT supply +
# concentration rows to Neon via db.write_mpt_supply_history. Without
# DATABASE_URL in the environment, db.pg_available() returns False and
# every PG mirror silently no-ops — the hourly cron would run cleanly,
# update mpt_snapshot.json on disk, and the /mpt/<id> sparkline on
# Render (which reads PG, not the disk file) would never advance.
# Sourcing ~/.config/xrpldashboard/env keeps the disk and PG sides
# aligned on every scheduled run.
set -euo pipefail

ENV_FILE="$HOME/.config/xrpldashboard/env"
if [[ ! -r "$ENV_FILE" ]]; then
  echo "[$(date '+%F %T')] ERROR: env file missing or unreadable: $ENV_FILE" >&2
  exit 78  # EX_CONFIG
fi

# shellcheck disable=SC1090
source "$ENV_FILE"

PYTHON="/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"
SCRIPT="/Users/charliebruce/xrpl_test/mpt_holders_refresh.py"

exec "$PYTHON" "$SCRIPT"
