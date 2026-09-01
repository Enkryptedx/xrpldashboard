#!/usr/bin/env bash
#
# Daily walker: refresh site_totals table + docs/SITE_TOTALS.json.
# Single source of truth for authoritative all-time counts (total_hits,
# human_hits, bot_hits, countries_all, countries_human, and T1-excluding
# variants). See ~/xrpl_test/site_totals_walker.py for full definitions
# and monotonic-decrease guard.
#
# Cadence: StartInterval=86400 (24h). Timing is load-anchored per Charlie's
# macos_startinterval_over_calendar rule — hour-of-day is not critical since
# it's a once-daily brief query (<1s wall-clock).
#
# Wrapper timeout belt: 60s. The query is trivial (few aggregate scans over
# page_views + one UPSERT + one JSON write); 60s covers a Neon cold-start
# edge case.

set -u
PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin
export PATH

LOG_DIR="/Users/charliebruce/xrpl_test/launchd_logs"
LOG_FILE="${LOG_DIR}/site_totals_walker.$(date +%Y-%m-%d).log"
mkdir -p "$LOG_DIR"

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" | tee -a "$LOG_FILE"; }

# Source env for DATABASE_URL_DIRECT + DATABASE_URL (walker_health + upsert).
ENV_FILE="$HOME/.config/xrpldashboard/env"
if [[ ! -r "$ENV_FILE" ]]; then
  log "ERROR: env file missing or unreadable: $ENV_FILE"
  exit 78  # EX_CONFIG
fi
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

log "site_totals_walker start (wrapper_timeout=60s)"

PYTHON="/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"
SCRIPT="/Users/charliebruce/xrpl_test/site_totals_walker.py"

perl -e 'alarm shift; exec @ARGV' 60 "$PYTHON" "$SCRIPT" 2>&1 | tee -a "$LOG_FILE"
RC=${PIPESTATUS[0]}

log "site_totals_walker end (rc=${RC})"
exit "$RC"
