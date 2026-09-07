#!/usr/bin/env bash
#
# Hourly memory-index size-guard walker.
#
# Runs scripts/memory_index_size_guard.py to verify MEMORY.md stays
# under the claude auto-memory read window (~25000B / ~200 lines).
# Pages via walker_health if it approaches the cap before a rule is
# silently truncated. Wired 2026-08-31 with the memory restructure.

set -euo pipefail
PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin
export PATH

REPO_ROOT="/Users/charliebruce/xrpl_test"
LOG_DIR="${REPO_ROOT}/launchd_logs"
LOG_FILE="${LOG_DIR}/memory_index_size_guard.$(date +%Y-%m-%d).log"
mkdir -p "$LOG_DIR"

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" | tee -a "$LOG_FILE"; }

VENV_PY="${REPO_ROOT}/venv_py311/bin/python"

ENV_FILE="${XRPLDASHBOARD_ENV:-/Users/charliebruce/.config/xrpldashboard/env}"
set -a
# shellcheck disable=SC1090
[[ -r "$ENV_FILE" ]] && source "$ENV_FILE" || true
set +a
export DATABASE_URL="${DATABASE_URL:-}"

WRAPPER_TIMEOUT_SEC="${MEMORY_GUARD_WRAPPER_TIMEOUT:-60}"

log "memory_index_size_guard start (wrapper_timeout=${WRAPPER_TIMEOUT_SEC}s)"

set +e
perl -e 'alarm shift @ARGV; exec @ARGV or die "exec: $!"' \
     "$WRAPPER_TIMEOUT_SEC" "$VENV_PY" "$REPO_ROOT/scripts/memory_index_size_guard.py" \
     >>"$LOG_FILE" 2>&1
rc=$?
set -e

log "memory_index_size_guard end (rc=${rc})"
exit "${rc}"
