#!/bin/bash
# Wrapper invoked by com.charliebruce.xrpldashboard.is_bot_writer.plist.
#
# Why this exists: is_bot_writer.py writes to Postgres via DATABASE_URL
# which must be sourced from the env file — launchd doesn't inherit the
# shell environment. Without it, pg_available() returns False and all
# schema/UPDATE operations silently no-op, leaving is_bot unset forever.
#
# flock -n prevents concurrent runs: if a previous instance is still
# running (backfill or full-resync), this invocation exits cleanly
# rather than running two classification passes simultaneously.
set -euo pipefail

ENV_FILE="$HOME/.config/xrpldashboard/env"
if [[ ! -r "$ENV_FILE" ]]; then
  echo "[$(date '+%F %T')] ERROR: env file missing or unreadable: $ENV_FILE" >&2
  exit 78  # EX_CONFIG — launchd will retry per StartInterval
fi

# shellcheck disable=SC1090
source "$ENV_FILE"

PYTHON="/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"
SCRIPT="/Users/charliebruce/xrpl_test/scripts/is_bot_writer.py"
LOCKFILE="/tmp/is_bot_writer.lock"

exec flock -n "$LOCKFILE" "$PYTHON" "$SCRIPT"
