#!/bin/bash
# Wrapper invoked by com.charliebruce.xrpldashboard.credentials_walker.plist.
#
# Replaces the daemon-in-gunicorn pattern that previously lived in
# credentials_state._refresh_loop. Now the walker is a single-shot
# script invoked by launchd, and the /credentials route reads from
# Postgres only.
#
# Sourcing ~/.config/xrpldashboard/env makes DATABASE_URL available so
# db.write_credentials_snapshot persists to Neon (Render reads from PG).
# Same pattern as run_mpt_snapshot.sh.
set -euo pipefail

ENV_FILE="$HOME/.config/xrpldashboard/env"
if [[ ! -r "$ENV_FILE" ]]; then
  echo "[$(date '+%F %T')] ERROR: env file missing or unreadable: $ENV_FILE" >&2
  exit 78  # EX_CONFIG
fi

# shellcheck disable=SC1090
source "$ENV_FILE"

PYTHON="/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"
SCRIPT="/Users/charliebruce/xrpl_test/credentials_walker.py"

exec "$PYTHON" "$SCRIPT"
