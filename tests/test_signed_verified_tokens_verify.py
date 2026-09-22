"""Regression: verified-tokens envelope round-trips through the verifier.

Charlie ruling 2026-09-22 Tue 5:22 PM ET: no env-flip until we have a
verifier that catches tampering. This test proves:
  - Every row carries citation_url and tier (schema completeness).
  - Sig-service round-trip produces a valid envelope.
  - Genuine envelope passes verify_envelope.
  - Tampered envelope FAILS verify with the ed25519 signature error.

Skips gracefully when the sig-service is locked or unreachable (CI /
laptop-without-service). Local dev / prod both have the sig-service up
per Charlie's setup.
"""
from __future__ import annotations
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)

import signed_verified_tokens as SVT  # noqa: E402


def _try_sign(env):
    """Return (canon_hex, sig_block) or (None, None) if sig-service is
    locked/unreachable/unavailable."""
    from signed_verified_tokens import canonical_hash_hex, sign_via_sig_service
    canon = canonical_hash_hex(env)
    try:
        sig = sign_via_sig_service(canon)
    except Exception as e:  # RuntimeError from sig-service failures
        return canon, None
    return canon, sig


def test_every_row_has_citation_and_tier():
    env = SVT.build_envelope()
    tokens = env["tokens"]
    assert tokens, "envelope has zero token rows"
    missing_cite = [t["ticker"] for t in tokens if not t.get("citation_url")]
    missing_tier = [t["ticker"] for t in tokens if not t.get("tier")]
    assert not missing_cite, f"rows missing citation_url: {missing_cite}"
    assert not missing_tier, f"rows missing tier: {missing_tier}"


def test_manifest_flag_tier_no_canonical_cases_only():
    """`_derive_manifest_flag_tier` handles ONLY entries without
    canonical issuers. Entries WITH canonical issuers get their tier
    from shared_tier_verifier.resolve_tier — see
    test_manifest_tier_matches_site_registry."""
    assert SVT._derive_manifest_flag_tier({"meme_name": True, "canonical_issuers": []}) == "meme_name"
    assert SVT._derive_manifest_flag_tier({"no_official_xrpl_issuer": True, "canonical_issuers": []}) == "informational"
    assert SVT._derive_manifest_flag_tier({"umbrella": True}) == "umbrella"
    assert SVT._derive_manifest_flag_tier({"canonical_issuers": []}) == "unknown"


def test_manifest_tier_matches_site_registry():
    """Charlie ruling 2026-09-22 Tue 5:38 PM ET: manifest tier per row MUST
    equal what shared_tier_verifier.resolve_tier returns for the same
    (currency_hex, issuer). USDC is the load-bearing case — canonical
    known but tier=self-described. This guards against future drift
    where the manifest could claim 'verified' for a token the site
    reports as anything weaker."""
    import shared_tier_verifier as stv
    env = SVT.build_envelope()
    tokens = env["tokens"]
    mismatches = []
    for t in tokens:
        if not t.get("canonical_issuers"):
            continue
        # Manifest reports the STRONGEST tier across issuers; verify each
        # per_issuer_tier record equals what resolve_tier says now.
        for rec in t.get("per_issuer_tier", []):
            fresh = stv.resolve_tier(t["currency_hex"], rec["issuer"], elevate=False)
            if fresh.tier != rec["tier"]:
                mismatches.append({
                    "ticker": t["ticker"], "issuer": rec["issuer"],
                    "manifest_tier": rec["tier"], "site_tier": fresh.tier,
                })
    assert not mismatches, (
        f"manifest tier drifted from site registry: {mismatches}"
    )


def test_manifest_canonical_issuer_known_flag_is_bool():
    """Every row must carry a boolean `canonical_issuer_known` — separate
    from the tier claim."""
    env = SVT.build_envelope()
    for t in env["tokens"]:
        assert isinstance(t.get("canonical_issuer_known"), bool), t["ticker"]
        # Coherence: bool matches truthy canonical_issuers list.
        expected = bool(t.get("canonical_issuers"))
        assert t["canonical_issuer_known"] == expected, (
            f"{t['ticker']}: canonical_issuer_known={t['canonical_issuer_known']} "
            f"but canonical_issuers={t.get('canonical_issuers')}"
        )


def test_verify_missing_signature_fields_fails():
    env = SVT.build_envelope()
    ok, errs = SVT.verify_envelope(env)  # no sig block at all
    assert ok is False
    assert any("missing signature field" in e for e in errs)


def test_verify_genuine_envelope_ok():
    env = SVT.build_envelope()
    canon, sig = _try_sign(env)
    if sig is None:
        pytest.skip("sig-service unreachable/locked — cannot exercise verify")
    signed = dict(env); signed.update(sig)
    ok, errs = SVT.verify_envelope(signed)
    assert ok, f"expected valid; got errs={errs}"


def test_verify_tampered_envelope_fails_with_signature_error():
    env = SVT.build_envelope()
    canon, sig = _try_sign(env)
    if sig is None:
        pytest.skip("sig-service unreachable/locked")
    signed = dict(env); signed.update(sig)
    # Insert a bogus row after signing — must break the signature.
    signed["tokens"] = list(signed["tokens"]) + [{"ticker": "BOGUS", "currency_hex": "BOG"}]
    ok, errs = SVT.verify_envelope(signed)
    assert ok is False
    assert any("Ed25519" in e or "signature" in e.lower() for e in errs)


def test_verify_wrong_fingerprint_fails():
    env = SVT.build_envelope()
    canon, sig = _try_sign(env)
    if sig is None:
        pytest.skip("sig-service unreachable/locked")
    signed = dict(env); signed.update(sig)
    signed["signing_key_fingerprint"] = "FF:FF:FF:FF:FF:FF:FF:FF"
    ok, errs = SVT.verify_envelope(signed)
    assert ok is False
    assert any("fingerprint" in e.lower() for e in errs)
