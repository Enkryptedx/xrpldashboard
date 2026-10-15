"""public_route_200_canary: agreement probes get probe_one's retry.

THE WOUND (measured 2026-10-08..15). The canary failed ten times in eight
days, and **every single failure was an agreement probe** — 6x ReadTimeout
and 1x ConnectError in the retained log, never a plain route probe. Paths:
3x `label_agree:Bitstamp_file:wallet`, 1x `label_agree:BeBe_AMM_derived:wallet`.

Cause: `probe_one` retries once (`for attempt in (1, 2)`, 1s pause) because a
heavy route can exceed TIMEOUT_S under load. The agreement probes called
`httpx.get` directly with **no retry** and hardcoded `"attempt": 1`, so a
single transient stall became a finding. And they target `/wallet/<addr>` —
the slowest route, doing live RPC per address — which is exactly where a
stall is most likely.

Why it went unnoticed for eight days: the failures were always isolated, so
`consecutive_failures` never reached l1_pager's `walker_failing` threshold of
3. `check_walker_findings` fires on `findings_count > 0` regardless, so it
paged ~once a day from a canary that was behaving exactly as written.

Hermetic: injected getter/sleeper, no HTTP, no DB, no network, run_walker()
never called.
"""
from __future__ import annotations

import inspect
import os
import re
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
import public_route_200_canary as c  # noqa: E402

WALLET_BODY = ('<span class="identity-name">Binance</span>'
               ' <div>source: pg_account_labels</div>')


class FakeResp:
    def __init__(self, status=200, text=WALLET_BODY, payload=None):
        self.status_code = status
        self.text = text
        self._payload = payload or {"data": {"name": "Binance"}}

    def json(self):
        return self._payload


class ConnectError(Exception):
    pass


class ReadTimeout(Exception):
    pass


# ── 1. the retry itself ─────────────────────────────────────────────
class TestGetWithRetry:
    def test_first_attempt_success_does_not_sleep(self):
        slept = []
        r, err, attempt = c._get_with_retry(
            "u", "ua", getter=lambda *a, **k: FakeResp(), sleeper=slept.append)
        assert err is None and attempt == 1
        assert r is not None
        assert slept == []

    def test_transient_failure_is_absorbed(self):
        """THE fix. One blip then success => no error, so no finding ever
        reaches walker_health. This is the ~1-page-per-day regression."""
        slept = []
        calls = {"n": 0}

        def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ConnectError("transient")
            return FakeResp()

        r, err, attempt = c._get_with_retry("u", "ua", getter=flaky,
                                            sleeper=slept.append)
        assert err is None, "a single blip must not surface as an error"
        assert attempt == 2
        assert r is not None
        assert len(slept) == 1

    def test_persistent_failure_reports_attempt_2(self):
        """Matches probe_one, which stamps attempt=2 on final failure."""
        slept = []
        r, err, attempt = c._get_with_retry(
            "u", "ua", getter=lambda *a, **k: (_ for _ in ()).throw(
                ConnectError("down")), sleeper=slept.append)
        assert r is None
        assert err == "ConnectError"
        assert attempt == 2
        assert len(slept) == 1

    @pytest.mark.parametrize("exc,name", [(ConnectError, "ConnectError"),
                                          (ReadTimeout, "ReadTimeout")])
    def test_error_name_is_the_exception_class(self, exc, name):
        """Both observed in the field; the reason string carries the class."""
        def dead(*a, **k):
            raise exc("x")
        _r, err, _a = c._get_with_retry("u", "ua", getter=dead,
                                        sleeper=lambda _s: None)
        assert err == name

    def test_sleep_uses_the_configured_pause(self):
        slept = []
        c._get_with_retry("u", "ua",
                          getter=lambda *a, **k: (_ for _ in ()).throw(
                              ConnectError("x")), sleeper=slept.append)
        assert slept == [c.RETRY_SLEEP_S]

    def test_single_attempt_mode_does_not_sleep(self):
        slept = []
        _r, err, attempt = c._get_with_retry(
            "u", "ua", attempts=1,
            getter=lambda *a, **k: (_ for _ in ()).throw(ConnectError("x")),
            sleeper=slept.append)
        assert (err, attempt) == ("ConnectError", 1)
        assert slept == []

    def test_zero_attempts_is_floored_to_one(self):
        """A misconfigured 0 must still make one request, not zero."""
        calls = {"n": 0}

        def count(*a, **k):
            calls["n"] += 1
            return FakeResp()

        _r, err, attempt = c._get_with_retry("u", "ua", attempts=0,
                                             getter=count,
                                             sleeper=lambda _s: None)
        assert calls["n"] == 1
        assert err is None and attempt == 1

    def test_request_shape_matches_the_other_probes(self):
        seen = {}

        def capture(url, **kwargs):
            seen["url"] = url
            seen.update(kwargs)
            return FakeResp()

        c._get_with_retry("https://x/y", "label-agree", getter=capture,
                          sleeper=lambda _s: None)
        assert seen["url"] == "https://x/y"
        assert seen["timeout"] == c.TIMEOUT_S
        assert seen["follow_redirects"] is True
        assert seen["headers"]["User-Agent"] == "public-route-canary/label-agree"


# ── 2. policy parity with probe_one (drift guard) ───────────────────
class TestPolicyParity:
    """The constants are set to match probe_one, which still spells its own
    policy inline. Rewiring probe_one's several attempt-stamped return paths
    was judged riskier than this fix warrants, so equality is enforced here
    instead of by sharing."""

    def test_constants(self):
        assert c.RETRY_ATTEMPTS == 2
        assert c.RETRY_SLEEP_S == 1.0

    def test_probe_one_attempt_count_still_matches(self):
        src = inspect.getsource(c.probe_one)
        m = re.search(r"for attempt in \(([\d, ]+)\)", src)
        assert m, "probe_one's attempt loop changed shape; re-check parity"
        attempts = [int(x) for x in m.group(1).split(",") if x.strip()]
        assert len(attempts) == c.RETRY_ATTEMPTS
        assert attempts == list(range(1, c.RETRY_ATTEMPTS + 1))

    def test_probe_one_sleep_still_matches(self):
        src = inspect.getsource(c.probe_one)
        m = re.search(r"time\.sleep\(([\d.]+)\)", src)
        assert m, "probe_one's retry pause changed shape; re-check parity"
        assert float(m.group(1)) == c.RETRY_SLEEP_S


# ── 3. the label probes are actually rewired ────────────────────────
class TestLabelProbesUseTheHelper:
    def test_no_direct_httpx_call_remains(self):
        src = inspect.getsource(c._address_label_agreement_probes)
        assert "httpx.get" not in src, (
            "an un-retried direct fetch is exactly the bug being fixed")
        assert "_get_with_retry" in src

    def test_persistent_failure_stamps_attempt_2(self, monkeypatch):
        """Was hardcoded 1, which understated a real two-attempt failure."""
        monkeypatch.setattr(c, "_get_with_retry",
                            lambda url, ua, **k: (None, "ConnectError", 2))
        findings = c._address_label_agreement_probes()
        wallet = [f for f in findings if f["path"].endswith(":wallet")]
        assert wallet, "expected one wallet finding per sampled address"
        for f in wallet:
            assert f["attempt"] == 2
            assert f["reason"] == "probe_error_ConnectError"
            assert f["ok"] is False
            assert f["status"] is None

    def test_successful_fetch_yields_no_network_finding(self, monkeypatch):
        monkeypatch.setattr(c, "_get_with_retry",
                            lambda url, ua, **k: (FakeResp(), None, 1))
        findings = c._address_label_agreement_probes()
        assert [f for f in findings if f["path"].endswith(":wallet")] == []

    def test_check_json_failure_is_still_swallowed(self, monkeypatch):
        """check_name is collected but never asserted on, so a miss must not
        manufacture a finding. Only /wallet failures are reportable."""
        def by_url(url, ua, **k):
            if "check.json" in url:
                return None, "ReadTimeout", 2
            return FakeResp(), None, 1

        monkeypatch.setattr(c, "_get_with_retry", by_url)
        findings = c._address_label_agreement_probes()
        assert all("check" not in f["path"] for f in findings)
        assert [f for f in findings if f["path"].endswith(":wallet")] == []

    def test_wrapper_still_never_raises(self, monkeypatch):
        """The whole probe set is best-effort; a surprise must not kill the
        canary run."""
        def boom(*a, **k):
            raise RuntimeError("unexpected")

        monkeypatch.setattr(c, "_get_with_retry", boom)
        assert c._address_label_agreement_probes() == []
