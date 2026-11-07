"""A probe that RAISES must fail the run, never read as pass or "not checked".

Both canaries already behave correctly, and nothing pinned it. This file is
the pin. It exists because the 2026-11-07 catch-all sweep could not tell the
difference between a handler that swallows an error and one that converts it
into a failure verdict: the relay canary assigns
`FAIL_probe_<ExcType>` and coverage_check appends `("RED", area, repr(exc))`,
and neither logs or re-raises, so a text/AST search flags both as silent.

The distinction that matters is behavioural, not textual, so assert the
behaviour: raise inside each probe and require the run to come out failed
with the exception type visible.
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import urllib.request

import pytest
import websockets

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import wss_relay_canary as relay  # noqa: E402


def _load_coverage_check():
    """ops/ has no __init__.py, so load it by path rather than as a package."""
    path = os.path.join(HERE, "ops", "coverage_check.py")
    spec = importlib.util.spec_from_file_location("coverage_check_mod", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Boom(RuntimeError):
    """Distinct type so the assertions prove the TYPE is carried through."""


# ── wss_relay_canary ────────────────────────────────────────────────────────

def test_raising_feed_probe_yields_FAIL_verdict_and_a_finding(monkeypatch):
    def _raise(*a, **k):
        raise Boom("probe exploded")

    # _run_feed_probes calls asyncio.run(_probe_feed(...)); patching asyncio.run
    # covers the raise regardless of where inside the coroutine it came from.
    monkeypatch.setattr(relay.asyncio, "run", _raise, raising=True)
    verdicts, findings = relay._run_feed_probes("wss://example.invalid")

    assert verdicts, "every configured feed must produce a verdict"
    for feed, verdict in verdicts.items():
        assert verdict.startswith("FAIL"), (feed, verdict)
        assert "Boom" in verdict, f"exception type must be visible: {verdict}"
    assert findings, "a raising probe must produce findings, not an empty list"
    assert all(f["severity"] == "high" for f in findings), findings


def test_raising_homepage_probe_reports_error_not_ok(monkeypatch):
    """_probe_homepage feeds an `ok = False` gate in main(), so returning
    anything equal to 'ok' here would turn an exception into a pass."""
    def _raise(*a, **k):
        raise Boom("no network")

    # _probe_homepage does a FUNCTION-LOCAL `import urllib.request as _ur`,
    # so there is no module attribute to patch -- patch the stdlib module it
    # resolves at call time.
    monkeypatch.setattr(urllib.request, "urlopen", _raise, raising=True)
    status, fresh, age = relay._probe_homepage()

    assert status != "ok", "a raised probe must never read as ok"
    assert status == "error_Boom", status
    assert fresh == "unknown"
    assert age is None


def test_raising_tx_sub_probe_is_reported_as_probe_error(monkeypatch):
    """_probe_tx_sub is `async def`, so it must be driven with asyncio.run --
    calling it bare returns a coroutine that compares unequal to everything
    and would make this test pass or fail for the wrong reason."""
    def _raise(*a, **k):
        raise Boom("socket gone")

    # websockets is a function-local import inside the probe too, so patch
    # the real module rather than a module attribute that does not exist.
    monkeypatch.setattr(websockets, "connect", _raise, raising=True)
    assert asyncio.run(
        relay._probe_tx_sub("wss://example.invalid", 1.0)) == "probe_error_Boom"


# ── ops/coverage_check ──────────────────────────────────────────────────────

def test_raising_check_exits_nonzero_with_a_RED_finding(capsys):
    """main() wraps each check; a raising check must become RED and exit 1,
    rather than being counted as GREEN or skipped."""
    cc = _load_coverage_check()

    def _raise():
        raise Boom("check exploded")

    cc.check_amendments = _raise
    cc.check_unl = _raise
    cc.check_whales = _raise
    cc.check_amm_pools = _raise
    cc.check_mpts = _raise
    cc.check_named_accounts = _raise

    with pytest.raises(SystemExit) as exc:
        cc.main()
    assert exc.value.code == 1, "a raising check must fail the run"
    out = capsys.readouterr().out
    assert "[RED]" in out, out[-400:]
    assert "Boom" in out, "the exception type must reach the operator"
    assert "0 RED" not in out, "a raising check must not summarise as 0 RED"


def test_all_rpc_nodes_failing_raises_rather_than_returning_empty(monkeypatch):
    """_rpc loops over nodes swallowing each error, then raises after the
    loop. Returning {} instead would let a caller treat a total node outage
    as 'no data', which is the false-green shape this file guards."""
    cc = _load_coverage_check()

    def _raise(*a, **k):
        raise Boom("node down")

    monkeypatch.setattr(cc.httpx, "post", _raise, raising=True)
    with pytest.raises(RuntimeError) as exc:
        cc._rpc("server_info", {})
    assert "all rippled nodes failed" in str(exc.value)
    assert "Boom" in str(exc.value), "the underlying cause must be carried"
