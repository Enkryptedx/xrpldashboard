"""The chain guard protects the WRITER and must never break a READER.

WHAT WENT WRONG (PR #162, reverted as #163)
-------------------------------------------
The first attempt put the fail-closed guard inside load_chain() and raised a
SystemExit subclass. Two mistakes compounded:

  1. load_chain() is on READ paths too. _load_previous_leaf_enabled_entries
     calls it, and through that so does _assemble_amendments_block, which
     serves /amendments.json and the MCP get_amendments tool. Those run on
     hosts with no chain at all -- signed_snapshots/ is gitignored, so the
     deployed Render tree never has one. A missing chain is NORMAL there.

  2. SystemExit derives from BaseException, so the route's `except Exception`
     could not catch it. It escaped as a bare SystemExit and killed the
     worker mid-request: three 40s `code=000` timeouts rather than a 500,
     for roughly an hour on 2026-11-18.

My 12 original tests all passed, because every one of them called
load_chain() directly. None went through a web route. That gap is the first
thing pinned below.

THE THREE FIXES
---------------
  * the guard lives in require_existing_chain(), called only from the write
    path; load_chain() stays permissive;
  * ChainMissingError is an Exception, containable by any normal caller;
  * the non-zero exit is decided in main(), not by the exception's type.
"""
from __future__ import annotations

import os
import sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

import signed_snapshot as ss  # noqa: E402


@pytest.fixture
def stub_fetches(monkeypatch):
    """Stub ONLY the two network fetches, never the assembler itself.

    The assembler's own body must stay real, because its hop into
    load_chain() -- via _load_previous_leaf_enabled_entries -- IS the code
    path under test. Stubbing _assemble_amendments_block would make this
    test pass against the broken commit and catch nothing.

    Replaced here, and nothing else:
      amendments_state.fetch_amendments_state_cached   (own-node RPC)
      amendments_network_votes.fetch_network_vote_tallies_cached
                                                       (data.xrpl.org)
    """
    import amendments_network_votes
    import amendments_state

    state = {
        "sourcing": "sovereign",
        "cached_age_seconds": 0,
        "in_flight": [{
            "name": "StubAmendment",
            "hash": "AB12",
            "network_vote": {
                "count": 21, "validations": 35, "threshold": 28,
                "as_of_iso": "2026-11-18T00:00:00Z",
                "source_url": "stub://votes",
            },
        }],
        "superseded": [],
        "unrecognized_enabled": [],
    }
    votes = {"cached_age_seconds": 0, "tallies": {}}
    monkeypatch.setattr(amendments_state, "fetch_amendments_state_cached",
                        lambda *a, **k: state)
    monkeypatch.setattr(amendments_network_votes,
                        "fetch_network_vote_tallies_cached",
                        lambda *a, **k: votes)
    return state


@pytest.fixture
def no_chain(tmp_path, monkeypatch):
    target = tmp_path / "signed_snapshots" / "chain.json"
    monkeypatch.setattr(ss, "CHAIN_PATH", str(target))
    monkeypatch.delenv(ss.INIT_CHAIN_ENV, raising=False)
    assert not os.path.exists(ss.CHAIN_PATH)
    return target


# ── 1. THE REGRESSION: a reader must survive a missing chain ─────────────

def test_amendments_json_route_survives_a_missing_chain(
    monkeypatch, tmp_path, stub_fetches
):
    """The exact case the reverted PR broke, and the exact case my previous
    tests all missed by calling load_chain() directly.

    Hermetic via stub_fetches (the two network calls only). The assembler,
    and its real hop into load_chain(), run unmodified -- verified by this
    test failing on 6bdb028 and passing here.
    """
    import app as flask_app

    monkeypatch.setattr(ss, "CHAIN_PATH",
                        str(tmp_path / "nope" / "chain.json"))
    monkeypatch.delenv(ss.INIT_CHAIN_ENV, raising=False)
    flask_app.app.config["TESTING"] = True
    with flask_app.app.test_client() as c:
        r = c.get("/amendments.json")
    assert r.status_code == 200, (
        f"/amendments.json returned {r.status_code} with no chain.json")


def test_assembler_really_runs_and_reaches_load_chain(no_chain, stub_fetches,
                                                      monkeypatch):
    """Guards the stub itself: prove the assembler body executed (our stub
    amendment appears in its output) AND that it called load_chain, so a
    future refactor cannot quietly make this test hermetic-but-blind."""
    import datetime as _dt

    seen = {"load_chain": 0}
    real = ss.load_chain
    monkeypatch.setattr(
        ss, "load_chain",
        lambda *a, **k: (seen.__setitem__("load_chain",
                                         seen["load_chain"] + 1)
                         or real(*a, **k)))
    block = ss._assemble_amendments_block(
        _dt.datetime.now(_dt.timezone.utc))
    assert isinstance(block, dict), "assembler did not run"
    assert "StubAmendment" in (block.get("per_amendment") or {}), (
        "stubbed fetch data never reached the assembler output")
    assert seen["load_chain"] >= 1, (
        "assembler no longer reaches load_chain -- this test is now blind "
        "to the PR #162 class of bug")


def test_load_chain_stays_permissive_for_readers(no_chain):
    """No raise, no init flag, just the empty default -- unchanged from
    before the guard existed."""
    chain = ss.load_chain()
    assert chain["leaves"] == []
    assert chain["current_root"] is None


def test_assembler_path_does_not_raise_without_a_chain(no_chain):
    """_load_previous_leaf_enabled_entries is the read path that reaches
    load_chain; it must tolerate an absent chain."""
    out = ss._load_previous_leaf_enabled_entries(
        now_utc=__import__("datetime").datetime.now(
            __import__("datetime").timezone.utc))
    assert out is not None or out is None  # must not raise


# ── 2. the error is containable ──────────────────────────────────────────

def test_error_is_an_exception_not_a_systemexit():
    assert issubclass(ss.ChainMissingError, Exception)
    assert not issubclass(ss.ChainMissingError, SystemExit)


def test_a_plain_except_exception_contains_it(no_chain):
    """What the route does. This is what failed before."""
    try:
        ss.require_existing_chain()
        caught = False
    except Exception:
        caught = True
    assert caught, "except Exception must be able to contain the guard"


# ── 3. the writer still fails closed ─────────────────────────────────────

def test_writer_guard_refuses_a_missing_chain(no_chain):
    with pytest.raises(ss.ChainMissingError) as exc:
        ss.require_existing_chain()
    msg = str(exc.value)
    assert "refusing to append" in msg
    assert "--init-chain" in msg and ss.INIT_CHAIN_ENV in msg


def test_refusal_writes_nothing(no_chain):
    with pytest.raises(ss.ChainMissingError):
        ss.require_existing_chain()
    assert not os.path.exists(no_chain)
    assert not os.path.exists(os.path.dirname(no_chain))


@pytest.mark.parametrize("val", ["", "0", "false", "no", "yes", "true", "2"])
def test_only_exactly_one_authorises(no_chain, monkeypatch, val):
    monkeypatch.setenv(ss.INIT_CHAIN_ENV, val)
    with pytest.raises(ss.ChainMissingError):
        ss.require_existing_chain()


def test_both_init_forms_authorise(no_chain, monkeypatch, capsys):
    ss.require_existing_chain(allow_init=True)
    assert "INITIALISING a new chain" in capsys.readouterr().err
    monkeypatch.setenv(ss.INIT_CHAIN_ENV, "1")
    ss.require_existing_chain()


def test_existing_chain_passes_and_is_untouched(tmp_path, monkeypatch):
    import json
    d = tmp_path / "signed_snapshots"; d.mkdir()
    p = d / "chain.json"
    payload = {"schema_version": 6, "current_root": "deadbeef",
               "leaves": [{"date": "2026-11-18", "leaf_hash": "aa"}]}
    p.write_text(json.dumps(payload))
    monkeypatch.setattr(ss, "CHAIN_PATH", str(p))
    monkeypatch.delenv(ss.INIT_CHAIN_ENV, raising=False)
    ss.require_existing_chain()                 # no raise
    ss.require_existing_chain(allow_init=True)  # must not clobber
    assert ss.load_chain() == payload


# ── 4. wiring ────────────────────────────────────────────────────────────

def test_sign_path_calls_the_guard_and_main_owns_the_exit_code():
    src = open(os.path.join(_REPO, "signed_snapshot.py"),
               encoding="utf-8").read()
    code = "\n".join(ln.split("#")[0] for ln in src.splitlines())
    assert "require_existing_chain()" in code, "write path lost the guard"
    assert "except ChainMissingError" in code, "main must contain it"
    assert "return 4" in code, "main must exit non-zero"
    assert '"--init-chain"' in src
    # load_chain itself must NOT raise the guard.
    start = code.index("def load_chain(")
    end = code.index("def require_existing_chain(")
    assert "ChainMissingError" not in code[start:end], (
        "the guard must not live inside load_chain -- that is what broke "
        "readers")
