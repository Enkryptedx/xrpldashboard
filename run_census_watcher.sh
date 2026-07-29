#!/bin/bash
# Launcher for census_watcher.py — sources env, uses venv python for psycopg.
# One-shot: exits with the walker's own exit code (0 census-ok, 2 waited-out,
# 130 interrupted, 3 unhandled, other = census walker's own non-zero).

set -u
cd /Users/charliebruce/xrpl_test
source ~/.config/xrpldashboard/env

STAMP=$(date +%Y-%m-%d_%H%M)
LOG=/Users/charliebruce/xrpl_test/launchd_logs/census_watcher_${STAMP}.log
PIDFILE=/Users/charliebruce/xrpl_test/census_watcher.pid

mkdir -p /Users/charliebruce/xrpl_test/launchd_logs
echo $$ > "$PIDFILE"

./venv/bin/python3 census_watcher.py >> "$LOG" 2>&1
EXIT=$?

rm -f "$PIDFILE"
exit $EXIT
