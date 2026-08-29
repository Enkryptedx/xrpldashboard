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

  # Auto-commit + push refreshed snapshot so Render redeploys with fresh
  # OFAC coverage. Guard: no diff = no commit (avoids empty daily commits
  # when the SDN list hasn't changed). Push failure is LOUD — nonzero exit
  # + walker_health end-write ok=False so the L1 pager surfaces it.
  #
  # Pathspec edge: `git commit -m ... -- "$GIT_SNAPSHOT"` scopes the
  # commit to that one file, but any hand-edited-but-uncommitted local
  # changes to ofac_sdn_addresses.json will get swept into cron's commit
  # too (working-tree state for that pathspec, ignoring the index).
  # Known + acceptable — this file is machine-owned; a human diff on it
  # is a rare edge and cron's daily commit is the right absorb point.
  cd "$REPO_ROOT"
  GIT_SNAPSHOT="ofac_sdn_addresses.json"
  if [[ -z "$(git status --porcelain -- "$GIT_SNAPSHOT" 2>/dev/null)" ]]; then
    log "  git: ${GIT_SNAPSHOT} unchanged — skip commit/push"
  else
    log "  git: ${GIT_SNAPSHOT} changed — commit+push"
    GIT_COMMIT_MSG="OFAC SDN daily refresh — ${COUNT} addresses ($(date -u +%Y-%m-%d))"
    if ! git commit -m "$GIT_COMMIT_MSG" -- "$GIT_SNAPSHOT" >>"$LOG_FILE" 2>&1; then
      GIT_FAIL="FAIL: git commit refused for ${GIT_SNAPSHOT}"
      log "  ${GIT_FAIL}"
      "$VENV_PY" -c "
import sys; sys.path.insert(0,'$REPO_ROOT')
import db; db.write_walker_health_end('refresh_ofac_sdn', ok=False, message='${GIT_FAIL}')
" 2>>"$LOG_FILE" || log "  walker_health end-write failed"
      exit 3
    fi
    PUSH_ARGS=""
    if [[ "${OFAC_GIT_DRY_RUN:-0}" == "1" ]]; then
      PUSH_ARGS="--dry-run"
      log "  git: OFAC_GIT_DRY_RUN=1 — push with --dry-run"
    fi
    if ! GIT_TERMINAL_PROMPT=0 GIT_ASKPASS=/bin/true SSH_ASKPASS=/bin/true \
         git push $PUSH_ARGS origin HEAD >>"$LOG_FILE" 2>&1; then
      GIT_FAIL="FAIL: git push refused — Render will not see refresh"
      log "  ${GIT_FAIL}"
      "$VENV_PY" -c "
import sys; sys.path.insert(0,'$REPO_ROOT')
import db; db.write_walker_health_end('refresh_ofac_sdn', ok=False, message='${GIT_FAIL}')
" 2>>"$LOG_FILE" || log "  walker_health end-write failed"
      exit 4
    fi
    log "  git: commit+push ok"
    MSG="ok — ${COUNT} addresses (pushed)"
  fi

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
