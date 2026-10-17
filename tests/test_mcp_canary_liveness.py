"""mcp_ua_matrix_canary: the probe must prove MCP is ALIVE, not just answering.

THE WOUND (found 2026-10-17). The canary probed
https://mcp.xrpldashboard.com/health -- a path that has NEVER existed; no
route is defined for it in mcp_server.py, where every "health" reference is
the walker_health DB table. The live endpoint returns 404 for it.

And the classification never compared the status. Its only fail trigger was a
Cloudflare 1010 marker in the body:

    if any(marker in body for marker in CF_1010_MARKERS):
        return verdict="fail_1010"
    return verdict="pass"          # <- everything else, including 404

`grep "status ==|200"` matched only the DOCSTRING, which claimed "Any UA that
returns HTTP 200 with the /health JSON is a PASS". The code did not implement
that. So the canary reported green for its entire life while knocking on a
door that was never built, and would have stayed green with the MCP server
completely dead.

WHAT STILL WORKED, in fairness: WAF-ban detection. The 404 came from the
ORIGIN behind the cloudflared tunnel, not Cloudflare's edge, so a request
reaching origin at all proved the UA was not banned. A banned UA returns
403 + "Error 1010" and was caught. The design fence held; only the liveness
implication was false.

THE FIX. Probe /mcp and require BOTH:
  * HTTP 406, and
  * the FastMCP JSON-RPC body marker "Client must accept text/event-stream"
Both, not either. A status alone could come from an intermediary; that body
can only be produced by FastMCP's streamable-http handler actually serving.
Verified stable across claude-user, a desktop Mozilla UA, python-urllib and
curl -- all four returned 406 with that exact body.

Transport errors are RETRIED once and then fail, rather than being skipped:
a persistent timeout is a real outage, while the retry is what stops a
single network flap from paging. Same retry-before-negative rule as
two_way_toml_verifier_walker.

Hermetic: injected fetcher and sleeper. No network, no DB, no wall-clock.
"""
from __future__ import annotations

import importlib.util
import pathlib

import pytest

ALIVE_BODY = (
    '{"jsonrpc":"2.0","id":"server-error","error":{"code":-32600,'
    '"message":"Not Acceptable: Client must accept text/event-stream"}}'
)
CF_1010_BODY = (
    "<html><head><title>Access denied</title></head><body>"
    "Error 1010: browser signature banned</body></html>"
)


def _load_canary():
    root = pathlib.Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "mcp_canary_liveness_under_test",
        root / "scripts" / "mcp_ua_matrix_canary.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def canary():
    return _load_canary()


def seq_fetcher(*results):
    """Fetcher yielding each (status, body, transport_err) in order."""
    calls = {"n": 0}

    def _f(ua):
        item = results[min(calls["n"], len(results) - 1)]
        calls["n"] += 1
        return item
    _f.calls = calls
    return _f


# ── the probe target ────────────────────────────────────────────────
class TestProbeTarget:
    def test_probes_mcp_not_health(self, canary):
        """/health never existed. That is the whole bug."""
        assert canary.MCP_PROBE_URL.endswith("/mcp")
        assert not hasattr(canary, "MCP_HEALTH_URL")

    def test_alive_contract_is_status_AND_marker(self, canary):
        assert canary.ALIVE_STATUS == 406
        assert canary.ALIVE_BODY_MARKER == "Client must accept text/event-stream"


# ── pass requires both halves ───────────────────────────────────────
class TestPassRequiresBoth:
    def test_406_with_marker_passes(self, canary):
        r = canary._probe_one("curl/8.4.0",
                              fetcher=seq_fetcher((406, ALIVE_BODY, None)),
                              sleeper=lambda _s: None)
        assert r["verdict"] == "pass"
        assert "fastmcp-alive" in r["detail"]

    def test_406_WITHOUT_marker_fails(self, canary):
        """A 406 from an intermediary is not proof FastMCP is serving."""
        r = canary._probe_one("curl/8.4.0",
                              fetcher=seq_fetcher((406, "Not Acceptable", None)),
                              sleeper=lambda _s: None)
        assert r["verdict"] == "fail_unexpected"

    def test_200_WITH_marker_fails(self, canary):
        """Status must be exactly the refusal we expect from a bare GET."""
        r = canary._probe_one("curl/8.4.0",
                              fetcher=seq_fetcher((200, ALIVE_BODY, None)),
                              sleeper=lambda _s: None)
        assert r["verdict"] == "fail_unexpected"


# ── THE regression: 404 must no longer read as a pass ───────────────
class TestHistoricalBug:
    def test_404_now_fails(self, canary):
        """Exactly what the canary saw every run while probing /health, and
        classified as a pass."""
        r = canary._probe_one("curl/8.4.0",
                              fetcher=seq_fetcher((404, "Not Found", None)),
                              sleeper=lambda _s: None)
        assert r["verdict"] == "fail_unexpected"
        assert "404" in r["detail"]

    @pytest.mark.parametrize("status", [200, 404, 500, 502, 503])
    def test_no_other_status_passes(self, canary, status):
        r = canary._probe_one("curl/8.4.0",
                              fetcher=seq_fetcher((status, "whatever", None)),
                              sleeper=lambda _s: None)
        assert r["verdict"] != "pass"

    def test_skip_transport_verdict_is_gone(self, canary):
        """It let a dead endpoint read as 'not a failure'."""
        src = (pathlib.Path(canary.__file__).read_text(encoding="utf-8")
               if getattr(canary, "__file__", None) else "")
        assert "skip_transport" not in src


# ── WAF detection must still win ────────────────────────────────────
class TestWafStillCaught:
    def test_1010_fails_even_with_alive_status(self, canary):
        """A ban must not be masked by the new status check."""
        r = canary._probe_one("Python-urllib/3.11",
                              fetcher=seq_fetcher((406, CF_1010_BODY, None)),
                              sleeper=lambda _s: None)
        assert r["verdict"] == "fail_1010"

    def test_403_ban_fails(self, canary):
        r = canary._probe_one("libwww-perl/6.68",
                              fetcher=seq_fetcher((403, CF_1010_BODY, None)),
                              sleeper=lambda _s: None)
        assert r["verdict"] == "fail_1010"


# ── retry-before-negative on transport ──────────────────────────────
class TestTransportRetry:
    def test_flap_then_success_passes(self, canary):
        """A single network blip must not page."""
        slept = []
        f = seq_fetcher((None, "", "URLError: timed out"),
                        (406, ALIVE_BODY, None))
        r = canary._probe_one("curl/8.4.0", fetcher=f, sleeper=slept.append)
        assert r["verdict"] == "pass"
        assert f.calls["n"] == 2
        assert slept == [canary.TRANSPORT_RETRY_SLEEP_SECONDS]

    def test_persistent_transport_error_fails(self, canary):
        """A sustained timeout is a real outage, not weather."""
        f = seq_fetcher((None, "", "URLError: timed out"))
        r = canary._probe_one("curl/8.4.0", fetcher=f,
                              sleeper=lambda _s: None)
        assert r["verdict"] == "fail_transport"
        assert f.calls["n"] == 2
        assert "twice" in r["detail"]

    def test_successful_probe_does_not_retry(self, canary):
        f = seq_fetcher((406, ALIVE_BODY, None))
        canary._probe_one("curl/8.4.0", fetcher=f, sleeper=lambda _s: None)
        assert f.calls["n"] == 1


# ── rollup counts every failure class ───────────────────────────────
def test_rollup_counts_all_fail_verdicts(canary):
    """Previously only fail_1010 counted, which is why a 404 across all 16
    UAs still produced ok=True."""
    src = pathlib.Path(canary.__file__).read_text(encoding="utf-8")
    for token in ("fail_unexpected", "fail_transport", "fails_1010"):
        assert token in src
    assert "ok = len(fails) == 0" in src
