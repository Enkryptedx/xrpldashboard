"""Guard walker: verify the sovereign XRPL_ env vars in
~/.config/xrpldashboard/env point at :5006 (Lenovo LAN non-admin RPC port
established Wave-0 2026-08-31).

Runs hourly per launchd. Reads the env FILE directly (not runtime env) —
we want to detect drift in the file itself. Any drift → walker_health
findings_count > 0 + message identifying which var(s). BetterStack /
/walker_health surface the alert on findings_count > 0.

Codified 2026-08-31 after an accidental scrollback-paste of a step (d)
`mv env.bak-2026-08-31 env` reversibility command reverted XRPL_LOCAL_NODE
:5006 → :5005 in a ~40-minute window. Simple value-drift detection —
no bak-file fingerprinting (per ruling: catch the drift regardless of cause).

── Why XRPL_CLIO_NODE is gone (Charlie ruling 2026-11-27) ──────────────

This guard shipped 2026-08-31 13:10 pinning FIVE vars to one address,
hours after the Wave-0 re-point that had just broken the MPT holders walk.
One of the five was literally named XRPL_CLIO_NODE, and `mpt_holders` is a
Clio-only method that rippled 3.4.1 answers with unknownCmd. So for 88 days
this guard PAGED IF A CLIO VARIABLE EVER POINTED AT CLIO, and reported
clean the entire time the walk was dead.

Two separate faults, worth keeping apart:

  1. The variable was dead. Audited 2026-11-27: XRPL_CLIO_NODE was read by
     NO production code — only by this guard, which pinned it.
     docs/SOVEREIGNTY_AUDIT.md GAP-3 had already recorded "no longer read
     by code" in September. Pinning it wrong produced zero symptoms AND
     zero signal, which is worse than having no variable at all: it reads
     as configured.
  2. The guard encoded "sovereign" as equality to a single address. A
     capability ("this endpoint answers the methods we call") is not a
     string comparison, and a guard built on the string will defend a
     method outage while reporting clean.

The single Clio setting is now MPT_HOLDERS_NODES, consumed through
tools/anchor_nodes.mpt_holders_nodes(), where the endpoint list, the env
override and the measurement that established the Clio-only finding all
live together. It is deliberately NOT guarded here: it must NOT equal our
own node, so pinning it to EXPECTED_VALUE would re-create fault 1 exactly.

XRPL_CLIO_NODE is therefore RETIRED rather than merely deleted — see
RETIRED below. Silently dropping it from EXPECTED would have left the guard
indifferent to its return.

── Guard-the-guard ─────────────────────────────────────────────

The old loop iterated EXPECTED and never looked at what the file actually
contained, so any XRPL_ variable absent from EXPECTED was invisible to it.
A sixth var could be added to the env file and this guard would report
"clean: all 5 XRPL_ vars" forever — the same shape of blind spot as a
walker that exits 0 while doing nothing.

The key sets must now MATCH: every XRPL_ variable present in the file has
to be either guarded (EXPECTED) or explicitly retired (RETIRED). Anything
else is an unguarded-variable finding. Reported by KEY NAME only — for an
unguarded variable there is no expected value to compare against, so the
name IS the finding, and the guard has no reason to copy unknown config
values into walker_health.
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

# Every sovereign var must point at Lenovo :5006. Any deviation is drift.
#
# These four are each read by real code paths that our own node CAN serve:
# XRPL_LOCAL_NODE / XRPL_NODE / XRPL_RPC broadly, and XRPL_MPT_NODE by
# mpt_data.py (the ledger_data walk + ledger_entry), both verified working
# against rippled 3.4.1 on 2026-11-27.
EXPECTED_VALUE = "http://192.168.40.95:5006"
EXPECTED = {
    "XRPL_LOCAL_NODE": EXPECTED_VALUE,
    "XRPL_NODE":       EXPECTED_VALUE,
    "XRPL_MPT_NODE":   EXPECTED_VALUE,
    "XRPL_RPC":        EXPECTED_VALUE,
}

# Variables deliberately removed from the env file. Present again => finding.
#
# Retirement is an assertion, not an absence. If XRPL_CLIO_NODE reappears,
# something restored an old env file (exactly the 2026-08-31 incident class
# this guard was built for) or someone re-added a Clio knob that no code
# reads. Either way it should be said out loud rather than ignored.
RETIRED = {
    "XRPL_CLIO_NODE": (
        "retired 2026-11-27: read by no production code and pinned to our "
        "own rippled, which cannot serve the Clio-only mpt_holders method. "
        "The single Clio setting is MPT_HOLDERS_NODES."
    ),
}

# Every XRPL_* assignment in the env file, so the guard can compare its own
# key set against reality instead of trusting EXPECTED to be complete.
_XRPL_KEY_RE = re.compile(r'^\s*(?:export\s+)?(XRPL_[A-Z0-9_]+)\s*=', re.M)


def _env_xrpl_keys(content: str) -> set[str]:
    """Names of every XRPL_ variable assigned in the env file.

    Names only, never values: this feeds the unguarded-variable finding,
    where by definition we have no expected value to compare against.
    """
    return set(_XRPL_KEY_RE.findall(content))


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


def evaluate(content: str) -> list[str]:
    """Every finding for one env-file body, as human-readable strings.

    Pure: no DB, no filesystem, no exit. main() is a thin wrapper around
    this so the findings logic is testable by CALLING it rather than by a
    test that re-implements the same loop and agrees with itself.

    Three independent axes:
      * each EXPECTED var missing or not equal to EXPECTED_VALUE
      * any RETIRED var present again
      * any XRPL_ var in the file that is neither EXPECTED nor RETIRED
        (guard-the-guard: the key sets must match)

    The last two report KEY NAMES ONLY — there is no expected value to
    compare an unguarded or retired variable against, so its value would
    be noise in walker_health rather than a diagnostic.
    """
    drift_msgs: list[str] = []
    for key, expected in EXPECTED.items():
        actual = _parse_env_var(content, key)
        if actual is None:
            drift_msgs.append(f"{key}=<MISSING>")
        elif actual != expected:
            drift_msgs.append(f"{key}={actual!r} (expected {expected!r})")

    present = _env_xrpl_keys(content)
    for key in sorted(present & set(RETIRED)):
        drift_msgs.append(f"{key}=<RETIRED-BUT-PRESENT>")
    for key in sorted(present - set(EXPECTED) - set(RETIRED)):
        drift_msgs.append(f"{key}=<UNGUARDED>")

    return drift_msgs


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

    drift_msgs = evaluate(content)
    findings = len(drift_msgs)
    if findings == 0:
        message = (
            f"clean: all {len(EXPECTED)} sovereign XRPL_ vars = {EXPECTED_VALUE}; "
            f"key sets match (no retired, no unguarded)"
        )
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
