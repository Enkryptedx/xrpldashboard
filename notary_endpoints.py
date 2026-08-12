"""XRPL Notary — Flask Blueprint for the /notary/* endpoints.

Proves a file existed unchanged at a point in time. Does NOT verify
identity or replace a notary public.

DARK BUILD — flagged off by default. All endpoints return HTTP 503
when NOTARY_ENABLED != "1". Registration in app.py is also gated on
the same flag, so this module can be imported freely without changing
the production surface.

Usage in app.py (add when NOTARY_ENABLED ships):

    from notary_endpoints import notary_bp, is_notary_enabled
    if is_notary_enabled():
        app.register_blueprint(notary_bp)

Endpoints (all dark until NOTARY_ENABLED=1):

    POST /notary/anchor       — anchor a SHA-256 digest for a publisher
    GET  /notary/receipt/<id> — fetch a receipt by id
    POST /notary/verify       — verify a receipt server-side (convenience)
    GET  /notary/chain/<pid>  — per-publisher receipt chain
    GET  /notary/spec         — redirect to the spec document

On flag-off, every endpoint returns:
    HTTP 503
    Cache-Control: no-store
    Retry-After: 604800
    Body: { "error": "notary_disabled", "reason": ..., "spec_url": ...,
            "disclaimer": ... }

The disclaimer ships in every response body regardless of flag state,
per docs/XRPL_NOTARY_SPEC.md §Day-one disclaimer.
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Optional

from flask import Blueprint, jsonify, make_response, redirect, request, url_for

from xrpl_notary_verify import DISCLAIMER

SITE_URL = os.environ.get("SITE_URL", "https://xrpldashboard.com").rstrip("/")

notary_bp = Blueprint("notary", __name__, url_prefix="/notary")


def is_notary_enabled() -> bool:
    """Return True iff NOTARY_ENABLED=1 is set in the environment.
    This is the only place the flag is read; callers use this function
    rather than os.environ directly so tests can monkeypatch one call site."""
    return os.environ.get("NOTARY_ENABLED", "0").strip() == "1"


# ── Shared 503 response ───────────────────────────────────────────────────────

def _disabled_response() -> Any:
    """Return the standard 503 body for all disabled endpoints.
    Retry-After: 604800 (1 week) is a soft signal, not a promise."""
    resp = make_response(
        jsonify({
            "error": "notary_disabled",
            "reason": (
                "public flag pending Indiana notary-statute counsel review; "
                "internal build only"
            ),
            "spec_url": f"{SITE_URL}/notary/spec",
            "disclaimer": DISCLAIMER,
        }),
        503,
    )
    resp.headers["Retry-After"] = "604800"
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ── POST /notary/anchor ───────────────────────────────────────────────────────

@notary_bp.route("/anchor", methods=["POST"])
def anchor():
    """Anchor a SHA-256 digest for a publisher.

    Request JSON:
        publisher_id  — DID or pubkey fingerprint identifying the publisher
        sha256_digest — hex SHA-256 of the file to anchor
        utc_date      — YYYY-MM-DD in UTC (optional; defaults to today)

    Response 200 (enabled):
        receipt, receipt_url, chain_url, disclaimer, anchor_pending_until

    Response 503 (disabled):
        error, reason, spec_url, disclaimer
    """
    if not is_notary_enabled():
        return _disabled_response()

    body = request.get_json(silent=True) or {}
    publisher_id = (body.get("publisher_id") or "").strip()
    sha256_digest = (body.get("sha256_digest") or "").strip().lower()
    utc_date = (body.get("utc_date") or "").strip()

    missing = [f for f, v in [
        ("publisher_id", publisher_id),
        ("sha256_digest", sha256_digest),
    ] if not v]
    if missing:
        return make_response(
            jsonify({"error": "missing_fields", "fields": missing, "disclaimer": DISCLAIMER}),
            400,
        )

    if len(sha256_digest) != 64 or not all(c in "0123456789abcdef" for c in sha256_digest):
        return make_response(
            jsonify({"error": "invalid_sha256_digest", "disclaimer": DISCLAIMER}),
            400,
        )

    from datetime import date, timezone
    if not utc_date:
        utc_date = date.today().isoformat()

    # Receipt id: date + first 8 hex chars of SHA-256(publisher_id|digest)
    id_src = hashlib.sha256(f"{publisher_id}|{sha256_digest}".encode()).hexdigest()[:8]
    receipt_id = f"{utc_date}-{id_src}"

    receipt: dict[str, Any] = {
        "protocol": "xrpldashboard/notary/v1",
        "receipt_id": receipt_id,
        "utc_date": utc_date,
        "utc_timestamp": None,  # filled at signing time
        "publisher_id": publisher_id,
        "sha256_digest": sha256_digest,
        "signature_algorithm": "ed25519",
        "signature": None,       # filled at signing time
        "signing_key_fingerprint": None,  # filled at signing time
        "disclaimer": DISCLAIMER,
        "onledger_anchor_tx": "pending",
        "onledger_anchor_ledger": None,
    }

    # TODO: sign receipt with Ed25519 signing key (SIGNING_KEY_PASSPHRASE)
    # and persist to storage when full signing pipeline ships.
    # For now, return the receipt shape with unsigned placeholders so
    # callers can verify the response contract.

    receipt_url = f"{SITE_URL}/notary/receipt/{receipt_id}"
    pid_encoded = publisher_id.replace(":", "%3A").replace("/", "%2F")
    chain_url = f"{SITE_URL}/notary/chain/{pid_encoded}.json"

    return jsonify({
        "receipt": receipt,
        "receipt_url": receipt_url,
        "chain_url": chain_url,
        "disclaimer": DISCLAIMER,
        "anchor_pending_until": None,  # filled when anchor cadence known
        "_note": "signing pipeline not yet wired; receipt fields are placeholders",
    })


# ── GET /notary/receipt/<id> ──────────────────────────────────────────────────

@notary_bp.route("/receipt/<receipt_id>")
def get_receipt(receipt_id: str):
    """Fetch a receipt by id. Returns the receipt JSON from §3 of the spec.

    Currently returns 501 (enabled, signing pipeline not yet wired) or
    503 (disabled)."""
    if not is_notary_enabled():
        return _disabled_response()

    # TODO: retrieve from persistent storage once signing pipeline ships.
    return make_response(
        jsonify({
            "error": "not_implemented",
            "detail": "receipt storage not yet wired",
            "receipt_id": receipt_id,
            "disclaimer": DISCLAIMER,
        }),
        501,
    )


# ── POST /notary/verify ───────────────────────────────────────────────────────

@notary_bp.route("/verify", methods=["POST"])
def verify():
    """Server-side receipt verification convenience endpoint.

    Delegates to xrpl_notary_verify.verify_receipt. The MIT verify lib
    is authoritative; this endpoint is a courtesy for callers who can't
    or don't want to run the lib themselves.

    Currently returns 501 (enabled, pubkey loading not yet wired) or
    503 (disabled)."""
    if not is_notary_enabled():
        return _disabled_response()

    body = request.get_json(silent=True) or {}
    receipt = body.get("receipt")
    if not receipt:
        return make_response(
            jsonify({"error": "missing_field_receipt", "disclaimer": DISCLAIMER}),
            400,
        )

    # TODO: load pubkey_pem from SNAPSHOT_PUBKEY_PEM_PATH and call
    # xrpl_notary_verify.verify_receipt(receipt, pubkey_pem).
    return make_response(
        jsonify({
            "error": "not_implemented",
            "detail": "pubkey loading not yet wired; use the MIT verify lib directly",
            "disclaimer": DISCLAIMER,
        }),
        501,
    )


# ── GET /notary/chain/<publisher_id> ─────────────────────────────────────────

@notary_bp.route("/chain/<path:publisher_id>")
def get_chain(publisher_id: str):
    """Per-publisher receipt chain (analogous to chain.json for signed snapshots).

    Currently returns 501 (enabled) or 503 (disabled)."""
    if not is_notary_enabled():
        return _disabled_response()

    return make_response(
        jsonify({
            "error": "not_implemented",
            "detail": "chain storage not yet wired",
            "publisher_id": publisher_id,
            "disclaimer": DISCLAIMER,
        }),
        501,
    )


# ── GET /notary/spec ──────────────────────────────────────────────────────────

@notary_bp.route("/spec")
def spec():
    """Redirect to the spec document. Serves even when flag is off —
    the spec IS the public commitment regardless of service state."""
    # When the spec is rendered as an HTML page this can redirect to
    # /methodology#notary or a dedicated /notary/about page. For now,
    # return the disclaimer and a pointer to the raw spec in the repo.
    return jsonify({
        "spec": "docs/XRPL_NOTARY_SPEC.md",
        "status": "dark_build",
        "enabled": is_notary_enabled(),
        "disclaimer": DISCLAIMER,
    })
