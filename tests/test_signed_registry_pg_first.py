"""PG-first serving for /.well-known/registry/<date>.json
(Charlie ruling 2026-09-21, Monday build item 4).

Two assertions:

1. **PG-alone serves every backfilled date** — with the disk directory
   temporarily renamed away, every backfilled date still returns 200
   from PG with `X-Registry-Source: pg`. This is the "delete nothing;
   PG alone works" proof Charlie asked for.

2. **Byte-fidelity vs disk** — the PG-reconstructed envelope is
   byte-identical to the disk file (`json.dumps(env, sort_keys=True,
   indent=2)`), so existing verifiers keep working with no change.

Skipped when PG isn't reachable (dev boxes without DATABASE_URL).
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _pg_reachable() -> bool:
    try:
        import db
        return db.pg_available()
    except Exception:
        return False


def test_route_pg_first_serves_backfilled_dates():
    if not _pg_reachable():
        print("SKIP: PG not reachable on this box")
        return
    import db
    dates = db.read_signed_registry_snapshot_dates()
    if not dates:
        print("SKIP: no rows in signed_registry_snapshots yet")
        return

    import app as app_module
    disk = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "signed_registry_snapshots",
    )
    disk_hidden = disk + ".hidden_for_pg_first_test"
    if not os.path.exists(disk):
        raise RuntimeError(f"expected disk dir at {disk}")
    os.rename(disk, disk_hidden)
    try:
        c = app_module.app.test_client()
        for d in dates:
            r = c.get(f"/.well-known/registry/{d}.json")
            assert r.status_code == 200, (
                f"PG-only serve failed for {d}: status={r.status_code}"
            )
            assert r.headers.get("X-Registry-Source") == "pg", (
                f"{d}: expected X-Registry-Source: pg, got "
                f"{r.headers.get('X-Registry-Source')!r}"
            )
    finally:
        os.rename(disk_hidden, disk)


def test_pg_envelope_byte_identical_to_disk_file():
    if not _pg_reachable():
        print("SKIP: PG not reachable")
        return
    import db
    disk = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "signed_registry_snapshots",
    )
    if not os.path.isdir(disk):
        print("SKIP: no disk directory")
        return
    for name in sorted(os.listdir(disk)):
        if not name.endswith(".json"):
            continue
        date_str = name[:-5]
        with open(os.path.join(disk, name), "rb") as f:
            disk_bytes = f.read()
        pg_env = db.read_signed_registry_snapshot(date_str)
        assert pg_env is not None, f"{date_str}: not in PG (backfill missed?)"
        pg_bytes = json.dumps(pg_env, sort_keys=True, indent=2).encode()
        assert disk_bytes == pg_bytes, (
            f"{date_str}: PG-reconstructed envelope differs from disk "
            f"(disk={len(disk_bytes)}B, pg={len(pg_bytes)}B). Existing "
            f"verifiers would break — reconstruction path is wrong."
        )


if __name__ == "__main__":
    test_route_pg_first_serves_backfilled_dates()
    test_pg_envelope_byte_identical_to_disk_file()
    print("ALL PASS")
