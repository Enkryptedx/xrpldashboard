"""Daily signed XRPL Token Registry snapshot.

Charlie ruling 2026-09-07: go — daily signed registry snapshot with
the receipt/registry key; its root already rides in tonight's anchored
snapshot as registry_state.

WHAT THIS DOES
--------------
Once per UTC day:
  1. Reads the same registry state that collect_registry_state() emits
     into the signed_snapshot chain (row counts, taxonomy version,
     Merkle root over token_category_history).
  2. Assembles the canonical registry envelope with schema_version,
     snapshot_date_utc, and all fields sort_keys-serialized.
  3. Computes SHA-256 of the canonical bytes → canonical_hash.
  4. Calls the sig-service /sign endpoint over 127.0.0.1:8842 with
     kind="registry".
  5. Attaches signature + key fingerprint to the envelope.
  6. Writes signed_registry_snapshots/YYYY-MM-DD.json.
  7. Flask route /.well-known/registry/YYYY-MM-DD.json serves it.

WHY SEPARATE FROM signed_snapshot.py
------------------------------------
The daily anchored snapshot chain (signed_snapshot.py) commits to a
canonical set of 7 anchored data metrics + 4 meta metrics for the
whole site. Registry state is one of those meta metrics (per commit
576fd6b). This artifact is a stand-alone registry file that a
regulator, auditor, or third-party verifier can fetch directly by
date without needing to understand the full snapshot chain.

The registry_state Merkle root in the anchored snapshot is
Charlie's tamper-evident commitment to what the registry looked like
that day. This file publishes the underlying registry, signed with
the same fingerprint that appears in registry_state alongside the
Merkle root. Two-way triangulation: reader can verify (a) this file's
signature against the pubkey, (b) this file's history_merkle_root
matches the value in that date's signed_snapshot.

FAIL MODES
----------
- sig-service is locked → walker records the failure and exits non-zero
  (the daily launchd cycle retries the next day; Charlie unlocks at
  next attention). No unsigned envelope is written to disk.
- sig-service is unreachable → same treatment as locked.
- Postgres is unavailable → SystemExit; walker retries next cycle.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import sys

import urllib.error
import urllib.request

import db
import signed_snapshot  # reuse _canonical_json, collect_registry_state


HERE = os.path.dirname(os.path.abspath(__file__))
SNAPSHOTS_DIR = os.path.join(HERE, "signed_registry_snapshots")

SIG_SERVICE_URL = os.environ.get(
    "SIG_SERVICE_URL", "http://127.0.0.1:8842"
)
SIG_SERVICE_TIMEOUT = float(os.environ.get("SIG_SERVICE_TIMEOUT", "5.0"))

SCHEMA_VERSION = 1
SIGNING_DOMAIN = "xrpldashboard/registry/v1"


def build_envelope(now_utc: dt.datetime | None = None) -> dict:
    """Build the unsigned registry envelope for today. Raises SystemExit
    on strict-refuse (mirrors collect_registry_state's discipline)."""
    if now_utc is None:
        now_utc = dt.datetime.now(dt.timezone.utc)
    date_str = now_utc.strftime("%Y-%m-%d")

    reg = signed_snapshot.collect_registry_state()
    val = reg["value"]

    envelope = {
        "schema_version": SCHEMA_VERSION,
        "signing_domain": SIGNING_DOMAIN,
        "snapshot_date_utc": date_str,
        "taxonomy_version": val["taxonomy_version"],
        "row_counts": {
            "issuer_facts": val["issuer_facts_count"],
            "token_facts": val["token_facts_count"],
            "token_category_current": val["token_category_current_count"],
            "token_category_history": val["token_category_history_count"],
        },
        "history_merkle_root_hex": val["history_merkle_root"],
        "merkle_scheme": val["merkle_scheme"],
        "history_row_order": val["history_row_order"],
        "envelope_built_at_utc": now_utc.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
    }
    return envelope


def canonical_hash_hex(envelope: dict) -> str:
    """SHA-256 hex over the canonical-JSON serialization of the envelope,
    excluding the signature block (which doesn't exist yet). This is
    what the sig-service signs."""
    canonical = signed_snapshot._canonical_json(envelope)
    return hashlib.sha256(canonical).hexdigest()


def sign_via_sig_service(canonical_hash_h: str) -> dict:
    """Post to the sig-service with kind='registry'. Returns the
    signature block from the sig-service response on success. Raises
    RuntimeError on failure with a human-readable reason."""
    response_id = _make_uuid_v4()
    body = {
        "canonical_hash": canonical_hash_h,
        "response_id": response_id,
        "kind": "registry",
        "issued_at_utc": dt.datetime.now(dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        ),
    }
    body_bytes = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        SIG_SERVICE_URL.rstrip("/") + "/sign",
        data=body_bytes,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=SIG_SERVICE_TIMEOUT) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        # Read the error body for a more useful message.
        body_txt = e.read().decode("utf-8", errors="replace")[:200]
        if e.code == 503:
            raise RuntimeError(
                "sig-service is locked; unlock at 127.0.0.1:8842/unlock first"
            )
        if e.code == 429:
            raise RuntimeError(
                f"sig-service rate-limited; retry_after="
                f"{e.headers.get('Retry-After', '?')}s"
            )
        raise RuntimeError(
            f"sig-service returned {e.code}: {body_txt}"
        )
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RuntimeError(
            f"sig-service unreachable: {type(e).__name__}: {e}"
        )
    data = json.loads(raw)
    return {
        "signature_ed25519_hex": data["signature_ed25519_hex"],
        "signing_key_fingerprint": data["signing_key_fingerprint"],
        "domain_separator": data["domain_separator"],
        "signed_at_utc": data["signed_at_utc"],
    }


def _make_uuid_v4() -> str:
    import uuid
    return str(uuid.uuid4())


def write_signed_snapshot(envelope: dict, sig_block: dict) -> str:
    """Write the fully-signed envelope to disk AND mirror to PG. Returns
    the disk file path.

    Charlie ruling 2026-09-21 (Monday build item 4): PG is now the
    authoritative store for served snapshots; disk stays as a
    resilience fallback and as the on-the-side git artifact. The
    daily-registry-push carve-out is retired — PG durability plus
    Neon PITR/backups mean the git push isn't needed to make the
    envelope reachable.

    PG-first mirror runs BEFORE the disk write so a PG failure fails
    loud (raises from write_signed_registry_snapshot) and the disk
    file never lands with a stale/absent PG counterpart. Disk write
    is atomic via tmp+rename, matching the previous shape.
    """
    canon_hex = canonical_hash_hex(envelope)

    # Mirror to PG first; a failure here raises out to main()'s error
    # path so the walker stamps ok=False and the disk file is not
    # written with a broken PG counterpart.
    try:
        import db as _db
        _db.write_signed_registry_snapshot(envelope, sig_block, canon_hex)
    except Exception:
        raise

    os.makedirs(SNAPSHOTS_DIR, exist_ok=True)
    signed = dict(envelope)
    signed["signature"] = sig_block
    signed["canonical_hash_hex"] = canon_hex

    date_str = envelope["snapshot_date_utc"]
    out_path = os.path.join(SNAPSHOTS_DIR, f"{date_str}.json")
    tmp_path = out_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(signed, f, sort_keys=True, indent=2)
    os.replace(tmp_path, out_path)
    return out_path


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--force", action="store_true",
        help="Overwrite an existing registry snapshot for the target date. "
             "Off by default: the walker refuses to re-sign a date that "
             "already has a snapshot on disk (Tier 0 debounce, 2026-09-19). "
             "Use only for corrective anchors.",
    )
    args = parser.parse_args()

    now_utc = dt.datetime.now(dt.timezone.utc)

    # DEBOUNCE (2026-09-19 Tier 0: a chain job never re-signs a date).
    # Mirrors signed_snapshot.py — see that file for the pull-plug
    # incident that motivated this. Same-day RunAtLoad after a cold boot
    # would recompute canonical_hash with post-boot registry state,
    # silently orphaning the morning's committed envelope. Skip path
    # does NOT touch walker_health — real silence still pages via the
    # freshness canary. --force overrides for corrective anchors.
    date_str = now_utc.strftime("%Y-%m-%d")
    snapshot_path = os.path.join(SNAPSHOTS_DIR, f"{date_str}.json")
    if not args.force and os.path.exists(snapshot_path):
        print(
            f"[signed_registry_snapshot] snapshot exists, skipping: "
            f"{snapshot_path} (--force to overwrite)"
        )
        return 0

    envelope = build_envelope(now_utc)
    canon_hex = canonical_hash_hex(envelope)
    print(
        f"[signed_registry_snapshot] date={envelope['snapshot_date_utc']} "
        f"taxonomy={envelope['taxonomy_version']} "
        f"registry_merkle_root={envelope['history_merkle_root_hex'][:16]}… "
        f"canonical_hash={canon_hex[:16]}…"
    )
    try:
        sig_block = sign_via_sig_service(canon_hex)
    except RuntimeError as e:
        print(
            f"[signed_registry_snapshot] SIGN FAILED: {e}",
            file=sys.stderr, flush=True,
        )
        return 1
    out_path = write_signed_snapshot(envelope, sig_block)
    print(
        f"[signed_registry_snapshot] wrote {out_path} "
        f"signed_by={sig_block['signing_key_fingerprint']}"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
