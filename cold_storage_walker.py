"""cold_storage_walker — Mac-side balance snapshotter for /cold-storage.

Every 15 min, iterates the tracked cohort (category=ripple in
named_accounts.json — the same list cold_storage.py's live fetcher walks)
via account_info(ledger_index=validated) through xrpl_client.get_client
(Mac's LOCAL_NODE = LAN Lenovo rippled). Upserts the balances into the
cold_storage_snapshot table in Neon. Renders read the table directly via
db.read_cold_storage_snapshot() so the /cold-storage route stops making
21 live account_info RPCs per page render.

Wired 2026-09-03 to kill ~214/hr walker_node_fallback rows churning
public Ripple servers. Same author intent as oracle_walker.py, rlusd_
refresher.py, escrows walker (background writer → DB read for the page).

Cadence: 15 min. Balances only change on the monthly release schedule,
but 15 min gives 3 write cycles inside the /cold-storage route's 45-min
staleness threshold — one missed fire is invisible, two consecutive
missed fires trip the SOURCING_STALE_CACHE banner.
"""
import logging
import sys
import time

from xrpl.models.requests import AccountInfo

import db
from cold_storage import _load_named
from xrpl_client import get_client

logging.basicConfig(
    format="%(asctime)s [cold_storage_walker] %(levelname)s %(message)s",
    level=logging.INFO,
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

WALKER_NAME = "cold_storage_walker"
WALKER_CADENCE_SECONDS = 900  # 15 min


def _fetch_balance(client, address):
    try:
        resp = client.request(
            AccountInfo(account=address, ledger_index="validated")
        )
    except Exception as e:
        log.warning("account_info request failed for %s: %s", address, type(e).__name__)
        return None
    result = resp.result or {}
    if "error" in result:
        log.warning("account_info error for %s: %s", address, result.get("error"))
        return None
    account_data = result.get("account_data") or {}
    try:
        drops = int(account_data.get("Balance", "0"))
    except (TypeError, ValueError):
        drops = 0
    ledger_index = result.get("ledger_index") or result.get("ledger_current_index") or 0
    try:
        ledger_index = int(ledger_index)
    except (TypeError, ValueError):
        ledger_index = 0
    return {
        "balance_xrp": drops / 1_000_000,
        "sequence": account_data.get("Sequence"),
        "owner_count": account_data.get("OwnerCount", 0),
        "ledger_index": ledger_index,
    }


def run() -> tuple[bool, int, str]:
    named = _load_named()
    addresses = [
        (addr, meta)
        for addr, meta in named.items()
        if isinstance(meta, dict) and meta.get("category") == "ripple"
    ]
    if not addresses:
        return False, 0, "no category=ripple addresses in named_accounts.json"

    client = get_client(WALKER_NAME)
    rows = []
    fetched_ok = 0
    max_ledger = 0
    for addr, _meta in addresses:
        bal = _fetch_balance(client, addr)
        if bal is None:
            # Preserve prior balance if any — but replace-on-write means
            # a missing row would drop from the read; keep the address
            # with fetch_ok=False + balance 0.0 so the route can render
            # the account with a per-row error indicator.
            rows.append({
                "address": addr,
                "balance_xrp": 0.0,
                "sequence": None,
                "owner_count": None,
                "ledger_index": 0,
                "fetch_ok": False,
            })
            continue
        fetched_ok += 1
        max_ledger = max(max_ledger, bal["ledger_index"])
        rows.append({
            "address": addr,
            "balance_xrp": bal["balance_xrp"],
            "sequence": bal["sequence"],
            "owner_count": bal["owner_count"],
            "ledger_index": bal["ledger_index"],
            "fetch_ok": True,
        })

    if fetched_ok == 0:
        return False, 0, f"all {len(addresses)} accounts errored — LAN rippled unreachable?"

    ok = db.replace_cold_storage_snapshot(rows)
    if not ok:
        return False, 0, "replace_cold_storage_snapshot returned False (DB unavailable / write error)"

    msg = (
        f"wrote {len(rows)} rows ({fetched_ok} ok, "
        f"{len(rows) - fetched_ok} error), max_ledger={max_ledger}"
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
