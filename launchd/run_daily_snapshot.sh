#!/bin/bash
# Wrapper invoked by com.charliebruce.xrpldashboard.daily_snapshot.plist.
#
# Why this exists: daily_snapshot.py mirrors the rollup into Postgres via
# db.write_snapshot_meta. Without DATABASE_URL in the environment,
# db.pg_available() returns False and the mirror silently no-ops — the
# disk file lands but /institutional (served by Render off Neon) keeps
# showing whatever stale meta row was last written. Sourcing the env file
# here keeps the disk and PG sides in sync on every scheduled run.
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
SCRIPT="/Users/charliebruce/xrpl_test/daily_snapshot.py"

exec "$PYTHON" "$SCRIPT"
