#!/bin/bash
# Wrapper invoked by com.charliebruce.xrpldashboard.is_bot_writer.plist.
#
# Why this exists: is_bot_writer.py writes to Postgres via DATABASE_URL
# which must be sourced from the env file — launchd doesn't inherit the
# shell environment. Without it, pg_available() returns False and all
# schema/UPDATE operations silently no-op, leaving is_bot unset forever.
#
# Concurrency control: mkdir-based atomic lock (portable across macOS
# where flock isn't native). A pid file inside the lockdir lets the next
# invocation distinguish a live long-running pass (backfill/resync) from
# a stale lock left by a crash or a kill -9. The stale-lock recovery
# writes a walker_health failure row so the drift shows up in the same
# telemetry channel the canary uses — the 2026-07-26 incident (~760
# consecutive skips over 2.7 days, is_bot never updated, canary +5476
# delta) taught us that "silently skipped" cannot look like "healthy."
set -euo pipefail

ENV_FILE="$HOME/.config/xrpldashboard/env"
if [[ ! -r "$ENV_FILE" ]]; then
  echo "[$(date '+%F %T')] ERROR: env file missing or unreadable: $ENV_FILE" >&2
  exit 78  # EX_CONFIG — launchd will retry per StartInterval
fi

set -a  # auto-export sourced vars — 2026-07-31 BetterStack silent-skip fix
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

PYTHON="/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"
SCRIPT="/Users/charliebruce/xrpl_test/scripts/is_bot_writer.py"
LOCKDIR="/tmp/is_bot_writer.lock"
PIDFILE="$LOCKDIR/pid"

_record_walker_failure() {
  # Write a walker_health failure row so a stale-lock recovery is visible
  # to /walker_health and to the canary's consecutive_failures signal.
  # Silent no-op if PG isn't reachable; the wrapper still proceeds.
  local msg="$1"
  "$PYTHON" -c "
import os, sys
sys.path.insert(0, '/Users/charliebruce/xrpl_test')
import db
db.write_walker_health_start('is_bot_writer_wrapper', cadence_seconds=300)
db.write_walker_health_end('is_bot_writer_wrapper', ok=False, message=os.environ.get('MSG',''))
" 2>/dev/null || true
}

if ! mkdir "$LOCKDIR" 2>/dev/null; then
  # Lock exists — decide whether it's a live long-running pass or stale.
  # Read the pid; if the process is gone, recover the lock and proceed.
  # If the pid is alive, skip cleanly (legitimate concurrency guard).
  STALE_PID=""
  if [[ -r "$PIDFILE" ]]; then
    STALE_PID="$(cat "$PIDFILE" 2>/dev/null || true)"
  fi
  if [[ -n "$STALE_PID" ]] && kill -0 "$STALE_PID" 2>/dev/null; then
    echo "[$(date '+%F %T')] is_bot_writer already running (pid=$STALE_PID) — skipping" >&2
    exit 0
  fi
  echo "[$(date '+%F %T')] WARN: recovering stale is_bot_writer lock (pid=${STALE_PID:-unknown} dead or missing)" >&2
  MSG="stale lock recovered from dead pid ${STALE_PID:-unknown}; wrapper proceeding" \
    _record_walker_failure "stale-lock"
  rmdir "$LOCKDIR" 2>/dev/null || rm -rf "$LOCKDIR"
  mkdir "$LOCKDIR"
fi

echo "$$" > "$PIDFILE"
trap 'rm -rf "$LOCKDIR" 2>/dev/null' EXIT

"$PYTHON" "$SCRIPT"
