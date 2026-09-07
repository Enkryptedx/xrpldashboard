"""Tests for the receipt-signing service (Option A).

These use the actual receipt_pubkey.pem committed in the repo and a
freshly-generated encrypted private key with a known passphrase (all
scoped to the tmp dir — the real Charlie's-Mac key + paper passphrase
are never touched).

Charlie ruling 2026-09-07: ONE fixed receipt schema. Every field
validated; anything else → 400. Domain separator prepended before
signing. Per-caller rate limit. 90d audit log. Localhost-only unlock.
"""
import base64
import json
import os
import tempfile
import unittest
import uuid
from datetime import datetime, timezone

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import sig_service


TEST_PASSPHRASE = "test-only-not-a-real-passphrase-2026"


def _make_scoped_keypair(tmpdir: str) -> tuple[str, str]:
    """Generate a fresh Ed25519 keypair, save encrypted PEM + PEM pubkey
    to tmpdir, and monkey-patch sig_service.KEY_FINGERPRINT to match
    the fresh pubkey so the KeyStore's fingerprint sanity check passes.

    Returns (privkey_path, pubkey_path)."""
    priv = Ed25519PrivateKey.generate()
    priv_pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.BestAvailableEncryption(
            TEST_PASSPHRASE.encode("utf-8")
        ),
    )
    pub_pem = priv.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    pub_raw = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )

    import hashlib
    hex_ = hashlib.sha256(pub_raw).hexdigest()[:16].upper()
    fp = ":".join(hex_[i:i+2] for i in range(0, len(hex_), 2))
    # Monkey-patch so KeyStore's sanity check passes with the test key.
    sig_service.KEY_FINGERPRINT = fp

    privkey_path = os.path.join(tmpdir, "priv.pem")
    pubkey_path = os.path.join(tmpdir, "pub.pem")
    with open(privkey_path, "wb") as f:
        f.write(priv_pem)
    with open(pubkey_path, "wb") as f:
        f.write(pub_pem)
    return privkey_path, pubkey_path


def _sample_body(**overrides) -> dict:
    body = {
        "canonical_hash": "a" * 64,
        "response_id": str(uuid.uuid4()),
        "kind": "check",
        "issued_at_utc": (
            datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        ),
    }
    body.update(overrides)
    return body


class ReceiptSchemaValidationTests(unittest.TestCase):

    def test_valid_body_returns_none(self):
        self.assertIsNone(sig_service.validate_sign_request(_sample_body()))

    def test_body_must_be_dict(self):
        self.assertIsNotNone(sig_service.validate_sign_request("not a dict"))
        self.assertIsNotNone(sig_service.validate_sign_request(["array"]))
        self.assertIsNotNone(sig_service.validate_sign_request(42))

    def test_missing_field_rejected(self):
        body = _sample_body()
        del body["canonical_hash"]
        reason = sig_service.validate_sign_request(body)
        self.assertIsNotNone(reason)
        self.assertIn("canonical_hash", reason)

    def test_extra_field_rejected(self):
        body = _sample_body(extra_bonus="unexpected")
        reason = sig_service.validate_sign_request(body)
        self.assertIsNotNone(reason)
        self.assertIn("unexpected", reason)

    def test_canonical_hash_must_be_64_lowercase_hex(self):
        self.assertIsNotNone(sig_service.validate_sign_request(
            _sample_body(canonical_hash="A" * 64)))  # uppercase
        self.assertIsNotNone(sig_service.validate_sign_request(
            _sample_body(canonical_hash="a" * 63)))  # too short
        self.assertIsNotNone(sig_service.validate_sign_request(
            _sample_body(canonical_hash="a" * 65)))  # too long
        self.assertIsNotNone(sig_service.validate_sign_request(
            _sample_body(canonical_hash="a" * 63 + "g")))  # bad char

    def test_response_id_must_be_uuid_v4(self):
        self.assertIsNotNone(sig_service.validate_sign_request(
            _sample_body(response_id="not-a-uuid")))
        # UUID v1 (starts with 1 in the third block) should be rejected
        self.assertIsNotNone(sig_service.validate_sign_request(
            _sample_body(response_id="12345678-1234-1234-1234-123456789abc")))

    def test_kind_must_be_check(self):
        self.assertIsNotNone(sig_service.validate_sign_request(
            _sample_body(kind="registry")))
        self.assertIsNotNone(sig_service.validate_sign_request(
            _sample_body(kind="")))

    def test_issued_at_utc_must_be_iso_utc(self):
        self.assertIsNotNone(sig_service.validate_sign_request(
            _sample_body(issued_at_utc="2026-09-07 15:00:00")))
        self.assertIsNotNone(sig_service.validate_sign_request(
            _sample_body(issued_at_utc="2026-09-07T15:00:00-04:00")))
        # Valid shapes should pass
        self.assertIsNone(sig_service.validate_sign_request(
            _sample_body(issued_at_utc="2026-09-07T15:00:00Z")))
        self.assertIsNone(sig_service.validate_sign_request(
            _sample_body(issued_at_utc="2026-09-07T15:00:00.123456Z")))


class KeyStoreTests(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.privkey_path, self.pubkey_path = _make_scoped_keypair(self.tmpdir)

    def test_starts_locked(self):
        ks = sig_service.KeyStore(self.privkey_path, self.pubkey_path)
        self.assertFalse(ks.is_unlocked())

    def test_unlock_with_correct_passphrase(self):
        ks = sig_service.KeyStore(self.privkey_path, self.pubkey_path)
        ks.unlock(TEST_PASSPHRASE.encode())
        self.assertTrue(ks.is_unlocked())

    def test_unlock_with_wrong_passphrase_raises(self):
        ks = sig_service.KeyStore(self.privkey_path, self.pubkey_path)
        with self.assertRaises(ValueError):
            ks.unlock(b"wrong-passphrase")

    def test_sign_locked_raises(self):
        ks = sig_service.KeyStore(self.privkey_path, self.pubkey_path)
        with self.assertRaises(RuntimeError):
            ks.sign("a" * 64)

    def test_sign_applies_domain_separator_and_verifies_with_pubkey(self):
        ks = sig_service.KeyStore(self.privkey_path, self.pubkey_path)
        ks.unlock(TEST_PASSPHRASE.encode())
        canonical_hash = "b" * 64
        sig = ks.sign(canonical_hash)

        # Independently verify by re-loading the pubkey and applying the
        # same domain separator + separator byte
        with open(self.pubkey_path, "rb") as f:
            pub = serialization.load_pem_public_key(f.read())
        signed_input = (
            sig_service.DOMAIN_SEPARATOR
            + sig_service.SEP_BYTE
            + bytes.fromhex(canonical_hash)
        )
        pub.verify(sig, signed_input)  # raises InvalidSignature on mismatch


class SignEndpointTests(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        priv_path, pub_path = _make_scoped_keypair(self.tmpdir)
        audit_dir = os.path.join(self.tmpdir, "audit")
        self.app = sig_service.create_app(
            privkey_path=priv_path,
            pubkey_path=pub_path,
            audit_dir=audit_dir,
        )
        self.client = self.app.test_client()

    def test_sign_when_locked_returns_503(self):
        resp = self.client.post("/sign", json=_sample_body())
        self.assertEqual(resp.status_code, 503)

    def test_unlock_from_loopback_then_sign_200(self):
        ur = self.client.post("/unlock", json={"passphrase": TEST_PASSPHRASE})
        self.assertEqual(ur.status_code, 200)
        r = self.client.post("/sign", json=_sample_body())
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertEqual(len(data["signature_ed25519_hex"]), 128)
        self.assertEqual(
            data["domain_separator"],
            sig_service.DOMAIN_SEPARATOR.decode(),
        )

    def test_unlock_wrong_passphrase_returns_400(self):
        ur = self.client.post("/unlock", json={"passphrase": "nope"})
        self.assertEqual(ur.status_code, 400)

    def test_schema_validation_returns_400(self):
        self.client.post("/unlock", json={"passphrase": TEST_PASSPHRASE})
        # missing kind
        body = _sample_body()
        del body["kind"]
        r = self.client.post("/sign", json=body)
        self.assertEqual(r.status_code, 400)
        # extra field
        r = self.client.post("/sign", json=_sample_body(extra="nope"))
        self.assertEqual(r.status_code, 400)
        # bad hash length
        r = self.client.post("/sign", json=_sample_body(canonical_hash="a"*63))
        self.assertEqual(r.status_code, 400)

    def test_rate_limit_returns_429_after_burst(self):
        self.client.post("/unlock", json={"passphrase": TEST_PASSPHRASE})
        # Fire past the per-token limit (default 60/min); expect 429 shortly.
        codes = []
        for _ in range(sig_service.RATE_LIMIT_PER_TOKEN + 5):
            r = self.client.post("/sign", json=_sample_body())
            codes.append(r.status_code)
        self.assertIn(429, codes,
                      f"expected rate-limit trigger, got {sorted(set(codes))}")

    def test_status_endpoint_reports_unlocked_state(self):
        r = self.client.get("/status")
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertFalse(d["unlocked"])
        self.client.post("/unlock", json={"passphrase": TEST_PASSPHRASE})
        r = self.client.get("/status")
        self.assertTrue(r.get_json()["unlocked"])
        self.assertEqual(r.get_json()["allowed_kinds"], ["check"])

    def test_audit_log_records_signed_events(self):
        self.client.post("/unlock", json={"passphrase": TEST_PASSPHRASE})
        self.client.post("/sign", json=_sample_body())
        # Read the audit log for today
        audit_dir = self.app.config["AUDIT_LOG"]._dir
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        audit_file = os.path.join(audit_dir, today + ".jsonl")
        self.assertTrue(os.path.exists(audit_file), f"no audit file at {audit_file}")
        lines = [json.loads(l) for l in open(audit_file) if l.strip()]
        events = [x["event"] for x in lines]
        self.assertIn("unlocked", events)
        self.assertIn("signed", events)

    def test_audit_log_records_schema_reject_reason(self):
        self.client.post("/unlock", json={"passphrase": TEST_PASSPHRASE})
        self.client.post("/sign", json=_sample_body(kind="not-check"))
        audit_dir = self.app.config["AUDIT_LOG"]._dir
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        lines = [json.loads(l) for l in open(os.path.join(audit_dir, today+".jsonl")) if l.strip()]
        reject_events = [x for x in lines if x["event"] == "sign_rejected_schema"]
        self.assertGreaterEqual(len(reject_events), 1)


if __name__ == "__main__":
    unittest.main()
