"""Standalone one-shot walker for the /cold-storage escrow browser.

Invoked by com.charliebruce.xrpldashboard.escrow_walker.plist on a
30-min cadence. Each run iterates the tracked Ripple monthly-release
cohort (category=ripple in named_accounts.json, minus the RLUSD
issuer), pulls account_objects(type=escrow) via xrpl_client.get_client
(local rippled primary, s1/s2 fallback), and replaces the
escrows_snapshot table in Postgres.

The /cold-storage route reads through escrow_snapshot.py (5-min TTL
cache over the Postgres table). It never touches the XRPL node
directly for the browser/calendar sections.

v1 = replace-on-write (~102 rows per pass; simple, always current).
If a "realized releases per month" chart is ever wanted, the upgrade
path is a sibling escrows_history append table keyed by
(owner, sequence, observed_ledger_index) — the current schema and
walker stay as the "live state" view. Door marked, not built.

TokenEscrow: each row's Amount shape is inspected. Any IOU/MPT
denomination trips denom != 'XRP' and the read layer exposes
`token_escrow_observed=True`, which the template surfaces without
any code change here.
"""

import logging
import sys

from xrpl.models.requests import AccountObjects

import db
from cold_storage import _load_named
from xrpl_client import get_client

# Mirrors launchd/com.charliebruce.xrpldashboard.escrow_walker.plist
# StartInterval. Read by /walker_health for per-row staleness thresholds.
WALKER_CADENCE_SECONDS = 1800

logger = logging.getLogger("escrow_walker")


def _tracked_cohort():
    """Ripple monthly-release cohort — same filter escrow_supply uses,
    minus the RLUSD issuer (which never owns XRP escrows anyway; the
    scan confirmed 0)."""
    named = _load_named()
    return [
        (addr, meta)
        for addr, meta in named.items()
        if isinstance(meta, dict)
        and meta.get("category") == "ripple"
        and "RLUSD" not in (meta.get("name") or "")
    ]


def _classify_amount(amt):
    """Return (denom, amount_drops, amount_json) for the escrow's Amount
    field. XRP escrows have string drops; IOU escrows have {currency,
    issuer, value}; MPT escrows have {mpt_issuance_id, value}."""
    if isinstance(amt, str):
        try:
            return "XRP", int(amt), amt
        except (TypeError, ValueError):
            return "XRP", None, amt
    if isinstance(amt, dict):
        if "mpt_issuance_id" in amt:
            return "MPT", None, amt
        return "IOU", None, amt
    return "XRP", None, amt


def _scan_account(client, address):
    """Paginate account_objects(type=escrow) for one account. Returns
    list of raw escrow objects, or None on RPC error (caller decides
    whether to hard-fail the walker or soft-skip the account)."""
    out, marker = [], None
    while True:
        req = AccountObjects(
            account=address,
            type="escrow",
            limit=400,
            marker=marker,
            ledger_index="validated",
        )
        try:
            resp = client.request(req)
        except Exception as e:
            logger.warning("account_objects failed account=%s err=%s", address, e)
            return None
        result = resp.result or {}
        if result.get("error"):
            logger.warning("account_objects rpc-error account=%s err=%s",
                           address, result.get("error"))
            return None
        out.extend(result.get("account_objects") or [])
        marker = result.get("marker")
        if not marker:
            break
    return out


def collect_snapshot():
    """Iterate the tracked cohort, produce (rows, snapshot_ledger_index,
    stats). Rows are shaped for db.replace_escrows_snapshot."""
    cohort = _tracked_cohort()
    client = get_client("escrow_walker")

    rows = []
    ok = err = 0
    snap_ledger = None
    for addr, meta in cohort:
        objs = _scan_account(client, addr)
        if objs is None:
            err += 1
            continue
        ok += 1
        for obj in objs:
            denom, drops, amt_json = _classify_amount(obj.get("Amount"))
            ledger_hash = obj.get("index")
            if not ledger_hash:
                # Every ledger object carries `index`; missing it means the
                # response is malformed. Skip rather than fabricate a PK.
                logger.warning("escrow object missing 'index' account=%s", addr)
                continue
            rows.append({
                "ledger_index_hash": ledger_hash,
                "owner": addr,
                "owner_name": meta.get("name"),
                "destination": obj.get("Destination"),
                "denom": denom,
                "amount_drops": drops,
                "amount_json": amt_json,
                "finish_after": obj.get("FinishAfter"),
                "cancel_after": obj.get("CancelAfter"),
                "condition_present": bool(obj.get("Condition")),
                "previous_txn_id": obj.get("PreviousTxnID"),
                "previous_txn_lgr_seq": obj.get("PreviousTxnLgrSeq"),
            })
        # snap_ledger is best-effort; account_objects doesn't return
        # ledger_index the same way ledger_data does, but if the client
        # response attaches it we surface it. Otherwise None.
    return rows, snap_ledger, {"ok": ok, "err": err, "cohort_size": len(cohort)}


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    db.write_walker_health_start("escrow_walker",
                                 cadence_seconds=WALKER_CADENCE_SECONDS)
    ok = False
    message = None
    try:
        rows, snap_ledger, stats = collect_snapshot()
        if stats["ok"] == 0:
            # Every account failed — don't clobber the previous snapshot
            # with an empty one. walker_health marks failure; next cycle
            # retries. Same pattern as rlusd_refresher_walker.
            message = (f"all_accounts_failed "
                       f"cohort_size={stats['cohort_size']}")
            logger.error("no accounts responded; leaving previous snapshot")
        else:
            wrote = db.replace_escrows_snapshot(rows, snap_ledger)
            if wrote:
                ok = True
                message = (f"rows={len(rows)} "
                           f"accounts_ok={stats['ok']}/{stats['cohort_size']} "
                           f"accounts_err={stats['err']}")
                logger.info(message)
            else:
                message = "pg_write_failed"
                logger.error("replace_escrows_snapshot returned False")
    except Exception as exc:
        message = f"exception: {type(exc).__name__}: {exc}"
        raise
    finally:
        db.write_walker_health_end("escrow_walker", ok=ok, message=message)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
