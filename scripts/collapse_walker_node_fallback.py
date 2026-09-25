"""collapse_walker_node_fallback.py — retention for walker_node_fallback.

Charlie ruling 2026-09-25 19:16 ET (go on the proposal): rows older than
--cutoff-days are COLLAPSED, one walker at a time, one transaction each,
before/after counts printed. A collapsed row keeps the record:

    walker_name = <walker>
    ts          = MAX(ts) of the (walker, UTC day, reason) group
    reason      = 'COLLAPSED <n> rows: <original reason>'

Standing policy: raw rows kept --cutoff-days (default 90 for the policy,
30 for tonight's first pass); collapsed rows kept forever.

Safety:
  - Dry run by default. --execute performs the writes.
  - Per walker: BEGIN → count raw rows in scope → INSERT collapsed rows →
    assert SUM(n) over the new collapsed rows == raw count → DELETE the raw
    rows in scope → COMMIT. Any mismatch → ROLLBACK for that walker.
  - Collapsed rows are excluded from scope (reason LIKE 'COLLAPSED %'), so
    re-runs are idempotent.
  - Never touches rows newer than the cutoff, so tools/l1_pager.py's 24h
    window is unaffected; its reason-prefix matches ('unreachable:%',
    'local_%') cannot trip on a 'COLLAPSED ...' reason.

Run (owner env, one-off, by hand — not a walker):
    set -a; . ~/.config/xrpldashboard/env; set +a
    ./venv/bin/python scripts/collapse_walker_node_fallback.py --cutoff-days 30
    ./venv/bin/python scripts/collapse_walker_node_fallback.py --cutoff-days 30 --execute
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--cutoff-days", type=int, default=90)
    p.add_argument("--execute", action="store_true", help="perform the writes")
    p.add_argument("--walker", action="append", default=None,
                   help="limit to these walker_name(s); default = every walker with rows in scope")
    args = p.parse_args()

    if not db.pg_available():
        print("REFUSE: DATABASE_URL not configured", file=sys.stderr)
        return 2

    cutoff_sql = f"NOW() - INTERVAL '{int(args.cutoff_days)} days'"
    scope = (f"walker_name = %s AND ts < {cutoff_sql} "
             f"AND reason NOT LIKE 'COLLAPSED %%'")

    # db.pg_connect() hands back a transactional connection (already
    # INTRANS after its session SETs); explicit commit/rollback below.
    with db.pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT walker_name, COUNT(*) FROM walker_node_fallback "
                f"WHERE ts < {cutoff_sql} AND reason NOT LIKE 'COLLAPSED %%' "
                f"GROUP BY 1 ORDER BY 2 DESC")
            walkers = [(w, int(n)) for w, n in cur.fetchall()]
            cur.execute("SELECT COUNT(*) FROM walker_node_fallback")
            total_before = int(cur.fetchone()[0])
        conn.rollback()

        if args.walker:
            walkers = [(w, n) for w, n in walkers if w in set(args.walker)]

        mode = "EXECUTE" if args.execute else "DRY-RUN"
        print(f"[{mode}] cutoff = {args.cutoff_days} days · table total before = {total_before:,} · "
              f"{len(walkers)} walker(s) with rows in scope")
        if not walkers:
            return 0

        grand_raw = grand_collapsed = 0
        for walker, n_scope in walkers:
            with conn.cursor() as cur:
                try:
                    cur.execute("SELECT COUNT(*) FROM walker_node_fallback WHERE " + scope, (walker,))
                    raw = int(cur.fetchone()[0])
                    cur.execute(
                        "SELECT COUNT(*) FROM ("
                        "  SELECT 1 FROM walker_node_fallback WHERE " + scope +
                        "  GROUP BY date_trunc('day', ts AT TIME ZONE 'UTC'), reason) g",
                        (walker,))
                    groups = int(cur.fetchone()[0])
                    if not args.execute:
                        conn.rollback()
                        print(f"  {walker}: raw={raw:,} → collapsed={groups:,}  (dry-run)")
                        grand_raw += raw
                        grand_collapsed += groups
                        continue

                    cur.execute(
                        "INSERT INTO walker_node_fallback (ts, walker_name, reason) "
                        "SELECT MAX(ts), walker_name, "
                        "       'COLLAPSED ' || COUNT(*) || ' rows: ' || reason "
                        "  FROM walker_node_fallback WHERE " + scope +
                        " GROUP BY walker_name, date_trunc('day', ts AT TIME ZONE 'UTC'), reason "
                        " RETURNING (regexp_match(reason, '^COLLAPSED (\\d+) rows'))[1]::bigint",
                        (walker,))
                    inserted = [int(r[0]) for r in cur.fetchall()]
                    if len(inserted) != groups or sum(inserted) != raw:
                        conn.rollback()
                        print(f"  {walker}: MISMATCH inserted={len(inserted)} groups={groups} "
                              f"sum={sum(inserted)} raw={raw} → ROLLED BACK")
                        continue
                    cur.execute("DELETE FROM walker_node_fallback WHERE " + scope, (walker,))
                    deleted = cur.rowcount
                    if deleted != raw:
                        conn.rollback()
                        print(f"  {walker}: MISMATCH deleted={deleted} raw={raw} → ROLLED BACK")
                        continue
                    conn.commit()
                    cur.execute("SELECT COUNT(*) FROM walker_node_fallback WHERE walker_name = %s", (walker,))
                    after = int(cur.fetchone()[0])
                    conn.rollback()
                    print(f"  {walker}: before(raw in scope)={raw:,} → collapsed rows={len(inserted):,} "
                          f"· deleted={deleted:,} · rows for walker now={after:,}  COMMITTED")
                    grand_raw += raw
                    grand_collapsed += len(inserted)
                except Exception as e:  # noqa: BLE001
                    conn.rollback()
                    print(f"  {walker}: ERROR {type(e).__name__}: {e} → ROLLED BACK")

        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM walker_node_fallback")
            total_after = int(cur.fetchone()[0])
        conn.rollback()
        print(f"[{mode}] raw in scope={grand_raw:,} → collapsed={grand_collapsed:,} · "
              f"table total {total_before:,} → {total_after:,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
