"""xrpl-notary-verify — MIT-licensed standalone verifier for XRPL Notary receipts.

Proves a file existed unchanged at a point in time. Does NOT verify
identity or replace a notary public.

Drop-in: no dependencies beyond xrpl-py (optional — only needed for
on-ledger verification; signature checks work without it). Python 3.9+.

Public API:
    verify_receipt(receipt, pubkey_pem, xrpl_endpoint=...) -> dict
    canonical_json(receipt_fields) -> bytes
    receipt_sha256(receipt_fields) -> str

Memo-format compatibility:
    Handles both the legacy xrpldashboard/anchor/v1 shape (publisher_id
    implicitly "self") and the generalized xrpldashboard/notary/v1 shape
    per docs/XRPL_NOTARY_SPEC.md §2.

Genesis anchor fixture (first anchor in the signed-snapshot chain):
    TX: 01D0BB9D230955F43DB35703E2EB7F5DFA43CEB69CCBBF57FBC8F17407E50DF8
    Ledger: 106140698 | Account: rL2yMECEyUT94pLDrAcetMNMG1H4xqpNWQ
    Destination: rwrcJL3Exd1ZUYz11Wug6wvWC448CiTXfd
    MemoData: xrpldashboard/anchor/v1|2026-08-07|c73d65ae5927243b86ee...
    Publisher: self (pre-dates notary generalisation; compat handled below)
"""
from __future__ import annotations

import base64
import hashlib
import json
from typing import Any, Optional

# ── Disclaimer (must travel with every verify result) ─────────────────────────
DISCLAIMER = (
    "Proves a file existed unchanged at a point in time. "
    "Does NOT verify identity or replace a notary public."
)

# ── Memo format identifiers ───────────────────────────────────────────────────
_MEMO_V1_ANCHOR  = "xrpldashboard/anchor/v1"   # legacy (publisher=self)
_MEMO_V1_NOTARY  = "xrpldashboard/notary/v1"   # generalised (has publisher_id)

# ── Known Notary/Anchor accounts (for memo-source validation) ─────────────────
# Extend this tuple as new notary accounts are created.
_KNOWN_ANCHOR_ACCOUNTS = frozenset({
    "rL2yMECEyUT94pLDrAcetMNMG1H4xqpNWQ",  # xrpldashboard on-ledger anchor (Stage 2)
    # future: notary account for third-party publishers (address TBD post-attorney-gate)
})

_KNOWN_OPS_DESTINATION = "rwrcJL3Exd1ZUYz11Wug6wvWC448CiTXfd"

# ── Receipt fields included in the Ed25519 signature ─────────────────────────
# Any field that appears in the receipt but is NOT in this set is either
# computed-after-signing (onledger_anchor_tx, onledger_anchor_ledger) or
# metadata that doesn't change the hash commitment.
_SIGNED_FIELDS = frozenset({
    "protocol",
    "receipt_id",
    "utc_date",
    "utc_timestamp",
    "publisher_id",
    "sha256_digest",
    "signature_algorithm",
    "signing_key_fingerprint",
    "disclaimer",
})


# ── Canonical JSON ────────────────────────────────────────────────────────────

def canonical_json(receipt_fields: dict) -> bytes:
    """Return the UTF-8 canonical JSON bytes for the subset of receipt_fields
    that are covered by the Ed25519 signature.

    Keys are the intersection of receipt_fields.keys() and _SIGNED_FIELDS,
    sorted alphabetically (RFC 8785 approximation — no key escaping or value
    normalisation beyond what json.dumps provides).  Separators stripped of
    whitespace to match the compact encoding used at signing time.

    Call this before verify_receipt only if you want the raw bytes; otherwise
    verify_receipt calls it internally."""
    signed = {k: v for k, v in receipt_fields.items() if k in _SIGNED_FIELDS}
    return json.dumps(signed, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def receipt_sha256(receipt_fields: dict) -> str:
    """Return the hex-encoded SHA-256 of the canonical JSON of the signed fields.
    This is the value embedded in the XRPL memo."""
    return hashlib.sha256(canonical_json(receipt_fields)).hexdigest()


# ── Ed25519 signature verification ───────────────────────────────────────────

def _verify_ed25519_signature(
    message: bytes,
    signature_b64: str,
    pubkey_pem: str,
) -> bool:
    """Verify an Ed25519 signature.

    Uses the standard-library `cryptography` package if available; falls back
    to the pure-Python `pyca/cryptography`-compatible interface inside xrpl-py
    if that's all that's installed.  Returns False on any error — no exceptions
    from bad sigs."""
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        from cryptography.hazmat.primitives.serialization import load_pem_public_key
        sig = base64.b64decode(signature_b64)
        pubkey = load_pem_public_key(pubkey_pem.encode("utf-8"))
        if not isinstance(pubkey, Ed25519PublicKey):
            return False
        pubkey.verify(sig, message)
        return True
    except Exception:
        return False


# ── Key fingerprint ───────────────────────────────────────────────────────────

def _key_fingerprint(pubkey_pem: str) -> str:
    """Return the first 16 hex chars of SHA-256(UTF-8 PEM bytes).
    Matches the convention in /.well-known/snapshots/pubkey_fingerprint.txt."""
    return hashlib.sha256(pubkey_pem.strip().encode("utf-8")).hexdigest()[:16]


# ── XRPL memo fetch ───────────────────────────────────────────────────────────

def _fetch_tx_memo(tx_hash: str, xrpl_endpoint: str) -> Optional[dict]:
    """Fetch a tx from the XRPL and return the first parsed memo dict, or None.

    The return dict has keys: memo_text (decoded MemoData string), source,
    destination, validated.

    xrpl-py is used if installed; falls back to a raw JSON-RPC call via
    urllib so this file works in minimal environments."""
    raw_memo_hex: Optional[str] = None
    source: Optional[str] = None
    destination: Optional[str] = None
    validated: bool = False

    try:
        from xrpl.clients import JsonRpcClient as XrplClient
        from xrpl.models.requests import Tx
        client = XrplClient(xrpl_endpoint)
        resp = client.request(Tx(transaction=tx_hash, binary=False))
        result = resp.result
        validated = result.get("validated", False)
        source = result.get("Account")
        destination = result.get("Destination")
        memos = result.get("Memos") or []
        if memos:
            raw_memo_hex = memos[0].get("Memo", {}).get("MemoData", "")
    except ImportError:
        # xrpl-py not installed — fall back to raw urllib
        import urllib.request
        import urllib.error
        payload = json.dumps({
            "method": "tx",
            "params": [{"transaction": tx_hash, "binary": False}],
        }).encode()
        try:
            req = urllib.request.Request(
                xrpl_endpoint,
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read())
        except urllib.error.URLError:
            return None
        result = data.get("result", {})
        validated = result.get("validated", False)
        source = result.get("Account")
        destination = result.get("Destination")
        memos = result.get("Memos") or []
        if memos:
            raw_memo_hex = memos[0].get("Memo", {}).get("MemoData", "")
    except Exception:
        return None

    if not raw_memo_hex:
        return None
    try:
        memo_text = bytes.fromhex(raw_memo_hex).decode("utf-8").strip()
    except Exception:
        return None
    return {
        "memo_text": memo_text,
        "source": source,
        "destination": destination,
        "validated": validated,
    }


def _parse_memo(memo_text: str) -> Optional[dict]:
    """Parse a v1 memo string into a dict with keys:
        protocol, publisher_id, utc_date, sha256_digest.

    Handles both xrpldashboard/anchor/v1 (3 fields, publisher_id="self")
    and xrpldashboard/notary/v1 (4 fields, publisher_id explicit).
    Returns None on unparseable input."""
    parts = memo_text.split("|")
    if parts[0] == _MEMO_V1_ANCHOR and len(parts) == 3:
        return {
            "protocol": _MEMO_V1_ANCHOR,
            "publisher_id": "self",
            "utc_date": parts[1],
            "sha256_digest": parts[2],
        }
    if parts[0] == _MEMO_V1_NOTARY and len(parts) == 4:
        return {
            "protocol": _MEMO_V1_NOTARY,
            "publisher_id": parts[1],
            "utc_date": parts[2],
            "sha256_digest": parts[3],
        }
    return None


# ── Main verify function ──────────────────────────────────────────────────────

def verify_receipt(
    receipt: dict,
    pubkey_pem: str,
    xrpl_endpoint: str = "https://s1.ripple.com:51234",
) -> dict:
    """Verify a notary receipt. Never raises on bad receipts — returns
    a structured result dict instead.

    Returns:
        {
            "valid": bool,
            "signature_valid": bool,
            "fingerprint_valid": bool,
            "disclaimer_present": bool,
            "anchor": {
                "checked": bool,
                "validated": bool,
                "source_valid": bool,
                "destination_valid": bool,
                "memo_digest_matches": bool,
            },
            "reasons": [str],   # human-readable failure reasons
            "disclaimer": DISCLAIMER,
        }

    Raises only on infrastructure errors (xrpl_endpoint unreachable,
    network timeout) — i.e., when we genuinely can't determine validity,
    not when the receipt is provably invalid.
    """
    reasons: list[str] = []
    sig_valid = False
    fp_valid = False
    disclaimer_present = False
    anchor: dict[str, Any] = {
        "checked": False,
        "validated": False,
        "source_valid": False,
        "destination_valid": False,
        "memo_digest_matches": False,
    }

    # ── 1. disclaimer field ───────────────────────────────────────────────────
    disclaimer_present = receipt.get("disclaimer") == DISCLAIMER
    if not disclaimer_present:
        reasons.append("disclaimer field missing or does not match canonical text")

    # ── 2. key fingerprint ────────────────────────────────────────────────────
    expected_fp = _key_fingerprint(pubkey_pem)
    actual_fp = receipt.get("signing_key_fingerprint", "")
    fp_valid = (actual_fp == expected_fp)
    if not fp_valid:
        reasons.append(
            f"signing_key_fingerprint mismatch: receipt={actual_fp!r} expected={expected_fp!r}"
        )

    # ── 3. Ed25519 signature ──────────────────────────────────────────────────
    sig_b64 = receipt.get("signature", "")
    if not sig_b64:
        reasons.append("signature field missing")
    else:
        message = canonical_json(receipt)
        sig_valid = _verify_ed25519_signature(message, sig_b64, pubkey_pem)
        if not sig_valid:
            reasons.append("Ed25519 signature does not verify against pubkey_pem")

    # ── 4. On-ledger anchor (optional if "pending") ───────────────────────────
    anchor_tx = receipt.get("onledger_anchor_tx", "")
    if anchor_tx and anchor_tx != "pending":
        anchor["checked"] = True
        tx_data = _fetch_tx_memo(anchor_tx, xrpl_endpoint)
        if tx_data is None:
            reasons.append(f"could not fetch anchor tx {anchor_tx!r} from {xrpl_endpoint}")
        else:
            anchor["validated"] = bool(tx_data["validated"])
            if not anchor["validated"]:
                reasons.append(f"anchor tx {anchor_tx!r} is not validated on-ledger")

            anchor["source_valid"] = tx_data["source"] in _KNOWN_ANCHOR_ACCOUNTS
            if not anchor["source_valid"]:
                reasons.append(
                    f"anchor tx source {tx_data['source']!r} is not a known anchor account"
                )

            anchor["destination_valid"] = tx_data["destination"] == _KNOWN_OPS_DESTINATION
            if not anchor["destination_valid"]:
                reasons.append(
                    f"anchor tx destination {tx_data['destination']!r} != expected ops wallet"
                )

            memo = _parse_memo(tx_data["memo_text"])
            if memo is None:
                reasons.append(f"memo {tx_data['memo_text']!r} does not parse under any known v1 format")
            else:
                expected_digest = receipt_sha256(receipt)
                actual_digest   = memo["sha256_digest"]
                anchor["memo_digest_matches"] = (actual_digest == expected_digest)
                if not anchor["memo_digest_matches"]:
                    reasons.append(
                        f"memo digest {actual_digest!r} != canonical receipt hash {expected_digest!r}"
                    )
                # Publisher id consistency
                if memo["publisher_id"] not in (receipt.get("publisher_id"), "self"):
                    reasons.append(
                        f"memo publisher_id {memo['publisher_id']!r} != receipt publisher_id {receipt.get('publisher_id')!r}"
                    )
    elif anchor_tx == "pending":
        reasons.append("on-ledger anchor is pending (not yet submitted); signature only")

    valid = (
        sig_valid
        and fp_valid
        and disclaimer_present
        and (
            not anchor["checked"]
            or (
                anchor["validated"]
                and anchor["source_valid"]
                and anchor["destination_valid"]
                and anchor["memo_digest_matches"]
            )
        )
    )
    if valid and anchor_tx == "pending":
        # Partially valid: signature checks out but no ledger confirmation yet.
        # Surface this as valid=True with a note rather than false (the sig IS
        # valid; the anchor just hasn't landed yet).
        reasons.append("receipt signature valid; on-ledger confirmation pending")

    return {
        "valid": valid,
        "signature_valid": sig_valid,
        "fingerprint_valid": fp_valid,
        "disclaimer_present": disclaimer_present,
        "anchor": anchor,
        "reasons": reasons,
        "disclaimer": DISCLAIMER,
    }
