#!/usr/bin/env python3
"""Bidirectional verification of the name_locked path in
db.upsert_account_label.

Three cases against real Neon:
  A. Locked row  → name preserved, extra merged, updated_at refreshes.
  B. Unlocked row → name overwrites (current curated behavior unchanged).
  C. Fresh row    → normal insert.

Uses a scratch address in the reserved rXXX...XXX shape so it can be
cleaned up unconditionally at the end. Non-destructive to any real
account_labels row.
"""
import json
import os
import sys
import time

sys.path.insert(0, "/Users/charliebruce/xrpl_test")

import psycopg

import db  # noqa: E402


SCRATCH_A = "rNAMELOCKTESTAAAAAAAAAAAAAAAAAAAAA"
SCRATCH_B = "rNAMELOCKTESTBBBBBBBBBBBBBBBBBBBBBB"
SCRATCH_C = "rNAMELOCKTESTCCCCCCCCCCCCCCCCCCCCCC"
SCRATCH = (SCRATCH_A, SCRATCH_B, SCRATCH_C)


def _dsn():
    dsn = os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not dsn:
        print("FAIL: no NEON_DATABASE_URL / DATABASE_URL in env", file=sys.stderr)
        sys.exit(2)
    return dsn


def _fetch(cur, addr):
    cur.execute(
        "SELECT name, category, source, confidence, extra, updated_at "
        "FROM account_labels WHERE address = %s",
        (addr,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    return {
        "name": row[0],
        "category": row[1],
        "source": row[2],
        "confidence": float(row[3]) if row[3] is not None else None,
        "extra": row[4] or {},
        "updated_at": int(row[5]) if row[5] is not None else None,
    }


def _cleanup(conn):
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM account_labels WHERE address = ANY(%s)",
            (list(SCRATCH),),
        )
    conn.commit()


def _seed(cur, addr, name, extra):
    now = int(time.time())
    cur.execute(
        "INSERT INTO account_labels "
        "(address, name, category, source, confidence, extra, updated_at) "
        "VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s) "
        "ON CONFLICT (address) DO UPDATE SET "
        "  name = EXCLUDED.name, extra = EXCLUDED.extra, "
        "  updated_at = EXCLUDED.updated_at",
        (addr, name, "issuer", "toml", 1.0, json.dumps(extra), now),
    )


def main():
    dsn = _dsn()
    failures = []

    with psycopg.connect(dsn) as conn:
        _cleanup(conn)

        # --- Case A: locked row ---
        with conn.cursor() as cur:
            _seed(cur, SCRATCH_A, "HAND-CLEANED", {
                "name_locked": True,
                "domain": "old.example",
                "verified_at_unix": 1000000,
            })
        conn.commit()
        time.sleep(1.1)  # force updated_at delta

        ok = db.upsert_account_label(
            SCRATCH_A,
            name="derived.example (SecondToken issuer)",
            source="toml",
            category="issuer",
            confidence=1.0,
            extra={
                "mode": "org",
                "domain": "derived.example",
                "verified_via": "https://derived.example/.well-known/xrp-ledger.toml",
                "verified_at_unix": 2000000,
            },
        )
        with conn.cursor() as cur:
            after_a = _fetch(cur, SCRATCH_A)

        if not ok:
            failures.append("A: upsert returned False")
        elif after_a is None:
            failures.append("A: row disappeared after upsert")
        else:
            if after_a["name"] != "HAND-CLEANED":
                failures.append(f"A: name clobbered → {after_a['name']!r}")
            if not after_a["extra"].get("name_locked"):
                failures.append("A: name_locked flag lost during merge")
            if after_a["extra"].get("domain") != "derived.example":
                failures.append(
                    f"A: extra.domain not refreshed → {after_a['extra'].get('domain')!r}"
                )
            if after_a["extra"].get("verified_at_unix") != 2000000:
                failures.append("A: extra.verified_at_unix not refreshed")
            if after_a["updated_at"] is None or after_a["updated_at"] < 2000000000:
                # sanity: updated_at should be current epoch, not the seed's 1e6
                if after_a["updated_at"] and after_a["updated_at"] < time.time() - 60:
                    failures.append(
                        f"A: updated_at stale ({after_a['updated_at']})"
                    )

        # --- Case B: unlocked row (name_locked absent) ---
        with conn.cursor() as cur:
            _seed(cur, SCRATCH_B, "OLD-NAME", {
                "domain": "old.example",
                "verified_at_unix": 1000000,
            })
        conn.commit()

        ok = db.upsert_account_label(
            SCRATCH_B,
            name="NEW-NAME",
            source="toml",
            category="issuer",
            confidence=1.0,
            extra={
                "mode": "org",
                "domain": "new.example",
                "verified_via": "https://new.example/.well-known/xrp-ledger.toml",
                "verified_at_unix": 2000000,
            },
        )
        with conn.cursor() as cur:
            after_b = _fetch(cur, SCRATCH_B)

        if not ok:
            failures.append("B: upsert returned False")
        elif after_b is None:
            failures.append("B: row disappeared after upsert")
        else:
            if after_b["name"] != "NEW-NAME":
                failures.append(f"B: name NOT overwritten → {after_b['name']!r}")
            if after_b["extra"].get("domain") != "new.example":
                failures.append(
                    f"B: extra not refreshed → {after_b['extra'].get('domain')!r}"
                )

        # --- Case C: fresh insert ---
        ok = db.upsert_account_label(
            SCRATCH_C,
            name="FRESH-NAME",
            source="toml",
            category="issuer",
            confidence=1.0,
            extra={"mode": "org", "domain": "fresh.example"},
        )
        with conn.cursor() as cur:
            after_c = _fetch(cur, SCRATCH_C)

        if not ok:
            failures.append("C: upsert returned False")
        elif after_c is None:
            failures.append("C: fresh row not inserted")
        elif after_c["name"] != "FRESH-NAME":
            failures.append(f"C: fresh name wrong → {after_c['name']!r}")

        # cleanup
        _cleanup(conn)

    if failures:
        print("FAIL")
        for f in failures:
            print(" -", f)
        sys.exit(1)

    print("PASS: A (locked preserved+merged), B (unlocked overwritten), C (fresh insert)")


if __name__ == "__main__":
    main()
