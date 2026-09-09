"""Receipt-signing service — Mac-side Ed25519 signer over the CF-Access tunnel.

Charlie ruling 2026-09-07 (afternoon): Option A from the design note
(triage/SIGNED_RECEIPT_DESIGN_NOTE_2026-09-06.md). Small Flask service
on the Mac that accepts a canonical-hash + minimal metadata, returns
an Ed25519 signature made with the receipt/registry keypair
(fingerprint A4:0F:B1:0A:9D:33:64:03). Render calls it over the
CF-Access-authenticated tunnel; +1 tunnel round-trip per /check.json
response, ~30-150ms typical.

CONSTRAINTS
-----------
1. The Ed25519 private key lives on the Mac and never leaves. This
   service NEVER exposes the private key material, only signatures.
2. ONE fixed receipt schema. Every field is validated; anything else
   → 400. No polymorphism, no "kind": "custom".
3. Domain separator 'xrpldashboard/receipt/v1' is prepended to every
   signed input before Ed25519 sign — verifiers apply the same
   separator, giving cross-domain replay protection against the
   snapshot key.
4. Per-caller-token rate limit (in-memory sliding window).
5. 90-day local audit log at ~/xrpl_test/launchd_logs/sig_service/
   YYYY-MM-DD.jsonl — daily rotated, older than 90d auto-pruned.
6. Passphrase is NOT stored in any file on this deploy. The service
   starts LOCKED. Charlie unlocks with a one-time `POST /unlock`
   from localhost only; after that the decrypted private key lives
   in memory until Mac reboot / service restart. Same posture as
   an SSH agent.

DEPLOY
------
See docs/SIG_SERVICE.md for the CF-Access tunnel wiring on the Render
side + the launchd install + the unlock ritual after Mac reboot.
"""
from __future__ import annotations

import base64
import collections
import hashlib
import json
import os
import re
import secrets
import threading
import time
import uuid
from datetime import datetime, timezone

from flask import Flask, jsonify, request, abort
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PRIVKEY_PATH = os.path.expanduser(
    "~/.config/xrpldashboard/receipt_ed25519_enc.pem"
)
DEFAULT_PUBKEY_PATH = os.path.join(HERE, "receipt_pubkey.pem")
AUDIT_DIR = os.path.join(HERE, "launchd_logs", "sig_service")

DOMAIN_SEPARATOR = b"xrpldashboard/receipt/v1"
SEP_BYTE = b"\x00"
KEY_FINGERPRINT = "A4:0F:B1:0A:9D:33:64:03"

# Rate limit: default 60 sign requests per minute per caller-token.
# In-memory sliding window; multi-worker deploys would need Redis.
RATE_LIMIT_WINDOW_SECONDS = 60
RATE_LIMIT_PER_TOKEN = 60

# Audit log retention.
AUDIT_RETENTION_DAYS = 90


# ---------------------------------------------------------------------------
# Key material — held in memory only after unlock
# ---------------------------------------------------------------------------

class KeyStore:
    """Thread-safe in-memory holder for the decrypted receipt private
    key. Starts locked. `unlock(passphrase)` loads + decrypts; `sign()`
    signs domain-separated payload. `lock()` clears the key from memory
    (best-effort — Python GC doesn't guarantee, but drops the reference)."""

    def __init__(self, privkey_path: str, pubkey_path: str):
        self._privkey_path = privkey_path
        self._pubkey_path = pubkey_path
        self._priv: Ed25519PrivateKey | None = None
        self._pub_raw: bytes | None = None
        self._lock = threading.Lock()

        # Load pubkey once at startup — it's public and non-sensitive.
        if os.path.exists(pubkey_path):
            with open(pubkey_path, "rb") as f:
                pub_pem = f.read()
            pub = serialization.load_pem_public_key(pub_pem)
            if not isinstance(pub, Ed25519PublicKey):
                raise SystemExit(
                    f"pubkey at {pubkey_path} is not Ed25519"
                )
            self._pub_raw = pub.public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw,
            )
            fp = self._compute_fingerprint(self._pub_raw)
            if fp != KEY_FINGERPRINT:
                raise SystemExit(
                    f"pubkey fingerprint mismatch: file has {fp}, "
                    f"expected {KEY_FINGERPRINT}"
                )

    @staticmethod
    def _compute_fingerprint(pub_raw: bytes) -> str:
        hex_ = hashlib.sha256(pub_raw).hexdigest()[:16].upper()
        return ":".join(hex_[i:i+2] for i in range(0, len(hex_), 2))

    def is_unlocked(self) -> bool:
        with self._lock:
            return self._priv is not None

    def unlock(self, passphrase: bytes) -> None:
        """Decrypt + load the private key. Raises ValueError on
        bad passphrase, FileNotFoundError if the file is missing."""
        with self._lock:
            if self._priv is not None:
                # Already unlocked — treat unlock() as idempotent
                return
            with open(self._privkey_path, "rb") as f:
                pem = f.read()
            priv = serialization.load_pem_private_key(pem, password=passphrase)
            if not isinstance(priv, Ed25519PrivateKey):
                raise ValueError("private key at path is not Ed25519")
            # Sanity: does the unlocked private key match our public key?
            derived_pub = priv.public_key().public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw,
            )
            if self._pub_raw is not None and derived_pub != self._pub_raw:
                raise ValueError(
                    "private key does not match published public key"
                )
            self._priv = priv

    def lock(self) -> None:
        with self._lock:
            self._priv = None

    def sign(self, canonical_hash_hex: str) -> bytes:
        """Sign the domain-separated payload:
            DOMAIN_SEPARATOR + 0x00 + bytes.fromhex(canonical_hash_hex)
        Raises RuntimeError if locked."""
        with self._lock:
            if self._priv is None:
                raise RuntimeError("keystore locked")
            hash_bytes = bytes.fromhex(canonical_hash_hex)
            signed_input = DOMAIN_SEPARATOR + SEP_BYTE + hash_bytes
            return self._priv.sign(signed_input)

    def fingerprint(self) -> str:
        return KEY_FINGERPRINT


# ---------------------------------------------------------------------------
# Rate limit — per-token sliding window, in-memory
# ---------------------------------------------------------------------------

class RateLimiter:
    def __init__(self, window_seconds: int, per_token_limit: int):
        self._window = window_seconds
        self._limit = per_token_limit
        self._buckets: dict[str, collections.deque[float]] = {}
        self._lock = threading.Lock()

    def check_and_record(self, token: str) -> tuple[bool, int]:
        """Return (allowed, retry_after_seconds). retry_after is 0 when
        allowed. Prunes expired timestamps in the bucket as a side effect."""
        now = time.time()
        with self._lock:
            bucket = self._buckets.setdefault(token, collections.deque())
            # Prune old timestamps outside the window.
            cutoff = now - self._window
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if len(bucket) >= self._limit:
                # oldest timestamp determines retry-after
                retry_after = max(1, int(bucket[0] + self._window - now))
                return False, retry_after
            bucket.append(now)
            return True, 0


# ---------------------------------------------------------------------------
# Audit log — append-only JSONL, daily-rotated, 90d retention
# ---------------------------------------------------------------------------

class AuditLog:
    def __init__(self, dir_path: str, retention_days: int):
        self._dir = dir_path
        self._retention = retention_days
        os.makedirs(dir_path, exist_ok=True)
        self._lock = threading.Lock()

    def _today_path(self) -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return os.path.join(self._dir, f"{stamp}.jsonl")

    def record(self, event: dict) -> None:
        """Append one JSON line. Never raises; audit failures must not
        block a signature (log to stderr instead).

        Charlie ruling 2026-09-09 morning after evening-analytics gap
        ("sig-log doesn't record UA — can't distinguish external vs
        JJ/canary"): every event now carries a `ua` field when a Flask
        request context is available. UA is the raw User-Agent header
        (may be empty string; may be spoofed — recorded verbatim, not
        interpreted). Events emitted outside a request context (unlock
        from env at boot, prune) get `ua="__no_request_context__"` so
        the field is always present and downstream queries can filter
        without null-handling."""
        try:
            event = dict(event)
            event.setdefault(
                "ts_utc",
                datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            )
            # Attach ua when in a Flask request context (best-effort).
            # Prefer X-Original-User-Agent (forwarded by sig_client.py
            # 2026-09-09) — that's the end-user's browser UA. Fall back
            # to the direct request UA (which is python-httpx/x.y.z for
            # web-app-driven signs). `ua_via`: "forwarded" when the
            # X-header was set, "direct" otherwise, so downstream tools
            # can filter without inference. Events outside a request
            # context get ua="__no_request_context__" + ua_via="boot".
            if "ua" not in event:
                try:
                    from flask import request as _req, has_request_context
                    if has_request_context():
                        forwarded = _req.headers.get("X-Original-User-Agent")
                        if forwarded:
                            event["ua"] = forwarded[:512]
                            event["ua_via"] = "forwarded"
                        else:
                            event["ua"] = (_req.headers.get("User-Agent") or "")[:512]
                            event["ua_via"] = "direct"
                    else:
                        event["ua"] = "__no_request_context__"
                        event["ua_via"] = "boot"
                except Exception:
                    event["ua"] = "__no_request_context__"
                    event["ua_via"] = "boot"
            line = json.dumps(event, sort_keys=True) + "\n"
            with self._lock:
                with open(self._today_path(), "a", encoding="utf-8") as f:
                    f.write(line)
        except Exception as e:
            import sys
            print(
                f"[sig_service] audit write failed: "
                f"{type(e).__name__}: {e}",
                file=sys.stderr, flush=True,
            )

    def prune(self) -> None:
        """Remove logs older than retention_days. Called periodically."""
        cutoff = time.time() - (self._retention * 86400)
        try:
            for name in os.listdir(self._dir):
                if not name.endswith(".jsonl"):
                    continue
                path = os.path.join(self._dir, name)
                if os.path.getmtime(path) < cutoff:
                    os.unlink(path)
        except FileNotFoundError:
            pass


# ---------------------------------------------------------------------------
# Receipt-schema validation — ONE fixed shape, refuse everything else
# ---------------------------------------------------------------------------

_HEX_64_RE = re.compile(r"^[0-9a-f]{64}$")
_UUID_V4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_ISO_UTC_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$"
)
_ALLOWED_KINDS = frozenset({"check", "registry"})
# "check"    → /check.json response envelope receipts (inline, per-request)
# "registry" → daily signed XRPL Token Registry snapshot (batch, once/day)
# Both use the same key + same domain separator. The canonical_hash
# differs by kind (each artifact's canonical serialization includes the
# kind field), so signatures can't be cross-replayed even with the
# shared domain separator.

_REQUIRED_FIELDS = frozenset({
    "canonical_hash",
    "response_id",
    "kind",
    "issued_at_utc",
})


def validate_sign_request(body: dict) -> str | None:
    """Return None on valid; string reason on invalid."""
    if not isinstance(body, dict):
        return "body must be a JSON object"
    keys = set(body.keys())
    if keys != _REQUIRED_FIELDS:
        extra = keys - _REQUIRED_FIELDS
        missing = _REQUIRED_FIELDS - keys
        parts = []
        if extra:
            parts.append(f"unexpected field(s): {sorted(extra)}")
        if missing:
            parts.append(f"missing required field(s): {sorted(missing)}")
        return "; ".join(parts) or "field mismatch"

    canonical_hash = body["canonical_hash"]
    if not isinstance(canonical_hash, str) or not _HEX_64_RE.match(canonical_hash):
        return "canonical_hash must be 64 lowercase hex chars"

    response_id = body["response_id"]
    if not isinstance(response_id, str) or not _UUID_V4_RE.match(response_id):
        return "response_id must be a UUID v4"

    kind = body["kind"]
    if kind not in _ALLOWED_KINDS:
        return f"kind must be one of {sorted(_ALLOWED_KINDS)}"

    issued_at_utc = body["issued_at_utc"]
    if not isinstance(issued_at_utc, str) or not _ISO_UTC_RE.match(issued_at_utc):
        return "issued_at_utc must be ISO-8601 UTC (YYYY-MM-DDTHH:MM:SS.fffZ)"

    return None


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------

def create_app(
    privkey_path: str = DEFAULT_PRIVKEY_PATH,
    pubkey_path: str = DEFAULT_PUBKEY_PATH,
    audit_dir: str = AUDIT_DIR,
) -> Flask:
    app = Flask(__name__)
    keystore = KeyStore(privkey_path, pubkey_path)
    app.config["KEYSTORE"] = keystore
    app.config["RATE_LIMITER"] = RateLimiter(
        RATE_LIMIT_WINDOW_SECONDS, RATE_LIMIT_PER_TOKEN
    )
    app.config["AUDIT_LOG"] = AuditLog(audit_dir, AUDIT_RETENTION_DAYS)

    # Auto-unlock at startup if RECEIPT_KEY_PASSPHRASE is in env — matches
    # the snapshot key's env-file custody pattern per Charlie's ruling
    # 2026-09-07 (afternoon revision). Paper remains the recovery copy
    # for cases where the env var is wiped (fresh Mac restore, launchd
    # env-file overwritten, etc.) but is no longer the operational path.
    # See docs/SIG_SERVICE.md §Custody.
    _env_pw = (os.environ.get("RECEIPT_KEY_PASSPHRASE") or "").strip()
    if _env_pw:
        try:
            keystore.unlock(_env_pw.encode("utf-8"))
            app.config["AUDIT_LOG"].record({
                "event": "unlocked_from_env",
                "key_fingerprint": keystore.fingerprint(),
            })
            import sys as _sys
            print(
                f"[sig_service] auto-unlocked from RECEIPT_KEY_PASSPHRASE at "
                f"startup (fingerprint {keystore.fingerprint()})",
                file=_sys.stderr, flush=True,
            )
        except Exception as e:
            import sys as _sys
            app.config["AUDIT_LOG"].record({
                "event": "unlocked_from_env_failed",
                "reason": type(e).__name__,
            })
            print(
                f"[sig_service] RECEIPT_KEY_PASSPHRASE auto-unlock FAILED: "
                f"{type(e).__name__}: {e}. "
                f"Service remains LOCKED; falls back to manual POST /unlock.",
                file=_sys.stderr, flush=True,
            )
            # Do not raise — service still starts, /status reflects locked,
            # /unlock endpoint still accepts a manual passphrase.
    else:
        import sys as _sys
        print(
            "[sig_service] RECEIPT_KEY_PASSPHRASE not set in env; service "
            "starts LOCKED. Unlock via localhost POST /unlock or restart "
            "with the env var set. See docs/SIG_SERVICE.md §Custody.",
            file=_sys.stderr, flush=True,
        )

    @app.route("/status", methods=["GET"])
    def status():
        ks: KeyStore = app.config["KEYSTORE"]
        return jsonify({
            "service": "xrpldashboard-sig-service",
            "unlocked": ks.is_unlocked(),
            "key_fingerprint": ks.fingerprint(),
            "domain_separator": DOMAIN_SEPARATOR.decode(),
            "allowed_kinds": sorted(_ALLOWED_KINDS),
        })

    @app.route("/unlock", methods=["POST"])
    def unlock():
        """Load + decrypt the private key. Localhost-only — the tunnel
        must not proxy this endpoint. Charlie runs this from an SSH
        session on the Mac after each service restart."""
        # Reject non-loopback callers.
        remote_ip = request.remote_addr or ""
        if remote_ip not in ("127.0.0.1", "::1", "localhost"):
            app.config["AUDIT_LOG"].record({
                "event": "unlock_rejected_non_loopback",
                "remote_ip": remote_ip,
            })
            abort(403, "unlock only accepted from loopback")

        # Passphrase in JSON body as {"passphrase": "..."} — the caller
        # is presumed to be Charlie feeding stdin from a `read -s` prompt.
        try:
            body = request.get_json(force=True, silent=True) or {}
        except Exception:
            abort(400, "body must be JSON")
        pw = body.get("passphrase")
        if not isinstance(pw, str) or not pw:
            abort(400, "passphrase must be a non-empty string")

        ks: KeyStore = app.config["KEYSTORE"]
        try:
            ks.unlock(pw.encode("utf-8"))
        except FileNotFoundError as e:
            app.config["AUDIT_LOG"].record({
                "event": "unlock_failed",
                "reason": "privkey_file_missing",
            })
            abort(500, f"private key file not found: {e}")
        except (ValueError, TypeError) as e:
            # Bad passphrase, wrong-key-shape, etc.
            app.config["AUDIT_LOG"].record({
                "event": "unlock_failed",
                "reason": type(e).__name__,
            })
            abort(400, f"unlock failed: {type(e).__name__}")

        app.config["AUDIT_LOG"].record({
            "event": "unlocked",
            "key_fingerprint": ks.fingerprint(),
        })
        return jsonify({
            "unlocked": True,
            "key_fingerprint": ks.fingerprint(),
        })

    @app.route("/sign", methods=["POST"])
    def sign():
        ks: KeyStore = app.config["KEYSTORE"]
        limiter: RateLimiter = app.config["RATE_LIMITER"]
        audit: AuditLog = app.config["AUDIT_LOG"]

        if not ks.is_unlocked():
            abort(503, "sig-service is locked; awaiting /unlock")

        # Content-Type must be JSON.
        if request.content_type and "application/json" not in request.content_type:
            abort(400, "Content-Type must be application/json")

        try:
            body = request.get_json(force=True, silent=True) or {}
        except Exception:
            abort(400, "body must be JSON")

        reason = validate_sign_request(body)
        if reason:
            audit.record({
                "event": "sign_rejected_schema",
                "reason": reason,
            })
            abort(400, f"schema: {reason}")

        # Rate limit: bucketed by caller-token from CF-Access header,
        # or by request.remote_addr as a fallback (both dev-local calls
        # and prod tunnel calls will always have one).
        caller_token = (
            request.headers.get("Cf-Access-Authenticated-User-Email")
            or request.headers.get("Cf-Access-Jwt-Assertion")
            or request.remote_addr
            or "unknown"
        )
        allowed, retry_after = limiter.check_and_record(caller_token)
        if not allowed:
            audit.record({
                "event": "sign_rate_limited",
                "caller_token_prefix": caller_token[:16],
                "retry_after_seconds": retry_after,
            })
            resp = jsonify({
                "error": "rate_limited",
                "retry_after_seconds": retry_after,
            })
            resp.status_code = 429
            resp.headers["Retry-After"] = str(retry_after)
            return resp

        # Sign.
        canonical_hash = body["canonical_hash"]
        try:
            signature = ks.sign(canonical_hash)
        except Exception as e:
            audit.record({
                "event": "sign_failed",
                "reason": f"{type(e).__name__}: {e}",
            })
            abort(500, f"sign failed: {type(e).__name__}")

        audit.record({
            "event": "signed",
            "response_id": body["response_id"],
            "canonical_hash_prefix": canonical_hash[:16],
            "kind": body["kind"],
            "issued_at_utc": body["issued_at_utc"],
        })

        signed_at_utc = datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        )
        return jsonify({
            "signature_ed25519_hex": signature.hex(),
            "signing_key_fingerprint": ks.fingerprint(),
            "domain_separator": DOMAIN_SEPARATOR.decode(),
            "signed_at_utc": signed_at_utc,
        })

    return app


def main() -> int:  # pragma: no cover — smoke test entry point
    import argparse
    parser = argparse.ArgumentParser(
        description="xrpldashboard receipt-signing service"
    )
    parser.add_argument("--bind", default="127.0.0.1:8842",
                        help="host:port to bind (default 127.0.0.1:8842)")
    parser.add_argument("--privkey", default=DEFAULT_PRIVKEY_PATH,
                        help="encrypted private key PEM path")
    parser.add_argument("--pubkey", default=DEFAULT_PUBKEY_PATH,
                        help="public key PEM path (for fingerprint check)")
    args = parser.parse_args()

    host, port = args.bind.rsplit(":", 1)
    app = create_app(privkey_path=args.privkey, pubkey_path=args.pubkey)
    app.run(host=host, port=int(port), debug=False)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
