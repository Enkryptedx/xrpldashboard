"""Sig-service client — POSTs canonical hashes to the Mac-side sig-service
over the CF-Access-authenticated tunnel and returns the signature block.

Charlie ruling 2026-09-07 (afternoon): Option A tunnel design, Access-
gated like the RPC tunnel. This client mirrors sovereign_tunnel_client's
CF-Access header pattern (CF_ACCESS_CLIENT_ID + CF_ACCESS_CLIENT_SECRET
env vars) but is scoped narrower — no retry cascade, no public fallback
(there IS no public sig-service to fall back to), just:

  - if SIG_TUNNEL_URL + CF-Access env are set → try the tunnel
  - on any failure → return None + fail-reason
  - caller ships the envelope with sig_ed25519: null + fail-reason set

Failure is DESIGNED to be safe: the /check.json envelope still ships,
just without a signature. Verifiers see the null and know why.
"""
import json
import os
import time
import uuid
from datetime import datetime, timezone

import httpx


SIG_TUNNEL_URL = (os.environ.get("SIG_TUNNEL_URL") or "").strip() or None
_CF_CLIENT_ID = (os.environ.get("CF_ACCESS_CLIENT_ID") or "").strip() or None
_CF_CLIENT_SECRET = (os.environ.get("CF_ACCESS_CLIENT_SECRET") or "").strip() or None
SIG_CONFIGURED = bool(SIG_TUNNEL_URL and _CF_CLIENT_ID and _CF_CLIENT_SECRET)

SIG_REQUEST_TIMEOUT_SECONDS = 4.0
SIG_RETRY_ATTEMPTS = 2   # one retry on transient network hiccup, then bail
SIG_RETRY_BASE_MS = 100


# Sourcing-flag values (analogous to sovereign_tunnel_client's naming)
SIG_STATUS_SIGNED = "signed"
SIG_STATUS_UNREACHABLE = "sig_unreachable"
SIG_STATUS_LOCKED = "sig_locked"
SIG_STATUS_RATE_LIMITED = "sig_rate_limited"
SIG_STATUS_REJECTED = "sig_rejected"      # schema/body error — Render-side bug
SIG_STATUS_NOT_CONFIGURED = "sig_not_configured"


def _iso_utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def sign_receipt(canonical_hash: str, kind: str = "check",
                 response_id: str | None = None,
                 caller_ua: str | None = None) -> tuple[dict | None, str]:
    """Sign the domain-separated (canonical_hash) via the Mac sig-service.

    Args:
        canonical_hash: 64 lowercase hex chars (SHA-256 of the receipt
            envelope's canonical serialization).
        kind: one of 'check' or 'registry'. Anything else = 400 from
            the sig-service; caller should validate before calling.
        response_id: UUID v4 for tracing. Generated if not provided.
        caller_ua: end-user User-Agent for the request that triggered
            this signing. Forwarded to sig-service as X-Original-User-
            Agent; sig-service records it in the audit log's `ua`
            field. Added 2026-09-09: without this, sig-service only
            sees the direct-caller UA (python-httpx/x.y.z) and can't
            distinguish external visitors from JJ/canary traffic.

    Returns:
        (sig_block, status) where sig_block is the sig-service response
        on success and None on failure. status is one of the
        SIG_STATUS_* constants.

    Fail-open: this function NEVER raises. Callers can safely call in a
    hot path and always render the envelope, even if signing failed.
    """
    if not SIG_CONFIGURED:
        return None, SIG_STATUS_NOT_CONFIGURED

    if response_id is None:
        response_id = str(uuid.uuid4())

    body = {
        "canonical_hash": canonical_hash,
        "response_id": response_id,
        "kind": kind,
        "issued_at_utc": _iso_utc_now(),
    }
    headers = {
        "Content-Type": "application/json",
        "CF-Access-Client-Id": _CF_CLIENT_ID,
        "CF-Access-Client-Secret": _CF_CLIENT_SECRET,
    }
    if caller_ua:
        # Truncate to 512 chars to match sig-service's record() cap and
        # avoid header-size limits on the tunnel path.
        headers["X-Original-User-Agent"] = caller_ua[:512]
    url = SIG_TUNNEL_URL.rstrip("/") + "/sign"

    last_error = SIG_STATUS_UNREACHABLE
    for attempt in range(1, SIG_RETRY_ATTEMPTS + 1):
        try:
            resp = httpx.post(
                url, json=body, headers=headers,
                timeout=SIG_REQUEST_TIMEOUT_SECONDS,
            )
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError):
            last_error = SIG_STATUS_UNREACHABLE
        else:
            if resp.status_code == 200:
                try:
                    return resp.json(), SIG_STATUS_SIGNED
                except (json.JSONDecodeError, ValueError):
                    last_error = SIG_STATUS_UNREACHABLE
            elif resp.status_code == 503:
                # Service locked — no retry helps; return immediately.
                return None, SIG_STATUS_LOCKED
            elif resp.status_code == 429:
                # Rate limited — one retry after Retry-After doesn't help
                # in a tight loop; return immediately so caller can decide.
                return None, SIG_STATUS_RATE_LIMITED
            elif resp.status_code == 400:
                # Schema error — Render-side bug; retry won't help.
                return None, SIG_STATUS_REJECTED
            else:
                last_error = SIG_STATUS_UNREACHABLE

        if attempt < SIG_RETRY_ATTEMPTS:
            time.sleep(SIG_RETRY_BASE_MS / 1000.0 * attempt)

    return None, last_error


def attach_to_envelope(envelope: dict, canonical_hash: str,
                      kind: str = "check") -> dict:
    """Convenience wrapper — sign a canonical_hash and attach the result
    to an envelope in-place. Sets envelope['sig_ed25519'] on success
    (or None on failure) plus envelope['sig_status'] and
    envelope['sig_unreachable_at_utc'] when applicable.

    Never raises. Returns the modified envelope for chained calls."""
    sig_block, status = sign_receipt(canonical_hash, kind=kind)
    if sig_block:
        envelope["sig_ed25519"] = sig_block["signature_ed25519_hex"]
        envelope["signing_key_fingerprint"] = sig_block["signing_key_fingerprint"]
        envelope["domain_separator"] = sig_block["domain_separator"]
        envelope["signed_at_utc"] = sig_block["signed_at_utc"]
        envelope["sig_status"] = SIG_STATUS_SIGNED
    else:
        envelope["sig_ed25519"] = None
        envelope["sig_status"] = status
        envelope["sig_unreachable_at_utc"] = _iso_utc_now()
    return envelope
