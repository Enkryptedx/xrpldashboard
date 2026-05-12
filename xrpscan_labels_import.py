"""
XRPSCAN well-known account labels importer.

Pulls the public `https://api.xrpscan.com/api/v1/names/well-known` list and
UPSERTs each verified entry into `account_labels` with `source='xrpscan'`.

Filtering rule: only entries with `verified: true` are imported. xrpscan
marks an entry verified when they've cross-checked it (domain proof, .toml,
or equivalent). Unverified entries are community-suggested but not proven —
publishing them would dilute the trust posture documented on /methodology.

Curated labels (manual/xrpscan/bithomp) always overwrite each other on
conflict; they will NEVER be clobbered by the derived (AMM/MPT) pass.
That priority is enforced server-side in db.upsert_account_label.

Usage:
    python xrpscan_labels_import.py             # default: verified only
    python xrpscan_labels_import.py --all       # include unverified
    python xrpscan_labels_import.py --dry-run   # fetch + summarise, no writes
"""

import argparse
import sys

import httpx

import db

XRPSCAN_URL = "https://api.xrpscan.com/api/v1/names/well-known"
HTTP_TIMEOUT = 30
USER_AGENT = "xrpldashboard/1.0 (+https://xrpldashboard.com)"


def fetch_well_known():
    r = httpx.get(XRPSCAN_URL, headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json()


def _label_extra(entry):
    """Keep the supporting fields xrpscan exposes so users can audit each
    label's provenance later. domain/twitter are the strongest signals."""
    out = {}
    for k in ("domain", "twitter", "desc", "verified"):
        v = entry.get(k)
        if v not in (None, ""):
            out[k] = v
    return out


def import_xrpscan(include_unverified=False, dry_run=False):
    try:
        entries = fetch_well_known()
    except Exception as e:
        print(f"[xrpscan] fetch failed: {e}", flush=True)
        return 1

    total = len(entries)
    skip_addr = skip_unverified = written = 0
    rows = []
    for e in entries:
        addr = e.get("account")
        name = e.get("name")
        if not addr or not name:
            skip_addr += 1
            continue
        if not include_unverified and not e.get("verified"):
            skip_unverified += 1
            continue
        rows.append(e)

    print(
        f"[xrpscan] fetched {total} entries · "
        f"skipped {skip_addr} missing addr/name · "
        f"skipped {skip_unverified} unverified · "
        f"importing {len(rows)}",
        flush=True,
    )

    if dry_run:
        for e in rows[:5]:
            print(f"  preview: {e.get('account')} -> {e.get('name')}", flush=True)
        return 0

    for e in rows:
        ok = db.upsert_account_label(
            address=e["account"],
            name=e["name"],
            source="xrpscan",
            category=None,
            confidence=0.9 if e.get("verified") else 0.7,
            extra=_label_extra(e),
        )
        if ok:
            written += 1

    counts = db.count_account_labels_by_source()
    print(
        f"[xrpscan] {written} written / {len(rows)} attempted. "
        f"account_labels totals by source: {counts}",
        flush=True,
    )
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--all", action="store_true",
        help="Include unverified xrpscan entries (default: verified only).",
    )
    ap.add_argument(
        "--dry-run", action="store_true",
        help="Fetch and summarise, don't write.",
    )
    args = ap.parse_args()

    if not db.pg_available():
        print("[xrpscan] DATABASE_URL unset; nothing to do.", flush=True)
        return 1

    return import_xrpscan(
        include_unverified=args.all,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    sys.exit(main())
