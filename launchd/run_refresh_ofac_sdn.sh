#!/usr/bin/env bash
#
# Daily refresh of the OFAC SDN digital-currency-address snapshot.
#
# Fetches the Treasury SDN.XML publication and extracts the
# digital-currency subset into ofac_sdn_addresses.json at repo root.
# Backed by write_walker_health_start/end so /walker_health + the L1
# pager see freshness (cadence 86400s = daily, staleness threshold
# 3x cadence = 72h before alarm).
#
# The wrapper handles:
#   1) env source (DATABASE_URL for walker_health writes) — launchd
#      does NOT inherit login shell env; same lesson as enrich_token_names.
#   2) Perl-alarm belt around the fetch (5-min default) — macOS ships
#      no `timeout`, and OFAC's endpoint has hung in the past.
#   3) walker_health start-write with cadence declaration + end-write
#      with the address-count message on ok, error message on fail.
#
# Atomic write of the JSON snapshot itself is inside refresh_ofac_sdn.py
# (temp-file + fsync + os.replace) — a mid-write kill no longer leaves
# a truncated file that check_data.py loads as empty coverage.
#
# Cadence: daily 06:00 EDT (per plist StartCalendarInterval). RunAtLoad
# is FALSE — a login after the scheduled hour won't force a re-fetch.

set -euo pipefail
PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin
export PATH

LOG_DIR="/Users/charliebruce/xrpl_test/launchd_logs"
LOG_FILE="${LOG_DIR}/refresh_ofac_sdn.$(date +%Y-%m-%d).log"
mkdir -p "$LOG_DIR"

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" | tee -a "$LOG_FILE"; }

REPO_ROOT="/Users/charliebruce/xrpl_test"
# Explicit venv Python: launchd PATH-resolved `python3` may resolve to
# an interpreter without psycopg installed. Same lesson as
# pg_backup_canary's six-day invisible-failure incident (2026-07-24 → 29).
VENV_PY="${REPO_ROOT}/venv/bin/python"

ENV_FILE="${XRPLDASHBOARD_ENV:-/Users/charliebruce/.config/xrpldashboard/env}"
set -a  # auto-export sourced vars — 2026-07-31 BetterStack silent-skip fix
# shellcheck disable=SC1090
[[ -r "$ENV_FILE" ]] && source "$ENV_FILE" || true
set +a
export DATABASE_URL="${DATABASE_URL:-}"

CADENCE_SEC=86400
FETCH_TIMEOUT_SEC="${OFAC_FETCH_TIMEOUT:-300}"

log "refresh_ofac_sdn start (timeout=${FETCH_TIMEOUT_SEC}s, cadence=${CADENCE_SEC}s)"

# walker_health start-write. Never fail the run because this failed —
# we want the fetch to attempt even if PG is unreachable.
"$VENV_PY" -c "
import sys; sys.path.insert(0,'$REPO_ROOT')
import db; db.write_walker_health_start('refresh_ofac_sdn', cadence_seconds=$CADENCE_SEC)
" 2>>"$LOG_FILE" || log "  walker_health start-write failed (proceeding)"

# Perl-alarm belt around the fetch. macOS base install has no `timeout`;
# BSD tooling would need coreutils. Perl's SIGALRM-based alarm is
# portable and guarantees a stuck TCP read on the SDN endpoint dies at
# FETCH_TIMEOUT_SEC rather than wedging the run forever.
set +e
perl -e 'alarm shift @ARGV; exec @ARGV or die "exec: $!"' \
     "$FETCH_TIMEOUT_SEC" "$VENV_PY" "$REPO_ROOT/scripts/refresh_ofac_sdn.py" \
     >>"$LOG_FILE" 2>&1
rc=$?
set -e

if (( rc == 0 )); then
  COUNT=$("$VENV_PY" -c "
import json
try:
    with open('$REPO_ROOT/ofac_sdn_addresses.json') as f:
        print(json.load(f).get('count', '?'))
except Exception as e:
    print('?')
" 2>/dev/null || echo '?')
  MSG="ok — ${COUNT} digital-currency addresses"
  log "  ${MSG}"
  "$VENV_PY" -c "
import sys; sys.path.insert(0,'$REPO_ROOT')
import db; db.write_walker_health_end('refresh_ofac_sdn', ok=True, message='${MSG}')
" 2>>"$LOG_FILE" || log "  walker_health end-write failed"
else
  MSG="fetch failed rc=${rc} (timeout=${FETCH_TIMEOUT_SEC}s)"
  log "  FAIL: ${MSG}"
  "$VENV_PY" -c "
import sys; sys.path.insert(0,'$REPO_ROOT')
import db; db.write_walker_health_end('refresh_ofac_sdn', ok=False, message='${MSG}')
" 2>>"$LOG_FILE" || log "  walker_health end-write failed"
fi

log "refresh_ofac_sdn end (rc=${rc})"
exit "${rc}"
