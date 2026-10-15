"""public_route_200_canary: tier_agree probes get the same retry.

Follow-up to the label_agree fix (2026-10-15). The label probes were the ones
actually failing — ten canary failures in eight days, all of them agreement
probes — but `_tier_agreement_probes` carried the identical gap: a direct
`httpx.get` with no retry and three hardcoded `"attempt": 1` sites. It simply
hadn't blipped yet in the retained log.

ONE DIFFERENCE FROM THE LABEL FIX, and it matters. For the label probes a
non-200 is not a finding (it just means no identity-name was parsed), so
retrying exceptions alone was enough. For the tier probes a non-200 **is** a
finding (`check.json http_<status>`), and `probe_one` re-probes a
non-acceptable status as well as a network error — a transient 502/503 from an
instance recycling mid-run is the same class of blip as a stall. So
`_get_with_retry` gained an optional `ok_statuses`, and a status outside it is
retried too.

That extension is backward compatible by construction: callers passing no
`ok_statuses` return on the first response exactly as before, which is
asserted below so the merged label-probe behaviour cannot regress.

Hermetic: injected getter/sleeper, no HTTP, no DB, no network, run_walker()
never called.
"""
from __future__ import annotations

import inspect
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
import public_route_200_canary as c  # noqa: E402

AGREE = {"data": {"tier": "verified", "tier_canonical": "verified"}}
DISAGREE = {"data": {"tier": "labeled", "tier_canonical": "verified"}}


class FakeResp:
    def __init__(self, status=200, payload=None, content=b"{}"):
        self.status_code = status
        self._payload = AGREE if payload is None else payload
        self.content = content

    def json(self):
        if self._payload is _BAD_JSON:
            raise ValueError("Expecting value")
        return self._payload


_BAD_JSON = object()


class ReadTimeout(Exception):
    pass


def getter_seq(*responses):
    """Return a getter yielding each response/exception in order."""
    seq = list(responses)
    state = {"i": 0}

    def _get(*a, **k):
        item = seq[min(state["i"], len(seq) - 1)]
        state["i"] += 1
        if isinstance(item, BaseException):
            raise item
        return item
    return _get


# ── 1. the ok_statuses extension ────────────────────────────────────
class TestOkStatusesRetry:
    def test_backward_compatible_without_ok_statuses(self):
        """The merged label-probe path must be untouched: any response, even
        a 503, returns on the first attempt."""
        slept = []
        r, err, attempt = c._get_with_retry(
            "u", "ua", getter=getter_seq(FakeResp(503)), sleeper=slept.append)
        assert (r.status_code, err, attempt) == (503, None, 1)
        assert slept == []

    def test_good_status_returns_immediately(self):
        slept = []
        r, err, attempt = c._get_with_retry(
            "u", "ua", ok_statuses={200},
            getter=getter_seq(FakeResp(200)), sleeper=slept.append)
        assert (r.status_code, err, attempt) == (200, None, 1)
        assert slept == []

    def test_transient_bad_status_is_absorbed(self):
        """THE new behaviour: 503 then 200 => success, so no finding."""
        slept = []
        r, err, attempt = c._get_with_retry(
            "u", "ua", ok_statuses={200},
            getter=getter_seq(FakeResp(503), FakeResp(200)),
            sleeper=slept.append)
        assert err is None
        assert r.status_code == 200
        assert attempt == 2
        assert len(slept) == 1

    def test_persistent_bad_status_returns_the_response(self):
        """err stays None so the caller reports http_<status>, not a network
        reason — the status is real information and must not be flattened."""
        r, err, attempt = c._get_with_retry(
            "u", "ua", ok_statuses={200},
            getter=getter_seq(FakeResp(503)), sleeper=lambda _s: None)
        assert r is not None and r.status_code == 503
        assert err is None
        assert attempt == 2

    def test_transient_exception_still_absorbed(self):
        r, err, attempt = c._get_with_retry(
            "u", "ua", ok_statuses={200},
            getter=getter_seq(ReadTimeout("x"), FakeResp(200)),
            sleeper=lambda _s: None)
        assert err is None and r.status_code == 200 and attempt == 2

    def test_persistent_exception_reports_network_reason(self):
        r, err, attempt = c._get_with_retry(
            "u", "ua", ok_statuses={200},
            getter=getter_seq(ReadTimeout("x")), sleeper=lambda _s: None)
        assert (r, err, attempt) == (None, "ReadTimeout", 2)


# ── 2. the tier probes are rewired ──────────────────────────────────
class TestTierProbesUseTheHelper:
    def test_no_direct_httpx_call_remains(self):
        src = inspect.getsource(c._tier_agreement_probes)
        assert "httpx.get" not in src, (
            "an un-retried direct fetch is the gap being closed")
        assert "_get_with_retry" in src
        assert "ok_statuses" in src

    def test_persistent_network_failure_stamps_attempt_2(self, monkeypatch):
        monkeypatch.setattr(c, "_get_with_retry",
                            lambda url, ua, **k: (None, "ReadTimeout", 2))
        findings = c._tier_agreement_probes()
        assert findings, "expected one finding per sampled pair"
        for f in findings:
            assert f["path"].startswith("tier_agree:")
            assert f["reason"] == "probe_error_ReadTimeout"
            assert f["attempt"] == 2, "hardcoded 1 understated a 2-try failure"
            assert f["status"] is None

    def test_persistent_bad_status_reports_http_with_attempt_2(self, monkeypatch):
        monkeypatch.setattr(c, "_get_with_retry",
                            lambda url, ua, **k: (FakeResp(503), None, 2))
        findings = c._tier_agreement_probes()
        assert findings
        for f in findings:
            assert f["reason"] == "check.json http_503"
            assert f["status"] == 503
            assert f["attempt"] == 2

    def test_agreeing_tiers_produce_no_finding(self, monkeypatch):
        monkeypatch.setattr(c, "_get_with_retry",
                            lambda url, ua, **k: (FakeResp(200, AGREE), None, 1))
        assert c._tier_agreement_probes() == []

    def test_real_mismatch_still_reported(self, monkeypatch):
        """The retry must not mask the semantic check this probe exists for."""
        monkeypatch.setattr(
            c, "_get_with_retry",
            lambda url, ua, **k: (FakeResp(200, DISAGREE), None, 1))
        findings = c._tier_agreement_probes()
        assert findings
        for f in findings:
            assert "tier(labeled)!=tier_canonical(verified)" in f["reason"]
            assert f["status"] == 200
            assert f["attempt"] == 1

    def test_malformed_body_is_a_probe_error_not_a_crash(self, monkeypatch):
        monkeypatch.setattr(
            c, "_get_with_retry",
            lambda url, ua, **k: (FakeResp(200, _BAD_JSON), None, 1))
        findings = c._tier_agreement_probes()
        assert findings
        for f in findings:
            assert f["reason"].startswith("probe_error_")
            assert f["status"] == 200

    def test_wrapper_still_never_raises(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("unexpected")
        monkeypatch.setattr(c, "_get_with_retry", boom)
        assert c._tier_agreement_probes() == []


# ── 3. parity still enforced ────────────────────────────────────────
class TestPolicyParity:
    def test_constants_unchanged(self):
        assert c.RETRY_ATTEMPTS == 2
        assert c.RETRY_SLEEP_S == 1.0

    def test_both_agreement_probe_sets_use_the_helper(self):
        """Neither probe family may keep a bare fetch."""
        for fn in (c._tier_agreement_probes,
                   c._address_label_agreement_probes):
            src = inspect.getsource(fn)
            assert "httpx.get" not in src
            assert "_get_with_retry" in src
