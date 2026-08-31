"""Guard walker: verify all 5 XRPL_ env vars in ~/.config/xrpldashboard/env
point at :5006 (Lenovo LAN non-admin RPC port established Wave-0 2026-08-31).

Runs hourly per launchd. Reads the env FILE directly (not runtime env) —
we want to detect drift in the file itself. Any drift → walker_health
findings_count > 0 + message identifying which var(s). BetterStack /
/walker_health surface the alert on findings_count > 0.

Codified 2026-08-31 after an accidental scrollback-paste of a step (d)
`mv env.bak-2026-08-31 env` reversibility command reverted XRPL_LOCAL_NODE
:5006 → :5005 in a ~40-minute window. Simple value-drift detection —
no bak-file fingerprinting (per ruling: catch the drift regardless of cause).
"""

from __future__ import annotations

import logging
import os
import re
import sys

import db

WALKER_NAME = "xrpl_env_drift_guard"
WALKER_CADENCE_SECONDS = 3600  # hourly

ENV_FILE = os.path.expanduser("~/.config/xrpldashboard/env")

# All 5 vars must point at Lenovo :5006. Any deviation is drift.
EXPECTED_VALUE = "http://192.168.40.95:5006"
EXPECTED = {
    "XRPL_LOCAL_NODE": EXPECTED_VALUE,
    "XRPL_NODE":       EXPECTED_VALUE,
    "XRPL_MPT_NODE":   EXPECTED_VALUE,
    "XRPL_CLIO_NODE":  EXPECTED_VALUE,
    "XRPL_RPC":        EXPECTED_VALUE,
}


def _parse_env_var(content: str, key: str) -> str | None:
    """Extract value from `export KEY=...` or `KEY=...`. Handles bare,
    double-quoted, and single-quoted forms. Returns None if not found."""
    pattern = rf'^\s*(?:export\s+)?{re.escape(key)}=(?P<val>.+?)\s*$'
    for line in content.splitlines():
        m = re.match(pattern, line)
        if m:
            val = m.group("val").strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
                val = val[1:-1]
            return val
    return None


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    db.write_walker_health_start(WALKER_NAME, cadence_seconds=WALKER_CADENCE_SECONDS)

    try:
        with open(ENV_FILE, "r") as f:
            content = f.read()
    except OSError as e:
        db.write_walker_health_end(
            WALKER_NAME,
            ok=False,
            message=f"env file unreadable: {type(e).__name__}: {e}",
        )
        sys.exit(1)

    drift_msgs: list[str] = []
    for key, expected in EXPECTED.items():
        actual = _parse_env_var(content, key)
        if actual is None:
            drift_msgs.append(f"{key}=<MISSING>")
        elif actual != expected:
            drift_msgs.append(f"{key}={actual!r} (expected {expected!r})")

    findings = len(drift_msgs)
    if findings == 0:
        message = f"clean: all {len(EXPECTED)} XRPL_ vars = {EXPECTED_VALUE}"
    else:
        message = f"DRIFT[{findings}]: {'; '.join(drift_msgs)}"

    # ok=True even with findings — the walker itself ran to completion.
    # findings_count is the alert axis; walker_health separates it from
    # ok/consecutive_failures (see write_walker_health_end docstring).
    logging.info(message)
    db.write_walker_health_end(
        WALKER_NAME,
        ok=True,
        message=message,
        findings_count=findings,
    )
    sys.exit(0)


if __name__ == "__main__":
    main()
