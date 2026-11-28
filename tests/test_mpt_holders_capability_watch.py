"""The weekly watch for `mpt_holders` arriving on our own node.

We read MPT holder counts from PUBLIC Clio endpoints, and the ONLY
justification is a capability gap: `mpt_holders` is a Clio method and
rippled 3.4.1 answers unknownCmd (measured 2026-11-27 against our own node
and xrplcluster.com, both 3.4.1, while Clio 2.8.0 answers it).

The risk is not that Clio breaks. It is that the gap CLOSES and nobody
notices -- we would keep sending someone else's servers 533 requests a
cycle for data our own node could answer, and holder figures would stay
labelled non-sovereign when they no longer had to be.
tools/anchor_nodes.py already says "re-check after a rippled upgrade so the
note cannot harden into a permanent refusal"; this makes that a job instead
of a comment nobody re-reads.

Note the INVERSION, which these tests pin deliberately: findings_count > 0
here is GOOD NEWS. An operator must not read it as a fault to chase.

No network: httpx.post is monkeypatched throughout.
"""

import pytest

import mpt_holders_capability_watch as W


class FakeResp:
    def __init__(self, result):
        self._result = result

    def json(self):
        return {"result": self._result}


class Boom:
    """Raises on the first n calls, then replays a body."""

    def __init__(self, n, body=None):
        self.n = n
        self.body = body if body is not None else {"mptokens": []}
        self.calls = 0

    def __call__(self, url, json=None, timeout=None, **kw):
        self.calls += 1
        if self.calls <= self.n:
            raise RuntimeError("connection refused")
        return FakeResp(self.body)


def _post(body, counter=None):
    def inner(url, json=None, timeout=None, **kw):
        if counter is not None:
            counter.append(url)
        return FakeResp(body)
    return inner


# ── it probes OUR node, never Clio ───────────────────────────────────

def test_it_probes_our_own_node_not_clio():
    """Asking Clio whether Clio implements a Clio method answers nothing."""
    import sys
    import os
    sys.path.insert(0, os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
    from anchor_nodes import mpt_holders_nodes
    assert W.OWN_NODE not in mpt_holders_nodes()
    for clio in mpt_holders_nodes():
        assert clio != W.OWN_NODE


def test_the_probed_url_is_the_node_we_passed(monkeypatch):
    seen = []
    monkeypatch.setattr(W.httpx, "post", _post({"error": "unknownCmd"}, seen))
    W.probe("http://my-node:5006", "ID", attempts=1)
    assert seen == ["http://my-node:5006"]


def test_it_sends_the_mpt_holders_method(monkeypatch):
    sent = {}

    def capture(url, json=None, timeout=None, **kw):
        sent.update(json or {})
        return FakeResp({"error": "unknownCmd"})

    monkeypatch.setattr(W.httpx, "post", capture)
    W.probe("http://n", "ID", attempts=1)
    assert sent["method"] == "mpt_holders"
    assert sent["params"][0]["mpt_issuance_id"] == "ID"


# ── reading the answer ───────────────────────────────────────────────

@pytest.mark.parametrize("err", ["unknownCmd", "unknownCommand", "notImpl"])
def test_unknown_method_errors_mean_not_supported(monkeypatch, err):
    monkeypatch.setattr(W.httpx, "post", _post({"error": err}))
    v = W.probe("http://n", "ID", attempts=1)
    assert v["supported"] is False
    assert v["error"] == err


def test_a_successful_answer_means_supported(monkeypatch):
    monkeypatch.setattr(W.httpx, "post", _post({"mptokens": [{"account": "rA"}]}))
    v = W.probe("http://n", "ID", attempts=1)
    assert v["supported"] is True


@pytest.mark.parametrize("err", ["invalidParams", "objectNotFound", "actNotFound"])
def test_a_parameter_error_still_proves_the_method_exists(monkeypatch, err):
    """To object to the PARAMETERS the server had to recognise the command.

    This is what stops the watch being defeated by the probe id going
    stale: a dead issuance id must not read as 'still unsupported'.
    """
    monkeypatch.setattr(W.httpx, "post", _post({"error": err}))
    v = W.probe("http://n", "ID", attempts=1)
    assert v["supported"] is True
    assert err in v["detail"]


def test_an_unparseable_body_is_indeterminate_not_supported(monkeypatch):
    monkeypatch.setattr(W.httpx, "post", _post("not-a-dict"))
    v = W.probe("http://n", "ID", attempts=1)
    assert v["supported"] is None


# ── retries: transport only ──────────────────────────────────────────

def test_transport_failure_retries_then_gives_up(monkeypatch):
    boom = Boom(n=99)
    monkeypatch.setattr(W.httpx, "post", boom)
    v = W.probe("http://n", "ID", attempts=3, pause=0, sleep=lambda s: None)
    assert v["supported"] is None
    assert boom.calls == 3


def test_a_transport_blip_then_an_answer_is_believed(monkeypatch):
    boom = Boom(n=1, body={"error": "unknownCmd"})
    monkeypatch.setattr(W.httpx, "post", boom)
    v = W.probe("http://n", "ID", attempts=3, pause=0, sleep=lambda s: None)
    assert v["supported"] is False
    assert boom.calls == 2


def test_an_answer_is_final_and_not_retried(monkeypatch):
    """A server that replied has already told us whether it knows the
    method; asking again cannot change that and wastes requests."""
    seen = []
    monkeypatch.setattr(W.httpx, "post", _post({"error": "unknownCmd"}, seen))
    W.probe("http://n", "ID", attempts=5, pause=0, sleep=lambda s: None)
    assert len(seen) == 1


# ── verdict mapping, including the inversion ─────────────────────────

def test_still_unsupported_is_zero_findings():
    f, msg = W.verdict_to_findings(
        {"supported": False, "error": "unknownCmd", "detail": "x"}, "http://n")
    assert f == 0
    assert "clean" in msg
    assert "justified" in msg


def test_capability_gained_is_a_finding_and_says_it_is_good_news():
    f, msg = W.verdict_to_findings(
        {"supported": True, "error": None, "detail": "answered successfully"},
        "http://n")
    assert f == 1
    assert "good news" in msg.lower()
    assert "MPT_HOLDERS_NODES" in msg
    # Names what to actually change, so the finding is actionable.
    assert "mpt_snapshot.py" in msg and "lending_snapshot.py" in msg


def test_indeterminate_is_a_finding_not_silence():
    """A watch that cannot see is not a watch that is happy."""
    f, msg = W.verdict_to_findings(
        {"supported": None, "error": None, "detail": "unreachable"}, "http://n")
    assert f == 1
    assert "INDETERMINATE" in msg


def test_the_three_outcomes_are_exhaustive():
    seen = set()
    for sup in (True, False, None):
        f, _ = W.verdict_to_findings(
            {"supported": sup, "error": None, "detail": "d"}, "http://n")
        seen.add((sup, f))
    assert seen == {(False, 0), (True, 1), (None, 1)}


# ── wiring ───────────────────────────────────────────────────────────

def test_cadence_is_weekly():
    """Keep equal to StartInterval in the plist."""
    assert W.WALKER_CADENCE_SECONDS == 604800


def test_main_returns_nonzero_only_when_there_is_a_finding(monkeypatch):
    calls = {}

    monkeypatch.setattr(W.db, "write_walker_health_start",
                        lambda *a, **k: None)
    monkeypatch.setattr(W.db, "write_walker_health_end",
                        lambda *a, **k: calls.update(k))

    monkeypatch.setattr(W.httpx, "post", _post({"error": "unknownCmd"}))
    assert W.main() == 0
    assert calls["findings_count"] == 0
    assert calls["ok"] is True

    monkeypatch.setattr(W.httpx, "post", _post({"mptokens": []}))
    assert W.main() == 1
    assert calls["findings_count"] == 1
    # ok stays True: the run completing and the result being noteworthy are
    # different questions. findings_count is the alert axis.
    assert calls["ok"] is True
