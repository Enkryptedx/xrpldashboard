"""
Daily UNL composition snapshot.

Fetches both canonical published XRPL validator lists (vl.ripple.com and
vl.xrplf.org), decodes each signed manifest server-side, and writes one
row per source per day into the `unl_snapshots` Postgres table. The diff
between consecutive snapshots powers the "Validator-set composition over
time" section on /network.

Daily cadence matches the observed real-world rotation rate (validator
sets change on a ~yearly basis per list; sequence bumps happen ~4x/year
but most are re-signings with the same validator set).

Per-validator payload includes the truncated manifest's Domain field
when present — truth-first: for validators without a published Domain,
the diff renders the truncated pubkey only, never a fabricated label.
"""

import base64
import datetime
import logging
import sys
import time

import httpx

from xrpl.core import binarycodec

import db
from network_state import RIPPLE_EPOCH, UNL_SOURCES, _fetch_unl


def _decode_manifest_domain(manifest_b64):
    """Extract the Domain field from a validator's base64-encoded manifest
    using xrpl-py's canonical STObject parser (xrpl.core.binarycodec).
    Returns the lowercased ASCII domain when published, None otherwise.

    This delegates to the same deserializer rippled uses, so it handles
    every well-formed manifest the network does — no heuristic tag scan.
    Validators that don't publish a Domain field genuinely yield None;
    the page renders pubkey-only for those (truth-first, never fabricated)."""
    if not manifest_b64:
        return None
    try:
        raw_hex = base64.b64decode(manifest_b64).hex().upper()
        obj = binarycodec.decode(raw_hex)
    except Exception:
        return None
    domain_hex = obj.get("Domain")
    if not domain_hex or not isinstance(domain_hex, str):
        return None
    try:
        domain = bytes.fromhex(domain_hex).decode("ascii").strip().lower()
    except (ValueError, UnicodeDecodeError):
        return None
    if 3 <= len(domain) <= 253 and "." in domain:
        return domain
    return None


def _expiration_iso(ripple_seconds):
    if ripple_seconds is None:
        return None
    dt = RIPPLE_EPOCH + datetime.timedelta(seconds=int(ripple_seconds))
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def build_snapshot_payload(blob):
    """Shape the decoded UNL blob into the stable payload we persist."""
    validators_in = blob.get("validators") or []
    validators_out = []
    for v in validators_in:
        pubkey = (v.get("validation_public_key") or "").upper()
        if not pubkey:
            continue
        manifest_b64 = v.get("manifest") or ""
        domain = _decode_manifest_domain(manifest_b64)
        validators_out.append({
            "pubkey": pubkey,
            "domain": domain,
            "manifest_b64": manifest_b64,
        })
    validators_out.sort(key=lambda r: r["pubkey"])
    return {
        "sequence": blob.get("sequence"),
        "expiration_iso": _expiration_iso(blob.get("expiration")),
        "validator_count": len(validators_out),
        "validators": validators_out,
    }


def run_once(snapshot_date=None):
    """Fetch + persist both UNLs. Returns a per-source dict mapping
    source key → "written" | "fetch_failed:<err>" | "pg_unavailable"."""
    log = logging.getLogger("unl_snapshot")
    fetched_at_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    results = {}
    for src in UNL_SOURCES:
        key = src["key"]
        blob, err = _fetch_unl(src["url"])
        if blob is None:
            log.warning("unl_snapshot fetch failed for %s: %s", key, err)
            results[key] = f"fetch_failed:{err}"
            continue
        payload = build_snapshot_payload(blob)
        wrote = db.write_unl_snapshot(
            key, payload, fetched_at_iso, snapshot_date=snapshot_date
        )
        if wrote:
            log.info(
                "unl_snapshot wrote %s seq=%s validators=%d domains=%d",
                key,
                payload.get("sequence"),
                payload.get("validator_count"),
                sum(1 for v in payload["validators"] if v["domain"]),
            )
            results[key] = "written"
        else:
            results[key] = "pg_unavailable"
    return results


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    db.write_walker_health_start("unl_snapshot")
    ok = False
    message = None
    try:
        t0 = time.time()
        results = run_once()
        elapsed = round(time.time() - t0, 2)
        print(
            f"[unl_snapshot] results={results} elapsed={elapsed}s",
            flush=True,
        )
        # Strict policy: /network's core claim is the cross-UNL overlap
        # between Ripple + XRPL Foundation lists. A partial fetch makes
        # that overlap undefined, so ok=True only when both sources wrote.
        # Treating partial as failure lets consecutive_failures escalate
        # and trip the staleness banner instead of silently resetting.
        written = [k for k, v in results.items() if v == "written"]
        failed = [f"{k}:{v}" for k, v in results.items() if v != "written"]
        ok = (not failed) and bool(written)
        message = f"written={written} failed={failed} elapsed={elapsed}s"
    except Exception as exc:
        message = f"exception: {type(exc).__name__}: {exc}"
        raise
    finally:
        db.write_walker_health_end("unl_snapshot", ok=ok, message=message)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
