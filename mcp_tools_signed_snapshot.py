"""Day 7 signed-snapshot tool batch for the xrpldashboard MCP server.

Two tools, the moat expressing itself as an MCP surface:

  1. `get_signed_snapshot(date_str)`  — retrieve a daily signed snapshot
                                        by ISO date (YYYY-MM-DD). Reads
                                        the disk file signed_snapshots/
                                        <date>.json (source of truth per
                                        signed_snapshot.py header) and
                                        surfaces the Ed25519-signed
                                        envelope wrapped in the standard
                                        MCP proof envelope. Raises when
                                        the date has no snapshot on disk
                                        — absence IS the signal.

  2. `verify_snapshot_signature(envelope)` — STATELESS verification. An
                                        agent hands back the exact
                                        envelope it received (data.signed)
                                        and this tool re-derives the leaf
                                        hash, checks the Ed25519 signature
                                        against the published pubkey,
                                        checks the audit path against the
                                        claimed chain root, and returns a
                                        verify_result: bool + the list of
                                        issues if any. No shared state
                                        with the writer — mirrors what a
                                        third-party verifier would do.

The moat expression: a caller who screenshots a metric today can, in
six months, hand the snapshot back into this tool from a completely
different site (or from a local Python REPL using the same
signed_snapshot.verify_envelope function) and prove the number
wasn't silently changed. Two tools, one round-trip receipt.

Envelope discipline (same rule the Day 2-4 batches follow):
- Every response routes through mcp_server.wrap_envelope(...); no bypass.
- Successful emits call mcp_server.stamp_tool_call(name) for the Q1
  heartbeat-gap watermark.
- Failures raise (empty date, missing file, malformed envelope, invalid
  signature) — never a stub envelope claiming a signature that isn't
  there.

methodology_url anchor: https://xrpldashboard.com/methodology#signed-snapshot
Anchor lives on the "MCP tool envelope sources" section next to the
sibling h3s (#ledger, #amendments, etc.). The existing #signed-snapshots
(plural) h2 further up the page carries the human-facing deep-dive; the
new h3 introduces the tools and links out to it.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Optional

import mcp_server

# Repo-local path to the signed-snapshot artefacts. Matches
# signed_snapshot.SNAPSHOTS_DIR — this module intentionally does NOT
# import signed_snapshot at module-load so the tool surface doesn't drag
# the `cryptography` dependency into a bare mcp_server startup. Verify
# lazily-imports it below.
HERE = os.path.dirname(os.path.abspath(__file__))
SNAPSHOTS_DIR = os.path.join(HERE, "signed_snapshots")
CHAIN_PATH = os.path.join(SNAPSHOTS_DIR, "chain.json")

# ISO-8601 date regex kept in-module — no third-party dep for one check.
import re as _re
_DATE_RE = _re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _iso_utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _snapshot_path(date_str: str) -> str:
    return os.path.join(SNAPSHOTS_DIR, f"{date_str}.json")


def _load_signed_snapshot(date_str: str) -> dict:
    """Read the on-disk signed snapshot for `date_str`. Raises when the
    file is absent or malformed — absence IS the signal, not a stub."""
    if not date_str or not _DATE_RE.match(date_str):
        raise RuntimeError(
            f"get_signed_snapshot: date_str must be ISO YYYY-MM-DD, got {date_str!r}"
        )
    path = _snapshot_path(date_str)
    if not os.path.exists(path):
        raise RuntimeError(
            f"get_signed_snapshot: no signed snapshot on disk for {date_str} "
            f"(searched {path}) — walker has not produced this date yet, "
            f"or the requested date predates the chain start"
        )
    try:
        with open(path) as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise RuntimeError(
            f"get_signed_snapshot: failed to read {path}: {type(e).__name__}: {e}"
        ) from e
    return payload


# ─────────────────────────────────────────────────────────────────────
# 1. get_signed_snapshot
# ─────────────────────────────────────────────────────────────────────

def tool_get_signed_snapshot(date_str: str) -> dict:
    """Return the Ed25519-signed daily snapshot for `date_str` wrapped in
    the standard MCP proof envelope.

    Data payload carries the full signed envelope (metrics, leaf_hash,
    chain_root, audit_path, signature_ed25519, signing_pubkey_fingerprint)
    exactly as written by signed_snapshot.sign_snapshot — an agent can
    hand this straight back to verify_snapshot_signature and complete
    the round-trip receipt.

    Envelope source: signed_snapshot_walker (daily launchd job that runs
    signed_snapshot.py). Freshness contract: daily (the walker runs once
    per UTC day; today's snapshot may be absent early in the day, which
    is the honest signal, not a stub).
    """
    signed = _load_signed_snapshot(date_str)
    data = {
        "snapshot_date_utc": signed.get("snapshot_date_utc"),
        "signing_domain": signed.get("signing_domain"),
        "schema_version": signed.get("schema_version"),
        "snapshot_taken_unix": signed.get("snapshot_taken_unix"),
        "metrics": signed.get("metrics", []),
        "errors": signed.get("errors", []),
        "leaf_hash": signed.get("leaf_hash"),
        "leaf_index": signed.get("leaf_index"),
        "leaves_total": signed.get("leaves_total"),
        "chain_root": signed.get("chain_root"),
        "previous_root": signed.get("previous_root"),
        "audit_path": signed.get("audit_path", []),
        "signature_ed25519": signed.get("signature_ed25519"),
        "signing_pubkey_fingerprint": signed.get("signing_pubkey_fingerprint"),
        "verifier_instructions": signed.get("verifier_instructions"),
    }
    envelope = mcp_server.wrap_envelope(
        data,
        source="signed_snapshot_walker",
        as_of=_iso_utc_now(),
        freshness_contract="daily",
        methodology_url="https://xrpldashboard.com/methodology#signed-snapshot",
        claims_ref="signed_snapshot_daily",
    )
    mcp_server.stamp_tool_call("get_signed_snapshot")
    return envelope


# ─────────────────────────────────────────────────────────────────────
# 2. verify_snapshot_signature
# ─────────────────────────────────────────────────────────────────────

def tool_verify_snapshot_signature(envelope: Any) -> dict:
    """Stateless signature verification. Accepts either:
      * the full MCP envelope returned by get_signed_snapshot (dict with
        `data` field containing the signed payload), or
      * the bare signed-snapshot payload dict itself.

    Delegates to signed_snapshot.verify_envelope, which is the same
    verification path a third-party verifier would run — no shared state
    with the writer, no I/O beyond loading the pinned public key.

    Return payload:
      * verify_result: bool                — True iff signature + audit
                                              path + leaf_hash + fingerprint
                                              all check out.
      * public_key_fingerprint: str        — the fingerprint the pubkey
                                              on disk resolved to (agent
                                              can cross-check against the
                                              DNS TXT pin).
      * issues: list[str]                  — empty on success; specific
                                              failure reasons on False.
      * snapshot_date_utc: str | None      — echoed from payload for
                                              round-trip clarity.

    Any missing-or-malformed payload path raises rather than returning
    verify_result=False — malformed input is a caller bug, not a
    verification result the agent should report to a user."""
    if envelope is None:
        raise RuntimeError("verify_snapshot_signature: envelope is None")

    if isinstance(envelope, dict) and "data" in envelope and isinstance(envelope["data"], dict):
        signed = envelope["data"]
    elif isinstance(envelope, dict):
        signed = envelope
    else:
        raise RuntimeError(
            f"verify_snapshot_signature: envelope must be a dict "
            f"(bare payload or {{data, proof, server}} MCP envelope), got {type(envelope).__name__}"
        )

    required = {"signing_domain", "schema_version", "snapshot_date_utc",
                "metrics", "leaf_hash", "leaf_index", "leaves_total",
                "chain_root", "audit_path", "signature_ed25519",
                "signing_pubkey_fingerprint"}
    missing = required - set(signed.keys())
    if missing:
        raise RuntimeError(
            f"verify_snapshot_signature: payload missing required fields: "
            f"{sorted(missing)}"
        )

    import signed_snapshot as _ss
    ok, issues = _ss.verify_envelope(signed)

    pub = _ss.load_public_key()
    from cryptography.hazmat.primitives import serialization
    pub_raw = pub.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    resolved_fp = _ss._fingerprint(pub_raw)

    data = {
        "verify_result": bool(ok),
        "public_key_fingerprint": resolved_fp,
        "issues": list(issues),
        "snapshot_date_utc": signed.get("snapshot_date_utc"),
        "chain_root": signed.get("chain_root"),
    }
    out = mcp_server.wrap_envelope(
        data,
        source="signed_snapshot.verify_envelope+pinned_pubkey",
        as_of=_iso_utc_now(),
        freshness_contract="≤ 5min",
        methodology_url="https://xrpldashboard.com/methodology#signed-snapshot",
        claims_ref="signed_snapshot_verify",
    )
    mcp_server.stamp_tool_call("verify_snapshot_signature")
    return out
