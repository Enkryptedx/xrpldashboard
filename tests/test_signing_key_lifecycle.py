"""Signing-key lifecycle tests — the cheap cases the tamper suite didn't cover.

Prior coverage in `test_mcp_tools_signed_snapshot.py`:
  - Tampered payload → verify_result False, "leaf_hash mismatch" issue.
  - Tampered signature → verify_result False, "signature did NOT verify" issue.
  - Round-trip with real on-disk snapshot + real pinned pubkey.

Gaps closed here (2026-08-12 signing-key lifecycle audit):
  - `sign-with-wrong-key rejected` — verify against an unrelated pubkey and
    prove the failure is loud (fingerprint mismatch + signature failure both
    surface in `issues`, verify_result is False).
  - `load_private_key fails loud when PEM absent` — the walker must exit
    rather than silently skip signing, or L1 pager blindness returns.
  - `load_public_key fails loud when PEM absent` — verification path must
    likewise raise, not silently return True.

Deferred (expensive, not in scope for cheap-tests pass):
  - Multi-epoch rotated-key verifier behaviour: requires the multi-key
    manifest schema documented in `docs/KEY_ROTATION_PROCEDURE.md`, which
    does not exist in code yet. File a follow-up when the first rotation
    approaches — the procedure doc is the spec.
"""
from __future__ import annotations

import copy
import glob
import os

import pytest

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import mcp_server
import mcp_tools_signed_snapshot
import signed_snapshot


def _install_stamp_noop(monkeypatch):
    monkeypatch.setattr(mcp_server, "stamp_tool_call", lambda name: None)


def _latest_snapshot_date() -> str | None:
    files = sorted(glob.glob(os.path.join(
        mcp_tools_signed_snapshot.SNAPSHOTS_DIR, "20??-??-??.json"
    )))
    if not files:
        return None
    return os.path.basename(files[-1]).removesuffix(".json")


# ─────────────────────────────────────────────────────────────────────
# sign-with-wrong-key: verifier must reject cleanly
# ─────────────────────────────────────────────────────────────────────

def test_verify_rejects_wrong_key(monkeypatch):
    """Point `load_public_key` at an unrelated Ed25519 pubkey and verify
    a real snapshot. Two things must fail: the fingerprint cross-check
    (renders the substitution visible) AND the signature check itself.
    Both are named in `issues`; verify_result is False."""
    date_str = _latest_snapshot_date()
    if date_str is None:
        pytest.skip("no signed snapshots on disk — run signed_snapshot.py first")
    _install_stamp_noop(monkeypatch)

    unrelated_pub = Ed25519PrivateKey.generate().public_key()
    monkeypatch.setattr(signed_snapshot, "load_public_key", lambda: unrelated_pub)

    fetched = mcp_tools_signed_snapshot.tool_get_signed_snapshot(date_str)
    bare = copy.deepcopy(fetched["data"])
    verified = mcp_tools_signed_snapshot.tool_verify_snapshot_signature(bare)

    assert verified["data"]["verify_result"] is False
    issues = verified["data"]["issues"]
    assert any("pubkey fingerprint mismatch" in i for i in issues), issues
    assert any("signature did NOT verify" in i for i in issues), issues


# ─────────────────────────────────────────────────────────────────────
# key-file-unreadable: fail loud, never silent
# ─────────────────────────────────────────────────────────────────────

def test_load_private_key_fails_loud_when_missing(monkeypatch, tmp_path):
    """If the encrypted PEM is gone (rotation half-done, disk fault, wrong
    HOME), the walker must exit — a silent skip would let unsigned or
    partially-signed state ship. `SystemExit` is what launchd surfaces."""
    monkeypatch.setattr(
        signed_snapshot, "PRIVKEY_ENC_PATH", str(tmp_path / "missing.pem")
    )
    with pytest.raises(SystemExit, match="private key not found"):
        signed_snapshot.load_private_key()


def test_load_public_key_fails_loud_when_missing(monkeypatch, tmp_path):
    """Symmetric guarantee on the verifier side: no pubkey → raise, not
    return True. Would otherwise let a rotation deploy that lost the
    pubkey pass verification checks silently."""
    monkeypatch.setattr(
        signed_snapshot, "PUBKEY_PEM_PATH", str(tmp_path / "missing_pub.pem")
    )
    with pytest.raises(SystemExit, match="public key not found"):
        signed_snapshot.load_public_key()
