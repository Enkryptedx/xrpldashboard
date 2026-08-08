"""Day 7 signed-snapshot MCP tool tests.

Four things verified beyond the shared envelope-shape guarantee:

  1. `get_signed_snapshot(date_str)` — envelope emitted, all signed-payload
     fields present in `data`, methodology_url resolves to the
     #signed-snapshot h3 anchor. Bad ISO date raises; absent file raises;
     absence is NEVER a stub.

  2. `verify_snapshot_signature(envelope)` — accepts both the full MCP
     envelope wrapper ({data, proof, server}) AND the bare signed payload
     dict. Returns verify_result:bool, public_key_fingerprint, issues[].

  3. Round-trip receipt — the moat, exercised on-camera: fetch via #18,
     hand to #19 (both shapes), get verify_result=True. This is the
     load-bearing test for Charlie's ship-gate demo.

  4. Tamper case — flip one byte in `metrics` (or `signature_ed25519`),
     verify_result MUST be False and `issues` MUST name what broke.
     Cryptographic integrity or nothing.

Underlying signed_snapshot.load_public_key is monkeypatched only where
we need to inject a controlled keypair; the round-trip test uses the
real pinned key against the live on-disk snapshots (this is the whole
point of the moat — the writer and the verifier share NO state).
"""
from __future__ import annotations

import copy
import glob
import json
import os

import pytest

import mcp_server
import mcp_tools_signed_snapshot


def _install_stamp_noop(monkeypatch):
    monkeypatch.setattr(mcp_server, "stamp_tool_call", lambda name: None)


def _latest_snapshot_date() -> str | None:
    """Return the most recent YYYY-MM-DD date with a live signed snapshot
    on disk, or None if none exist (in which case round-trip tests skip)."""
    files = sorted(glob.glob(os.path.join(
        mcp_tools_signed_snapshot.SNAPSHOTS_DIR, "20??-??-??.json"
    )))
    if not files:
        return None
    return os.path.basename(files[-1]).removesuffix(".json")


# ─────────────────────────────────────────────────────────────────────
# get_signed_snapshot — envelope, shape, absence-raises
# ─────────────────────────────────────────────────────────────────────

def test_get_signed_snapshot_envelope_and_shape(monkeypatch):
    date_str = _latest_snapshot_date()
    if date_str is None:
        pytest.skip("no signed snapshots on disk — run signed_snapshot.py first")
    _install_stamp_noop(monkeypatch)

    env = mcp_tools_signed_snapshot.tool_get_signed_snapshot(date_str)

    assert set(env.keys()) == {"data", "proof", "server"}
    assert env["proof"]["source"] == "signed_snapshot_walker"
    assert env["proof"]["freshness_contract"] == "daily"
    assert env["proof"]["methodology_url"].endswith("#signed-snapshot")
    assert env["proof"]["claims_ref"] == "signed_snapshot_daily"

    d = env["data"]
    required = {
        "snapshot_date_utc", "signing_domain", "schema_version",
        "metrics", "leaf_hash", "leaf_index", "leaves_total",
        "chain_root", "audit_path", "signature_ed25519",
        "signing_pubkey_fingerprint",
    }
    assert required <= set(d.keys())
    assert d["snapshot_date_utc"] == date_str

    # schema_version 3+ artifacts self-describe their verification path:
    # a cold verifier landing on this JSON alone reaches both the key
    # and the spec in one hop. Older artifacts stay verifiable but new
    # writes carry these two fields.
    if d.get("schema_version", 0) >= 3:
        assert d["pubkey_url"] == (
            "https://xrpldashboard.com/.well-known/snapshots/pubkey.pem"
        )
        assert d["verifier_spec_url"] == (
            "https://xrpldashboard.com/methodology#signed-snapshots"
        )


def test_get_signed_snapshot_bad_iso_date_raises(monkeypatch):
    _install_stamp_noop(monkeypatch)
    for bad in ("", "not-a-date", "2026-8-2", "20260802", "2026/08/02"):
        with pytest.raises(RuntimeError, match="ISO YYYY-MM-DD"):
            mcp_tools_signed_snapshot.tool_get_signed_snapshot(bad)


def test_get_signed_snapshot_missing_file_raises(monkeypatch):
    _install_stamp_noop(monkeypatch)
    # A date far outside the chain range — the walker will never have
    # produced this. Absence IS the signal, not a stub envelope.
    with pytest.raises(RuntimeError, match="no signed snapshot on disk"):
        mcp_tools_signed_snapshot.tool_get_signed_snapshot("1970-01-01")


# ─────────────────────────────────────────────────────────────────────
# verify_snapshot_signature — envelope, both input shapes, round-trip
# ─────────────────────────────────────────────────────────────────────

def test_verify_snapshot_signature_none_raises(monkeypatch):
    _install_stamp_noop(monkeypatch)
    with pytest.raises(RuntimeError, match="envelope is None"):
        mcp_tools_signed_snapshot.tool_verify_snapshot_signature(None)


def test_verify_snapshot_signature_wrong_type_raises(monkeypatch):
    _install_stamp_noop(monkeypatch)
    for bad in ("string", 42, [1, 2, 3]):
        with pytest.raises(RuntimeError, match="envelope must be a dict"):
            mcp_tools_signed_snapshot.tool_verify_snapshot_signature(bad)


def test_verify_snapshot_signature_missing_fields_raises(monkeypatch):
    _install_stamp_noop(monkeypatch)
    with pytest.raises(RuntimeError, match="missing required fields"):
        mcp_tools_signed_snapshot.tool_verify_snapshot_signature({"snapshot_date_utc": "2026-08-02"})


def test_verify_snapshot_signature_round_trip_full_envelope(monkeypatch):
    """The moat, exercised: fetch via #18, hand the FULL envelope back
    to #19, get verify_result=True. This is the ship-gate demo call."""
    date_str = _latest_snapshot_date()
    if date_str is None:
        pytest.skip("no signed snapshots on disk — run signed_snapshot.py first")
    _install_stamp_noop(monkeypatch)

    fetched = mcp_tools_signed_snapshot.tool_get_signed_snapshot(date_str)
    verified = mcp_tools_signed_snapshot.tool_verify_snapshot_signature(fetched)

    assert set(verified.keys()) == {"data", "proof", "server"}
    assert verified["proof"]["source"] == "signed_snapshot.verify_envelope+pinned_pubkey"
    assert verified["proof"]["freshness_contract"] == "≤ 5min"
    assert verified["proof"]["methodology_url"].endswith("#signed-snapshot")
    assert verified["proof"]["claims_ref"] == "signed_snapshot_verify"

    assert verified["data"]["verify_result"] is True, verified["data"]["issues"]
    assert verified["data"]["issues"] == []
    assert verified["data"]["snapshot_date_utc"] == date_str
    # Fingerprint is the 8-byte SHA-256 prefix rendered as colon-hex.
    assert verified["data"]["public_key_fingerprint"].count(":") == 7


def test_verify_snapshot_signature_round_trip_bare_payload(monkeypatch):
    """Same round-trip, but handing over the bare signed payload dict
    (as would happen if an agent stripped the MCP wrapper somewhere)."""
    date_str = _latest_snapshot_date()
    if date_str is None:
        pytest.skip("no signed snapshots on disk — run signed_snapshot.py first")
    _install_stamp_noop(monkeypatch)

    fetched = mcp_tools_signed_snapshot.tool_get_signed_snapshot(date_str)
    bare = fetched["data"]
    verified = mcp_tools_signed_snapshot.tool_verify_snapshot_signature(bare)

    assert verified["data"]["verify_result"] is True, verified["data"]["issues"]
    assert verified["data"]["issues"] == []


def test_verify_snapshot_signature_tamper_metrics_fails(monkeypatch):
    """Flip one value in the metrics array — leaf_hash must no longer
    derive to the claimed value, and verify_result MUST be False."""
    date_str = _latest_snapshot_date()
    if date_str is None:
        pytest.skip("no signed snapshots on disk — run signed_snapshot.py first")
    _install_stamp_noop(monkeypatch)

    fetched = mcp_tools_signed_snapshot.tool_get_signed_snapshot(date_str)
    bare = copy.deepcopy(fetched["data"])
    # Tamper: flip a byte in the first metric's value.
    if not bare["metrics"]:
        pytest.skip("live snapshot has no metrics — tamper test not exercisable")
    m = bare["metrics"][0]
    orig = m.get("value")
    if isinstance(orig, (int, float)):
        m["value"] = (orig or 0) + 1
    elif isinstance(orig, str):
        m["value"] = orig + "!"
    else:
        m["value"] = "tampered"

    verified = mcp_tools_signed_snapshot.tool_verify_snapshot_signature(bare)
    assert verified["data"]["verify_result"] is False
    assert any("leaf_hash mismatch" in i for i in verified["data"]["issues"])


def test_verify_snapshot_signature_tamper_signature_fails(monkeypatch):
    """Flip one byte in the Ed25519 signature — signature check must
    fail, leaf_hash + audit path still pass, so issues names ONLY the
    signature."""
    date_str = _latest_snapshot_date()
    if date_str is None:
        pytest.skip("no signed snapshots on disk — run signed_snapshot.py first")
    _install_stamp_noop(monkeypatch)

    fetched = mcp_tools_signed_snapshot.tool_get_signed_snapshot(date_str)
    bare = copy.deepcopy(fetched["data"])
    sig = bare["signature_ed25519"]
    # Flip the low nibble of the last hex byte — still valid hex, but a
    # different signature that will not verify.
    tampered_last = f"{(int(sig[-1], 16) ^ 0x1):x}"
    bare["signature_ed25519"] = sig[:-1] + tampered_last

    verified = mcp_tools_signed_snapshot.tool_verify_snapshot_signature(bare)
    assert verified["data"]["verify_result"] is False
    assert any("signature did NOT verify" in i for i in verified["data"]["issues"])
    # Leaf hash + audit path + fingerprint should all still be fine.
    assert not any("leaf_hash mismatch" in i for i in verified["data"]["issues"])
    assert not any("audit_path does NOT reconstruct" in i for i in verified["data"]["issues"])
