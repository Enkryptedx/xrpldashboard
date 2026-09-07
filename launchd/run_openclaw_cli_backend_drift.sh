#!/usr/bin/env bash
#
# Hourly OpenClaw CLI-backend drift walker.
#
# Runs scripts/openclaw_cli_backend_drift.py to verify the
# --fallback-model claude-opus-4-8 monkey-patch is present in every
# live cli-backend-*.js file under OPENCLAW_DIST_DIR. Pages via
# walker_health if the flag disappears (Homebrew/npm upgrade side-effect).
#
# Cadence: 3600s (1 hour). Alarm surface: /walker_health row for
# openclaw_cli_backend_drift with findings_count>0 fires the L1 pager
# on the next tick. Flag re-appearance auto-clears via the walker's
# next green run.

set -euo pipefail
PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin
export PATH

REPO_ROOT="/Users/charliebruce/xrpl_test"
LOG_DIR="${REPO_ROOT}/launchd_logs"
LOG_FILE="${LOG_DIR}/openclaw_cli_backend_drift.$(date +%Y-%m-%d).log"
mkdir -p "$LOG_DIR"

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" | tee -a "$LOG_FILE"; }

VENV_PY="${REPO_ROOT}/venv_py311/bin/python"

ENV_FILE="${XRPLDASHBOARD_ENV:-/Users/charliebruce/.config/xrpldashboard/env}"
set -a
# shellcheck disable=SC1090
[[ -r "$ENV_FILE" ]] && source "$ENV_FILE" || true
set +a
export DATABASE_URL="${DATABASE_URL:-}"

# Timeout belt — a filesystem scan of a small dist dir should take
# well under a second; 60s is generous even for a wedged NFS mount.
WRAPPER_TIMEOUT_SEC="${OPENCLAW_DRIFT_WRAPPER_TIMEOUT:-60}"

log "openclaw_cli_backend_drift start (wrapper_timeout=${WRAPPER_TIMEOUT_SEC}s)"

set +e
perl -e 'alarm shift @ARGV; exec @ARGV or die "exec: $!"' \
     "$WRAPPER_TIMEOUT_SEC" "$VENV_PY" "$REPO_ROOT/scripts/openclaw_cli_backend_drift.py" \
     >>"$LOG_FILE" 2>&1
rc=$?
set -e

log "openclaw_cli_backend_drift end (rc=${rc})"
exit "${rc}"
