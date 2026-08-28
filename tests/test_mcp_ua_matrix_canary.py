"""Sanity tests for scripts/mcp_ua_matrix_canary.py.

The canary itself only runs on the Mac under launchd; these tests
lock in the UA-matrix contract so a future refactor can't quietly drop
the specific UAs (Python-urllib, libwww-perl) that motivated the WAF
skip rule. If either of those two disappears from the matrix, the
canary loses its regression teeth for the 2026-08-27 fix.
"""

import importlib.util
import os
import pathlib
import pytest


def _load_canary():
    """Load scripts/mcp_ua_matrix_canary.py without adding scripts/ to
    sys.path (it isn't a package). Path-based spec avoids side effects
    on other tests."""
    root = pathlib.Path(__file__).resolve().parent.parent
    script_path = root / "scripts" / "mcp_ua_matrix_canary.py"
    spec = importlib.util.spec_from_file_location(
        "mcp_ua_matrix_canary_under_test", script_path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def canary():
    return _load_canary()


def test_ua_matrix_contains_both_previously_blocked_uas(canary):
    """Python-urllib and libwww-perl were the exact two UAs the CF
    Bot Fight signature blocked pre-2026-08-27. They MUST stay in the
    matrix — they're the whole point of the canary."""
    ua_matrix = canary.UA_MATRIX
    assert any(ua.startswith("Python-urllib") for ua in ua_matrix), (
        "Python-urllib absent from UA_MATRIX — canary won't catch WAF "
        "skip-rule regression on the primary blocked UA"
    )
    assert any(ua.startswith("libwww-perl") for ua in ua_matrix), (
        "libwww-perl absent from UA_MATRIX — canary won't catch WAF "
        "skip-rule regression on the secondary blocked UA"
    )


def test_ua_matrix_carries_ai_agent_client_classes(canary):
    """Full 16-client-class regression: browsers, generic scripts,
    curl/wget, ai-agents. If the audit-matrix shape is intact, so is
    the canary's coverage."""
    ua_matrix = canary.UA_MATRIX
    assert len(ua_matrix) >= 15, (
        f"UA_MATRIX has {len(ua_matrix)} entries — expected the 16-"
        f"client-class matrix from the QUADFECTA external audit"
    )
    joined = " ".join(ua_matrix)
    for expected in ("GPTBot", "ClaudeBot", "PerplexityBot", "curl", "Wget"):
        assert expected in joined, (
            f"UA_MATRIX missing an entry for {expected!r} — audit-matrix "
            f"shape drift means the canary undercovers"
        )


def test_cf_1010_markers_detect_the_actual_error_body(canary):
    """The canary decides fail vs pass by looking for specific strings in
    the response body. If Cloudflare changes the wording, the canary
    goes silent-green while the real error persists. These marker
    constants must catch the substrings the CF 1010 error page carries."""
    markers = canary.CF_1010_MARKERS
    # A representative CF 1010 response body — snippet from the 2026-08-26
    # audit capture. If CF rewords this, update the markers alongside a
    # fresh audit capture.
    sample_body = (
        "<!DOCTYPE html><html><head><title>Access denied</title></head>"
        "<body><h1>Error 1010</h1><p>The owner of this website "
        "(mcp.xrpldashboard.com) has banned your access based on your "
        "browser's signature (1010 IssueID).</p></body></html>"
    )
    assert any(marker in sample_body for marker in markers), (
        f"CF_1010_MARKERS={markers} did not fire on a canonical 1010 "
        f"error body — canary will silent-green on real block events"
    )


def test_walker_name_is_stable(canary):
    """The walker_health row this canary writes is keyed by name. If we
    rename it, the answer_plausibility monitor keeps waiting on the old
    row and pages on stale, then we silence-tune around it. Lock it."""
    assert canary.WALKER_NAME == "mcp_ua_matrix_canary"
    assert canary.WALKER_CADENCE_SECONDS == 86400  # daily
