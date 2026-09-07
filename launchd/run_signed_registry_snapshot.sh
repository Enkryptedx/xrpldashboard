#!/usr/bin/env bash
#
# Daily signed registry snapshot wrapper. Fires once per UTC day at
# 02:15 UTC (75 minutes after the daily signed_snapshot at 01:00 UTC —
# leaves headroom for both to complete cleanly).
#
# Reads registry state via signed_snapshot.collect_registry_state(),
# calls the sig-service /sign endpoint with kind=registry, writes
# signed_registry_snapshots/YYYY-MM-DD.json. Serves at
# /.well-known/registry/<date>.json via app.py.
#
# Fail modes:
#   - sig-service locked → walker exits non-zero, no file written,
#     next cycle retries the next day
#   - Postgres unavailable → walker exits non-zero, same retry semantics
#   - Neither is fatal to the site — the daily anchored snapshot chain
#     still commits registry_state (Merkle root) even if this walker
#     misses a day
set -euo pipefail

REPO="/Users/charliebruce/xrpl_test"
PYTHON="${REPO}/venv/bin/python3"
SCRIPT="${REPO}/signed_registry_snapshot.py"
LOG_DIR="${REPO}/launchd_logs"

mkdir -p "${LOG_DIR}"

log() {
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"
}

ENV_FILE="${HOME}/.config/xrpldashboard/env"
if [[ -f "${ENV_FILE}" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
  set +a
else
  log "FAIL: env file missing at ${ENV_FILE}"
  exit 1
fi

if [[ ! -x "${PYTHON}" ]]; then
  log "FAIL: repo venv Python missing at ${PYTHON}"
  exit 1
fi

log "signed_registry_snapshot start"
exec "${PYTHON}" "${SCRIPT}"
