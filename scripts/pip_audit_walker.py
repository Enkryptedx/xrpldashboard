#!/usr/bin/env python3
"""pip-audit walker — weekly Python dependency vulnerability scan.

Runs pip-audit against requirements.txt and writes results into
walker_health so the L1 pager surfaces them.

Two distinct signals — do not conflate (lesson filed 2026-08-29):

  ok=True + findings_count=0 → pip-audit ran clean, no vulns. Green.
  ok=True + findings_count=N → pip-audit ran clean, N vulns surfaced.
      This is a paged signal via l1_pager.check_walker_findings, NOT
      a run failure. The walker did its job correctly. Bumping
      consecutive_failures here pages after 2 clean runs of a walker
      that's doing what it was built to do — the original bug.
  ok=False → the run itself broke: binary missing, subprocess timeout,
      JSON malformed, empty dep list. Increments consecutive_failures,
      pages via check_walker_failing after 3 consecutive breaks.

Message format on findings (surfaced to /walker_health + the pager):
    "N findings across M pkgs: pillow@11.0.0(PYSEC-2026-165,PYSEC-2026-2250),
     cryptography@48.0.0(CVE-2026-XYZ)"
Message format on run failure:
    "pip-audit run failed: <error summary>"

WALKER_MESSAGE_MUTES escape valve (see tools/l1_pager.py): when a
finding is acknowledged but not yet fixable, Charlie can add the CVE
ID substring to the mutes dict with an expiry date. The pager skips
the alert while that substring appears in the message; new findings
with different substrings still page. Applies to both check_walker_findings
and check_walker_failing paths — same escape valve, same fingerprint.

Cadence: 604800s (7 days). Launchd StartInterval matches.
"""
import datetime
import json
import logging
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db

logging.basicConfig(
    format="%(asctime)s [pip_audit_walker] %(levelname)s %(message)s",
    level=logging.INFO,
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

WALKER_NAME = "pip_audit_walker"
WALKER_CADENCE_SECONDS = 7 * 86400  # 604800 — matches plist StartInterval

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_REQUIREMENTS = os.path.join(REPO_ROOT, "requirements.txt")
DEFAULT_PIP_AUDIT = os.path.join(REPO_ROOT, "venv_py312", "bin", "pip-audit")
DEFAULT_TIMEOUT_SEC = 600  # 10 min — pip-audit is network-bound (advisory DB)


def _format_findings(deps: list[dict]) -> tuple[int, int, str]:
    """Turn pip-audit JSON `dependencies` list into (n_findings, n_pkgs, msg).

    n_findings counts distinct vuln IDs per package (not deduped
    across packages — same CVE hitting two packages counts twice, which
    matches how the reader thinks about "how many rows have a vuln").
    n_pkgs counts packages with ≥1 vuln.
    msg is the accumulated finding string used by WALKER_MESSAGE_MUTES.
    """
    hits = []
    n_findings = 0
    for dep in deps:
        vulns = dep.get("vulns") or []
        if not vulns:
            continue
        ids = sorted({v.get("id", "?") for v in vulns})
        n_findings += len(ids)
        name = dep.get("name", "?")
        ver = dep.get("version", "?")
        hits.append(f"{name}@{ver}({','.join(ids)})")
    n_pkgs = len(hits)
    msg = "; ".join(hits) if hits else ""
    return n_findings, n_pkgs, msg


def run_pip_audit(requirements: str, pip_audit_bin: str,
                  timeout_sec: int) -> tuple[bool, int, str]:
    """Run pip-audit, parse JSON, return (ok, findings_count, message).

    ok=True → pip-audit *executed* cleanly (subprocess ran, JSON parsed).
              findings_count may be 0 or N — both are clean executions.
              Reserving ok=False for actual run failures avoids the
              conflation lesson from 2026-08-29: consecutive_failures
              incremented for two PERFECT runs that found 21 CVEs.
    ok=False → the run itself broke (binary missing, JSON malformed,
               subprocess timeout, empty dep list). findings_count=0
               is a "we couldn't tell" default in this branch.

    Distinct signals: consecutive_failures tracks run failures; the
    pager fires on findings_count > 0 via check_walker_findings() with
    the same WALKER_MESSAGE_MUTES escape valve that gates run failures.
    """
    if not os.path.exists(pip_audit_bin):
        return False, 0, f"pip-audit run failed: binary missing at {pip_audit_bin}"
    if not os.path.exists(requirements):
        return False, 0, f"pip-audit run failed: requirements file missing at {requirements}"

    try:
        result = subprocess.run(
            [pip_audit_bin, "-r", requirements, "--format", "json"],
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
    except subprocess.TimeoutExpired:
        return False, 0, f"pip-audit run failed: timeout after {timeout_sec}s"
    except OSError as e:
        return False, 0, f"pip-audit run failed: {e.__class__.__name__}: {e}"

    # pip-audit exits 1 when it finds vulns — that's the SIGNAL, not a
    # run failure. We only treat exit 2 (audit error) as a run failure.
    # Even so, we parse stdout first because valid JSON on stdout with
    # a non-zero exit is still parseable dependency data.
    stdout = result.stdout or ""
    stderr = result.stderr or ""

    try:
        payload = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        # No JSON = pip-audit didn't get to a report. Surface stderr tail.
        tail = (stderr[-200:] or stdout[-200:] or "no output").strip()
        return False, 0, f"pip-audit run failed: no JSON output (exit={result.returncode}): {tail}"

    deps = payload.get("dependencies") or []
    if not isinstance(deps, list) or not deps:
        return False, 0, f"pip-audit run failed: empty dependency list in output (exit={result.returncode})"

    n_findings, n_pkgs, hit_msg = _format_findings(deps)

    if n_findings == 0:
        return True, 0, f"0 findings — {len(deps)} packages checked"

    return True, n_findings, f"{n_findings} findings across {n_pkgs} pkgs: {hit_msg}"


def main() -> int:
    requirements = os.environ.get("PIP_AUDIT_REQUIREMENTS", DEFAULT_REQUIREMENTS)
    pip_audit_bin = os.environ.get("PIP_AUDIT_BIN", DEFAULT_PIP_AUDIT)
    timeout_sec = int(os.environ.get("PIP_AUDIT_TIMEOUT_SEC", DEFAULT_TIMEOUT_SEC))

    db.write_walker_health_start(WALKER_NAME, cadence_seconds=WALKER_CADENCE_SECONDS)

    log.info("pip-audit start: bin=%s reqs=%s timeout=%ds",
             pip_audit_bin, requirements, timeout_sec)

    ok, n_findings, msg = run_pip_audit(requirements, pip_audit_bin, timeout_sec)
    if ok and n_findings == 0:
        log.info("pip-audit PASS: %s", msg)
    elif ok:
        log.warning("pip-audit ran cleanly, %d findings surfaced: %s", n_findings, msg)
    else:
        log.error("pip-audit FAIL: %s", msg)

    db.write_walker_health_end(
        WALKER_NAME,
        ok=ok,
        message=msg,
        findings_count=n_findings if ok else None,
    )
    # Exit non-zero only on true run failures. Findings on a clean run
    # are a paged signal via findings_count, not a launchd-visible fault.
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
