"""Signed verified-tokens manifest walker (Charlie ruling 2026-09-21
Mon PM, item 4).

Produces an hourly Ed25519-signed manifest of every ticker where we
have verified the canonical issuer. The manifest is served publicly at
`/.well-known/verified-tokens.json` (PG-first, disk fallback) so
downstream consumers (wallets, indexers, agents) can pin a signed
snapshot instead of re-scraping.

Design notes:

- **Signing**: same receipt key as `/check.json` and the daily
  registry snapshot. Domain separator `xrpldashboard/receipt/v1` is
  applied by the sig-service before Ed25519 verify.
- **Cadence**: hourly. `snapshot_hour_utc` is the UTC hour floor at
  build time; ON CONFLICT DO NOTHING makes re-running within the
  same hour a no-op.
- **Source of truth**: `ticker_canonical_issuers.json` (curator-
  maintained). Each row is a plain dict: ticker, currency_hex,
  canonical_issuers[], citation_url, no_official_xrpl_issuer,
  meme_name, gateway_or_bridge, tier, last_reviewed_at.
- **Env gate**: `SIGNED_VERIFIED_TOKENS_ENABLED=1` required — Charlie
  ruling "first cycle overnight; nothing published until it verifies".
  Without the env, the walker exits 0 with a log line and does NOT
  write to PG.

Verification recipe is on /methodology.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import db
import signed_snapshot  # for _canonical_json
from signed_registry_snapshot import sign_via_sig_service


SCHEMA_VERSION = 1
SIGNING_DOMAIN = "xrpldashboard/verified-tokens/v1"
CANONICAL_ISSUERS_PATH = os.path.join(HERE, "ticker_canonical_issuers.json")
DISK_PATH = os.path.join(HERE, "signed_verified_tokens_latest.json")
WALKER_NAME = "signed_verified_tokens"
WALKER_CADENCE_SECONDS = 3600


SCHEMA_NOTE = (
    "verified = the canonical issuer proved control of its brand's "
    "domain (either via an xrp-ledger.toml two-way match or a "
    "curator-confirmed public statement cited by the citation_url). "
    "This is a signal about issuer provenance; it is NOT an "
    "endorsement of the token's economics, legal standing, or "
    "safety. Absence from this list does NOT mean 'unsafe' — some "
    "tickers are absent because a domain claim isn't the "
    "appropriate identity primitive (memecoins), because no "
    "canonical issuer exists on XRPL (BTC/ETH natively), or because "
    "the issuer is a curator-registered gateway (see gateway_or_bridge)."
)


def _hour_floor_utc(now_utc: dt.datetime) -> str:
    """Return the UTC hour floor as an ISO string with 00 minutes/seconds.
    This is the row PK — same-hour re-run is a no-op."""
    floored = now_utc.replace(minute=0, second=0, microsecond=0)
    return floored.strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_canonical_registry() -> dict:
    with open(CANONICAL_ISSUERS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _hex_from_ticker(ticker: str) -> str:
    """Return the 40-char hex currency code for a ticker. 3-char tickers
    stay as 3-char ASCII (that's the on-ledger form); longer names are
    already hex in the registry (e.g., RLUSD's 524C555344…)."""
    entry = _load_canonical_registry().get(ticker)
    # If registry has a direct hex form use it (RLUSD case)
    if isinstance(entry, dict) and entry.get("currency_hex"):
        return entry["currency_hex"]
    # Otherwise the ticker IS the currency code (USD, EUR, BTC, etc.
    # — 3 ASCII chars, not hex)
    return ticker


def _derive_manifest_flag_tier(entry: dict) -> str:
    """Fallback tier for entries WITHOUT canonical_issuers. Used when
    no (currency_hex, issuer) pair exists to look up in
    token_category_current (memes, natively-elsewhere, umbrella).

    Charlie ruling 2026-09-22 Tue 5:38 PM ET: for entries WITH
    canonical_issuers, tier MUST come from the site's real registry
    (shared_tier_verifier.resolve_tier over token_category_current),
    never from the presence of canonical_issuers. USDC is the
    load-bearing example — canonical_issuer_known but tier=self-described
    (no two-way TOML). This helper handles only the no-canonical case.
    """
    if entry.get("umbrella"):
        return "umbrella"
    if entry.get("meme_name"):
        return "meme_name"
    if entry.get("no_official_xrpl_issuer"):
        return "informational"
    return "unknown"


# Site 5-tier order (strongest → weakest) — used when a ticker has
# multiple canonical issuers with different tiers; the manifest reports
# the STRONGEST since the ticker as a whole is at least that verified.
_SITE_TIER_STRENGTH = {
    "verified": 5,
    "self-described": 4,
    "labeled": 3,
    "bare": 2,
    "unknown": 1,
}


def _site_tier_for(currency_hex: str, canonical_issuers: list) -> tuple[str, list]:
    """Look up the site tier for (currency_hex, issuer) pairs via
    shared_tier_verifier.resolve_tier. Returns (strongest_tier,
    per_issuer_records) where per_issuer_records is a list of dicts
    {issuer, tier, source, citation_url} for audit / verify tests.

    NEVER derived from the presence of canonical_issuers — the whole
    point of Charlie's 2026-09-22 5:38 PM ruling is that "canonical
    known" and "verified tier" are separate claims. USDC has canonical
    known but tier=self-described.
    """
    import shared_tier_verifier as stv
    per_issuer = []
    strongest = None
    strongest_strength = 0
    for issuer in canonical_issuers:
        rec = stv.resolve_tier(currency_hex, issuer, elevate=False)
        per_issuer.append({
            "issuer": issuer,
            "tier": rec.tier,
            "source": rec.source,
            "citation_url": rec.citation_url,
        })
        s = _SITE_TIER_STRENGTH.get(rec.tier, 0)
        if s > strongest_strength:
            strongest_strength = s
            strongest = rec.tier
    if strongest is None:
        strongest = "unknown"
    return strongest, per_issuer


def _row_for_ticker(ticker: str, entry: dict, verified_at_iso: str) -> dict:
    """Assemble one manifest row. Charlie's schema per row:
    ticker, currency_hex, canonical_issuers[], citation_url,
    no_official_xrpl_issuer, meme_name, gateway_or_bridge, tier,
    last_reviewed_at."""
    canonical = entry.get("canonical_issuers") or []
    # currency_hex: 40-char if the ticker is a longer name (RLUSD),
    # otherwise the 3-char code itself (USD/EUR/BTC/etc.). The registry
    # keys are already tickers, not hex.
    if len(ticker) == 3:
        currency_hex = ticker
    else:
        # For longer names, hex-encode uppercase ASCII to 40 chars
        raw = ticker.encode("ascii", errors="replace")
        currency_hex = raw.hex().upper().ljust(40, "0")[:40]
    # Charlie ruling 2026-09-22 Tue 5:38 PM ET: `canonical_issuer_known`
    # is a curator claim (we know WHICH XRPL address the brand uses);
    # `tier` is the site's real registry tier from token_category_current
    # via shared_tier_verifier.resolve_tier. They are SEPARATE — USDC
    # has canonical_issuer_known but tier=self-described (no two-way TOML).
    canonical_issuer_known = bool(canonical)
    if canonical:
        site_tier, per_issuer = _site_tier_for(currency_hex, canonical)
    else:
        # No canonical issuer to resolve — fall back to the flag-derived
        # meta-tier that categorises WHY the ticker is on the list
        # (meme_name / informational / umbrella / unknown).
        site_tier = _derive_manifest_flag_tier(entry)
        per_issuer = []
    return {
        "ticker": ticker,
        "currency_hex": currency_hex,
        "canonical_issuers": sorted(canonical),
        "canonical_issuer_known": canonical_issuer_known,
        "citation_url": entry.get("citation") or entry.get("source_url"),
        "no_official_xrpl_issuer": bool(entry.get("no_official_xrpl_issuer")),
        "meme_name": bool(entry.get("meme_name")),
        # Curator-registered gateways (Bitstamp/GateHub for USD, EUR, BTC
        # gateway IOUs) and bridge-issued wraps (Axelar/Midas) get flagged.
        # Falls back to False when the entry doesn't declare it — the
        # curator sets it explicitly during review.
        "gateway_or_bridge": bool(entry.get("gateway_or_bridge")),
        # Tier from the site registry (never from canonical_issuers
        # presence). Per-issuer records included so a verifier can
        # re-execute the tier lookup independently.
        "tier": site_tier,
        "per_issuer_tier": per_issuer,
        "last_reviewed_at": entry.get("last_reviewed_at"),
    }


def build_envelope(now_utc: dt.datetime | None = None) -> dict:
    """Assemble the unsigned envelope."""
    if now_utc is None:
        now_utc = dt.datetime.now(dt.timezone.utc)
    registry = _load_canonical_registry()
    tokens = []
    for ticker, entry in registry.items():
        if ticker.startswith("_") or not isinstance(entry, dict):
            continue
        tokens.append(_row_for_ticker(ticker, entry, now_utc.isoformat()))
    tokens.sort(key=lambda r: r["ticker"])

    envelope = {
        "schema_version": SCHEMA_VERSION,
        "signing_domain": SIGNING_DOMAIN,
        "snapshot_hour_utc": _hour_floor_utc(now_utc),
        "envelope_built_at_utc": now_utc.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "schema_note": SCHEMA_NOTE,
        "tokens": tokens,
    }
    return envelope


def canonical_hash_hex(envelope: dict) -> str:
    canonical = signed_snapshot._canonical_json(envelope)
    return hashlib.sha256(canonical).hexdigest()


# ── Verifier ────────────────────────────────────────────────────────────
#
# Charlie ruling 2026-09-22 Tue 5:22 PM ET: the verified-tokens envelope
# has its own shape (hourly, no leaf/audit_path/chain) so signed_snapshot's
# verify_envelope isn't a fit. Own verifier below.
#
# Contract: the sig-service (sig_service.py) signs
#     b"xrpldashboard/receipt/v1" + b"\x00" + bytes.fromhex(canonical_hash_hex)
# using the ed25519 receipt key. Verifier re-derives the canonical hash
# from the stripped envelope body, checks the signature against the
# published receipt pubkey, and verifies the fingerprint matches.

RECEIPT_PUBKEY_PEM_PATH = os.path.join(HERE, "receipt_pubkey.pem")
RECEIPT_DOMAIN_SEPARATOR = b"xrpldashboard/receipt/v1"
RECEIPT_SEP_BYTE = b"\x00"

# Fields written by the sig-service that must be stripped from the
# envelope body before recomputing the canonical hash.
_SIG_BLOCK_FIELDS = (
    "signature_ed25519_hex",
    "signing_key_fingerprint",
    "domain_separator",
    "signed_at_utc",
)


def _load_receipt_pubkey(pem_path: str = RECEIPT_PUBKEY_PEM_PATH):
    from cryptography.hazmat.primitives.serialization import load_pem_public_key
    with open(pem_path, "rb") as f:
        return load_pem_public_key(f.read())


def _fingerprint_from_pubkey(pub) -> str:
    """Match sig_service.py's fingerprint format: first 8 bytes of sha256
    of raw public key, colon-separated XX:XX:XX:XX:XX:XX:XX:XX."""
    from cryptography.hazmat.primitives import serialization
    raw = pub.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    digest = hashlib.sha256(raw).hexdigest()[:16].upper()
    return ":".join(digest[i:i + 2] for i in range(0, 16, 2))


def verify_envelope(signed: dict, pubkey_pem_path: str = RECEIPT_PUBKEY_PEM_PATH
                    ) -> tuple[bool, list[str]]:
    """Independent verification path for a signed verified-tokens envelope.
    Re-derives canonical hash from the stripped body, verifies ed25519
    signature over the domain-separated hash bytes using the receipt
    pubkey, cross-checks the fingerprint.

    Returns (ok, issues). issues is empty on success.
    """
    from cryptography.exceptions import InvalidSignature
    issues: list[str] = []

    # 1) Required signature fields present
    for f in _SIG_BLOCK_FIELDS:
        if f not in signed:
            issues.append(f"missing signature field: {f}")
    if issues:
        return False, issues

    # 2) Domain separator matches
    if signed["domain_separator"] != RECEIPT_DOMAIN_SEPARATOR.decode():
        issues.append(
            f"domain_separator mismatch (file={signed['domain_separator']!r}, "
            f"expected={RECEIPT_DOMAIN_SEPARATOR.decode()!r})"
        )

    # 3) Recompute canonical hash from stripped envelope body
    body = {k: v for k, v in signed.items() if k not in _SIG_BLOCK_FIELDS}
    canon_hex = canonical_hash_hex(body)

    # 4) Load receipt pubkey + verify fingerprint
    try:
        pub = _load_receipt_pubkey(pubkey_pem_path)
    except Exception as e:
        return False, [f"cannot load receipt pubkey at {pubkey_pem_path}: {e}"]
    local_fp = _fingerprint_from_pubkey(pub)
    if local_fp != signed["signing_key_fingerprint"]:
        issues.append(
            f"pubkey fingerprint mismatch (file={signed['signing_key_fingerprint']}, "
            f"local={local_fp})"
        )

    # 5) Verify signature over domain-separator + 0x00 + hash_bytes
    hash_bytes = bytes.fromhex(canon_hex)
    signed_input = RECEIPT_DOMAIN_SEPARATOR + RECEIPT_SEP_BYTE + hash_bytes
    try:
        sig_bytes = bytes.fromhex(signed["signature_ed25519_hex"])
    except ValueError as e:
        return False, [f"signature_ed25519_hex is not valid hex: {e}"]
    try:
        pub.verify(sig_bytes, signed_input)
    except InvalidSignature:
        issues.append("Ed25519 signature did NOT verify against receipt pubkey")

    # 6) Envelope shape sanity
    for f in ("schema_version", "signing_domain", "snapshot_hour_utc",
              "envelope_built_at_utc", "schema_note", "tokens"):
        if f not in signed:
            issues.append(f"envelope missing field: {f}")
    if signed.get("signing_domain") != SIGNING_DOMAIN:
        issues.append(f"signing_domain mismatch (file={signed.get('signing_domain')}, "
                      f"expected={SIGNING_DOMAIN})")

    return not issues, issues


def write_signed_to_disk(signed_envelope: dict) -> str:
    """Atomic write of the signed envelope to disk (fallback path for
    the well-known route)."""
    tmp = DISK_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(signed_envelope, f, sort_keys=True, indent=2)
    os.replace(tmp, DISK_PATH)
    return DISK_PATH


def main() -> int:
    db.write_walker_health_start(WALKER_NAME, cadence_seconds=WALKER_CADENCE_SECONDS)
    ok = False
    message = "not_yet_stamped"
    try:
        # Env gate — Charlie ruling: nothing published until proven
        # through one overnight cycle. Without the env, the walker
        # exits successfully with a log line and does NOT touch PG or
        # disk.
        if os.environ.get("SIGNED_VERIFIED_TOKENS_ENABLED") not in ("1", "true", "yes"):
            message = "env_gate_off (SIGNED_VERIFIED_TOKENS_ENABLED not set)"
            ok = True
            print(f"[signed_verified_tokens] {message} — dry-run only, not writing")
            # Still exercise the build path so the walker fails loud if
            # the envelope shape breaks; just don't sign or persist.
            envelope = build_envelope()
            print(
                f"[signed_verified_tokens] dry-envelope: "
                f"snapshot_hour_utc={envelope['snapshot_hour_utc']} "
                f"tokens={len(envelope['tokens'])}"
            )
            return 0

        now_utc = dt.datetime.now(dt.timezone.utc)
        envelope = build_envelope(now_utc)
        canon_hex = canonical_hash_hex(envelope)
        try:
            sig_block = sign_via_sig_service(canon_hex)
        except RuntimeError as e:
            message = f"sign_failed: {e}"
            print(f"[signed_verified_tokens] SIGN FAILED: {e}", file=sys.stderr, flush=True)
            ok = False
            return 1

        signed = dict(envelope)
        signed["signature"] = sig_block
        signed["canonical_hash_hex"] = canon_hex

        # PG mirror first — fails loud if writer breaks
        db.write_signed_verified_tokens(envelope, sig_block, canon_hex)
        # Then disk fallback
        write_signed_to_disk(signed)

        message = (
            f"tokens={len(envelope['tokens'])} "
            f"hour={envelope['snapshot_hour_utc']} "
            f"hash={canon_hex[:16]}"
        )
        print(f"[signed_verified_tokens] wrote {DISK_PATH} + PG · {message}")
        ok = True
        return 0
    except Exception as e:
        message = f"exception: {type(e).__name__}: {e}"
        ok = False
        raise
    finally:
        db.write_walker_health_end(
            WALKER_NAME, ok=ok,
            message=message or ("clean_no_message" if ok else "unlabeled_failure"),
        )


if __name__ == "__main__":
    sys.exit(main())
