#!/bin/bash
# Wrapper invoked by com.charliebruce.xrpldashboard.unl_snapshot.plist.
#
# Why this exists: unl_snapshot.py writes to Neon via db.write_unl_snapshot.
# Without DATABASE_URL in the environment, db.pg_available() returns False
# and the run silently no-ops — disk has no snapshot table, the row never
# lands, and tomorrow's /network diff helper stays in cold-start state with
# no operator-visible signal that anything failed. Sourcing the env file
# here keeps the daily UNL row writing reliably.
set -euo pipefail

ENV_FILE="$HOME/.config/xrpldashboard/env"
if [[ ! -r "$ENV_FILE" ]]; then
  echo "[$(date '+%F %T')] ERROR: env file missing or unreadable: $ENV_FILE" >&2
  exit 78  # EX_CONFIG
fi

# shellcheck disable=SC1090
source "$ENV_FILE"

PYTHON="/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"
SCRIPT="/Users/charliebruce/xrpl_test/unl_snapshot.py"

exec "$PYTHON" "$SCRIPT"
