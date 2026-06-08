"""
bridge_signer_walker — append-only history of Axelar XRPL Gateway
SignerList state for the /sidechain page.

Bootstrap: on first run (no existing rows), reads current SignerList via
account_objects and writes one row with tx_hash='BOOTSTRAP'. The bootstrap
row anchors all subsequent rotation queries — its ledger_index is the
"known good" floor below which steady-state scanning does not look.

Steady-state: on each subsequent run, scans account_tx for the gateway in
the validated-ledger range (last_scanned_ledger, current_validated],
filters TransactionType == 'SignerListSet' with tesSUCCESS, and INSERTs
new rows. PK conflict on (ledger_index, tx_hash) skips silently so
overlapping re-runs are idempotent.

Cadence: hourly (3600s) via launchd. SignerListSet rotations are rare
events (verifier set changes are infrequent); hourly catches narrow
windows without lag. Hourly slot is sparsely occupied — only one other
walker (mpt_holders_refresh) runs hourly, so no stampede risk.

walker_health try/finally hooks land in Step 3.
launchd plist registration lands in Step 3.

Run modes:
  python3 bridge_signer_walker.py              # full run
  python3 bridge_signer_walker.py --dry-run    # print, write nothing
  python3 bridge_signer_walker.py --bootstrap-only
                                               # write bootstrap row if missing, skip scan
"""

import argparse
import datetime as dt
import json
import os
import sys
import time

from xrpl.clients import JsonRpcClient
from xrpl.models.requests import AccountObjects, AccountTx, Ledger

import db

XRPL_NODE = os.environ.get("XRPL_NODE", "https://s1.ripple.com:51234")
AXELAR_GATEWAY = "rfmS3zqrQrka8wVyhXifEeyTwe8AMz2Yhw"
WALKER_NAME = "bridge_signer_walker"
WALKER_CADENCE_SECONDS = 3600

# Sentinel value for the tx_hash column when the row was created from
# an account_objects read (no underlying tx) rather than an observed
# SignerListSet transaction. Documented in bridge_signer_history DDL.
BOOTSTRAP_TX_HASH = "BOOTSTRAP"

# Safety ceiling on per-run scan range. Hourly cadence means steady-state
# windows are ~1200 ledgers (~3.3s close time × 3600s). Cap prevents a
# runaway scan if the walker has been offline for months and resumes
# pointing at a very old last_scanned_ledger.
MAX_LEDGERS_PER_RUN = 2_500_000  # ~96 days at 3.3s close time

# Offset from Unix epoch (1970-01-01) to rippled epoch (2000-01-01) in
# seconds. Transaction envelopes from account_tx carry a `date` field
# expressed in rippled epoch — convert by adding this offset.
RIPPLED_EPOCH_OFFSET = 946_684_800


def _safe_request(client, request):
    try:
        resp = client.request(request)
        if "error" in resp.result:
            return None
        return resp.result
    except Exception:
        return None


def _rippled_date_to_utc(date_seconds):
    """Convert rippled-epoch seconds (since 2000-01-01 UTC) to a UTC
    datetime. Returns None on missing or unparseable input so callers
    can chain fallbacks."""
    if date_seconds is None:
        return None
    try:
        unix_ts = int(date_seconds) + RIPPLED_EPOCH_OFFSET
        return dt.datetime.fromtimestamp(unix_ts, tz=dt.timezone.utc)
    except (TypeError, ValueError):
        return None


def _resolve_close_time(client, ledger_index, retries=1):
    """Fetch a ledger's close_time via Ledger lookup. One retry with a
    brief sleep before falling back to now() — covers transient RPC
    blips without masking persistent failures.

    Only the bootstrap path calls this. The steady-state scan path
    reads `date` directly off each account_tx envelope and never hits
    this code under normal operation. See close_time strategy notes
    in scan_signerlistset_events()."""
    if not ledger_index:
        return dt.datetime.now(dt.timezone.utc)
    for attempt in range(retries + 1):
        result = _safe_request(client, Ledger(ledger_index=int(ledger_index)))
        if result and result.get("ledger"):
            cts = result["ledger"].get("close_time_iso")
            if cts:
                try:
                    return dt.datetime.fromisoformat(cts.replace("Z", "+00:00"))
                except ValueError:
                    pass
        if attempt < retries:
            time.sleep(0.5)
    # Final fallback: row still gets a timestamp so downstream queries
    # don't break, but the value may drift from the actual ledger close
    # by however long the bootstrap took to run. Editorial trade-off
    # — accepted only because bootstrap runs once per gateway lifetime
    # and the steady-state path doesn't share this fallback.
    return dt.datetime.now(dt.timezone.utc)


def _parse_signer_entries(raw):
    """Normalize XRPL SignerEntries array to [{account, weight}, ...]."""
    out = []
    for e in raw or []:
        entry = e.get("SignerEntry") or {}
        out.append({
            "account": entry.get("Account"),
            "weight": entry.get("SignerWeight"),
        })
    return out


def fetch_current_signer_list(client):
    """Read the Axelar gateway's current SignerList via account_objects.

    Returns dict with ledger_index, close_time, quorum, signer_count,
    signer_entries — or None on RPC failure / no SignerList found.
    """
    result = _safe_request(
        client,
        AccountObjects(
            account=AXELAR_GATEWAY,
            type="signer_list",
            ledger_index="validated",
        ),
    )
    if not result:
        return None
    objs = result.get("account_objects") or []
    if not objs:
        return None
    sl = objs[0]
    entries = _parse_signer_entries(sl.get("SignerEntries"))
    ledger_index = result.get("ledger_index") or result.get("ledger_current_index")
    return {
        "ledger_index": int(ledger_index) if ledger_index else None,
        "close_time": _resolve_close_time(client, ledger_index),
        "quorum": sl.get("SignerQuorum"),
        "signer_count": len(entries),
        "signer_entries": entries,
    }


def has_bootstrap_row(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM bridge_signer_history WHERE tx_hash = %s LIMIT 1",
            (BOOTSTRAP_TX_HASH,),
        )
        return cur.fetchone() is not None


def last_scanned_ledger(conn):
    """MAX(ledger_index) across rotation rows only. Returns None when
    no rotations have been recorded yet."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT MAX(ledger_index) FROM bridge_signer_history "
            "WHERE tx_hash != %s",
            (BOOTSTRAP_TX_HASH,),
        )
        row = cur.fetchone()
        return int(row[0]) if row and row[0] is not None else None


def bootstrap_ledger(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT ledger_index FROM bridge_signer_history "
            "WHERE tx_hash = %s LIMIT 1",
            (BOOTSTRAP_TX_HASH,),
        )
        row = cur.fetchone()
        return int(row[0]) if row else None


def write_signer_row(conn, ledger_index, close_time, quorum,
                     signer_count, signer_entries, tx_hash):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO bridge_signer_history "
            "(ledger_index, close_time, quorum, signer_count, "
            " signer_entries, tx_hash, written_at) "
            "VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s) "
            "ON CONFLICT (ledger_index, tx_hash) DO NOTHING",
            (
                ledger_index,
                close_time,
                quorum,
                signer_count,
                json.dumps(signer_entries),
                tx_hash,
                int(time.time()),
            ),
        )
        return cur.rowcount


def write_bootstrap_row(conn, snapshot):
    return write_signer_row(
        conn,
        snapshot["ledger_index"],
        snapshot["close_time"],
        snapshot["quorum"],
        snapshot["signer_count"],
        snapshot["signer_entries"],
        BOOTSTRAP_TX_HASH,
    )


def current_validated_ledger(client):
    result = _safe_request(client, Ledger(ledger_index="validated"))
    if not result:
        return None
    li = result.get("ledger_index")
    if not li and result.get("ledger"):
        li = result["ledger"].get("ledger_index")
    return int(li) if li else None


def scan_signerlistset_events(client, conn, from_ledger, to_ledger, dry_run=False):
    """Page through account_tx for the gateway in (from_ledger, to_ledger];
    filter TransactionType == 'SignerListSet' with tesSUCCESS; INSERT new
    rows. PK conflict on re-runs is idempotent.

    Returns count of new rows added (or candidate rows during dry-run).
    """
    if from_ledger >= to_ledger:
        return 0
    added = 0
    marker = None
    while True:
        req = AccountTx(
            account=AXELAR_GATEWAY,
            ledger_index_min=int(from_ledger) + 1,
            ledger_index_max=int(to_ledger),
            limit=200,
            marker=marker,
        )
        result = _safe_request(client, req)
        if not result:
            break
        for envelope in result.get("transactions", []) or []:
            tx = envelope.get("tx") or envelope.get("tx_json") or {}
            if tx.get("TransactionType") != "SignerListSet":
                continue
            # Only successful rotations enter the history. Failed
            # SignerListSet transactions (tecNO_PERMISSION, tefBAD_QUORUM,
            # etc.) didn't actually change the SignerList — they're
            # attempted rotations, not rotations. /sidechain v1 doesn't
            # surface failed attempts. If a future feature needs an
            # "attempted rotation" feed (e.g., adversarial-action audit),
            # add a separate column or table rather than mixing the two.
            meta = envelope.get("meta") or {}
            if isinstance(meta, dict) and meta.get("TransactionResult") != "tesSUCCESS":
                continue
            entries = _parse_signer_entries(tx.get("SignerEntries"))
            ledger_index = envelope.get("ledger_index") or tx.get("ledger_index")
            tx_hash = (envelope.get("hash")
                       or tx.get("hash")
                       or envelope.get("transaction", {}).get("hash"))
            if not tx_hash or not ledger_index:
                continue
            # Prefer the envelope's `date` field (rippled epoch seconds,
            # tx-precise) over a separate Ledger lookup. This is the
            # editorial-precision path — no extra RPC, no fallback drift.
            # Only falls through to _resolve_close_time if the envelope
            # is missing `date` entirely, which is rare for validated
            # transactions.
            close_time = _rippled_date_to_utc(
                tx.get("date") or envelope.get("date")
            )
            if close_time is None:
                close_time = _resolve_close_time(client, ledger_index)
            if dry_run:
                added += 1
                continue
            n = write_signer_row(
                conn,
                int(ledger_index),
                close_time,
                tx.get("SignerQuorum"),
                len(entries),
                entries,
                tx_hash,
            )
            added += n
        marker = result.get("marker")
        if not marker:
            break
    if not dry_run:
        conn.commit()
    return added


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="Scan + report, write nothing")
    parser.add_argument("--bootstrap-only", action="store_true",
                        help="Write bootstrap row if missing, skip event scan")
    args = parser.parse_args()

    if not db.pg_available():
        print("Postgres not configured (DATABASE_URL); aborting.")
        return 2

    # Skip walker_health tracking in --dry-run so verification runs
    # don't pollute the health table with rows that aren't real
    # production runs. Mirrors daily_snapshot.py:343 precedent.
    track_health = not args.dry_run
    if track_health:
        db.write_walker_health_start(
            WALKER_NAME, cadence_seconds=WALKER_CADENCE_SECONDS,
        )

    _ok = False
    _msg = "init"
    bootstrap_written = 0
    rotations_added = 0
    from_ledger = None
    to_ledger = None
    try:
        client = JsonRpcClient(XRPL_NODE)

        with db.pg_connect() as conn:
            if not has_bootstrap_row(conn):
                snap = fetch_current_signer_list(client)
                if not snap or not snap.get("ledger_index"):
                    _msg = "bootstrap_fetch_failed"
                    print("ERROR: bootstrap failed — could not fetch current "
                          "SignerList for Axelar XRPL gateway")
                    return 1
                if args.dry_run:
                    print(f"DRY bootstrap: ledger={snap['ledger_index']} "
                          f"quorum={snap['quorum']} "
                          f"signers={snap['signer_count']}")
                else:
                    bootstrap_written = write_bootstrap_row(conn, snap)
                    conn.commit()
                    print(f"bootstrap: wrote {bootstrap_written} row "
                          f"@ ledger={snap['ledger_index']} "
                          f"quorum={snap['quorum']} "
                          f"signers={snap['signer_count']}")
            else:
                print("bootstrap: already present, skipping")

            if args.bootstrap_only:
                _ok = True
                _msg = f"bootstrap_only bootstrap_written={bootstrap_written}"
                return 0

            from_ledger = last_scanned_ledger(conn) or bootstrap_ledger(conn) or 0
            to_ledger = current_validated_ledger(client)
            if to_ledger is None:
                _msg = "current_validated_unresolved"
                print("ERROR: could not resolve current validated ledger")
                return 1
            if to_ledger - from_ledger > MAX_LEDGERS_PER_RUN:
                print(f"WARN: range {from_ledger}..{to_ledger} exceeds cap; "
                      f"truncating to last {MAX_LEDGERS_PER_RUN} ledgers")
                from_ledger = to_ledger - MAX_LEDGERS_PER_RUN

            rotations_added = scan_signerlistset_events(
                client, conn, from_ledger, to_ledger, dry_run=args.dry_run,
            )
            mode = "DRY " if args.dry_run else ""
            print(f"{mode}scan: range=({from_ledger}, {to_ledger}] "
                  f"events_added={rotations_added}")
            _ok = True
            _msg = (f"bootstrap_written={bootstrap_written} "
                    f"rotations_added={rotations_added} "
                    f"range=({from_ledger},{to_ledger}]")
            return 0
    except Exception as e:
        _msg = f"{type(e).__name__}: {e}"
        raise
    finally:
        if track_health:
            try:
                db.write_walker_health_end(WALKER_NAME, ok=_ok, message=_msg)
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
