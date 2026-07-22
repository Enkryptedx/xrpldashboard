#!/bin/bash
# Wrapper for com.charliebruce.xrpldashboard.answer_plausibility_walker.plist.
#
# Phase 1 seed of the Truth Audit Layer 2 (Answer Plausibility) walker.
# Reads live Neon and evaluates R1/R2/R4 + UNDECLARED_WALKER against the
# metric inventory in docs/TRUTH_AUDIT_DESIGN.md. Alarms land in
# answer_plausibility_alarms (append-only); walker_health.ok is TRUE
# whenever the walker itself ran cleanly.
set -euo pipefail

ENV_FILE="$HOME/.config/xrpldashboard/env"
if [[ ! -r "$ENV_FILE" ]]; then
  echo "[$(date '+%F %T')] ERROR: env file missing or unreadable: $ENV_FILE" >&2
  exit 78  # EX_CONFIG
fi

# shellcheck disable=SC1090
source "$ENV_FILE"

PYTHON="/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"
SCRIPT="/Users/charliebruce/xrpl_test/answer_plausibility_walker.py"

exec "$PYTHON" "$SCRIPT"
