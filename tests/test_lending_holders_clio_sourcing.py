"""The lending depositor walk is sourced from Clio and fails loudly.

`mpt_holders` is Clio-only: rippled 3.4.1 answers `unknownCmd`.
lending_snapshot used to send it to XRPL_NODE (via lending_data), which the
env drift guard pins to our own rippled -- so every depositor count was
structurally unobtainable, exactly like the MPT holders walk that sat dead
for 88 days.

It was never caught because it could not fire: the LendingProtocol amendment
is not enabled, so `activated` is False, `brokers` is empty, and the
enrichment loop has no rows to walk. A latent fault that arms itself the day
lending activates, in a walker that runs every 15 minutes. PR #196 routed
only mpt_snapshot.py; this was the second caller, found by auditing every
RPC method our jobs call against our own node on 2026-11-27.

A SECOND, independent bug lived in the same function: it read
`result["holders"]`, but Clio returns the page under `mptokens` (measured
against s2-clio 2026-11-27). So even against a healthy Clio endpoint it
would have counted zero -- and reported that zero as a real depositor count.
Both are pinned here, because fixing only the endpoint would have produced a
walk that reached Clio and still always answered 0.

No network: httpx.post is monkeypatched throughout.
"""

import pytest

import lending_snapshot as L


OUR_OWN_NODE = "http://192.168.40.95:5006"


@pytest.fixture(autouse=True)
def _reset_dead_endpoints():
    """_HOLDERS_DEAD is module-global and persists across a run."""
    L._HOLDERS_DEAD.clear()
    yield
    L._HOLDERS_DEAD.clear()


class FakePost:
    """Records every URL posted to and replays scripted responses."""

    def __init__(self, by_url=None, default=None, exc_urls=()):
        self.by_url = by_url or {}
        self.default = default if default is not None else {"mptokens": []}
        self.exc_urls = set(exc_urls)
        self.urls = []

    def __call__(self, url, json=None, timeout=None, **kw):
        self.urls.append(url)
        if url in self.exc_urls:
            raise RuntimeError("connection refused")
        body = self.by_url.get(url, self.default)
        if callable(body):
            body = body(json)
        return FakeResp(body)


class FakeResp:
    def __init__(self, result):
        self._result = result

    def json(self):
        return {"result": self._result}


# ── the core fix: Clio, never our own node ───────────────────────────

def test_the_walk_targets_the_clio_endpoint_list(monkeypatch):
    fp = FakePost(default={"mptokens": [{"account": "rA"}]})
    monkeypatch.setattr(L.httpx, "post", fp)
    L._count_mpt_holders("ABC")
    assert fp.urls, "no RPC was attempted"
    assert fp.urls[0] in L.mpt_holders_nodes()


def test_the_walk_never_touches_our_own_node(monkeypatch):
    """THE regression. Our own node cannot serve this method at all."""
    fp = FakePost(default={"mptokens": []})
    monkeypatch.setattr(L.httpx, "post", fp)
    L._count_mpt_holders("ABC")
    for url in fp.urls:
        assert OUR_OWN_NODE not in url
        assert "192.168.40.95" not in url


def test_it_shares_the_one_endpoint_list_with_the_mpt_walk():
    """One list, one env override -- not a second copy to drift."""
    import mpt_snapshot
    assert L.mpt_holders_nodes() == mpt_snapshot.mpt_holders_nodes()


# ── the second bug: Clio returns `mptokens`, not `holders` ───────────

def test_the_page_is_read_from_mptokens(monkeypatch):
    fp = FakePost(default={"mptokens": [{"account": "rA"}, {"account": "rB"}]})
    monkeypatch.setattr(L.httpx, "post", fp)
    got = L._count_mpt_holders("ABC")
    assert got["count"] == 2
    assert got["reason"] == "complete"


def test_a_holders_only_payload_counts_zero_not_two(monkeypatch):
    """Pins the bug's shape: the OLD key must no longer be believed. If
    `holders` were still read, this would report 2 depositors."""
    fp = FakePost(default={"holders": [{"account": "rA"}, {"account": "rB"}]})
    monkeypatch.setattr(L.httpx, "post", fp)
    got = L._count_mpt_holders("ABC")
    assert got["count"] == 0
    assert got["reason"] == "no_holders"


# ── source labelling ─────────────────────────────────────────────────

def test_the_answering_endpoint_is_recorded_as_the_source(monkeypatch):
    nodes = L.mpt_holders_nodes()
    fp = FakePost(default={"mptokens": [{"account": "rA"}]})
    monkeypatch.setattr(L.httpx, "post", fp)
    got = L._count_mpt_holders("ABC")
    assert got["source"] == nodes[0]


def test_a_depositor_count_is_never_sovereign(monkeypatch):
    """Standing fact, not a per-run outcome: our own node cannot answer
    this method at any endpoint."""
    fp = FakePost(default={"mptokens": [{"account": "rA"}]})
    monkeypatch.setattr(L.httpx, "post", fp)
    for payload in ("ABC", None):
        assert L._count_mpt_holders(payload)["sovereign"] is False


def test_every_reason_is_from_the_declared_enum(monkeypatch):
    allowed = {"complete", "no_holders", "incomplete",
               "rpc_unsupported", "rpc_error", "pending"}
    fp = FakePost(default={"mptokens": []})
    monkeypatch.setattr(L.httpx, "post", fp)
    assert L._count_mpt_holders("ABC")["reason"] in allowed
    assert L._count_mpt_holders(None)["reason"] in allowed


# ── failure behaviour, mirroring mpt_snapshot ────────────────────────

def test_unknown_cmd_everywhere_is_rpc_unsupported(monkeypatch):
    fp = FakePost(default={"error": "unknownCmd", "error_code": 32})
    monkeypatch.setattr(L.httpx, "post", fp)
    got = L._count_mpt_holders("ABC")
    assert got["reason"] == "rpc_unsupported"
    assert got["count"] is None
    assert got["source"] is None


def test_transport_failure_everywhere_is_rpc_error(monkeypatch):
    fp = FakePost(exc_urls=L.mpt_holders_nodes())
    monkeypatch.setattr(L.httpx, "post", fp)
    got = L._count_mpt_holders("ABC")
    assert got["reason"] == "rpc_error"
    assert got["count"] is None


def test_an_unsupported_endpoint_is_asked_once_then_abandoned(monkeypatch):
    """A rippled server cannot be talked into implementing the method."""
    nodes = L.mpt_holders_nodes()
    fp = FakePost(
        by_url={nodes[0]: {"error": "unknownCmd"},
                nodes[1]: {"mptokens": [{"account": "rA"}]}},
    )
    monkeypatch.setattr(L.httpx, "post", fp)
    first = L._count_mpt_holders("ABC")
    assert first["count"] == 1 and first["source"] == nodes[1]
    before = len(fp.urls)
    L._count_mpt_holders("DEF")
    tried_after = fp.urls[before:]
    assert nodes[0] not in tried_after, "dead endpoint was asked again"


def test_it_fails_over_to_the_second_endpoint(monkeypatch):
    nodes = L.mpt_holders_nodes()
    fp = FakePost(by_url={nodes[1]: {"mptokens": []}}, exc_urls=[nodes[0]])
    monkeypatch.setattr(L.httpx, "post", fp)
    got = L._count_mpt_holders("ABC")
    assert got["reason"] == "no_holders"
    assert got["source"] == nodes[1]


def test_page_cap_mid_chain_is_incomplete_not_a_count(monkeypatch):
    """A partial count is not a count."""
    fp = FakePost(default={"mptokens": [{"account": "rA"}], "marker": "more"})
    monkeypatch.setattr(L.httpx, "post", fp)
    monkeypatch.setattr(L, "HOLDER_PAGE_CAP", 3)
    got = L._count_mpt_holders("ABC")
    assert got["reason"] == "incomplete"
    assert got["count"] is None


def test_a_missing_share_mpt_id_is_pending_not_an_error(monkeypatch):
    fp = FakePost()
    monkeypatch.setattr(L.httpx, "post", fp)
    got = L._count_mpt_holders(None)
    assert got["reason"] == "pending" and got["count"] is None
    assert fp.urls == [], "no RPC should be attempted without an id"


# ── unsupported_share denominator ────────────────────────────────────

def test_share_counts_attempts_only():
    """not_attempted and pending never reached the method; counting them
    would let a fully broken endpoint list hide behind a pile of skips."""
    counts = {"rpc_unsupported": 2, "not_attempted": 100, "pending": 50}
    assert L.unsupported_share(counts) == 1.0


def test_share_is_zero_when_nothing_was_attempted():
    assert L.unsupported_share({"not_attempted": 30}) == 0.0
    assert L.unsupported_share({}) == 0.0


def test_share_is_proportional():
    assert L.unsupported_share(
        {"rpc_unsupported": 1, "complete": 3}) == 0.25


# ── run_once: fails loudly, and stays quiet pre-activation ───────────

def _run(monkeypatch, data, fp=None, tmp_path=None):
    monkeypatch.setattr(L, "fetch_lending_data", lambda: data)
    if fp is not None:
        monkeypatch.setattr(L.httpx, "post", fp)
    return L.run_once(path=str(tmp_path / "snap.json"))


def test_dominant_unsupported_fails_the_run_loudly(monkeypatch, tmp_path):
    data = {"ok": True, "activated": True,
            "brokers": [{"share_mpt_id": f"ID{i}"} for i in range(4)]}
    fp = FakePost(default={"error": "unknownCmd"})
    out = _run(monkeypatch, data, fp, tmp_path)
    assert out["ok"] is False, "a run that produced nothing reported green"
    enr = out["depositor_enrich"]
    assert enr["unsupported_share"] == 1.0
    assert "MPT_HOLDERS_NODES" in enr["alert"]
    assert "Clio-only" in enr["alert"]


def test_a_healthy_run_stays_ok_and_records_provenance(monkeypatch, tmp_path):
    data = {"ok": True, "activated": True,
            "brokers": [{"share_mpt_id": "ID1"}, {"share_mpt_id": "ID2"}]}
    fp = FakePost(default={"mptokens": [{"account": "rA"}]})
    out = _run(monkeypatch, data, fp, tmp_path)
    assert out["ok"] is True
    enr = out["depositor_enrich"]
    assert "alert" not in enr
    assert enr["sovereign"] is False
    assert enr["counts"]["complete"] == 2
    assert enr["endpoints"] == list(L.mpt_holders_nodes())


def test_pre_activation_does_not_enrich_or_alert(monkeypatch, tmp_path):
    """The live state today: LendingProtocol disabled, brokers empty. The
    walk must not fire, and must not be reported as a failure."""
    data = {"ok": True, "activated": False, "brokers": []}
    fp = FakePost()
    out = _run(monkeypatch, data, fp, tmp_path)
    assert out["ok"] is True
    assert "depositor_enrich" not in out
    assert fp.urls == []


def test_depositors_stays_int_or_none_for_the_template(monkeypatch, tmp_path):
    """templates/lending.html prints b.depositors behind an `is not none`
    guard, so the provenance must travel in SEPARATE keys."""
    data = {"ok": True, "activated": True,
            "brokers": [{"share_mpt_id": "ID1"}, {"share_mpt_id": None}]}
    fp = FakePost(default={"mptokens": [{"account": "rA"}]})
    out = _run(monkeypatch, data, fp, tmp_path)
    good, missing = out["brokers"]
    assert good["depositors"] == 1
    assert missing["depositors"] is None
    for b in out["brokers"]:
        assert isinstance(b["depositors"], int) or b["depositors"] is None
        assert b["depositors_sovereign"] is False
        assert "depositors_reason" in b


def test_brokers_past_the_cut_are_marked_not_attempted(monkeypatch, tmp_path):
    data = {"ok": True, "activated": True,
            "brokers": [{"share_mpt_id": f"ID{i}"} for i in range(3)]}
    fp = FakePost(default={"mptokens": []})
    monkeypatch.setattr(L, "fetch_lending_data", lambda: data)
    monkeypatch.setattr(L.httpx, "post", fp)
    out = L.run_once(path=str(tmp_path / "s.json"), enrich_top_n=1)
    assert out["brokers"][0]["depositors_reason"] == "no_holders"
    for b in out["brokers"][1:]:
        assert b["depositors_reason"] == "not_attempted"
        assert b["depositors"] is None
    assert out["depositor_enrich"]["counts"]["not_attempted"] == 2
