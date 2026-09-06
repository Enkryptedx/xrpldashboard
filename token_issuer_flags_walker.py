"""token_issuer_flags_walker — Mac-side AccountRoot flags snapshotter
for /token/<currency>/<issuer>'s "Ledger-level capabilities" panel.

Every 30 min, enumerates DISTINCT issuers from token_volume via
db.read_token_volume_issuers(), fetches AccountInfo(signer_lists=True)
per issuer through xrpl_client.get_client (Mac's LOCAL_NODE = LAN Lenovo
rippled), and upserts the flag/rate/key/signer-list summary into
token_issuer_flags_snapshot in Neon. The /token detail route reads that
table directly via db.read_token_issuer_flags() so the page stops
firing one live AccountInfo RPC per render.

Wired 2026-09-06 (approved Sep 4, dropped from every work order until
this one). Kills ~3/day walker_node_fallback rows for walker_name=
token_page. Same author intent as cold_storage_walker /
escrow_supply_walker (background writer → DB read).

Cadence: 30 min. Issuer flags change rarely (a TransferRate tweak or
a RegularKey rotation is a manual AccountSet transaction); 30 min gives
3 write cycles inside the /token route's 90-min staleness threshold —
one missed fire is invisible, two consecutive missed fires trip the
stale-cache banner.

Fetch failures are per-issuer: a single AccountInfo error leaves that
issuer with fetch_ok=False in the snapshot but doesn't abort the batch.
Only a total wipeout (0 fetched_ok across all issuers, i.e. LAN rippled
unreachable) returns (False, ...) so walker_health goes red.
"""
import logging
import sys
import time

from xrpl.models.requests import AccountInfo

import db
from xrpl_client import get_client

logging.basicConfig(
    format="%(asctime)s [token_issuer_flags_walker] %(levelname)s %(message)s",
    level=logging.INFO,
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

WALKER_NAME = "token_issuer_flags_walker"
WALKER_CADENCE_SECONDS = 1800  # 30 min


def _fetch_flags(client, issuer):
    """Return dict of snapshot fields for one issuer, or None on error.

    signer_lists=True piggybacks multi-sig detection on the same
    round-trip — same call shape as token_data._capability_signals()
    was making live per page render."""
    try:
        resp = client.request(
            AccountInfo(account=issuer, ledger_index="validated", signer_lists=True)
        )
    except Exception as e:
        log.warning("account_info request failed for %s: %s", issuer, type(e).__name__)
        return None
    result = resp.result or {}
    if "error" in result:
        # account_not_found is legitimate — a token_volume row can outlive its
        # issuer if the account was deleted. Snapshot with zero-flags fetch_ok=True
        # so the page can render "issuer no longer exists" via existing checks
        # rather than a stale banner. Everything else is a fetch error.
        err = result.get("error")
        if err == "actNotFound":
            return {
                "flags": 0,
                "transfer_rate": None,
                "regular_key": None,
                "has_signer_list": False,
                "domain_hex": None,
                "ledger_index": int(result.get("ledger_current_index") or 0),
                "fetch_ok": True,
            }
        log.warning("account_info error for %s: %s", issuer, err)
        return None

    acct = result.get("account_data") or {}
    # signer_lists may live on account_data OR at the top level depending
    # on rippled version — token_data.py handles both shapes; we do too.
    signer_lists = acct.get("signer_lists") or result.get("signer_lists") or []
    try:
        transfer_rate = int(acct["TransferRate"]) if acct.get("TransferRate") else None
    except (TypeError, ValueError):
        transfer_rate = None
    try:
        ledger_index = int(result.get("ledger_index") or result.get("ledger_current_index") or 0)
    except (TypeError, ValueError):
        ledger_index = 0
    try:
        flags = int(acct.get("Flags") or 0)
    except (TypeError, ValueError):
        flags = 0
    return {
        "flags": flags,
        "transfer_rate": transfer_rate,
        "regular_key": acct.get("RegularKey"),
        "has_signer_list": bool(signer_lists),
        "domain_hex": acct.get("Domain"),
        "ledger_index": ledger_index,
        "fetch_ok": True,
    }


def run() -> tuple[bool, int, str]:
    issuers = db.read_token_volume_issuers()
    if not issuers:
        return False, 0, "no distinct issuers in token_volume (PG unavailable or empty table?)"

    client = get_client(WALKER_NAME)
    rows = []
    fetched_ok = 0
    max_ledger = 0
    for issuer in issuers:
        snap = _fetch_flags(client, issuer)
        if snap is None:
            rows.append({
                "issuer": issuer,
                "flags": 0,
                "transfer_rate": None,
                "regular_key": None,
                "has_signer_list": False,
                "domain_hex": None,
                "ledger_index": 0,
                "fetch_ok": False,
            })
            continue
        fetched_ok += 1
        max_ledger = max(max_ledger, snap["ledger_index"])
        rows.append({"issuer": issuer, **snap})

    if fetched_ok == 0:
        return False, 0, f"all {len(issuers)} issuers errored — LAN rippled unreachable?"

    ok = db.replace_token_issuer_flags_snapshot(rows)
    if not ok:
        return False, 0, "replace_token_issuer_flags_snapshot returned False"

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
