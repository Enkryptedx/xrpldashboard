"""Amendment majority walker — record Majorities transitions at flag ledgers.

Charlie ruling 2026-09-25: after five outlets (CoinDesk, Yahoo/TheStreet,
Blockto, WordUp, Crinance) printed "PermissionDelegationV1_1 activates
Oct 5 11:18 UTC" sourced to /amendments — a date that was CORRECT at
publication but silently slipped to Oct 8 21:25 UTC when the majority
reset — we record majority gained/lost/regained transitions at the moment
they happen instead of bisecting flag ledgers after the fact.

What it does (READ-ONLY against XRPL, writes only to our own PG history
table):
  1. Read the Amendments ledger object (7DB0788C…) from our own node at
     the current validated ledger.
  2. For each amendment currently in `Majorities`, upsert an
     amendment_majority_history row keyed by (amendment_hash,
     majority_close_time). majority_close_time is stable for the life of
     a continuous majority window and CHANGES on reset — so a new
     CloseTime for the same amendment is a NEW row = a recorded reset.
  3. For any amendment_hash that has an OPEN row (removed_seen_ledger IS
     NULL) whose majority_close_time is NOT in the current Majorities set,
     stamp removed_* — the majority was lost.
  4. activation_eta = majority_close_time + 14 days (1,209,600 s), the
     same window rippled uses and amendments_state.py already applies.

Cadence: intended to run at flag-ledger granularity (every 256 ledgers,
~15 min) from launchd. Each run is a point observation; first_seen /
last_seen bracket the window we actually watched it present. Idempotent:
re-running at the same ledger only advances last_seen.

Source label: 'own_node_flag_ledger'. This is a first-party ledger read,
NOT a VHS reconstruction — distinct provenance from
amendment_tally_reconstructions.

Read path: jj-style read of the ledger object; the ONLY write is to
amendment_majority_history via the walker env (owner creds through the
launchd wrapper, same as every other walker). Never writes to the app.

Usage:
    python3 scripts/amendment_majority_walker.py
    python3 scripts/amendment_majority_walker.py --ledger 107212033   # pin a ledger (backfill)
    python3 scripts/amendment_majority_walker.py --dry-run            # print, don't write
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import httpx  # noqa: E402

import db  # noqa: E402
import xrpl_client as xc  # noqa: E402

XRPL_EPOCH_OFFSET = 946684800
AMENDMENTS_LEDGER_INDEX = (
    "7DB0788C020F02780A673DC74757F23823FA3014C1866E72CC4CD8B226CD6EF4"
)
ACTIVATION_WINDOW_SECONDS = 14 * 24 * 3600  # 1,209,600


def _iso(xrpl_close):
    """XRPL epoch seconds -> UTC ISO string."""
    if xrpl_close is None:
        return None
    return time.strftime(
        "%Y-%m-%dT%H:%M:%SZ",
        time.gmtime(int(xrpl_close) + XRPL_EPOCH_OFFSET),
    )


def _post(method, params):
    """Own-node-first RPC. Falls back to public nodes like xrpl_client does,
    but this walker only READS, so a public fallback is acceptable for
    resilience (the value we write is the ledger's own Majority.CloseTime,
    identical whichever honest node serves it)."""
    body = {"method": method, "params": [params]}
    last_err = None
    for url in [xc.LOCAL_NODE] + xc.PUBLIC_NODES:
        try:
            r = httpx.post(url, json=body, timeout=20)
            res = r.json().get("result", {})
            if res and res.get("status") != "error":
                return res, url
            last_err = res.get("error_message") or res.get("error")
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
            continue
    raise RuntimeError(f"all nodes failed for {method}: {last_err}")


def read_state(ledger_index="validated"):
    """Return (ledger_idx, ledger_close_time, {hash: (name, close_time)})."""
    feat, _ = _post("feature", {})
    features = feat.get("features") or {}
    names = {
        h: (i.get("name") if isinstance(i, dict) else None)
        for h, i in features.items()
    }
    le, src = _post(
        "ledger_entry",
        {"index": AMENDMENTS_LEDGER_INDEX, "ledger_index": ledger_index},
    )
    node = le.get("node") or {}
    lidx = le.get("ledger_index")
    # this ledger's own close time (for first/last/removed stamping)
    lr, _ = _post("ledger", {"ledger_index": lidx or ledger_index})
    lclose = (lr.get("ledger") or {}).get("close_time")
    majorities = {}
    for entry in node.get("Majorities") or []:
        m = (entry or {}).get("Majority") or {}
        h = m.get("Amendment")
        if not h:
            continue
        majorities[h] = (names.get(h), m.get("CloseTime"))
    return lidx, lclose, src, majorities


def _vote_at(cur, amendment_hash):
    """Best-effort UNL vote count + threshold from the most recent
    tally reconstruction for this amendment (informational only)."""
    try:
        cur.execute(
            """
            SELECT unl_votes_count, unl_threshold
              FROM amendment_tally_reconstructions
             WHERE amendment_hash = %s
             ORDER BY as_of_date DESC
             LIMIT 1
            """,
            (amendment_hash,),
        )
        row = cur.fetchone()
        return (row[0], row[1]) if row else (None, None)
    except Exception:  # noqa: BLE001
        return (None, None)


def run(ledger_index="validated", dry_run=False):
    lidx, lclose, src, majorities = read_state(ledger_index)
    now_iso = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[majority_walker] ledger={lidx} close={_iso(lclose)} "
          f"src={src} majorities={len(majorities)} dry_run={dry_run}")

    upserts = []
    for h, (name, close_time) in majorities.items():
        act_close = (close_time + ACTIVATION_WINDOW_SECONDS
                     if close_time is not None else None)
        upserts.append((h, name, close_time, act_close))
        print(f"  present: {name or h[:12]} close={_iso(close_time)} "
              f"-> activation {_iso(act_close)}")

    if dry_run:
        print("[majority_walker] dry-run: no writes")
        return {"ledger": lidx, "present": len(majorities), "wrote": 0}

    wrote = 0
    removed = 0
    with db.pg_connect() as conn:
        with conn.cursor() as cur:
            # 1) upsert present majorities
            for h, name, close_time, act_close in upserts:
                vc, thr = _vote_at(cur, h)
                cur.execute(
                    """
                    INSERT INTO amendment_majority_history (
                        amendment_hash, amendment_name,
                        majority_close_time, majority_close_iso,
                        activation_eta_close, activation_eta_iso,
                        first_seen_ledger, first_seen_close_time, first_seen_iso,
                        last_seen_ledger, last_seen_close_time, last_seen_iso,
                        vote_count_at_first, unl_threshold,
                        source, updated_at_iso
                    ) VALUES (
                        %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s
                    )
                    ON CONFLICT (amendment_hash, majority_close_time) DO UPDATE SET
                        amendment_name = COALESCE(EXCLUDED.amendment_name,
                                                  amendment_majority_history.amendment_name),
                        last_seen_ledger     = EXCLUDED.last_seen_ledger,
                        last_seen_close_time = EXCLUDED.last_seen_close_time,
                        last_seen_iso        = EXCLUDED.last_seen_iso,
                        updated_at_iso       = EXCLUDED.updated_at_iso
                    """,
                    (
                        h, name, close_time, _iso(close_time),
                        act_close, _iso(act_close),
                        lidx, lclose, _iso(lclose),
                        lidx, lclose, _iso(lclose),
                        vc, thr,
                        "own_node_flag_ledger", now_iso,
                    ),
                )
                wrote += 1

            # 2) stamp removals: open rows whose (hash, close_time) is no
            #    longer present in the current Majorities set.
            present_keys = {(h, ct) for h, (_, ct) in majorities.items()}
            cur.execute(
                """
                SELECT amendment_hash, majority_close_time
                  FROM amendment_majority_history
                 WHERE removed_seen_ledger IS NULL
                """
            )
            for h, ct in cur.fetchall():
                if (h, ct) in present_keys:
                    continue
                cur.execute(
                    """
                    UPDATE amendment_majority_history
                       SET removed_seen_ledger = %s,
                           removed_close_time  = %s,
                           removed_iso         = %s,
                           updated_at_iso      = %s
                     WHERE amendment_hash = %s
                       AND majority_close_time = %s
                       AND removed_seen_ledger IS NULL
                    """,
                    (lidx, lclose, _iso(lclose), now_iso, h, ct),
                )
                removed += 1
                print(f"  REMOVED stamped: {h[:12]} close_time={_iso(ct)} "
                      f"gone as of ledger {lidx} ({_iso(lclose)})")
        conn.commit()

    print(f"[majority_walker] wrote/updated={wrote} removed_stamped={removed}")
    return {"ledger": lidx, "present": len(majorities),
            "wrote": wrote, "removed": removed}


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--ledger", default="validated",
                   help="Ledger index to read (default: validated). "
                        "Pass a specific index for backfill.")
    p.add_argument("--dry-run", action="store_true",
                   help="Print observed majorities; write nothing.")
    args = p.parse_args()
    ledger = args.ledger
    if ledger != "validated":
        ledger = int(ledger)
    run(ledger_index=ledger, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
