"""load_chain() fails closed when chain.json is missing.

THE HOLE THIS CLOSES (2026-11-18)
---------------------------------
load_chain() used to return a fresh empty chain whenever chain.json was
absent. That conflates two situations the code cannot tell apart:

  * a legitimate first-ever run, where an empty chain is correct;
  * the signer running from a tree with no chain data -- a git worktree, a
    fresh clone, a renamed SNAPSHOTS_DIR, a restored machine whose volume
    is not mounted.

In the second case the old behaviour silently started a NEW chain: leaf_index
0, previous_root None, a fresh root published as authoritative, and 182
existing leaves plus 15 on-ledger anchors orphaned with nothing in the output
saying so.

Not hypothetical: CHAIN_PATH is derived from __file__, so simply importing
this module from a worktree already points it at a directory with no
snapshots. That is exactly how a chain_link verification failure was briefly
mistaken for a code regression earlier the same day.

Both paths are pinned here: refusal by default, and init only when
explicitly authorised.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

import signed_snapshot as ss  # noqa: E402


@pytest.fixture
def missing_chain(tmp_path, monkeypatch):
    """Point CHAIN_PATH at a path that does not exist."""
    target = tmp_path / "signed_snapshots" / "chain.json"
    monkeypatch.setattr(ss, "CHAIN_PATH", str(target))
    monkeypatch.delenv(ss.INIT_CHAIN_ENV, raising=False)
    assert not os.path.exists(ss.CHAIN_PATH)
    return target


@pytest.fixture
def real_chain(tmp_path, monkeypatch):
    """A populated chain, as production has."""
    d = tmp_path / "signed_snapshots"
    d.mkdir()
    target = d / "chain.json"
    payload = {
        "schema_version": 6,
        "current_root": "deadbeef",
        "leaves": [{"date": "2026-11-17", "leaf_hash": "aa"},
                   {"date": "2026-11-18", "leaf_hash": "bb"}],
    }
    target.write_text(json.dumps(payload))
    monkeypatch.setattr(ss, "CHAIN_PATH", str(target))
    monkeypatch.delenv(ss.INIT_CHAIN_ENV, raising=False)
    return payload


# ── path 1: refuse ───────────────────────────────────────────────────────

def test_missing_chain_refuses_by_default(missing_chain):
    with pytest.raises(ss.ChainMissingError) as exc:
        ss.load_chain()
    msg = str(exc.value)
    assert "REFUSING to start a fresh chain" in msg
    assert "--init-chain" in msg, "the message must say how to proceed"
    assert ss.INIT_CHAIN_ENV in msg


def test_refusal_is_a_nonzero_exit(missing_chain):
    """SystemExit with a message => non-zero exit, reason on stderr."""
    assert issubclass(ss.ChainMissingError, SystemExit)
    with pytest.raises(SystemExit) as exc:
        ss.load_chain()
    assert exc.value.code != 0


def test_refusal_writes_nothing(missing_chain):
    with pytest.raises(ss.ChainMissingError):
        ss.load_chain()
    assert not os.path.exists(missing_chain), "must not create a chain"
    assert not os.path.exists(os.path.dirname(missing_chain))


def test_refusal_does_not_depend_on_a_stale_env_value(missing_chain,
                                                      monkeypatch):
    """Only the exact value "1" authorises init."""
    for val in ("", "0", "false", "no", "yes", "true", "2"):
        monkeypatch.setenv(ss.INIT_CHAIN_ENV, val)
        with pytest.raises(ss.ChainMissingError):
            ss.load_chain()


# ── path 2: explicit init ────────────────────────────────────────────────

def test_explicit_init_argument_returns_an_empty_chain(missing_chain):
    chain = ss.load_chain(allow_init=True)
    assert chain["leaves"] == []
    assert chain["current_root"] is None
    assert chain["schema_version"] == ss.SCHEMA_VERSION


def test_explicit_init_env_returns_an_empty_chain(missing_chain, monkeypatch):
    monkeypatch.setenv(ss.INIT_CHAIN_ENV, "1")
    chain = ss.load_chain()
    assert chain["leaves"] == []


def test_init_announces_itself(missing_chain, capsys):
    ss.load_chain(allow_init=True)
    err = capsys.readouterr().err
    assert "INITIALISING a new chain" in err
    assert "explicitly authorised" in err


# ── the normal path is untouched ─────────────────────────────────────────

def test_existing_chain_loads_unchanged(real_chain):
    chain = ss.load_chain()
    assert chain == real_chain
    assert len(chain["leaves"]) == 2


def test_existing_chain_ignores_the_init_flag(real_chain):
    """A stray --init-chain must never discard a real chain."""
    chain = ss.load_chain(allow_init=True)
    assert len(chain["leaves"]) == 2, "init must not clobber an existing chain"
    assert chain["current_root"] == "deadbeef"


# ── the CLI surface ──────────────────────────────────────────────────────

def test_cli_exposes_init_chain_and_routes_it_to_the_env():
    src = open(os.path.join(_REPO, "signed_snapshot.py"),
               encoding="utf-8").read()
    assert '"--init-chain"' in src
    code = "\n".join(ln.split("#")[0] for ln in src.splitlines())
    assert f'os.environ[INIT_CHAIN_ENV] = "1"' in code, (
        "the flag must reach load_chain's single check")


def test_help_mentions_the_flag():
    out = subprocess.run(
        [sys.executable, os.path.join(_REPO, "signed_snapshot.py"), "--help"],
        capture_output=True, text=True, timeout=120)
    assert "--init-chain" in out.stdout, out.stderr[:400]


# ── verification must NOT fail closed ────────────────────────────────────

def test_verify_path_still_guards_chain_path_itself():
    """A verifier legitimately runs without a chain and should degrade to
    'could not verify the chain link' -- not refuse. That read is separate
    from load_chain() and must stay existence-checked."""
    src = open(os.path.join(_REPO, "signed_snapshot.py"),
               encoding="utf-8").read()
    assert "if os.path.exists(CHAIN_PATH):" in src, (
        "the verify path's own existence check disappeared")
