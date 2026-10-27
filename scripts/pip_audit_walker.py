#!/usr/bin/env python3
"""pip-audit walker — weekly Python dependency vulnerability scan.

Runs pip-audit against requirements.txt AND against the installed set
(`pip freeze`) of each venv that runs our code, then writes results into
walker_health so the L1 pager surfaces them.

Why both (lesson filed 2026-10-27): auditing only requirements.txt reports
the PINS, never what is actually importable at runtime. Two blind spots
this closes:

  1. A merged pin bump does not reach any venv — nothing reinstalls them.
     The pin file can read Pillow==13.0.0 while every venv still imports
     12.3.0, and the pin-file audit calls that green.
  2. A pin file never lists transitive or orphaned packages. The Mac
     walker venv carried pypdf 6.19.0 (11 CVEs) and opentelemetry-api
     1.44.0 (1 CVE), neither pinned by us, while the pin-file audit said
     "0 findings — 76 packages checked".

venv_py312 is deliberately NOT audited: it is the tooling venv that holds
the pip-audit binary itself and runs none of our code.

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
import re
import subprocess
import sys
import tempfile

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
DEFAULT_FREEZE_TIMEOUT_SEC = 60  # `pip freeze` is local/cheap; ssh adds a hop

# Venvs that RUN OUR CODE. venv_py312 is excluded on purpose (tooling venv,
# holds pip-audit itself, runs none of our code).
#
# Each target: (label, python_executable, ssh_host_or_None)
DEFAULT_VENV_TARGETS = [
    ("venv", os.path.join(REPO_ROOT, "venv", "bin", "python"), None),
    ("venv_py311", os.path.join(REPO_ROOT, "venv_py311", "bin", "python"), None),
    ("lenovo_venv", "/home/charlie/xrpldashboard/.venv/bin/python", "rippled-node"),
]

# A freeze line we can hand to pip-audit: strict `name==version`. Editable
# installs, VCS/URL requirements and `-e .` lines are dropped rather than
# failing the whole audit.
_FREEZE_LINE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*==[^\s#]+$")


def _parse_venv_targets(spec: str) -> list[tuple[str, str, str | None]]:
    """Parse PIP_AUDIT_VENVS="label=/path/to/python[@sshhost],...".

    Empty/unset string → DEFAULT_VENV_TARGETS. An explicit "none" disables
    venv auditing entirely (pin-file-only, the pre-2026-10-27 behaviour).
    """
    spec = (spec or "").strip()
    if not spec:
        return list(DEFAULT_VENV_TARGETS)
    if spec.lower() == "none":
        return []
    targets: list[tuple[str, str, str | None]] = []
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        label, _, rest = chunk.partition("=")
        rest = rest.strip()
        host = None
        if "@" in rest:
            rest, _, host = rest.partition("@")
            host = host.strip() or None
        targets.append((label.strip(), rest.strip(), host))
    return targets


def _sanitize_freeze(text: str) -> list[str]:
    """Keep only strict name==version lines from a pip freeze dump."""
    return [
        ln.strip() for ln in (text or "").splitlines()
        if _FREEZE_LINE_RE.match(ln.strip())
    ]


def read_freeze(python_exe: str, ssh_host: str | None,
                timeout_sec: int) -> tuple[list[str] | None, str | None]:
    """Return (freeze_lines, err). err is None on success.

    Remote reads use BatchMode=yes so a missing key fails fast instead of
    prompting for a credential.
    """
    if ssh_host:
        cmd = [
            "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
            ssh_host, f"{python_exe} -m pip freeze",
        ]
    else:
        if not os.path.exists(python_exe):
            return None, f"python missing at {python_exe}"
        cmd = [python_exe, "-m", "pip", "freeze"]

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout_sec,
        )
    except subprocess.TimeoutExpired:
        return None, f"freeze timeout after {timeout_sec}s"
    except OSError as e:
        return None, f"freeze {e.__class__.__name__}: {e}"

    if result.returncode != 0:
        tail = ((result.stderr or "")[-160:]).strip() or "no stderr"
        return None, f"freeze exit={result.returncode}: {tail}"

    lines = _sanitize_freeze(result.stdout)
    if not lines:
        return None, "freeze produced no name==version lines"
    return lines, None


def audit_venv(label: str, python_exe: str, ssh_host: str | None,
               pip_audit_bin: str, timeout_sec: int,
               freeze_timeout_sec: int) -> tuple[bool, int, str]:
    """Audit one venv's INSTALLED set. Returns (ok, n_findings, message).

    Same ok semantics as run_pip_audit: ok=False means the run broke, not
    that vulnerabilities were found.
    """
    lines, err = read_freeze(python_exe, ssh_host, freeze_timeout_sec)
    if err:
        return False, 0, err

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", suffix=f".{label}.freeze.txt", delete=False,
        ) as fh:
            fh.write("\n".join(lines) + "\n")
            tmp_path = fh.name
        return _audit_requirements_file(tmp_path, pip_audit_bin, timeout_sec)
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


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
    return _audit_requirements_file(requirements, pip_audit_bin, timeout_sec)


def _audit_requirements_file(requirements: str, pip_audit_bin: str,
                             timeout_sec: int) -> tuple[bool, int, str]:
    """Run pip-audit against any requirements-format file.

    Shared by the pin-file audit and every per-venv freeze audit so both
    paths parse results, count findings and classify run failures
    identically.
    """
    if not os.path.exists(pip_audit_bin):
        return False, 0, f"pip-audit run failed: binary missing at {pip_audit_bin}"

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


def build_summary(
    results: list[tuple[str, bool, int, str, bool]],
) -> tuple[bool, int, str]:
    """Fold per-scope results into (overall_ok, total_findings, message).

    overall_ok is False only when a scope that MUST be readable broke.
    A remote venv we could not reach is reported as `unreachable` in the
    message but does NOT flip ok — weekly ssh flakiness on the Lenovo must
    not page as a walker failure, and it must not masquerade as a clean
    audit either. Local scopes are deterministic, so their failures do
    flip ok.
    """
    total = 0
    overall_ok = True
    parts = []
    for label, ok, n, msg, required in results:
        if not ok:
            parts.append(f"{label}=ERROR({msg})")
            if required:
                overall_ok = False
            continue
        total += n
        parts.append(f"{label}={n}" if n == 0 else f"{label}={n}:{msg}")
    return overall_ok, total, "; ".join(parts)


def main() -> int:
    requirements = os.environ.get("PIP_AUDIT_REQUIREMENTS", DEFAULT_REQUIREMENTS)
    pip_audit_bin = os.environ.get("PIP_AUDIT_BIN", DEFAULT_PIP_AUDIT)
    timeout_sec = int(os.environ.get("PIP_AUDIT_TIMEOUT_SEC", DEFAULT_TIMEOUT_SEC))
    freeze_timeout_sec = int(os.environ.get(
        "PIP_AUDIT_FREEZE_TIMEOUT_SEC", DEFAULT_FREEZE_TIMEOUT_SEC))
    venv_targets = _parse_venv_targets(os.environ.get("PIP_AUDIT_VENVS", ""))

    db.write_walker_health_start(WALKER_NAME, cadence_seconds=WALKER_CADENCE_SECONDS)

    log.info("pip-audit start: bin=%s reqs=%s venvs=%s timeout=%ds",
             pip_audit_bin, requirements,
             ",".join(t[0] for t in venv_targets) or "none", timeout_sec)

    results = []

    ok, n_findings, msg = run_pip_audit(requirements, pip_audit_bin, timeout_sec)
    results.append(("reqs", ok, n_findings, msg, True))
    if ok and n_findings == 0:
        log.info("pip-audit reqs PASS: %s", msg)
    elif ok:
        log.warning("pip-audit reqs: %d findings surfaced: %s", n_findings, msg)
    else:
        log.error("pip-audit reqs FAIL: %s", msg)

    for label, python_exe, ssh_host in venv_targets:
        v_ok, v_n, v_msg = audit_venv(
            label, python_exe, ssh_host, pip_audit_bin,
            timeout_sec, freeze_timeout_sec,
        )
        # A remote venv is best-effort: unreachable is reported, not paged.
        results.append((label, v_ok, v_n, v_msg, ssh_host is None))
        if v_ok and v_n == 0:
            log.info("pip-audit %s PASS: %s", label, v_msg)
        elif v_ok:
            log.warning("pip-audit %s: %d findings surfaced: %s", label, v_n, v_msg)
        else:
            log.error("pip-audit %s FAIL: %s", label, v_msg)

    ok, n_findings, msg = build_summary(results)

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
