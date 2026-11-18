"""/amendments.json serves the signed leaf, and never hangs on upstream.

WHY (2026-11-18)
----------------
The route called _assemble_amendments_block() live on EVERY request. That
assembler makes two upstream calls -- own-node RPC and the data.xrpl.org
vote fetch -- with no cache, so public latency was whatever upstream
happened to be. Measured on a cold Render instance minutes after a deploy:

    200 in 9.41s, 200 in 42.68s, 200 in 5.78s

Warmed it settles to ~1-1.9s, but every deploy reopens that cold window, and
Render redeploys on every push to main -- including the nightly one-file OFAC
data commit, because autoDeploy is true with no buildFilter. A 42s response
on a machine-readable route trips the public-route canary's ReadTimeout.

The signed leaf already holds this exact block, written by the daily signer,
and reading it is local. So: leaf first, live only as a bounded fallback.

Hermetic: the assembler is stubbed in every test here, so nothing touches
the network. Unlike the chain-guard test, the assembler is NOT the code under
test -- the route's source selection is -- so stubbing it wholesale is
correct here.
"""
from __future__ import annotations

import json
import os
import sys
import time

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

import app as flask_app  # noqa: E402

LEAF_BLOCK = {
    "threshold_display": "29/35",
    "as_of_unix": 1794000000,
    "per_amendment": {
        "LeafAmendment": {
            "hash": "AB12", "enabled": False,
            "network_votes": {"count": 22, "validations": 35,
                              "threshold": 28, "votes_needed": 29},
        }
    },
}


@pytest.fixture
def client():
    flask_app.app.config["TESTING"] = True
    with flask_app.app.test_client() as c:
        yield c


@pytest.fixture
def no_live(monkeypatch):
    """Fail loudly if the live assembler is reached when it shouldn't be."""
    calls = {"n": 0}

    def _spy(*a, **k):
        calls["n"] += 1
        return {"per_amendment": {"LiveAmendment": {"hash": "FF"}},
                "threshold_display": "29/35"}

    import signed_snapshot
    monkeypatch.setattr(signed_snapshot, "_assemble_amendments_block", _spy)
    return calls


def _body(resp):
    return json.loads(resp.data.decode("utf-8"))


def _block(resp):
    d = _body(resp)
    return (d.get("data") or d).get("amendments_block") or {}


def _served_from(resp):
    d = _body(resp)
    return (d.get("data") or d).get("served_from")


# ── leaf first ───────────────────────────────────────────────────────────

def test_serves_the_signed_leaf_without_touching_upstream(
    client, monkeypatch, no_live
):
    monkeypatch.setattr(flask_app, "_list_signed_snapshots",
                        lambda *a, **k: ["2026-11-18"])
    monkeypatch.setattr(flask_app, "_read_signed_envelope",
                        lambda d: {"leaf": True})
    import amendments_permalink as ap
    monkeypatch.setattr(ap, "amendments_block_from_envelope",
                        lambda env: dict(LEAF_BLOCK))

    r = client.get("/amendments.json")
    assert r.status_code == 200
    assert "LeafAmendment" in _block(r).get("per_amendment", {})
    assert _served_from(r) == "signed_leaf:2026-11-18"
    assert no_live["n"] == 0, (
        "leaf was available but the route still called upstream")


def test_picks_the_newest_leaf(client, monkeypatch, no_live):
    monkeypatch.setattr(flask_app, "_list_signed_snapshots",
                        lambda *a, **k: ["2026-11-16", "2026-11-18",
                                         "2026-11-17"])
    monkeypatch.setattr(flask_app, "_read_signed_envelope",
                        lambda d: {"d": d})
    import amendments_permalink as ap
    monkeypatch.setattr(ap, "amendments_block_from_envelope",
                        lambda env: dict(LEAF_BLOCK))
    assert _served_from(client.get("/amendments.json")) == \
        "signed_leaf:2026-11-18"


# ── bounded live fallback ────────────────────────────────────────────────

def test_falls_back_to_live_when_no_leaf(client, monkeypatch, no_live):
    monkeypatch.setattr(flask_app, "_list_signed_snapshots",
                        lambda *a, **k: [])
    r = client.get("/amendments.json")
    assert r.status_code == 200
    assert _served_from(r) == "live"
    assert no_live["n"] == 1
    assert "LiveAmendment" in _block(r).get("per_amendment", {})


def test_an_empty_leaf_block_falls_through_to_live(client, monkeypatch,
                                                  no_live):
    """A leaf with no per_amendment is not usable -- do not serve {}."""
    monkeypatch.setattr(flask_app, "_list_signed_snapshots",
                        lambda *a, **k: ["2026-11-18"])
    monkeypatch.setattr(flask_app, "_read_signed_envelope",
                        lambda d: {"leaf": True})
    import amendments_permalink as ap
    monkeypatch.setattr(ap, "amendments_block_from_envelope",
                        lambda env: {"per_amendment": {}})
    assert _served_from(client.get("/amendments.json")) == "live"
    assert no_live["n"] == 1


def test_an_unreadable_leaf_falls_through_rather_than_500(client, monkeypatch,
                                                         no_live):
    def _boom(*a, **k):
        raise RuntimeError("leaf unreadable")
    monkeypatch.setattr(flask_app, "_list_signed_snapshots", _boom)
    r = client.get("/amendments.json")
    assert r.status_code == 200
    assert _served_from(r) == "live"


# ── THE POINT: a slow upstream cannot hang the request ───────────────────

def test_a_slow_upstream_is_bounded_and_does_not_42_second(
    client, monkeypatch
):
    """The 42.68s measurement, reproduced and bounded.

    The assembler sleeps far past the budget; the request must still come
    back promptly and say the block was unavailable rather than wait.
    """
    import signed_snapshot
    monkeypatch.setattr(flask_app, "_list_signed_snapshots",
                        lambda *a, **k: [])
    monkeypatch.setattr(flask_app, "_AMENDMENTS_LIVE_TIMEOUT_S", 0.3)

    def _slow(*a, **k):
        time.sleep(30)
        return {"per_amendment": {"TooLate": {}}}

    monkeypatch.setattr(signed_snapshot, "_assemble_amendments_block", _slow)

    t0 = time.time()
    r = client.get("/amendments.json")
    elapsed = time.time() - t0

    assert r.status_code == 503, "an unanswerable request must not read 200"
    assert elapsed < 10, f"request waited {elapsed:.1f}s on a slow upstream"
    assert _served_from(r) == "unavailable"
    assert _block(r) == {}


def test_the_bound_is_configurable_and_sane():
    assert isinstance(flask_app._AMENDMENTS_LIVE_TIMEOUT_S, (int, float))
    assert 0 < flask_app._AMENDMENTS_LIVE_TIMEOUT_S <= 10


# -- Render reality: no signed_snapshots/ files, leaf must come from PG ----

def test_leaf_comes_from_postgres_with_no_local_file(client, monkeypatch,
                                                    no_live, tmp_path):
    """Render has no signed_snapshots/ files at all -- the Mac writes them.

    Both helpers are PG-first with disk fallback (_list_signed_snapshots:
    "Prefer Postgres (Render has no disk access to the Mac-written files)").
    This proves the leaf path works with the disk directory pointed at an
    EMPTY dir, so only the Postgres readers can be supplying it. Without
    this, the whole fix could have been a no-op in production.
    """
    import db

    empty = tmp_path / "no_snapshots"
    empty.mkdir()
    monkeypatch.setattr(flask_app, "SIGNED_SNAPSHOTS_DIR", str(empty))

    # Postgres is the ONLY source of truth here.
    monkeypatch.setattr(db, "read_signed_snapshot_dates",
                        lambda *a, **k: ["2026-11-18"])
    monkeypatch.setattr(
        db, "read_signed_snapshot",
        lambda d: {"snapshot_date_utc": d, "schema_version": 6,
                   "metrics": [{"name": "amendments_block",
                               "value": dict(LEAF_BLOCK)}]})

    r = client.get("/amendments.json")
    assert r.status_code == 200
    assert _served_from(r) == "signed_leaf:2026-11-18", (
        "leaf not served from Postgres when no local file exists")
    assert "LeafAmendment" in _block(r).get("per_amendment", {})
    assert no_live["n"] == 0, "fell back to upstream despite a PG leaf"
    assert not list(empty.iterdir()), "test wrote to disk; PG was not proven"


# -- the freshness contract must follow the source ------------------------

def _freshness(resp):
    d = _body(resp)
    meta = d.get("meta") or d.get("source") or d
    return json.dumps(d)


def test_leaf_is_not_served_under_a_one_hour_contract(client, monkeypatch,
                                                      no_live):
    """A signed leaf is written once a day. Serving it while promising
    "<= 1h" would be a false promise to every machine reader -- the
    contract is the thing they trust."""
    monkeypatch.setattr(flask_app, "_list_signed_snapshots",
                        lambda *a, **k: ["2026-11-18"])
    monkeypatch.setattr(flask_app, "_read_signed_envelope",
                        lambda d: {"leaf": True})
    import amendments_permalink as ap
    monkeypatch.setattr(ap, "amendments_block_from_envelope",
                        lambda env: dict(LEAF_BLOCK))

    raw = _freshness(client.get("/amendments.json"))
    assert "24h" in raw, "leaf served without stating its real 24h bound"
    assert "\u2264 1h\"" not in raw, "leaf served under the old 1h promise"
    # and the reader can pin the exact age from served_from
    assert "signed_leaf:2026-11-18" in raw


def test_live_keeps_the_one_hour_contract(client, monkeypatch, no_live):
    monkeypatch.setattr(flask_app, "_list_signed_snapshots",
                        lambda *a, **k: [])
    raw = _freshness(client.get("/amendments.json"))
    assert "1h" in raw and "live" in raw


def test_unavailable_is_503_retry_after_and_uncacheable(client, monkeypatch):
    """Charlie ruling 2026-11-18: a 200 with an empty block is a FALSE
    ANSWER -- a machine reader cannot tell "no amendments in flight" from
    "we could not find out", and the second is an error.

    Also pins no-store: a cached empty block would outlive the outage that
    produced it, so a 60s public cache would keep serving the failure after
    the upstream recovered.
    """
    import signed_snapshot
    monkeypatch.setattr(flask_app, "_list_signed_snapshots",
                        lambda *a, **k: [])
    monkeypatch.setattr(flask_app, "_AMENDMENTS_LIVE_TIMEOUT_S", 0.2)
    monkeypatch.setattr(signed_snapshot, "_assemble_amendments_block",
                        lambda *a, **k: (_ for _ in ()).throw(
                            RuntimeError("upstream down")))
    r = client.get("/amendments.json")
    assert r.status_code == 503
    assert r.headers.get("Retry-After") == "60"
    assert r.headers.get("Cache-Control") == "no-store"
    assert "public" not in (r.headers.get("Cache-Control") or "")
    # body is unchanged from the 200 shape, just carried under 503
    assert _block(r) == {}
    assert _served_from(r) == "unavailable"
    assert "unavailable" in _freshness(r)


def test_successful_paths_keep_200_and_public_caching(client, monkeypatch,
                                                      no_live):
    """The 503 must be scoped to the failure path only."""
    monkeypatch.setattr(flask_app, "_list_signed_snapshots",
                        lambda *a, **k: ["2026-11-18"])
    monkeypatch.setattr(flask_app, "_read_signed_envelope",
                        lambda d: {"leaf": True})
    import amendments_permalink as ap
    monkeypatch.setattr(ap, "amendments_block_from_envelope",
                        lambda env: dict(LEAF_BLOCK))
    r = client.get("/amendments.json")
    assert r.status_code == 200
    assert "public" in (r.headers.get("Cache-Control") or "")
    assert r.headers.get("Retry-After") is None


def test_route_no_longer_assembles_live_unconditionally():
    """Source guard: the old shape called the assembler first thing."""
    src = open(os.path.join(_REPO, "app.py"), encoding="utf-8").read()
    start = src.index("def amendments_json(")
    body = src[start:start + 3000]
    leaf_at = body.find("_list_signed_snapshots")
    live_at = body.find("_amendments_block_live_bounded")
    assert leaf_at != -1 and live_at != -1
    assert leaf_at < live_at, "the leaf must be consulted before live"
