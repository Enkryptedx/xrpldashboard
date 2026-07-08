"""Baseline ledger-vocabulary snapshotter — Phase 0 of Total Ledger
Awareness.

Fetches `server_definitions` (machine-readable vocabulary the node itself
advertises) and `server_info.build_version` from the LOCAL rippled node
only. Writes the singleton `ledger_definitions` row; appends a
`ledger_definitions_history` delta row on hash change.

Why local-only, no public fallback: a public node returns ITS build's
vocabulary, not ours. If our local node is down the correct answer is
STALE (walker_health flags it), not "here's what somebody else knows."

Cadence: 6h (StartInterval=21600). server_definitions is stable across
normal operation; it only changes on rippled version bumps. 6h caps the
drift window between a version bump and the Coverage Register seeing it.

Sentinels: `Invalid: -1` appears in both TRANSACTION_TYPES and
LEDGER_ENTRY_TYPES; it's a rippled marker, not a real type. Excluded here
so Phase 1b's three-way diff doesn't render "defined-but-unseen: Invalid"
as a permanent noise row in an alarm-only surface.
"""

from __future__ import annotations

import json
import logging
import sys
import time
import urllib.request

import db


WALKER_CADENCE_SECONDS = 21600  # mirrors launchd StartInterval — 6h
LOCAL_RIPPLED_URL = "http://localhost:5005"
RPC_TIMEOUT_S = 8

# Non-real vocabulary entries the node advertises. Both TRANSACTION_TYPES
# and LEDGER_ENTRY_TYPES include `Invalid: -1` — a marker, not a type.
# Kept as a documented exclusion set so a future rippled build adding new
# sentinels can be pinned here (rather than silently poisoning the diff).
SENTINEL_TX_TYPES = frozenset({"Invalid"})
SENTINEL_ENTRY_TYPES = frozenset({"Invalid"})

logger = logging.getLogger("ledger_definitions_walker")


def _rpc(method: str, params: dict) -> dict:
    req = urllib.request.Request(
        LOCAL_RIPPLED_URL,
        data=json.dumps({"method": method, "params": [params]}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=RPC_TIMEOUT_S) as resp:
        body = json.loads(resp.read().decode())
    result = body.get("result") or {}
    if result.get("status") != "success":
        raise RuntimeError(
            f"{method} failed: status={result.get('status')} "
            f"error={result.get('error')}"
        )
    return result


def _strip_sentinels(payload: dict) -> tuple[dict, dict]:
    """Return (tx_types_map, entry_types_map) with sentinels removed."""
    tx = dict(payload.get("TRANSACTION_TYPES") or {})
    entry = dict(payload.get("LEDGER_ENTRY_TYPES") or {})
    for name in SENTINEL_TX_TYPES:
        tx.pop(name, None)
    for name in SENTINEL_ENTRY_TYPES:
        entry.pop(name, None)
    return tx, entry


def collect():
    """Pull server_definitions + server_info from local rippled. Returns
    (hash, build_version, tx_types, entry_types, payload)."""
    defs = _rpc("server_definitions", {})
    info = _rpc("server_info", {})
    hash_val = defs.get("hash")
    if not hash_val:
        # Older rippled builds may omit the canonical hash. Fall back to
        # a deterministic self-hash rather than fail — 3.1.3 has it, but
        # this walker should keep working across upgrades.
        import hashlib
        canon = json.dumps(defs, sort_keys=True).encode()
        hash_val = "self:" + hashlib.sha256(canon).hexdigest()
        logger.warning("server_definitions.hash missing — self-hashed")
    tx_types, entry_types = _strip_sentinels(defs)
    build_version = (info.get("info") or {}).get("build_version")
    return hash_val, build_version, tx_types, entry_types, defs


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    db.write_walker_health_start(
        "ledger_definitions_walker",
        cadence_seconds=WALKER_CADENCE_SECONDS,
    )
    ok = False
    message = None
    try:
        hash_val, build_version, tx_types, entry_types, payload = collect()
        result = db.write_ledger_definitions(
            hash_val=hash_val,
            fetched_at=int(time.time()),
            build_version=build_version,
            tx_types=tx_types,
            entry_types=entry_types,
            payload=payload,
        )

        if result["changed"]:
            # Distinct tag per removal-vs-addition. Removals mean node
            # downgrade or something genuinely wrong; additions are the
            # expected shape after a rippled version bump. Neither is
            # rate-limited — a definitions change is an event, not a
            # stream, and the operator should always see it.
            if result["tx_types_removed"] or result["entry_types_removed"]:
                print(
                    f"[ledger_definitions] !!! DEFINITIONS REGRESSED "
                    f"hash={hash_val[:16]}.. "
                    f"prev={(result['hash_prev'] or 'None')[:16]}.. "
                    f"build_version={build_version} "
                    f"tx_removed={result['tx_types_removed']} "
                    f"entry_removed={result['entry_types_removed']} "
                    f"tx_added={result['tx_types_added']} "
                    f"entry_added={result['entry_types_added']}",
                    flush=True,
                )
            else:
                print(
                    f"[ledger_definitions] !!! DEFINITIONS CHANGED "
                    f"hash={hash_val[:16]}.. "
                    f"prev={(result['hash_prev'] or 'None')[:16]}.. "
                    f"build_version={build_version} "
                    f"tx_added={result['tx_types_added']} "
                    f"entry_added={result['entry_types_added']} "
                    f"(field-level or metadata change — payload archived "
                    f"in ledger_definitions_history)",
                    flush=True,
                )

        ok = True
        message = (
            f"hash={hash_val[:16]}.. build_version={build_version} "
            f"tx_types={len(tx_types)} entry_types={len(entry_types)} "
            f"changed={result['changed']}"
        )
        logger.info(message)
    except Exception as exc:
        message = f"exception: {type(exc).__name__}: {exc}"
        raise
    finally:
        db.write_walker_health_end(
            "ledger_definitions_walker", ok=ok, message=message
        )
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
