"""chain_link_pg — finish verify_envelope's chain-link check from Postgres.

`signed_snapshot.verify_envelope` proves signature, leaf hash, audit path
and fingerprint from the envelope alone, but its chain-link step (does
`previous_root` equal the Merkle root of every earlier leaf?) only knows
DISK evidence: `chain.json` or the prior-day file. Hosts without those
files (Render, and any third party that only fetched one leaf) get the
soft note "chain_link: could not verify …", which every caller had been
collapsing into a hard FAILED verdict — production `/snapshots/verify`
said VERIFICATION FAILED for every date from 2026-09-04 to 2026-09-26, and
the MCP tool `verify_snapshot_signature` returned verify_result=false.

Postgres holds the same chain (`signed_snapshots` rows + chain head), so
this module completes the step from it:
  (a) recompute the Merkle root over the PG chain's leaves[:leaf_index]
      and compare with the envelope's previous_root; else
  (b) compare previous_root with the prior LEAF's chain_root (prior leaf
      by index, not prior calendar day, so the 2026-09-16..18 and
      2026-07-15 gaps bridge correctly).
If PG has nothing either, the soft note stays — callers must then report
"partial", never "failed" (see amendments_permalink.verdict_from_verify).
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import db  # noqa: E402

SOFT_PREFIX = "chain_link: could not verify"


def _chain_meta():
    """PG-first chain head; disk chain.json fallback (local dev)."""
    chain = db.read_signed_snapshot_chain()
    if chain:
        return chain
    try:
        import signed_snapshot as ss
        with open(ss.CHAIN_PATH) as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return None


def complete_chain_link(envelope: dict, ok: bool, issues: list[str]) -> tuple[bool, list[str], str | None]:
    """Return (ok, issues, verified_via). `verified_via` is 'chain',
    'prior_leaf', or None when nothing in PG/disk could finish the check
    (the soft note is then left in place). Never raises."""
    issues = list(issues)
    soft = [i for i in issues if i.startswith(SOFT_PREFIX)]
    if not soft:
        return ok, issues, None
    try:
        import signed_snapshot as ss
        verified_via = None
        leaf_index = envelope.get("leaf_index")
        prev_claimed = envelope.get("previous_root")
        chain = _chain_meta()
        leaves = (chain or {}).get("leaves") or []
        if isinstance(leaf_index, int) and len(leaves) > leaf_index > 0:
            prev = [bytes.fromhex(le["leaf_hash"]) for le in leaves[:leaf_index]]
            computed = ss._merkle_root(prev).hex()
            if computed != prev_claimed:
                issues.append(
                    f"chain_link: previous_root mismatch "
                    f"(file={str(prev_claimed)[:24]}…, "
                    f"chain[0..{leaf_index - 1}]={computed[:24]}…)")
            verified_via = "chain"
        elif isinstance(leaf_index, int) and leaf_index > 0:
            prior = db.read_signed_snapshot_by_leaf_index(leaf_index - 1)
            if prior and prior.get("chain_root"):
                if prior["chain_root"] != prev_claimed:
                    issues.append(
                        f"chain_link: previous_root != prior-leaf chain_root "
                        f"(prior leaf {leaf_index - 1} chain_root="
                        f"{prior['chain_root'][:24]}…, our previous_root="
                        f"{str(prev_claimed)[:24]}…)")
                verified_via = "prior_leaf"
        if verified_via:
            issues = [i for i in issues if i not in soft]
            return (not issues), issues, verified_via
        return ok, issues, None
    except Exception as e:  # noqa: BLE001 — keep the soft note, never raise
        issues.append(f"chain_link: PG completion failed: {type(e).__name__}")
        return False, issues, None
