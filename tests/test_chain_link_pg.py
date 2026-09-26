"""chain_link_pg — finishing verify_envelope's chain-link step from
Postgres on hosts with no disk chain files (Render). Regression for the
2026-09-04 → 2026-09-26 window in which prod /snapshots/verify and the MCP
verify_snapshot_signature tool reported FAILED / false for every leaf."""
from __future__ import annotations

import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import chain_link_pg as C  # noqa: E402
import db  # noqa: E402
import mcp_server  # noqa: E402
import mcp_tools_signed_snapshot as T  # noqa: E402
import signed_snapshot as ss  # noqa: E402

FIXTURE = os.path.join(HERE, "fixtures", "signed_leaf_2026-09-25.json")
SOFT = "chain_link: could not verify (no chain.json and no prior-day file on disk) — x"


def _leaf():
    with open(FIXTURE) as f:
        return json.load(f)


@pytest.fixture
def no_disk(monkeypatch):
    """Render-like host: no chain.json, no prior-day file."""
    monkeypatch.setattr(ss, "CHAIN_PATH", "/nonexistent/chain.json")
    monkeypatch.setattr(ss, "SNAPSHOTS_DIR", "/nonexistent")
    monkeypatch.setattr(mcp_server, "stamp_tool_call", lambda name: None)


def test_no_soft_note_is_passthrough():
    ok, issues, via = C.complete_chain_link(_leaf(), True, [])
    assert (ok, issues, via) == (True, [], None)


def test_prior_leaf_from_pg_completes_the_check(monkeypatch, no_disk):
    env = _leaf()
    monkeypatch.setattr(db, "read_signed_snapshot_chain", lambda: None)
    monkeypatch.setattr(db, "read_signed_snapshot_by_leaf_index",
                        lambda i: {"chain_root": env["previous_root"]} if i == env["leaf_index"] - 1 else None)
    ok, issues, via = C.complete_chain_link(env, False, [SOFT])
    assert (ok, issues, via) == (True, [], "prior_leaf")


def test_prior_leaf_mismatch_is_a_real_failure(monkeypatch, no_disk):
    env = _leaf()
    monkeypatch.setattr(db, "read_signed_snapshot_chain", lambda: None)
    monkeypatch.setattr(db, "read_signed_snapshot_by_leaf_index", lambda i: {"chain_root": "00" * 20})
    ok, issues, via = C.complete_chain_link(env, False, [SOFT])
    assert ok is False and via == "prior_leaf"
    assert issues and issues[0].startswith("chain_link: previous_root != prior-leaf chain_root")


def test_pg_empty_keeps_soft_note(monkeypatch, no_disk):
    monkeypatch.setattr(db, "read_signed_snapshot_chain", lambda: None)
    monkeypatch.setattr(db, "read_signed_snapshot_by_leaf_index", lambda i: None)
    ok, issues, via = C.complete_chain_link(_leaf(), False, [SOFT])
    assert (ok, issues, via) == (False, [SOFT], None)


def test_mcp_tool_true_on_render_like_host_with_pg(monkeypatch, no_disk):
    """The 09-04..09-26 defect shape: real leaf, no disk files. With the
    prior leaf available from PG the tool must say true, and say how."""
    env = _leaf()
    monkeypatch.setattr(db, "read_signed_snapshot_chain", lambda: None)
    monkeypatch.setattr(db, "read_signed_snapshot_by_leaf_index",
                        lambda i: {"chain_root": env["previous_root"]} if i == env["leaf_index"] - 1 else None)
    out = T.tool_verify_snapshot_signature(env)["data"]
    assert out["verify_result"] is True
    assert out["issues"] == []
    assert out["chain_link_via"] == "prior_leaf"


def test_mcp_tool_false_and_unproven_when_nothing_available(monkeypatch, no_disk):
    monkeypatch.setattr(db, "read_signed_snapshot_chain", lambda: None)
    monkeypatch.setattr(db, "read_signed_snapshot_by_leaf_index", lambda i: None)
    out = T.tool_verify_snapshot_signature(_leaf())["data"]
    assert out["verify_result"] is False
    assert out["chain_link_via"] == "unproven"
    assert any(i.startswith("chain_link: could not verify") for i in out["issues"])
