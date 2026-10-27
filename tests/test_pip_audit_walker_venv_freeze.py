"""pip_audit_walker: audit the INSTALLED set of each code-running venv.

Guards the 2026-10-27 lesson: auditing only requirements.txt reports the
pins, never what is importable at runtime. A merged pin bump reaches no
venv (nothing reinstalls them), and a pin file never lists transitive or
orphaned packages — the Mac walker venv carried pypdf 6.19.0 (11 CVEs)
and opentelemetry-api 1.44.0 while the pin-file audit said "0 findings".

No DB, no network: every subprocess boundary is stubbed.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WALKER_PATH = os.path.join(REPO_ROOT, "scripts", "pip_audit_walker.py")


def _load():
    if REPO_ROOT not in sys.path:
        sys.path.insert(0, REPO_ROOT)
    spec = importlib.util.spec_from_file_location("paw_under_test", WALKER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


paw = _load()


# ---------------------------------------------------------------- targets

def test_default_targets_are_the_code_running_venvs():
    labels = [t[0] for t in paw.DEFAULT_VENV_TARGETS]
    assert labels == ["venv", "venv_py311", "lenovo_venv"]


def test_venv_py312_is_never_audited():
    """Tooling venv holds pip-audit itself and runs none of our code."""
    for label, python_exe, _host in paw.DEFAULT_VENV_TARGETS:
        assert "venv_py312" not in python_exe, label
        assert label != "venv_py312"


def test_lenovo_target_is_remote_and_mac_targets_are_local():
    by_label = {t[0]: t for t in paw.DEFAULT_VENV_TARGETS}
    assert by_label["venv"][2] is None
    assert by_label["venv_py311"][2] is None
    assert by_label["lenovo_venv"][2] == "rippled-node"


def test_parse_venv_targets_defaults_when_unset():
    assert paw._parse_venv_targets("") == list(paw.DEFAULT_VENV_TARGETS)


def test_parse_venv_targets_none_disables_venv_auditing():
    assert paw._parse_venv_targets("none") == []


def test_parse_venv_targets_custom_spec_with_ssh_host():
    got = paw._parse_venv_targets("a=/x/bin/python,b=/y/bin/python@host1")
    assert got == [("a", "/x/bin/python", None), ("b", "/y/bin/python", "host1")]


# ---------------------------------------------------------------- freeze

def test_sanitize_freeze_keeps_only_pinned_lines():
    raw = "\n".join([
        "pypdf==6.19.0",
        "-e git+https://example.invalid/x#egg=x",
        "Pillow==13.0.0",
        "",
        "# a comment",
        "some-pkg @ file:///tmp/wheel.whl",
        "opentelemetry-api==1.44.0",
    ])
    assert paw._sanitize_freeze(raw) == [
        "pypdf==6.19.0", "Pillow==13.0.0", "opentelemetry-api==1.44.0",
    ]


def test_read_freeze_remote_uses_batchmode_so_it_never_prompts(monkeypatch):
    """A missing key must fail fast, never sit on a credential prompt."""
    seen = {}

    class R:
        returncode = 0
        stdout = "pypdf==6.19.0\n"
        stderr = ""

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return R()

    monkeypatch.setattr(paw.subprocess, "run", fake_run)
    lines, err = paw.read_freeze("/remote/bin/python", "somehost", 30)
    assert err is None and lines == ["pypdf==6.19.0"]
    assert seen["cmd"][0] == "ssh"
    assert "BatchMode=yes" in seen["cmd"]
    assert "somehost" in seen["cmd"]


def test_read_freeze_local_missing_python_is_an_error(monkeypatch):
    lines, err = paw.read_freeze("/definitely/not/here/python", None, 30)
    assert lines is None
    assert "python missing" in err


def test_read_freeze_rejects_empty_freeze(monkeypatch):
    class R:
        returncode = 0
        stdout = "# nothing pinned\n"
        stderr = ""

    monkeypatch.setattr(paw.subprocess, "run", lambda *a, **k: R())
    lines, err = paw.read_freeze("/remote/bin/python", "h", 30)
    assert lines is None
    assert "no name==version" in err


# ---------------------------------------------------------------- audit

def _audit_payload(pkgs):
    return json.dumps({"dependencies": [
        {"name": n, "version": v, "vulns": [{"id": i} for i in ids]}
        for n, v, ids in pkgs
    ]})


# Both paths must be real files on disk: read_freeze() guards on
# os.path.exists(python_exe) for a LOCAL target, and
# _audit_requirements_file() guards on os.path.exists(pip_audit_bin).
# sys.executable satisfies both without touching the filesystem guards.
#
# Dispatch the stub on the presence of "-r" (only the pip-audit call has
# it), NOT on a substring of the joined command: the temp freeze file is
# named "*.freeze.txt", so matching on "freeze" sends the pip-audit call
# back into the freeze stub and the audit silently returns
# "no JSON output". Both traps bit this file on its first two runs.
FAKE_AUDIT_BIN = sys.executable
FAKE_PYTHON = sys.executable


def _is_audit_call(cmd):
    return "-r" in cmd


def test_audit_venv_surfaces_installed_only_findings(monkeypatch):
    """The exact blind spot: pypdf is in no requirements file."""
    class Freeze:
        returncode = 0
        stdout = "pypdf==6.19.0\nPillow==13.0.0\n"
        stderr = ""

    class Audit:
        returncode = 1  # pip-audit exits 1 when it finds vulns
        stdout = _audit_payload([
            ("pypdf", "6.19.0", ["CVE-2026-113792", "CVE-2026-113795"]),
            ("Pillow", "13.0.0", []),
        ])
        stderr = ""

    def fake_run(cmd, **kw):
        return Audit() if _is_audit_call(cmd) else Freeze()

    monkeypatch.setattr(paw.subprocess, "run", fake_run)

    ok, n, msg = paw.audit_venv(
        "venv", FAKE_PYTHON, None, FAKE_AUDIT_BIN, 60, 30)
    assert ok is True
    assert n == 2
    assert "pypdf@6.19.0" in msg
    assert "CVE-2026-113792" in msg


def test_audit_venv_cleans_up_its_temp_freeze_file(monkeypatch):
    written = {}

    class Freeze:
        returncode = 0
        stdout = "Pillow==13.0.0\n"
        stderr = ""

    class Audit:
        returncode = 0
        stdout = _audit_payload([("Pillow", "13.0.0", [])])
        stderr = ""

    def fake_run(cmd, **kw):
        if not _is_audit_call(cmd):
            return Freeze()
        written["path"] = cmd[cmd.index("-r") + 1]
        return Audit()

    monkeypatch.setattr(paw.subprocess, "run", fake_run)
    ok, n, _ = paw.audit_venv(
        "venv", FAKE_PYTHON, None, FAKE_AUDIT_BIN, 60, 30)
    assert ok is True and n == 0
    assert written["path"], "pip-audit was never invoked on the freeze file"
    assert not os.path.exists(written["path"])


# ---------------------------------------------------------------- summary

def test_build_summary_sums_findings_across_every_scope():
    ok, total, msg = paw.build_summary([
        ("reqs", True, 0, "0 findings — 76 packages checked", True),
        ("venv", True, 12, "pypdf@6.19.0(CVE-1)", True),
        ("venv_py311", True, 0, "", True),
        ("lenovo_venv", True, 1, "x@1(CVE-2)", False),
    ])
    assert ok is True
    assert total == 13
    assert "reqs=0" in msg and "venv=12" in msg and "venv_py311=0" in msg


def test_remote_unreachable_is_reported_but_does_not_page():
    ok, total, msg = paw.build_summary([
        ("reqs", True, 0, "clean", True),
        ("venv", True, 0, "", True),
        ("lenovo_venv", False, 0, "freeze timeout after 60s", False),
    ])
    assert ok is True, "ssh flakiness must not increment consecutive_failures"
    assert "lenovo_venv=ERROR" in msg, "but it must not look clean either"
    assert total == 0


def test_local_venv_failure_does_flip_ok():
    ok, _total, msg = paw.build_summary([
        ("reqs", True, 0, "clean", True),
        ("venv", False, 0, "python missing at /v/bin/python", True),
    ])
    assert ok is False
    assert "venv=ERROR" in msg


def test_requirements_failure_still_flips_ok():
    ok, _total, _msg = paw.build_summary([
        ("reqs", False, 0, "pip-audit run failed: binary missing", True),
    ])
    assert ok is False


def test_findings_message_keeps_cve_substrings_for_mutes():
    """WALKER_MESSAGE_MUTES matches on CVE substrings — keep them intact."""
    _ok, _total, msg = paw.build_summary([
        ("reqs", True, 0, "clean", True),
        ("venv", True, 1, "pypdf@6.19.0(CVE-2026-113792)", True),
    ])
    assert "CVE-2026-113792" in msg
