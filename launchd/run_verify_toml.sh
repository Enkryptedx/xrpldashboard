#!/bin/bash
# Wrapper invoked by com.charliebruce.xrpldashboard.verify_toml.plist.
#
# Weekly first-party identity scanner. Discovers new XRPL accounts via
# xrp-ledger.toml and their on-chain Domain field; writes to
# named_accounts.json AND (via db.upsert_account_label) to Postgres
# account_labels.
#
# The wrapper is required because launchd doesn't inherit the login
# shell env. Without sourcing ~/.config/xrpldashboard/env, DATABASE_URL
# is unset — db._get_writer_conn() returns None — and both the
# walker_health writes AND the account_labels dual-writes silently
# no-op. Same latent bug as lending_snapshot (surfaced 2026-07-08).
# Named-accounts.json still writes fine because that's a local file
# path with no DB dep; the Postgres side was the silent half.
#
# 2026-09-04 rework:
#   * --limit 15000: bounded per-run walk (raised from 10000 after
#     the first bounded run measured 4.86 addr/sec on LAN rippled;
#     15k fits comfortably inside the 3600s wrapper timeout).
#   * Two-phase walk (Phase A = addresses first-seen in events.db
#     since last run's HWM, capped at 5000; Phase B = cursor walk
#     through the old active-address set with remaining budget). New
#     addresses are the only set with any real chance of gaining a
#     fresh Domain field, so they get first crack at the budget.
#   * Cursor in launchd_state/verify_toml_cursor.json (last_address
#     for Phase B, last_new_scan_ts high-water mark for Phase A).
#   * Perl SIGALRM belt at 3600s (1 hour): matches rippled_cfg_drift_
#     guard's pattern. In-script SIGALRM handler preserves cursor +
#     writes clean walker_health_end(ok=false, timeout).
#   * Env sourcing also exports XRPL_LOCAL_NODE, which the Python
#     script now prefers over XRPL_RPC — LAN Lenovo rippled instead
#     of Ripple public s1.
set -euo pipefail

ENV_FILE="$HOME/.config/xrpldashboard/env"
if [[ ! -r "$ENV_FILE" ]]; then
  echo "[$(date '+%F %T')] ERROR: env file missing or unreadable: $ENV_FILE" >&2
  exit 78  # EX_CONFIG
fi

set -a  # auto-export sourced vars — 2026-07-31 BetterStack silent-skip fix
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

PYTHON="/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"
SCRIPT="/Users/charliebruce/xrpl_test/verify_toml_accounts.py"
LIMIT=15000
TIMEOUT=3600  # 60 min. First-run measurement: 4.86 addresses/sec on LAN
              # = ~3086s for 15000 addresses. Comfortable margin. Raise
              # LIMIT (not TIMEOUT) if we want faster cycle-through of
              # events.db's ~500k active addresses.

LOG_DIR="/Users/charliebruce/xrpl_test/launchd_logs"
mkdir -p "$LOG_DIR"

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] verify_toml start (wrapper_timeout=${TIMEOUT}s, --limit=${LIMIT})"

perl -e 'alarm shift; exec @ARGV' "$TIMEOUT" "$PYTHON" "$SCRIPT" --limit "$LIMIT"
RC=$?

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] verify_toml end (rc=${RC})"
exit "$RC"
