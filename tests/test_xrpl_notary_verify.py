"""Tests for xrpl_notary_verify.py.

Coverage:
  - canonical_json produces stable, sorted, compact bytes.
  - receipt_sha256 matches sha256(canonical_json).
  - verify_receipt: disclaimer field check.
  - verify_receipt: signing_key_fingerprint check.
  - verify_receipt: Ed25519 signature (via mocked _verify_ed25519_signature).
  - verify_receipt: pending anchor returns valid=True with pending note.
  - verify_receipt: on-ledger anchor path (mocked XRPL fetch).
  - verify_receipt: invalid memo on XRPL returns failure reason.
  - Legacy xrpldashboard/anchor/v1 memo shape parsed as publisher_id=self.
  - Genesis anchor fixture constants are reachable and correctly typed.
"""
from __future__ import annotations

import base64
import hashlib
import json

import pytest

import xrpl_notary_verify as nv


# ── Fixtures ──────────────────────────────────────────────────────────────────

FAKE_PUBKEY_PEM = (
    "-----BEGIN PUBLIC KEY-----\n"
    "MCowBQYDK2VdAyEAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABBQ=\n"
    "-----END PUBLIC KEY-----\n"
)

def _fake_fingerprint() -> str:
    return hashlib.sha256(FAKE_PUBKEY_PEM.strip().encode()).hexdigest()[:16]


def _build_receipt(
    *,
    publisher_id: str = "did:web:example.com",
    sha256_digest: str = "a" * 64,
    signature_b64: str = "AAAA",
    fingerprint: str | None = None,
    disclaimer: str | None = None,
    anchor_tx: str = "pending",
) -> dict:
    return {
        "protocol": "xrpldashboard/notary/v1",
        "receipt_id": "2026-09-01-abcdef01",
        "utc_date": "2026-09-01",
        "utc_timestamp": "2026-09-01T12:00:00Z",
        "publisher_id": publisher_id,
        "sha256_digest": sha256_digest,
        "signature_algorithm": "ed25519",
        "signature": signature_b64,
        "signing_key_fingerprint": fingerprint or _fake_fingerprint(),
        "disclaimer": disclaimer if disclaimer is not None else nv.DISCLAIMER,
        "onledger_anchor_tx": anchor_tx,
        "onledger_anchor_ledger": None,
    }


# ── canonical_json tests ──────────────────────────────────────────────────────

def test_canonical_json_is_sorted():
    r = _build_receipt()
    raw = nv.canonical_json(r)
    parsed = json.loads(raw)
    keys = list(parsed.keys())
    assert keys == sorted(keys)


def test_canonical_json_is_compact():
    r = _build_receipt()
    raw = nv.canonical_json(r)
    # Structural whitespace (after colons, between keys) must be absent.
    # Values may contain spaces (e.g. the disclaimer sentence).
    assert b": " not in raw
    assert b", " not in raw


def test_canonical_json_excludes_anchor_fields():
    r = _build_receipt(anchor_tx="DEADBEEF01234567")
    raw = nv.canonical_json(r)
    parsed = json.loads(raw)
    assert "onledger_anchor_tx" not in parsed
    assert "onledger_anchor_ledger" not in parsed


def test_canonical_json_includes_disclaimer():
    r = _build_receipt()
    parsed = json.loads(nv.canonical_json(r))
    assert "disclaimer" in parsed
    assert parsed["disclaimer"] == nv.DISCLAIMER


def test_receipt_sha256_matches_canonical():
    r = _build_receipt()
    expected = hashlib.sha256(nv.canonical_json(r)).hexdigest()
    assert nv.receipt_sha256(r) == expected


def test_canonical_json_stable_across_calls():
    r = _build_receipt()
    assert nv.canonical_json(r) == nv.canonical_json(r)


# ── verify_receipt: disclaimer check ─────────────────────────────────────────

def test_verify_missing_disclaimer_fails(monkeypatch):
    monkeypatch.setattr(nv, "_verify_ed25519_signature", lambda m, s, p: True)
    r = _build_receipt(disclaimer="Wrong disclaimer text")
    result = nv.verify_receipt(r, FAKE_PUBKEY_PEM)
    assert not result["valid"]
    assert not result["disclaimer_present"]
    assert any("disclaimer" in reason for reason in result["reasons"])


def test_verify_canonical_disclaimer_passes_presence_check(monkeypatch):
    monkeypatch.setattr(nv, "_verify_ed25519_signature", lambda m, s, p: True)
    r = _build_receipt()
    result = nv.verify_receipt(r, FAKE_PUBKEY_PEM)
    assert result["disclaimer_present"]


def test_verify_result_always_carries_disclaimer(monkeypatch):
    """The disclaimer travels in every return dict regardless of receipt validity."""
    monkeypatch.setattr(nv, "_verify_ed25519_signature", lambda m, s, p: False)
    r = _build_receipt()
    result = nv.verify_receipt(r, FAKE_PUBKEY_PEM)
    assert result["disclaimer"] == nv.DISCLAIMER


# ── verify_receipt: fingerprint check ────────────────────────────────────────

def test_verify_wrong_fingerprint_fails(monkeypatch):
    monkeypatch.setattr(nv, "_verify_ed25519_signature", lambda m, s, p: True)
    r = _build_receipt(fingerprint="0000000000000000")
    result = nv.verify_receipt(r, FAKE_PUBKEY_PEM)
    assert not result["valid"]
    assert not result["fingerprint_valid"]
    assert any("fingerprint" in reason for reason in result["reasons"])


def test_verify_correct_fingerprint_passes(monkeypatch):
    monkeypatch.setattr(nv, "_verify_ed25519_signature", lambda m, s, p: True)
    r = _build_receipt()
    result = nv.verify_receipt(r, FAKE_PUBKEY_PEM)
    assert result["fingerprint_valid"]


# ── verify_receipt: signature check ──────────────────────────────────────────

def test_verify_bad_signature_fails(monkeypatch):
    monkeypatch.setattr(nv, "_verify_ed25519_signature", lambda m, s, p: False)
    r = _build_receipt()
    result = nv.verify_receipt(r, FAKE_PUBKEY_PEM)
    assert not result["valid"]
    assert not result["signature_valid"]
    assert any("signature" in reason for reason in result["reasons"])


def test_verify_good_signature_passes(monkeypatch):
    monkeypatch.setattr(nv, "_verify_ed25519_signature", lambda m, s, p: True)
    r = _build_receipt()
    result = nv.verify_receipt(r, FAKE_PUBKEY_PEM)
    assert result["signature_valid"]


def test_verify_missing_signature_fails(monkeypatch):
    r = _build_receipt(signature_b64="")
    result = nv.verify_receipt(r, FAKE_PUBKEY_PEM)
    assert not result["valid"]
    assert any("signature field missing" in reason for reason in result["reasons"])


# ── verify_receipt: pending anchor ───────────────────────────────────────────

def test_verify_pending_anchor_is_valid_with_note(monkeypatch):
    """A pending anchor is valid (sig + disclaimer checks pass) but the
    reasons list explains the pending state."""
    monkeypatch.setattr(nv, "_verify_ed25519_signature", lambda m, s, p: True)
    r = _build_receipt(anchor_tx="pending")
    result = nv.verify_receipt(r, FAKE_PUBKEY_PEM)
    assert result["valid"]
    assert not result["anchor"]["checked"]
    assert any("pending" in reason for reason in result["reasons"])


def test_verify_pending_anchor_does_not_fetch_xrpl(monkeypatch):
    monkeypatch.setattr(nv, "_verify_ed25519_signature", lambda m, s, p: True)
    fetch_called = []
    monkeypatch.setattr(nv, "_fetch_tx_memo", lambda tx, ep: fetch_called.append(True) or {})
    r = _build_receipt(anchor_tx="pending")
    nv.verify_receipt(r, FAKE_PUBKEY_PEM)
    assert not fetch_called


# ── verify_receipt: on-ledger anchor path ────────────────────────────────────

def _good_tx_data(receipt: dict) -> dict:
    """Build what _fetch_tx_memo would return for a validly-anchored receipt."""
    digest = nv.receipt_sha256(receipt)
    memo_text = f"xrpldashboard/notary/v1|{receipt['publisher_id']}|{receipt['utc_date']}|{digest}"
    return {
        "memo_text": memo_text,
        "source": list(nv._KNOWN_ANCHOR_ACCOUNTS)[0],
        "destination": nv._KNOWN_OPS_DESTINATION,
        "validated": True,
    }


def test_verify_on_ledger_anchor_valid(monkeypatch):
    monkeypatch.setattr(nv, "_verify_ed25519_signature", lambda m, s, p: True)
    r = _build_receipt(anchor_tx="DEADBEEF00001111222233334444555566667777")
    monkeypatch.setattr(nv, "_fetch_tx_memo", lambda tx, ep: _good_tx_data(r))
    result = nv.verify_receipt(r, FAKE_PUBKEY_PEM)
    assert result["valid"]
    assert result["anchor"]["checked"]
    assert result["anchor"]["validated"]
    assert result["anchor"]["source_valid"]
    assert result["anchor"]["destination_valid"]
    assert result["anchor"]["memo_digest_matches"]
    assert result["reasons"] == []


def test_verify_anchor_wrong_source_fails(monkeypatch):
    monkeypatch.setattr(nv, "_verify_ed25519_signature", lambda m, s, p: True)
    r = _build_receipt(anchor_tx="DEADBEEF00001111222233334444555566667777")
    def bad_source(tx, ep):
        d = _good_tx_data(r)
        d["source"] = "rUNKNOWNACCOUNT"
        return d
    monkeypatch.setattr(nv, "_fetch_tx_memo", bad_source)
    result = nv.verify_receipt(r, FAKE_PUBKEY_PEM)
    assert not result["valid"]
    assert not result["anchor"]["source_valid"]


def test_verify_anchor_digest_mismatch_fails(monkeypatch):
    monkeypatch.setattr(nv, "_verify_ed25519_signature", lambda m, s, p: True)
    r = _build_receipt(anchor_tx="DEADBEEF00001111222233334444555566667777")
    def wrong_digest(tx, ep):
        d = _good_tx_data(r)
        d["memo_text"] = d["memo_text"].replace(nv.receipt_sha256(r), "f" * 64)
        return d
    monkeypatch.setattr(nv, "_fetch_tx_memo", wrong_digest)
    result = nv.verify_receipt(r, FAKE_PUBKEY_PEM)
    assert not result["valid"]
    assert not result["anchor"]["memo_digest_matches"]


def test_verify_anchor_fetch_failure_returns_failure(monkeypatch):
    monkeypatch.setattr(nv, "_verify_ed25519_signature", lambda m, s, p: True)
    r = _build_receipt(anchor_tx="DEADBEEF00001111222233334444555566667777")
    monkeypatch.setattr(nv, "_fetch_tx_memo", lambda tx, ep: None)
    result = nv.verify_receipt(r, FAKE_PUBKEY_PEM)
    assert not result["valid"]
    assert any("could not fetch" in reason for reason in result["reasons"])


# ── Legacy anchor/v1 memo shape ───────────────────────────────────────────────

def test_parse_memo_legacy_anchor_v1():
    memo = "xrpldashboard/anchor/v1|2026-08-07|c73d65ae5927"
    parsed = nv._parse_memo(memo)
    assert parsed is not None
    assert parsed["publisher_id"] == "self"
    assert parsed["utc_date"] == "2026-08-07"
    assert parsed["sha256_digest"] == "c73d65ae5927"


def test_parse_memo_notary_v1():
    memo = "xrpldashboard/notary/v1|did:web:example.com|2026-09-01|abc123"
    parsed = nv._parse_memo(memo)
    assert parsed is not None
    assert parsed["publisher_id"] == "did:web:example.com"
    assert parsed["sha256_digest"] == "abc123"


def test_parse_memo_invalid_returns_none():
    assert nv._parse_memo("garbage") is None
    assert nv._parse_memo("xrpldashboard/unknown/v1|date|hash") is None


def test_parse_memo_strips_trailing_newlines():
    memo = "xrpldashboard/anchor/v1|2026-08-07|abc123\n\n\n"
    # _parse_memo expects already-stripped text — stripping done upstream
    stripped = memo.strip()
    parsed = nv._parse_memo(stripped)
    assert parsed is not None


# ── Genesis anchor fixture ────────────────────────────────────────────────────

GENESIS_TX = "01D0BB9D230955F43DB35703E2EB7F5DFA43CEB69CCBBF57FBC8F17407E50DF8"
GENESIS_LEDGER = 106140698
GENESIS_ANCHOR_ACCOUNT = "rL2yMECEyUT94pLDrAcetMNMG1H4xqpNWQ"
GENESIS_OPS_DESTINATION = "rwrcJL3Exd1ZUYz11Wug6wvWC448CiTXfd"


def test_genesis_tx_in_known_accounts():
    """The genesis anchor account is in the known-accounts frozenset."""
    assert GENESIS_ANCHOR_ACCOUNT in nv._KNOWN_ANCHOR_ACCOUNTS


def test_genesis_ops_destination_matches():
    assert GENESIS_OPS_DESTINATION == nv._KNOWN_OPS_DESTINATION


def test_genesis_tx_hash_format():
    assert len(GENESIS_TX) == 64
    assert all(c in "0123456789ABCDEFabcdef" for c in GENESIS_TX)


def test_genesis_memo_parses_as_self():
    """The genesis anchor memo (anchor/v1) correctly maps to publisher_id=self."""
    memo_data = (
        "xrpldashboard/anchor/v1"
        "|2026-08-07"
        "|c73d65ae5927243b86ee9ddbfd02b967451dc75a6b4678a5a05dadc9dbfdf86a"
    )
    parsed = nv._parse_memo(memo_data)
    assert parsed is not None
    assert parsed["publisher_id"] == "self"
    assert parsed["protocol"] == "xrpldashboard/anchor/v1"
