#!/bin/bash
# Wrapper invoked by com.charliebruce.xrpldashboard.lending_snapshot.plist.
#
# XLS-66 lending snapshot writer. Dormant pre-activation: writes an
# "activated=False" snapshot in ~0.3s. Post-activation, walks
# LoanBroker/Vault/Loan ledger objects and enriches top-N brokers with
# mpt_holders for depositor counts.
#
# The wrapper is required for walker_health writes. Previously the plist
# invoked python3 directly, but launchd doesn't inherit the login shell
# env, so DATABASE_URL was unset at runtime. db._get_writer_conn()
# returned None, write_walker_health_end() silently no-op'd, and
# walker_health.lending_snapshot went stale on 2026-05-31 while the
# JSON snapshot kept writing every 15 min. Coverage Register flagged it
# 2026-07-07 (37d stale). Fix: source the env file before exec, same
# pattern as coverage_register_walker + credentials_walker.
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
SCRIPT="/Users/charliebruce/xrpl_test/lending_snapshot.py"

exec "$PYTHON" "$SCRIPT"
