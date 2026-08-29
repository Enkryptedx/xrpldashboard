#!/usr/bin/env bash
#
# Weekly pip-audit walker — Python dependency vulnerability scan.
#
# Runs scripts/pip_audit_walker.py against requirements.txt using the
# dedicated tooling venv (venv_py312/bin/pip-audit). The Python walker
# handles its own walker_health start/end writes; this wrapper only
# provides env source + timeout belt + log capture.
#
# Cadence: 604800s (7 days). Alarm surface: /walker_health row for
# pip_audit_walker fires the L1 pager when consecutive_failures ≥3.
# WALKER_MESSAGE_MUTES in tools/l1_pager.py is the acknowledged-finding
# escape valve — silence a specific CVE ID substring by expiry date
# without silencing new findings.

set -euo pipefail
PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin
export PATH

REPO_ROOT="/Users/charliebruce/xrpl_test"
LOG_DIR="${REPO_ROOT}/launchd_logs"
LOG_FILE="${LOG_DIR}/pip_audit_walker.$(date +%Y-%m-%d).log"
mkdir -p "$LOG_DIR"

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" | tee -a "$LOG_FILE"; }

# Runner Python — the app venv (Python 3.11), used because it has the
# db.py-required deps (psycopg) already installed. The AUDITED venv is
# venv_py312 (tooling only, holds pip-audit itself).
VENV_PY="${REPO_ROOT}/venv_py311/bin/python"

ENV_FILE="${XRPLDASHBOARD_ENV:-/Users/charliebruce/.config/xrpldashboard/env}"
set -a
# shellcheck disable=SC1090
[[ -r "$ENV_FILE" ]] && source "$ENV_FILE" || true
set +a
export DATABASE_URL="${DATABASE_URL:-}"

# Timeout belt — pip-audit is network-bound (PyPI advisory DB). Cap the
# whole wrapper at 12 min so a wedged network read can't hang the run.
# The Python walker itself has its own 10-min subprocess timeout; this
# belt is defense in depth. macOS has no `timeout` — use perl SIGALRM.
WRAPPER_TIMEOUT_SEC="${PIP_AUDIT_WRAPPER_TIMEOUT:-720}"

log "pip_audit_walker start (wrapper_timeout=${WRAPPER_TIMEOUT_SEC}s)"

set +e
perl -e 'alarm shift @ARGV; exec @ARGV or die "exec: $!"' \
     "$WRAPPER_TIMEOUT_SEC" "$VENV_PY" "$REPO_ROOT/scripts/pip_audit_walker.py" \
     >>"$LOG_FILE" 2>&1
rc=$?
set -e

log "pip_audit_walker end (rc=${rc})"
exit "${rc}"
