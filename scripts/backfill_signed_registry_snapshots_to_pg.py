"""One-shot backfill: import every existing signed_registry_snapshots/*.json
disk file into the PG signed_registry_snapshots table (Charlie ruling
2026-09-21, Monday build item 4).

Idempotent — the writer uses `ON CONFLICT (snapshot_date_utc) DO
NOTHING` so re-running against a partially-backfilled DB is safe.
Prints per-date outcome and a final summary.
"""
from __future__ import annotations

import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import db


def main() -> int:
    snapshots_dir = os.path.join(HERE, "signed_registry_snapshots")
    files = sorted(glob.glob(os.path.join(snapshots_dir, "*.json")))
    if not files:
        print(f"no files in {snapshots_dir}")
        return 0

    inserted = 0
    skipped = 0
    failed = 0
    for path in files:
        try:
            with open(path, "r", encoding="utf-8") as f:
                signed = json.load(f)
            # Split disk-file shape back into envelope | signature | hash
            signature = signed.pop("signature", None)
            canonical_hash_hex = signed.pop("canonical_hash_hex", None)
            if signature is None or canonical_hash_hex is None:
                print(f"  SKIP  {os.path.basename(path)}: missing signature or canonical_hash_hex")
                failed += 1
                continue
            envelope = signed  # remaining keys are the pre-sign envelope
            date_str = envelope["snapshot_date_utc"]

            # Read-before-write for accurate insert vs skip counts
            existing = db.read_signed_registry_snapshot(date_str)
            db.write_signed_registry_snapshot(envelope, signature, canonical_hash_hex)
            if existing is None:
                print(f"  INS   {date_str}")
                inserted += 1
            else:
                print(f"  SKIP  {date_str} (already in PG)")
                skipped += 1
        except Exception as e:
            print(f"  FAIL  {os.path.basename(path)}: {type(e).__name__}: {e}")
            failed += 1

    print()
    print(f"Summary: inserted={inserted} skipped={skipped} failed={failed} total={len(files)}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
