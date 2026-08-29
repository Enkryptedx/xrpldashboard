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
import re
import subprocess
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
CLAIMS_YAML_PATH = os.path.join(HERE, "CLAIMS.yaml")
APP_PY_PATH = os.path.join(HERE, "app.py")

SCHEMA_VERSION = 4

# v4 walker-health-summary thresholds (mirror /walker_health severity buckets;
# if these drift from the app-side page the digest becomes worthless as
# cross-check evidence, so any change here MUST update the /walker_health
# code path in the same commit — anti-Layer-4 lesson from the design doc).
WALKER_STATE_GREEN_MAX_CADENCE_MULTIPLE = 2   # age ≤ 2×cadence AND ok=true
WALKER_STATE_STALE_MAX_CADENCE_MULTIPLE = 8   # 2×cadence < age ≤ 8×cadence
WALKER_STATE_DEAD_CONSECUTIVE_FAILURES = 3    # ≥3 failures → dead regardless of age
SIGNING_DOMAIN = "xrpldashboard.com/signed_snapshot/v1"
WALKER_CADENCE_SECONDS = 86400  # run_signed_snapshot.sh called daily via launchd

# Self-describing verification URLs published inside every signed artifact
# (schema_version 3+) and inside chain.json. A verifier landing on any single
# JSON reaches both the key and the spec in one hop — no external navigation
# needed. Added 2026-08-09 in response to external-audit finding that the
# fingerprint was present but the key location and canonical-serialization
# spec were not reachable from the artifact itself.
PUBKEY_URL = "https://xrpldashboard.com/.well-known/snapshots/pubkey.pem"
VERIFIER_SPEC_URL = "https://xrpldashboard.com/methodology#signed-snapshots"


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


# ---------------------------------------------------------------------------
# v4 metric collectors — walker_health_summary, claims_index_state,
# editorial_state. Each takes an explicit `now_utc` (frozen at build-time by
# `build_snapshot`) so two dry-runs within one wall-clock second produce
# byte-identical digests (acceptance gate 4d). Each RAISES on SoT failure
# (strict-refuse per §5 ruling — never stamp a guess).
# ---------------------------------------------------------------------------

def _walker_state(row: dict, now_utc: dt.datetime) -> tuple[str, float | None]:
    """Classify one walker_health row into (state, age_multiples_of_cadence).

    state ∈ {"green", "stale", "dead"}. Cadence-less rows collapse to dead
    with age_multiples=None so the digest flips immediately if a walker is
    ever registered without declaring its cadence (schema violation that
    should be visible on-chain, not silently averaged in)."""
    cadence = row.get("cadence_seconds")
    consecutive = row.get("consecutive_failures") or 0
    ok = bool(row.get("last_run_ok"))
    last_success = row.get("last_success_at")

    if consecutive >= WALKER_STATE_DEAD_CONSECUTIVE_FAILURES:
        return "dead", None if cadence in (None, 0) else round(
            _age_multiples(now_utc, last_success, cadence), 1
        )

    if cadence in (None, 0) or last_success is None:
        return "dead", None

    multiples = _age_multiples(now_utc, last_success, cadence)
    multiples_1dp = round(multiples, 1)

    if ok and multiples <= WALKER_STATE_GREEN_MAX_CADENCE_MULTIPLE:
        return "green", multiples_1dp
    if multiples > WALKER_STATE_STALE_MAX_CADENCE_MULTIPLE:
        return "dead", multiples_1dp
    return "stale", multiples_1dp


def _age_multiples(now_utc: dt.datetime, last_success: dt.datetime, cadence_seconds: int) -> float:
    """Age in units of the walker's declared cadence. now_utc is frozen at
    build-time; last_success comes from the row. Any tz-naive datetime is
    treated as UTC (walker_health stores TIMESTAMPTZ but read_walker_health_all
    surfaces naive datetimes in some code paths — defensive coercion)."""
    if last_success.tzinfo is None:
        last_success = last_success.replace(tzinfo=dt.timezone.utc)
    age_seconds = (now_utc - last_success).total_seconds()
    if age_seconds < 0:
        age_seconds = 0.0
    return age_seconds / float(cadence_seconds)


def collect_walker_health_summary(now_utc: dt.datetime, read_walker_health_all=None) -> dict:
    """v4 metric collector. Reads the full walker_health table via
    db.read_walker_health_all() (dependency-injected for tests), classifies
    each row against the same thresholds /walker_health uses, and returns
    the {name, value, unit, source} metric. Raises SystemExit on strict-
    refuse (empty read, PG unavailable, unreadable) — the whole point of
    this metric is proof-of-our-own-machinery-health; stamping without it
    would lie about the very thing it commits to."""
    if read_walker_health_all is None:
        import db
        if not db.pg_available():
            raise SystemExit(
                "STRICT-REFUSE (v4 §5a): walker_health_summary requires PG; "
                "DATABASE_URL not set / pg_available()=False"
            )
        read_walker_health_all = db.read_walker_health_all

    try:
        rows = read_walker_health_all()
    except Exception as e:
        raise SystemExit(
            f"STRICT-REFUSE (v4 §5a): walker_health_summary read failed: "
            f"{type(e).__name__}: {e}"
        )

    if not rows:
        raise SystemExit(
            "STRICT-REFUSE (v4 §5a): walker_health_summary got 0 rows — "
            "either walker_health table is empty or the read silently failed"
        )

    detail = []
    for row in sorted(rows, key=lambda r: r["walker_name"]):
        state, multiples = _walker_state(row, now_utc)
        detail.append({
            "walker": row["walker_name"],
            "state": state,
            "consecutive_failures": int(row.get("consecutive_failures") or 0),
            "age_multiples_of_cadence": multiples,
        })

    digest = hashlib.sha256(_canonical_json(detail)).hexdigest()
    counts = {"green": 0, "stale": 0, "dead": 0}
    for entry in detail:
        counts[entry["state"]] += 1

    return {
        "name": "walker_health_summary",
        "value": {
            "total_walkers": len(detail),
            "green_count": counts["green"],
            "stale_count": counts["stale"],
            "dead_count": counts["dead"],
            "walkers_digest_sha256": digest,
        },
        "unit": "walkers",
        "source": "walker_health table via db.read_walker_health_all()",
    }


def collect_claims_index_state(claims_yaml_path: str = None, git_short_reader=None) -> dict:
    """v4 metric collector. SHA of CLAIMS.yaml + git-short of the last
    commit that touched it + structural counts (page_count, claim_count).
    Raises SystemExit on strict-refuse (file missing, unparseable, git
    unavailable). Byte-hash + counts are redundant on purpose — defense
    in depth per §1b ruling."""
    path = claims_yaml_path or CLAIMS_YAML_PATH
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as e:
        raise SystemExit(
            f"STRICT-REFUSE (v4 §5b): claims_index_state cannot read "
            f"{path}: {type(e).__name__}: {e}"
        )

    sha = hashlib.sha256(raw).hexdigest()

    try:
        import yaml
    except ImportError as e:
        raise SystemExit(
            f"STRICT-REFUSE (v4 §5b): claims_index_state needs PyYAML "
            f"to parse {path}: {e}"
        )

    try:
        doc = yaml.safe_load(raw) or {}
    except yaml.YAMLError as e:
        raise SystemExit(
            f"STRICT-REFUSE (v4 §5b): claims_index_state cannot parse "
            f"{path}: {type(e).__name__}: {e}"
        )

    pages = doc.get("pages") or {}
    if not isinstance(pages, dict):
        raise SystemExit(
            f"STRICT-REFUSE (v4 §5b): claims_index_state got non-dict "
            f"'pages' key in {path}"
        )

    page_count = len(pages)
    claim_count = 0
    for page in pages.values():
        if isinstance(page, dict):
            claims = page.get("claims") or []
            if isinstance(claims, list):
                claim_count += len(claims)

    if git_short_reader is None:
        def git_short_reader(p):
            try:
                out = subprocess.check_output(
                    ["git", "log", "-n1", "--format=%h", "--", p],
                    cwd=HERE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    timeout=10,
                )
                return out.strip()
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
                raise RuntimeError(f"git log failed: {type(e).__name__}: {e}")

    try:
        git_short = git_short_reader(path)
    except Exception as e:
        raise SystemExit(
            f"STRICT-REFUSE (v4 §5b): claims_index_state cannot read "
            f"git-short for {path}: {e}"
        )

    if not git_short:
        raise SystemExit(
            f"STRICT-REFUSE (v4 §5b): claims_index_state got empty "
            f"git-short for {path}"
        )

    return {
        "name": "claims_index_state",
        "value": {
            "page_count": page_count,
            "claim_count": claim_count,
            "claims_yaml_sha256": sha,
            "claims_yaml_git_short": git_short,
        },
        "unit": "claims",
        "source": "CLAIMS.yaml file hash + git log",
    }


_LAST_VERIFIED_RE = re.compile(
    r'^LAST_VERIFIED_(\w+)\s*=\s*"(\d{4}-\d{2}-\d{2})"',
    flags=re.M,
)


def collect_editorial_state(app_py_path: str = None) -> dict:
    """v4 metric collector — FRESHNESS-ONLY (§1c ruling 2026-08-27 22:22 ET).
    Regex-enumerates every `LAST_VERIFIED_* = "YYYY-MM-DD"` constant in
    app.py source and commits them as a sort_keys-ordered nested dict.

    New LAST_VERIFIED_* constants added later are picked up automatically
    without touching this file. Correction/wound registries are deferred
    to v5 (separate design sitting).

    Raises SystemExit on strict-refuse (app.py unreadable OR zero stamps
    found — a codebase that lost all its freshness stamps between v4 ship
    and this run is a signal, not a normal state)."""
    path = app_py_path or APP_PY_PATH
    try:
        with open(path, "r", encoding="utf-8") as f:
            source = f.read()
    except OSError as e:
        raise SystemExit(
            f"STRICT-REFUSE (v4 §5c): editorial_state cannot read "
            f"{path}: {type(e).__name__}: {e}"
        )

    stamps: dict[str, str] = {}
    for match in _LAST_VERIFIED_RE.finditer(source):
        name, date = match.group(1), match.group(2)
        stamps[f"LAST_VERIFIED_{name}"] = date

    if not stamps:
        raise SystemExit(
            f"STRICT-REFUSE (v4 §5c): editorial_state found zero "
            f"LAST_VERIFIED_* constants in {path} — either the source "
            f"moved or every freshness stamp was removed"
        )

    return {
        "name": "editorial_state",
        "value": {
            "last_verified_stamps": stamps,
        },
        "unit": "editorial",
        "source": "app.py LAST_VERIFIED_* constants (regex-enumerated at stamp time)",
    }


def collect_metrics(now_utc: dt.datetime | None = None) -> tuple[list[dict], list[str]]:
    """Return (metrics, errors). Each metric: {name, value, unit, source}.
    Missing sources for v1-v3 metrics are recorded as errors and absent
    from the metric list — the chain still proceeds (a snapshot with fewer
    metrics is honest; one with fabricated metrics would not be).

    v4 metrics (walker_health_summary, claims_index_state, editorial_state)
    use STRICT-REFUSE semantics per §5 — a missing v4 SoT raises SystemExit,
    because those metrics ARE the proof-of-our-own-machinery-health and
    stamping without them would silently lie about the very thing they
    commit to.

    now_utc is frozen at the top of build_snapshot and threaded into
    walker_health_summary so age_multiples_of_cadence doesn't drift across
    consecutive dry-runs (acceptance gate 4d)."""
    if now_utc is None:
        now_utc = dt.datetime.now(dt.timezone.utc)

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

    # v4 metrics — appended at end (§2 insertion-order ruling). Each raises
    # SystemExit on SoT failure (§5 strict-refuse); we do NOT swallow into
    # `errors` because the whole point of these three is proof-of-our-own-
    # machinery-health, and a silently-absent metric would defeat that.
    metrics.append(collect_walker_health_summary(now_utc))
    metrics.append(collect_claims_index_state())
    metrics.append(collect_editorial_state())

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

def build_snapshot(date_str: str, now_utc: dt.datetime | None = None) -> dict:
    """Pure metric collection + structuring. Signing is a separate step
    (sign_snapshot) so tests can drive build without holding a key.

    now_utc is frozen ONCE here and threaded through collect_metrics so
    every v4 metric that uses "current time" (walker age vs cadence) sees
    the same instant. Two dry-runs within a wall-clock second must produce
    byte-identical digests — acceptance gate 4d. Callers that need to
    control time in tests can pass now_utc explicitly."""
    if now_utc is None:
        now_utc = dt.datetime.now(dt.timezone.utc)
    metrics, errors = collect_metrics(now_utc=now_utc)
    started_at = int(now_utc.timestamp())
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
        "pubkey_url": PUBKEY_URL,
        "verifier_spec_url": VERIFIER_SPEC_URL,
        "verifier_instructions": f"See {VERIFIER_SPEC_URL}",
    }

    if not dry_run:
        _update_schema_version_history(chain, snap["snapshot_date_utc"])
        chain["schema_version"] = SCHEMA_VERSION
        chain["pubkey_url"] = PUBKEY_URL
        chain["verifier_spec_url"] = VERIFIER_SPEC_URL
        chain["current_root"] = current_root.hex()
        chain.setdefault("root_history", []).append({
            "date": snap["snapshot_date_utc"],
            "root": current_root.hex(),
        })
        write_chain(chain)
        write_signed_snapshot(signed)

    return signed


def _update_schema_version_history(chain: dict, snapshot_date: str):
    """Maintain chain.json's schema_version_history array (§3 ruling).
    Records the version transition: closes the outgoing version's row
    with last_snapshot_date, opens the incoming version's row with
    first_snapshot_date. Idempotent — re-runs of the same-day stamp
    don't duplicate rows."""
    history = chain.setdefault("schema_version_history", [])
    prev_version = chain.get("schema_version")

    if prev_version is not None and prev_version != SCHEMA_VERSION:
        # Close the outgoing version's row IF not already recorded.
        closed = any(
            row.get("version") == prev_version and "last_snapshot_date" in row
            for row in history
        )
        if not closed:
            history.append({
                "version": prev_version,
                "last_snapshot_date": _last_snapshot_date_for_version(
                    chain, prev_version, snapshot_date
                ),
            })
        opened = any(
            row.get("version") == SCHEMA_VERSION and "first_snapshot_date" in row
            for row in history
        )
        if not opened:
            history.append({
                "version": SCHEMA_VERSION,
                "first_snapshot_date": snapshot_date,
            })
    elif not history and prev_version == SCHEMA_VERSION:
        # First run under a new-version chain that predates the history
        # array — bootstrap a single row so future transitions have context.
        history.append({
            "version": SCHEMA_VERSION,
            "first_snapshot_date": snapshot_date,
        })


def _last_snapshot_date_for_version(chain: dict, prev_version: int, incoming_date: str) -> str:
    """Best-effort last-snapshot date under the outgoing version. We don't
    record per-leaf schema_version in chain.json today, so the closest
    honest answer is the newest leaf date strictly before today's stamp.
    Falls back to the incoming date if the chain has no prior leaves
    (edge case: brand-new chain jumping straight to v4)."""
    prior_dates = [
        leaf["date"] for leaf in chain.get("leaves", [])
        if leaf.get("date") and leaf["date"] < incoming_date
    ]
    if prior_dates:
        return max(prior_dates)
    return incoming_date


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
