#!/usr/bin/env bash
#
# Receipt-signing service wrapper. Runs the Flask app under the repo
# venv. Bound to 127.0.0.1 only — external reach is via the Cloudflare
# Tunnel that terminates at this loopback address (sig.xrpldashboard.com
# → 127.0.0.1:8842).
#
# The service starts LOCKED. Charlie unlocks after each service start
# via a localhost curl against POST /unlock — the passphrase is on
# paper only, never in env or Keychain. See docs/SIG_SERVICE.md.
#
# Passphrase is intentionally NOT read from the env file. Do not add
# a "convenience" SIG_SERVICE_PASSPHRASE variable to the env — that
# would defeat the paper-only custody rule.

set -euo pipefail

REPO="/Users/charliebruce/xrpl_test"
PYTHON="${REPO}/venv/bin/python3"
SERVICE="${REPO}/sig_service.py"
BIND="${SIG_SERVICE_BIND:-127.0.0.1:8842}"

LOG_DIR="${REPO}/launchd_logs"
mkdir -p "${LOG_DIR}" "${LOG_DIR}/sig_service"

log() {
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"
}

if [[ ! -x "${PYTHON}" ]]; then
  log "FAIL: repo venv Python missing at ${PYTHON}"
  exit 1
fi

if [[ ! -f "${SERVICE}" ]]; then
  log "FAIL: sig_service.py missing at ${SERVICE}"
  exit 1
fi

if [[ ! -f "${HOME}/.config/xrpldashboard/receipt_ed25519_enc.pem" ]]; then
  log "FAIL: encrypted receipt private key missing at ~/.config/xrpldashboard/receipt_ed25519_enc.pem"
  log "      Generate via docs/SIG_SERVICE.md § Install (Mac side)."
  exit 1
fi

log "sig_service start (bind=${BIND})"
exec "${PYTHON}" "${SERVICE}" --bind "${BIND}"
