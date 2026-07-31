#!/bin/bash
# Wrapper invoked by com.charliebruce.xrpldashboard.mpt_snapshot.plist.
#
# Why this exists: mpt_snapshot.py mirrors the rollup into Neon via
# db.write_mpt_snapshot + db.write_mpt_supply_history. Without
# DATABASE_URL in the environment, db.pg_available() returns False and
# both writes silently no-op — the JSON file on disk updates fine, but
# Render (which reads PG) sees stale numbers indefinitely. Sourcing
# ~/.config/xrpldashboard/env here keeps the disk and PG sides aligned
# on every scheduled run.
#
# Same pattern as run_daily_snapshot.sh + run_mpt_holders_refresh.sh.
set -euo pipefail

ENV_FILE="$HOME/.config/xrpldashboard/env"
if [[ ! -r "$ENV_FILE" ]]; then
  echo "[$(date '+%F %T')] ERROR: env file missing or unreadable: $ENV_FILE" >&2
  exit 78  # EX_CONFIG
fi

set -a  # auto-export sourced vars — 2026-07-31 BetterStack silent-skip fix
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

PYTHON="/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"
SCRIPT="/Users/charliebruce/xrpl_test/mpt_snapshot.py"

exec "$PYTHON" "$SCRIPT"
