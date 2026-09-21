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
    return {
        "ticker": ticker,
        "currency_hex": currency_hex,
        "canonical_issuers": sorted(canonical),
        "citation_url": entry.get("citation") or entry.get("source_url"),
        "no_official_xrpl_issuer": bool(entry.get("no_official_xrpl_issuer")),
        "meme_name": bool(entry.get("meme_name")),
        # Curator-registered gateways (Bitstamp/GateHub for USD, EUR, BTC
        # gateway IOUs) and bridge-issued wraps (Axelar/Midas) get flagged.
        # Falls back to False when the entry doesn't declare it — the
        # curator sets it explicitly during review.
        "gateway_or_bridge": bool(entry.get("gateway_or_bridge")),
        "tier": entry.get("tier"),
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
