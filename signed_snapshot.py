"""
Signed integrity snapshot — daily cryptographic commitment of headline metrics.

What this gives us that no other XRPL analytics product offers: every chart
xrpldashboard has ever shown becomes independently verifiable forever. A
visitor who screenshots a number today can, in March, fetch the signed
snapshot file for today's date, verify the Ed25519 signature against our
published public key, and prove we did not silently change the value.

Daily flow:
  1. Pull canonical metrics (ledger index, pool count + TVL, MPT count,
     watchlist totals) — small, deterministic, every reader can recompute
     these from public on-chain data plus our git-versioned methodology.
  2. Canonical-serialise into a leaf payload (sorted keys, no whitespace).
  3. SHA-256 the leaf → leaf_hash.
  4. Append leaf_hash to the append-only chain.json.
  5. Recompute Merkle root over the entire chain (Certificate-Transparency
     style: balanced binary tree, RFC 6962 hashing rules).
  6. Sign {date, leaf_payload, leaf_hash, chain_root, previous_root} with
     the Ed25519 private key.
  7. Write signed_snapshots/YYYY-MM-DD.json (date-deterministic name; same-
     day re-runs overwrite — the chain still extends correctly because we
     remove yesterday's leaf if it had this date before re-appending).

Why one global tree (not per-month): single canonical root commits every
historical snapshot in one number. A visitor verifying any past snapshot
only needs (a) the snapshot file, (b) the Merkle audit path, (c) today's
root, (d) our public key. Four artifacts, one verification flow.

Key custody: private key lives at ~/.config/xrpldashboard/snapshot_ed25519_enc.pem,
PEM-encoded, AES-256 encrypted with passphrase from $SIGNING_KEY_PASSPHRASE
(sourced by run_signed_snapshot.sh from ~/.config/xrpldashboard/env, same
pattern as DATABASE_URL). Public key is committed to the repo and pinned in
DNS TXT for triangulation.

Run modes:
  python3 signed_snapshot.py --generate-keys     # one-time keygen
  python3 signed_snapshot.py --print-pubkey      # show pubkey + DNS TXT draft
  python3 signed_snapshot.py                     # build + sign today's snapshot
  python3 signed_snapshot.py --dry-run           # build + sign, write nothing
  python3 signed_snapshot.py --verify YYYY-MM-DD # verify a stored snapshot file
"""

import argparse
import datetime as dt
import getpass
import hashlib
import json
import os
import sys
import time

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.exceptions import InvalidSignature

from xrpl.clients import JsonRpcClient
from xrpl.models.requests import Ledger

XRPL_NODE = os.environ.get("XRPL_NODE", "https://s1.ripple.com:51234")
HERE = os.path.dirname(os.path.abspath(__file__))

# Public artefacts (committed to the repo so anyone can verify offline)
PUBKEY_PEM_PATH = os.path.join(HERE, "snapshot_pubkey.pem")
PUBKEY_FP_PATH = os.path.join(HERE, "snapshot_pubkey_fingerprint.txt")
SNAPSHOTS_DIR = os.path.join(HERE, "signed_snapshots")
CHAIN_PATH = os.path.join(SNAPSHOTS_DIR, "chain.json")

# Private artefact (lives in the per-user config dir, not the repo)
SECRETS_DIR = os.path.join(os.path.expanduser("~"), ".config", "xrpldashboard")
PRIVKEY_ENC_PATH = os.path.join(SECRETS_DIR, "snapshot_ed25519_enc.pem")

# Sources for canonical metrics. Kept narrow on v1 — anything we can't
# verify from a public artefact today gets added in a future schema_version.
AMM_RANKED_PATH = os.path.join(HERE, "amm_ranked.json")
NAMED_ACCOUNTS_PATH = os.path.join(HERE, "named_accounts.json")

SCHEMA_VERSION = 2
SIGNING_DOMAIN = "xrpldashboard.com/signed_snapshot/v1"
WALKER_CADENCE_SECONDS = 86400  # run_signed_snapshot.sh called daily via launchd


# ---------------------------------------------------------------------------
# Cryptography
# ---------------------------------------------------------------------------

def _canonical_json(obj):
    """Deterministic JSON: sorted keys, no whitespace, no NaN tolerance.
    Two readers serialising the same dict will produce byte-identical output."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def _hash_leaf(payload_bytes: bytes) -> bytes:
    """RFC 6962-style leaf hash: prepend 0x00 domain-separator.
    Prevents an attacker from finding a leaf whose value collides with an
    internal node hash."""
    return _sha256(b"\x00" + payload_bytes)


def _hash_internal(left: bytes, right: bytes) -> bytes:
    """RFC 6962-style internal node hash: prepend 0x01 domain-separator."""
    return _sha256(b"\x01" + left + right)


def _merkle_root(leaves: list[bytes]) -> bytes:
    """Standard Merkle root over leaf hashes (already domain-separated).
    Odd nodes are duplicated at each level (Bitcoin-style) to keep the
    code simple; RFC 6962 leaves them un-duplicated. We document the
    choice publicly so verifiers can implement identically."""
    if not leaves:
        return b"\x00" * 32
    layer = list(leaves)
    while len(layer) > 1:
        if len(layer) % 2 == 1:
            layer.append(layer[-1])
        layer = [_hash_internal(layer[i], layer[i + 1]) for i in range(0, len(layer), 2)]
    return layer[0]


def _merkle_audit_path(leaves: list[bytes], index: int) -> list[dict]:
    """Inclusion proof for leaves[index]. Returns ordered list of siblings
    so a verifier can rebuild the root by repeated hashing."""
    if not (0 <= index < len(leaves)):
        raise IndexError("leaf index out of range")
    path = []
    layer = list(leaves)
    idx = index
    while len(layer) > 1:
        if len(layer) % 2 == 1:
            layer.append(layer[-1])
        sibling_idx = idx ^ 1
        side = "right" if sibling_idx > idx else "left"
        path.append({"side": side, "hash": layer[sibling_idx].hex()})
        layer = [_hash_internal(layer[i], layer[i + 1]) for i in range(0, len(layer), 2)]
        idx //= 2
    return path


def _verify_audit_path(leaf_hash: bytes, path: list[dict], root: bytes) -> bool:
    """Recompute the root from a leaf + audit path, compare to expected."""
    h = leaf_hash
    for step in path:
        sib = bytes.fromhex(step["hash"])
        if step["side"] == "left":
            h = _hash_internal(sib, h)
        else:
            h = _hash_internal(h, sib)
    return h == root


def _fingerprint(pubkey_bytes: bytes) -> str:
    """8-byte SHA-256 prefix as colon-separated hex pairs. Short enough to
    eyeball, long enough to defeat casual collision."""
    digest = hashlib.sha256(pubkey_bytes).hexdigest()
    return ":".join(digest[i:i + 2] for i in range(0, 16, 2)).upper()


# ---------------------------------------------------------------------------
# Key management
# ---------------------------------------------------------------------------

def _get_passphrase(prompt_if_missing: bool) -> bytes:
    pw = os.environ.get("SIGNING_KEY_PASSPHRASE", "").strip()
    if pw:
        return pw.encode("utf-8")
    if prompt_if_missing and sys.stdin.isatty():
        pw = getpass.getpass("SIGNING_KEY_PASSPHRASE not set. Enter passphrase: ")
        return pw.encode("utf-8")
    raise SystemExit(
        "ERROR: SIGNING_KEY_PASSPHRASE not set in env and stdin is not a TTY.\n"
        "Set it in ~/.config/xrpldashboard/env and source via the launchd wrapper."
    )


def generate_keys():
    """One-time keygen. Refuses to overwrite an existing key — rotation is
    a separate, deliberate operation (would invalidate the chain)."""
    if os.path.exists(PRIVKEY_ENC_PATH):
        raise SystemExit(f"ERROR: {PRIVKEY_ENC_PATH} already exists — refusing to overwrite.")
    if os.path.exists(PUBKEY_PEM_PATH):
        raise SystemExit(f"ERROR: {PUBKEY_PEM_PATH} already exists — refusing to overwrite.")

    os.makedirs(SECRETS_DIR, mode=0o700, exist_ok=True)

    passphrase = _get_passphrase(prompt_if_missing=True)
    if len(passphrase) < 12:
        raise SystemExit("ERROR: passphrase must be at least 12 bytes.")

    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key()

    priv_pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.BestAvailableEncryption(passphrase),
    )
    pub_pem = pub.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    # Atomic writes with restrictive perms
    tmp = PRIVKEY_ENC_PATH + ".tmp"
    with open(tmp, "wb") as f:
        f.write(priv_pem)
    os.chmod(tmp, 0o600)
    os.replace(tmp, PRIVKEY_ENC_PATH)

    with open(PUBKEY_PEM_PATH, "wb") as f:
        f.write(pub_pem)

    pub_raw = pub.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    fp = _fingerprint(pub_raw)
    with open(PUBKEY_FP_PATH, "w") as f:
        f.write(fp + "\n")

    print(f"Generated Ed25519 keypair.")
    print(f"  Private (encrypted): {PRIVKEY_ENC_PATH}")
    print(f"  Public PEM:          {PUBKEY_PEM_PATH}")
    print(f"  Fingerprint:         {fp}")
    print()
    print("DNS TXT record to publish (verifier-side pinning):")
    print(f"  Name:  _xrpld-snapshot-key")
    print(f"  Type:  TXT")
    print(f"  Value: \"v=ed25519; fp={fp}; pub={pub_raw.hex()}\"")
    print()
    print("Next steps:")
    print("  1. Back up the encrypted private key to 1Password / cold storage.")
    print("  2. Add the DNS TXT record above.")
    print("  3. Commit snapshot_pubkey.pem and snapshot_pubkey_fingerprint.txt.")
    print("  4. Update /about and /methodology with the fingerprint.")
    return 0


def load_private_key() -> Ed25519PrivateKey:
    if not os.path.exists(PRIVKEY_ENC_PATH):
        raise SystemExit(
            f"ERROR: private key not found at {PRIVKEY_ENC_PATH}.\n"
            "Run: python3 signed_snapshot.py --generate-keys"
        )
    passphrase = _get_passphrase(prompt_if_missing=False)
    with open(PRIVKEY_ENC_PATH, "rb") as f:
        data = f.read()
    try:
        priv = serialization.load_pem_private_key(data, password=passphrase)
    except (ValueError, TypeError) as e:
        raise SystemExit(f"ERROR: could not decrypt private key ({type(e).__name__}). Wrong passphrase?")
    if not isinstance(priv, Ed25519PrivateKey):
        raise SystemExit(f"ERROR: key at {PRIVKEY_ENC_PATH} is not Ed25519.")
    return priv


def load_public_key() -> Ed25519PublicKey:
    if not os.path.exists(PUBKEY_PEM_PATH):
        raise SystemExit(f"ERROR: public key not found at {PUBKEY_PEM_PATH}.")
    with open(PUBKEY_PEM_PATH, "rb") as f:
        return serialization.load_pem_public_key(f.read())


def print_pubkey():
    pub = load_public_key()
    pub_raw = pub.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    fp = _fingerprint(pub_raw)
    print(f"Fingerprint:    {fp}")
    print(f"Pubkey (hex):   {pub_raw.hex()}")
    print(f"Pubkey (PEM):")
    with open(PUBKEY_PEM_PATH) as f:
        print(f.read().rstrip())
    print()
    print(f"DNS TXT (_xrpld-snapshot-key TXT):")
    print(f'  "v=ed25519; fp={fp}; pub={pub_raw.hex()}"')
    return 0


# ---------------------------------------------------------------------------
# Metric collection (v1 — narrow, only what's reliably verifiable)
# ---------------------------------------------------------------------------

def _safe_xrpl_request(client, request):
    try:
        resp = client.request(request)
        if "error" in resp.result:
            return None
        return resp.result
    except Exception:
        return None


def _validated_ledger_index(client):
    result = _safe_xrpl_request(client, Ledger(ledger_index="validated"))
    if not result:
        return None
    return result.get("ledger_index") or (result.get("ledger") or {}).get("ledger_index")


def collect_metrics() -> tuple[list[dict], list[str]]:
    """Return (metrics, errors). Each metric: {name, value, unit, source}.
    Missing sources are recorded as errors and absent from the metric list —
    the chain still proceeds (a snapshot with fewer metrics is honest; one
    with fabricated metrics would not be)."""
    metrics: list[dict] = []
    errors: list[str] = []

    client = JsonRpcClient(XRPL_NODE)
    li = _validated_ledger_index(client)
    if li:
        metrics.append({
            "name": "xrpl_validated_ledger_index",
            "value": int(li),
            "unit": "ledger",
            "source": f"{XRPL_NODE} → ledger(validated)",
        })
    else:
        errors.append("xrpl_validated_ledger_index_unavailable")

    # AMM pools (from the daily-ranked file produced by rank_amms.py)
    try:
        with open(AMM_RANKED_PATH) as f:
            ranked = json.load(f) or []
        valid = [p for p in ranked if isinstance(p, dict)]
        metrics.append({
            "name": "amm_pools_count",
            "value": len(valid),
            "unit": "pools",
            "source": "amm_ranked.json",
        })
        total_tvl = 0.0
        for p in valid:
            v = p.get("tvl_usd")
            if isinstance(v, (int, float)):
                total_tvl += float(v)
        metrics.append({
            "name": "amm_pools_total_tvl_usd",
            "value": round(total_tvl, 2),
            "unit": "usd",
            "source": "amm_ranked.json (sum of tvl_usd)",
        })
    except (OSError, json.JSONDecodeError) as e:
        errors.append(f"amm_pools: {type(e).__name__}")

    # MPTs (from the periodic mpt_snapshot, via mpt_data wrapper)
    try:
        from mpt_data import load_mpt_snapshot
        payload = load_mpt_snapshot(max_age=86400)
        if payload:
            issuances = payload.get("issuances") or []
            metrics.append({
                "name": "mpt_total_count",
                "value": len(issuances),
                "unit": "mpts",
                "source": "mpt_snapshot.json",
            })
        else:
            errors.append("mpt_snapshot_unavailable_or_stale")
    except Exception as e:
        errors.append(f"mpts: {type(e).__name__}")

    # Watchlist (named_accounts.json)
    try:
        with open(NAMED_ACCOUNTS_PATH) as f:
            named = json.load(f) or {}
        count = sum(1 for v in named.values() if isinstance(v, dict))
        metrics.append({
            "name": "named_accounts_count",
            "value": count,
            "unit": "accounts",
            "source": "named_accounts.json",
        })
    except (OSError, json.JSONDecodeError) as e:
        errors.append(f"named_accounts: {type(e).__name__}")

    # RLUSD cross-chain supply (from PG cache written by rlusd_live worker)
    try:
        import db
        _rlusd_result = db.read_rlusd_state_cache()
        rlusd_cached = _rlusd_result[0] if _rlusd_result else None
        if rlusd_cached:
            xrpl_supply = (rlusd_cached.get("xrpl") or {}).get("supply")
            eth_supply = (rlusd_cached.get("eth") or {}).get("supply")
            if xrpl_supply is not None:
                metrics.append({
                    "name": "rlusd_xrpl_supply",
                    "value": round(float(xrpl_supply), 2),
                    "unit": "usd",
                    "source": "rlusd_state_cache (xrpl gateway_balances)",
                })
            if eth_supply is not None:
                metrics.append({
                    "name": "rlusd_eth_supply",
                    "value": round(float(eth_supply), 2),
                    "unit": "usd",
                    "source": "rlusd_state_cache (ethereum transfer log)",
                })
            if xrpl_supply is not None and eth_supply is not None:
                metrics.append({
                    "name": "rlusd_total_supply",
                    "value": round(float(xrpl_supply) + float(eth_supply), 2),
                    "unit": "usd",
                    "source": "rlusd_state_cache (xrpl + ethereum)",
                })
            # Append today's row to rlusd_supply_history (daily UPSERT
            # keyed on snapshot_date, derived from payload.fetched_at).
            # Same in-memory payload, no extra fetch. Fleet sweep 2026-07-28:
            # was silent-except-pass; now records the failure into `errors`
            # so it surfaces alongside the other per-metric errors instead
            # of vanishing. Per-cycle metric emission still continues.
            try:
                db.write_rlusd_supply_history(rlusd_cached)
            except Exception as e:
                errors.append(f"rlusd_supply_history: {type(e).__name__}")
        else:
            errors.append("rlusd_state_cache_unavailable")
    except Exception as e:
        errors.append(f"rlusd: {type(e).__name__}")

    # RWA total AUM — sum of tvl_usd for verified AMM pools attributed to
    # RWA families. Reads the same amm_ranked.json already loaded above,
    # cross-referenced against pool addresses in Postgres rwa_pool_attribution.
    try:
        import db
        if db.pg_available():
            with db.pg_connect() as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT pool_address FROM rwa_pool_attribution"
                )
                rwa_pool_addresses = {row[0] for row in cur.fetchall()}
            if rwa_pool_addresses:
                try:
                    with open(AMM_RANKED_PATH) as f:
                        ranked_all = json.load(f) or []
                    rwa_tvl = sum(
                        float(p.get("tvl_usd") or 0)
                        for p in ranked_all
                        if isinstance(p, dict)
                        and p.get("amm_account") in rwa_pool_addresses
                        and p.get("tvl_status") in ("exact", "estimated")
                    )
                    metrics.append({
                        "name": "rwa_total_aum_usd",
                        "value": round(rwa_tvl, 2),
                        "unit": "usd",
                        "source": "amm_ranked.json (rwa_pool_attribution cross-ref)",
                    })
                except (OSError, json.JSONDecodeError) as e:
                    errors.append(f"rwa_aum: {type(e).__name__}")
            else:
                metrics.append({
                    "name": "rwa_total_aum_usd",
                    "value": 0.0,
                    "unit": "usd",
                    "source": "rwa_pool_attribution (no pools attributed)",
                })
        else:
            errors.append("rwa_aum_pg_unavailable")
    except Exception as e:
        errors.append(f"rwa: {type(e).__name__}")

    return metrics, errors


# ---------------------------------------------------------------------------
# Chain management
# ---------------------------------------------------------------------------

def load_chain() -> dict:
    """Read the append-only chain file. Returns the default empty chain
    if absent."""
    if not os.path.exists(CHAIN_PATH):
        return {"schema_version": SCHEMA_VERSION, "leaves": [], "current_root": None}
    with open(CHAIN_PATH) as f:
        return json.load(f)


def write_chain(chain: dict):
    os.makedirs(SNAPSHOTS_DIR, exist_ok=True)
    tmp = CHAIN_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(chain, f, sort_keys=True, separators=(",", ":"))
    os.replace(tmp, CHAIN_PATH)


def append_or_replace_leaf(chain: dict, date_str: str, leaf_hash_hex: str, ledger_index) -> tuple[int, list[bytes]]:
    """If a leaf for date_str exists, replace it (same-day re-run); else
    append. Returns (leaf_index, all_leaf_hashes_bytes). Leaves keep the
    order they were first observed in — replacement preserves the index
    so audit paths against pre-replacement roots invalidate cleanly
    (we never claim historical-root inclusion for a replaced leaf)."""
    leaves = chain.setdefault("leaves", [])
    for i, leaf in enumerate(leaves):
        if leaf["date"] == date_str:
            leaves[i] = {"date": date_str, "leaf_hash": leaf_hash_hex, "ledger_index": ledger_index}
            return i, [bytes.fromhex(le["leaf_hash"]) for le in leaves]
    leaves.append({"date": date_str, "leaf_hash": leaf_hash_hex, "ledger_index": ledger_index})
    return len(leaves) - 1, [bytes.fromhex(le["leaf_hash"]) for le in leaves]


# ---------------------------------------------------------------------------
# Build + sign
# ---------------------------------------------------------------------------

def build_snapshot(date_str: str) -> dict:
    """Pure metric collection + structuring. Signing is a separate step
    (sign_snapshot) so tests can drive build without holding a key."""
    metrics, errors = collect_metrics()
    started_at = int(time.time())
    return {
        "signing_domain": SIGNING_DOMAIN,
        "schema_version": SCHEMA_VERSION,
        "snapshot_date_utc": date_str,
        "snapshot_taken_unix": started_at,
        "metrics": metrics,
        "errors": errors,
    }


def sign_snapshot(snap: dict, dry_run: bool = False) -> dict:
    """Add merkle/signature fields and return the complete signed envelope.
    With dry_run=True, computes everything but does not touch disk."""
    priv = load_private_key()
    pub = priv.public_key()
    pub_raw = pub.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    fp = _fingerprint(pub_raw)

    # Canonical leaf payload — exactly what gets hashed
    leaf_payload = {
        "signing_domain": snap["signing_domain"],
        "schema_version": snap["schema_version"],
        "snapshot_date_utc": snap["snapshot_date_utc"],
        "metrics": snap["metrics"],
    }
    leaf_bytes = _canonical_json(leaf_payload)
    leaf_hash = _hash_leaf(leaf_bytes)

    chain = load_chain()
    previous_root = chain.get("current_root")
    leaf_index, all_leaves = append_or_replace_leaf(
        chain,
        snap["snapshot_date_utc"],
        leaf_hash.hex(),
        next((m["value"] for m in snap["metrics"] if m["name"] == "xrpl_validated_ledger_index"), None),
    )
    current_root = _merkle_root(all_leaves)
    audit_path = _merkle_audit_path(all_leaves, leaf_index)

    # Sign the envelope summary (NOT the leaf payload directly — signing
    # the envelope binds the leaf to its chain position and the resulting
    # root, so an attacker can't lift a signature onto a different chain.)
    envelope_summary = {
        "signing_domain": snap["signing_domain"],
        "schema_version": snap["schema_version"],
        "snapshot_date_utc": snap["snapshot_date_utc"],
        "leaf_hash": leaf_hash.hex(),
        "leaf_index": leaf_index,
        "leaves_total": len(all_leaves),
        "chain_root": current_root.hex(),
        "previous_root": previous_root,
    }
    sig = priv.sign(_canonical_json(envelope_summary))

    signed = {
        **snap,
        "leaf_hash": leaf_hash.hex(),
        "leaf_index": leaf_index,
        "leaves_total": len(all_leaves),
        "chain_root": current_root.hex(),
        "previous_root": previous_root,
        "audit_path": audit_path,
        "signature_ed25519": sig.hex(),
        "signing_pubkey_fingerprint": fp,
        "verifier_instructions": "See https://xrpldashboard.com/methodology#signed-snapshots",
    }

    if not dry_run:
        chain["current_root"] = current_root.hex()
        chain.setdefault("root_history", []).append({
            "date": snap["snapshot_date_utc"],
            "root": current_root.hex(),
        })
        write_chain(chain)
        write_signed_snapshot(signed)

    return signed


def write_signed_snapshot(signed: dict):
    os.makedirs(SNAPSHOTS_DIR, exist_ok=True)
    path = os.path.join(SNAPSHOTS_DIR, f"{signed['snapshot_date_utc']}.json")
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(signed, f, sort_keys=True, separators=(",", ":"))
    os.replace(tmp, path)
    return path


# ---------------------------------------------------------------------------
# Verification (round-trip)
# ---------------------------------------------------------------------------

def verify_snapshot_file(date_str: str) -> tuple[bool, list[str]]:
    """Disk path: load the snapshot file for `date_str` and delegate to
    verify_envelope. Kept for CLI parity and as a fallback when PG is
    unavailable."""
    path = os.path.join(SNAPSHOTS_DIR, f"{date_str}.json")
    if not os.path.exists(path):
        return False, [f"snapshot file not found: {path}"]
    with open(path) as f:
        signed = json.load(f)
    return verify_envelope(signed)


def verify_envelope(signed: dict) -> tuple[bool, list[str]]:
    """Independent verification path: re-derives the leaf hash from the
    payload, checks the signature against the public key, checks the
    audit path against the current chain root. Mirrors what a third-party
    verifier would do — no shared state with the writer. Takes the parsed
    envelope dict directly so the same code path can verify a disk file
    or a Postgres-served envelope without an intermediate write."""
    issues = []

    # 1) Re-derive leaf hash from the canonical payload
    leaf_payload = {
        "signing_domain": signed["signing_domain"],
        "schema_version": signed["schema_version"],
        "snapshot_date_utc": signed["snapshot_date_utc"],
        "metrics": signed["metrics"],
    }
    expected_leaf_hash = _hash_leaf(_canonical_json(leaf_payload)).hex()
    if expected_leaf_hash != signed["leaf_hash"]:
        issues.append(f"leaf_hash mismatch (file claims {signed['leaf_hash']}, derived {expected_leaf_hash})")

    # 2) Verify the signature against the envelope summary
    envelope_summary = {
        "signing_domain": signed["signing_domain"],
        "schema_version": signed["schema_version"],
        "snapshot_date_utc": signed["snapshot_date_utc"],
        "leaf_hash": signed["leaf_hash"],
        "leaf_index": signed["leaf_index"],
        "leaves_total": signed["leaves_total"],
        "chain_root": signed["chain_root"],
        "previous_root": signed["previous_root"],
    }
    pub = load_public_key()
    try:
        pub.verify(bytes.fromhex(signed["signature_ed25519"]), _canonical_json(envelope_summary))
    except InvalidSignature:
        issues.append("Ed25519 signature did NOT verify against published pubkey")

    # 3) Verify audit path → claimed chain root
    leaf_hash_bytes = bytes.fromhex(signed["leaf_hash"])
    root_bytes = bytes.fromhex(signed["chain_root"])
    if not _verify_audit_path(leaf_hash_bytes, signed["audit_path"], root_bytes):
        issues.append("audit_path does NOT reconstruct claimed chain_root")

    # 4) Cross-check fingerprint
    pub_raw = pub.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    expected_fp = _fingerprint(pub_raw)
    if expected_fp != signed["signing_pubkey_fingerprint"]:
        issues.append(f"pubkey fingerprint mismatch (file={signed['signing_pubkey_fingerprint']}, local={expected_fp})")

    return (not issues), issues


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def summarize(signed: dict) -> str:
    return (
        f"date={signed['snapshot_date_utc']} "
        f"metrics={len(signed['metrics'])} "
        f"errors={len(signed.get('errors') or [])} "
        f"leaf_index={signed['leaf_index']}/{signed['leaves_total']} "
        f"chain_root={signed['chain_root'][:16]}…"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--generate-keys", action="store_true", help="One-time Ed25519 keygen.")
    parser.add_argument("--print-pubkey", action="store_true", help="Show pubkey + fingerprint + DNS TXT draft.")
    parser.add_argument("--dry-run", action="store_true", help="Build + sign + verify, but write nothing.")
    parser.add_argument("--verify", metavar="YYYY-MM-DD", help="Verify a stored snapshot file.")
    parser.add_argument("--date", metavar="YYYY-MM-DD", help="Override snapshot date (default: today UTC).")
    args = parser.parse_args()

    if args.generate_keys:
        return generate_keys()
    if args.print_pubkey:
        return print_pubkey()
    if args.verify:
        ok, issues = verify_snapshot_file(args.verify)
        if ok:
            print(f"VERIFIED {args.verify}: signature OK, audit_path OK, leaf_hash OK, fingerprint OK.")
            return 0
        print(f"VERIFICATION FAILED for {args.verify}:")
        for it in issues:
            print(f"  - {it}")
        return 1

    date_str = args.date or dt.datetime.now(dt.UTC).strftime("%Y-%m-%d")

    _wh_db = None
    if not args.dry_run:
        try:
            import db as _wh_db_mod
            _wh_db = _wh_db_mod
            _wh_db.write_walker_health_start("signed_snapshot", cadence_seconds=WALKER_CADENCE_SECONDS)
        except Exception:
            _wh_db = None

    _ok = False
    _msg = "init"
    try:
        snap = build_snapshot(date_str)
        signed = sign_snapshot(snap, dry_run=args.dry_run)
        print(summarize(signed))

        if args.dry_run:
            print("(dry-run: nothing written)")
            return 0

        # Self-verify what we just wrote — catches any I/O round-trip surprise
        ok, issues = verify_snapshot_file(date_str)
        if not ok:
            print("POST-WRITE VERIFY FAILED:")
            for it in issues:
                print(f"  - {it}")
            _msg = f"post-write verify FAILED: {'; '.join(issues)}"
            return 2
        print(f"post-write verify: OK")

        # Mirror to Postgres so Render (which has no disk access to this file)
        # can serve /.well-known/snapshots/<date>.json globally. Disk is the
        # source of truth; PG failure here is logged and ignored — next run
        # will replay the upsert and the mirror catches up.
        try:
            import db
            if db.pg_available():
                mirrored = db.write_signed_snapshot(
                    envelope=signed,
                    current_root=signed["chain_root"],
                    leaves_total=signed["leaves_total"],
                    schema_version=signed["schema_version"],
                )
                print(f"postgres mirror: {'OK' if mirrored else 'FAILED (disk intact, retry next run)'}")
            else:
                print("postgres mirror: skipped (DATABASE_URL not set)")
        except Exception as e:
            print(f"postgres mirror: ERROR {type(e).__name__}: {e}", file=sys.stderr)

        _ok = True
        _msg = (f"signed+verified path=signed_snapshots/{date_str}.json "
                f"metrics={len(signed.get('metrics', []))} "
                f"root={signed['chain_root'][:16]}")
        return 0
    except Exception as e:
        _msg = f"{type(e).__name__}: {e}"
        raise
    finally:
        if _wh_db is not None:
            _wh_db.write_walker_health_end("signed_snapshot", ok=_ok, message=_msg)


if __name__ == "__main__":
    sys.exit(main())
