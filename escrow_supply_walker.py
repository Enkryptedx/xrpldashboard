"""escrow_supply_walker — Mac-side EscrowCreate-total snapshotter for
/cold-storage's "locked" figure.

Every 15 min, iterates the same category=ripple cohort as
cold_storage_walker (minus the RLUSD issuer per escrow_supply.py logic)
and sums EscrowCreate object amounts across all of them via paginated
account_objects(type=escrow). Upserts one aggregate row into the
escrow_supply_snapshot singleton. The /cold-storage route reads via
db.read_escrow_supply_snapshot() and never touches XRPL live.

Wired 2026-09-03 to kill ~52/hr walker_node_fallback rows churning
public Ripple servers for a figure that changes once a month.

Cadence: 15 min (matches cold_storage_walker). The escrow total only
changes on the monthly release schedule; 15 min is deliberately over-
frequent so a single missed fire is invisible in the SOURCING_STALE_
CACHE calculation (45-min threshold = 3 missed fires).
"""
import logging
import sys
import time

from xrpl.models.requests import AccountObjects

import db
from cold_storage import _load_named
from xrpl_client import get_client

logging.basicConfig(
    format="%(asctime)s [escrow_supply_walker] %(levelname)s %(message)s",
    level=logging.INFO,
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

WALKER_NAME = "escrow_supply_walker"
WALKER_CADENCE_SECONDS = 900  # 15 min


def _sum_account_escrows(client, address):
    total = 0.0
    count = 0
    max_ledger = 0
    marker = None
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
            log.warning("account_objects failed for %s: %s", address, type(e).__name__)
            return None
        result = resp.result or {}
        if "error" in result:
            log.warning("account_objects error for %s: %s", address, result.get("error"))
            return None
        li = result.get("ledger_index") or result.get("ledger_current_index") or 0
        try:
            max_ledger = max(max_ledger, int(li))
        except (TypeError, ValueError):
            pass
        for obj in result.get("account_objects") or []:
            amt = obj.get("Amount")
            if isinstance(amt, str):
                try:
                    total += int(amt) / 1_000_000
                    count += 1
                except (TypeError, ValueError):
                    pass
        marker = result.get("marker")
        if not marker:
            break
    return {"total_xrp": total, "object_count": count, "ledger_index": max_ledger}


def run() -> tuple[bool, int, str]:
    named = _load_named()
    addresses = [
        addr
        for addr, meta in named.items()
        if isinstance(meta, dict)
        and meta.get("category") == "ripple"
        and "RLUSD" not in (meta.get("name") or "")
    ]
    if not addresses:
        return False, 0, "no category=ripple addresses in named_accounts.json"

    client = get_client(WALKER_NAME)
    total_xrp = 0.0
    object_count = 0
    fetched_ok = 0
    max_ledger = 0
    for addr in addresses:
        r = _sum_account_escrows(client, addr)
        if r is None:
            continue
        fetched_ok += 1
        total_xrp += r["total_xrp"]
        object_count += r["object_count"]
        max_ledger = max(max_ledger, r["ledger_index"])

    if fetched_ok == 0:
        return False, 0, f"all {len(addresses)} accounts errored — LAN rippled unreachable?"

    ok = db.upsert_escrow_supply_snapshot(
        total_xrp=total_xrp,
        object_count=object_count,
        accounts_scanned=fetched_ok,
        accounts_total=len(addresses),
        ledger_index=max_ledger,
    )
    if not ok:
        return False, 0, "upsert_escrow_supply_snapshot returned False (DB unavailable / write error)"

    msg = (
        f"total={total_xrp:,.0f} XRP · objects={object_count} · "
        f"accounts={fetched_ok}/{len(addresses)} · max_ledger={max_ledger}"
    )
    return True, 0, msg


def main() -> int:
    db.write_walker_health_start(WALKER_NAME, cadence_seconds=WALKER_CADENCE_SECONDS)
    log.info("start")
    t0 = time.time()
    try:
        ok, n_findings, message = run()
    except Exception as e:
        log.exception("unhandled exception")
        db.write_walker_health_end(WALKER_NAME, ok=False,
                                   message=f"unhandled: {type(e).__name__}: {e}")
        return 1
    elapsed = time.time() - t0
    message = f"{message} · elapsed={elapsed:.1f}s"
    if ok:
        log.info("PASS: %s", message)
    else:
        log.error("FAIL: %s", message)
    db.write_walker_health_end(
        WALKER_NAME,
        ok=ok,
        message=message,
        findings_count=n_findings if ok else None,
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
