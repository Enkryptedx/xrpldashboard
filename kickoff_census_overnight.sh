#!/bin/bash
# One-shot overnight kickoff for census_escrow_phase1c.py.
# Sleeps until target time then runs the walker with hardened preconditions.
# Written 2026-07-12 15:10 EDT for a fresh-anchor re-run after 2026-07-12
# walk truncated at 30% coverage (silent marker-null failure at load=505).

set -u

TARGET="2026-07-13 02:30:00"
LOG_DIR="/Users/charliebruce/xrpl_test/launchd_logs"
WALKER="/Users/charliebruce/xrpl_test/census_escrow_phase1c.py"
PIDFILE="/Users/charliebruce/xrpl_test/kickoff_census_overnight.pid"

# macOS-flavored date parse
TARGET_EPOCH=$(date -j -f "%Y-%m-%d %H:%M:%S" "$TARGET" +%s)
NOW_EPOCH=$(date +%s)
DELAY=$(( TARGET_EPOCH - NOW_EPOCH ))

STAMP=$(date +%Y-%m-%d_%H%M)
LOG="${LOG_DIR}/census_escrow_phase1c_overnight_${STAMP}.log"

mkdir -p "$LOG_DIR"

{
  echo "kickoff_census_overnight: launched at $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  echo "target local time: $TARGET"
  echo "delay seconds: $DELAY"
  echo "walker: $WALKER"
  echo "pidfile: $PIDFILE"
  echo "---"
} >> "$LOG"

echo $$ > "$PIDFILE"

if [ "$DELAY" -gt 0 ]; then
  sleep "$DELAY"
fi

echo "wakeup at $(date -u '+%Y-%m-%dT%H:%M:%SZ'), launching census walker" >> "$LOG"
cd /Users/charliebruce/xrpl_test
/usr/bin/python3 "$WALKER" >> "$LOG" 2>&1
EXIT=$?
echo "census walker exited with status $EXIT at $(date -u '+%Y-%m-%dT%H:%M:%SZ')" >> "$LOG"

rm -f "$PIDFILE"
exit $EXIT
