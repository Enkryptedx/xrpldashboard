"""escrow_unlock_walker — daily observer of realized escrow releases.

Walks account_tx for the 20-account Ripple monthly-release cohort
(category=ripple in named_accounts.json MINUS the RLUSD issuer, the same
filter escrow_supply.py and escrow_walker.py use) over a 3-day lookback and
UPSERTs every EscrowFinish / EscrowCreate it sees into escrow_unlock_events.
Backs the "monthly release history" section on /cold-storage and its machine
twin /cold-storage/unlocks.json.

Cadence: DAILY via launchd, StartCalendarInterval + RunAtLoad=true. The
monthly unlock lands at 00:00:10 UTC on the 1st (8:00:10 PM ET the evening
before), so a daily tick with a 3-day lookback sees every unlock three times
before the window slides past it. RunAtLoad matters specifically here: user
LaunchAgents miss StartCalendarInterval fires during the FileVault
loginwindow gap, and this Mac is manual-restore on power loss. RunAtLoad
catches the missed slot at the next login, and the UPSERT makes the
double-run free.

    ~/Library/LaunchAgents/com.charliebruce.xrpldashboard.escrow_unlock_walker.plist
      StartCalendarInterval = { Hour = 4, Minute = 20 }   # 04:20 ET daily
      RunAtLoad             = true

04:20 ET is deliberately not on the hour and well clear of both the 22:00 ET
pg_dump window and the 21:00 ET signed-leaf job.

WHY THE OWN-NODE READ CANNOT BE ASSUMED TO COVER THE WINDOW
-----------------------------------------------------------
Our own rippled keeps a ~54k-ledger window (tools/anchor_nodes.py documents
the incident where this bit the Friday anchor reminder: it asked our node
first, got zero anchors because they had aged out, and reported "unknown"
while the canary saw all 12 fine). ~54k ledgers is roughly 2.2 days at ~3.5s
per ledger — SHORTER than this walker's 3-day lookback. So coverage is
MEASURED, not estimated: the walker reads the node's earliest complete
ledger, asks that ledger for its close time, and only trusts the own-node
pass for the span it actually holds. Anything older falls back to the shared
full-history witness list in tools/anchor_nodes.py.

Rows are tagged source='own-node' or source='witness:<host>', and
db.record_escrow_unlock_events() enforces the precedence: a witness row is
upgraded to own-node when the sovereign read catches up, and an own-node row
is NEVER downgraded to a witness read.

FINDINGS (findings_count > 0 → l1_pager.check_walker_findings pages)
--------------------------------------------------------------------
  * the lookback window was not fully covered by own node OR witness
  * a month inside the 12-month display window has zero observed rows
A hole in the history is reported loudly rather than rendered as a month
that happened to have no releases. The walker still exits ok=True when it
wrote what it could reach — findings are a separate signal from failure.

Scope note: EscrowFinish may be submitted by ANY account. Scoping by
submitter would be wrong — the third-party submitter seen on the Sept 2026
cohort releases finished 364 unrelated escrows that same month. This walker
scopes by escrow OWNER, which for EscrowFinish is the tx's `Owner` field,
not `Account`.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
import zoneinfo
from datetime import datetime, timedelta, timezone

from xrpl.clients import JsonRpcClient
from xrpl.models.requests import AccountTx, Ledger, ServerInfo

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import db  # noqa: E402
from escrow_walker import _classify_amount  # noqa: E402
from tools.anchor_nodes import full_history_nodes  # noqa: E402
from xrpl_client import get_client  # noqa: E402

logging.basicConfig(
    format="%(asctime)s [escrow_unlock_walker] %(levelname)s %(message)s",
    level=logging.INFO,
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

WALKER_NAME = "escrow_unlock_walker"
WALKER_CADENCE_SECONDS = 86_400  # daily

LOOKBACK_DAYS = 3
RIPPLE_EPOCH = 946684800
UTC = timezone.utc
ET_ZONE = zoneinfo.ZoneInfo("America/New_York")

NAMED_ACCOUNTS_PATH = os.path.join(HERE, "named_accounts.json")

ESCROW_TX_TYPES = ("EscrowFinish", "EscrowCreate")

# How many months of history the display window covers, for the gap check.
HISTORY_MONTHS = 12

# account_tx page size. 200 is the usual server-side cap for a non-admin
# connection; the walker pages with a marker rather than raising this.
PAGE_LIMIT = 200


def ripple_time_to_utc(ripple_seconds) -> datetime | None:
    """XRPL `date` fields count seconds since 2000-01-01, not the Unix epoch."""
    try:
        return datetime.fromtimestamp(int(ripple_seconds) + RIPPLE_EPOCH, tz=UTC)
    except (TypeError, ValueError):
        return None


def load_cohort() -> dict:
    """The 20-account monthly-release cohort: category=ripple minus the RLUSD
    issuer. Same filter as escrow_supply.fetch_escrow_locked and
    escrow_walker — kept identical on purpose so the history section and the
    locked hero can never disagree about who is in the cohort."""
    try:
        with open(NAMED_ACCOUNTS_PATH) as f:
            named = json.load(f) or {}
    except (OSError, json.JSONDecodeError) as e:
        log.error("named_accounts.json unreadable: %s", type(e).__name__)
        return {}
    return {
        addr: meta
        for addr, meta in named.items()
        if isinstance(meta, dict)
        and meta.get("category") == "ripple"
        and "RLUSD" not in (meta.get("name") or "")
    }


def _escrow_from_meta(meta: dict) -> dict:
    """Pull the released Escrow object's FinalFields out of tx metadata.

    EscrowFinish carries NO Amount field of its own — the amount released is
    whatever the deleted Escrow ledger object held. Reading it from the
    DeletedNode's FinalFields is the only way to get the figure without a
    second lookup, and it is the authoritative value because it is what the
    ledger actually removed.
    """
    for node in meta.get("AffectedNodes") or []:
        deleted = node.get("DeletedNode") or {}
        if deleted.get("LedgerEntryType") != "Escrow":
            continue
        return deleted.get("FinalFields") or {}
    return {}


def row_from_tx(entry: dict, cohort: dict, source: str) -> dict | None:
    """Map one account_tx entry to an escrow_unlock_events row.

    Returns None when the entry is not a cohort-owned escrow lifecycle
    transaction, or did not succeed. Both filters matter:

      * OWNER scope — for EscrowFinish the owner is `Owner`, while `Account`
        is merely whoever submitted it. Scoping by submitter would drag in
        every unrelated escrow a third party finished in the same month.
      * tesSUCCESS only — a failed EscrowFinish still costs a fee and still
        appears in account_tx, but it released nothing. Recording it would
        inflate the released total with transactions the ledger rejected.
    """
    tx = entry.get("tx_json") or entry.get("tx") or {}
    meta = entry.get("meta") or entry.get("metaData") or {}
    if not isinstance(meta, dict):
        return None

    tx_type = tx.get("TransactionType")
    if tx_type not in ESCROW_TX_TYPES:
        return None

    tx_result = meta.get("TransactionResult")
    if tx_result != "tesSUCCESS":
        return None

    if tx_type == "EscrowCreate":
        owner = tx.get("Account")
        destination = tx.get("Destination")
        amount_raw = tx.get("Amount")
    else:  # EscrowFinish
        owner = tx.get("Owner")
        final = _escrow_from_meta(meta)
        destination = final.get("Destination")
        amount_raw = final.get("Amount")

    if owner not in cohort:
        return None

    # `date` (ripple-epoch seconds) is the long-standing shape; api_version 2
    # returns close_time_iso instead. Try the numeric form first, then ISO.
    # Never coerce a missing value: int(False) would silently date the row to
    # the year 2000 and file a real release under the wrong month.
    close_time = ripple_time_to_utc(tx.get("date") or entry.get("date"))
    if close_time is None:
        iso = entry.get("close_time_iso") or tx.get("close_time_iso")
        if iso:
            try:
                close_time = datetime.fromisoformat(
                    str(iso).replace("Z", "+00:00")
                ).astimezone(UTC)
            except ValueError:
                close_time = None
    if close_time is None:
        log.warning("tx %s has no usable date; skipped", tx.get("hash"))
        return None

    tx_hash = tx.get("hash") or entry.get("hash")
    ledger_index = entry.get("ledger_index") or tx.get("ledger_index")
    if not tx_hash or ledger_index is None:
        log.warning("escrow tx missing hash/ledger_index; skipped")
        return None

    if amount_raw is None:
        # An EscrowFinish whose metadata carried no deleted Escrow node is
        # malformed for our purposes. Skip rather than write a row that would
        # violate the amount_drops constraint or fabricate a zero release.
        log.warning("escrow tx %s has no Amount in tx or meta; skipped", tx_hash)
        return None

    denom, drops, amount_json = _classify_amount(amount_raw)
    if denom == "XRP" and drops is None:
        log.warning("escrow tx %s has unparseable XRP amount; skipped", tx_hash)
        return None

    return {
        "tx_hash": tx_hash,
        "tx_type": tx_type,
        "ledger_index": int(ledger_index),
        "close_time": close_time,
        "denom": denom,
        "amount_drops": drops,
        "amount_json": amount_json,
        "escrow_owner": owner,
        "escrow_destination": destination,
        "submitter": tx.get("Account"),
        "tx_result": tx_result,
        "source": source,
    }


def _fetch_account_escrow_txs(client, address, cutoff, cohort, source):
    """Page account_tx backwards for one account until past the cutoff.

    Returns (rows, ok). ok=False means the account could not be read at all,
    which the caller counts toward the coverage decision instead of silently
    treating as "no releases this month".
    """
    rows, marker = [], None
    while True:
        try:
            resp = client.request(AccountTx(
                account=address,
                ledger_index_min=-1,
                ledger_index_max=-1,
                limit=PAGE_LIMIT,
                marker=marker,
                forward=False,
            ))
        except Exception as e:
            log.warning("account_tx failed account=%s src=%s err=%s",
                        address, source, type(e).__name__)
            return rows, False
        result = resp.result or {}
        if result.get("error"):
            log.warning("account_tx rpc-error account=%s src=%s err=%s",
                        address, source, result.get("error"))
            return rows, False

        transactions = result.get("transactions") or []
        past_cutoff = False
        for entry in transactions:
            tx = entry.get("tx_json") or entry.get("tx") or {}
            when = ripple_time_to_utc(tx.get("date") or entry.get("date"))
            if when is not None and when < cutoff:
                # account_tx with forward=False is newest-first, so the first
                # entry older than the cutoff means every remaining page is
                # older too. Stop paging rather than walking full history.
                past_cutoff = True
                break
            row = row_from_tx(entry, cohort, source)
            if row is not None:
                rows.append(row)

        marker = result.get("marker")
        if past_cutoff or not marker:
            break
    return rows, True


def own_node_earliest_close_time(client) -> datetime | None:
    """Close time of the earliest ledger our own node still holds.

    This is the coverage measurement that keeps the ~54k-ledger window from
    silently truncating the lookback. Returns None when it cannot be
    determined, which the caller treats as "assume no coverage" and falls
    back to a witness — the safe direction.
    """
    try:
        info = (client.request(ServerInfo()).result or {}).get("info") or {}
        complete = (info.get("complete_ledgers") or "").strip()
        if not complete or complete == "empty":
            return None
        # complete_ledgers looks like "32570-32570,54321000-54375000".
        # The LAST range is the live window; its start is the oldest ledger
        # continuously available up to the tip.
        earliest = int(complete.split(",")[-1].split("-")[0])
        lg = client.request(Ledger(ledger_index=earliest)).result or {}
        closed = (lg.get("ledger") or {}).get("close_time")
        return ripple_time_to_utc(closed)
    except Exception as e:
        log.warning("own-node coverage probe failed: %s", type(e).__name__)
        return None


def missing_months(months_present, window_months=HISTORY_MONTHS, now=None):
    """UTC month labels inside the display window with no observed rows.

    Pure so the gap logic is testable without a database. `months_present` is
    whatever db.read_escrow_unlock_months_present() returned (datetimes or
    dates); the comparison is on 'YYYY-MM' labels.
    """
    now = now or datetime.now(UTC)
    present = {m.strftime("%Y-%m") for m in months_present}
    expected = []
    year, month = now.year, now.month
    for _ in range(window_months):
        expected.append(f"{year:04d}-{month:02d}")
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    return [m for m in expected if m not in present]


def run() -> tuple[bool, int, str]:
    cohort = load_cohort()
    if not cohort:
        return False, 0, "no category=ripple addresses in named_accounts.json"

    if not db.ensure_escrow_unlock_events_table():
        return False, 0, "ensure_escrow_unlock_events_table returned False (DB unavailable / DDL failed)"

    now = datetime.now(UTC)
    cutoff = now - timedelta(days=LOOKBACK_DAYS)
    findings = []
    all_rows = []

    # --- Pass 1: our own node, for whatever span it actually holds ---------
    own_client = get_client(WALKER_NAME)
    earliest = own_node_earliest_close_time(own_client)
    own_covers_window = earliest is not None and earliest <= cutoff
    own_ok_accounts = 0
    for addr in cohort:
        rows, ok = _fetch_account_escrow_txs(
            own_client, addr, cutoff, cohort, db.SOURCE_OWN_NODE)
        all_rows.extend(rows)
        own_ok_accounts += 1 if ok else 0

    own_pass_usable = own_ok_accounts == len(cohort)
    if earliest is None:
        log.info("own-node coverage: UNKNOWN (probe failed) — witness pass will run")
    else:
        log.info("own-node earliest ledger close=%s cutoff=%s covers_window=%s",
                 earliest.isoformat(), cutoff.isoformat(), own_covers_window)

    # --- Pass 2: witness fallback, only when the own node is short --------
    witness_used = None
    if not (own_covers_window and own_pass_usable):
        reason = ("own node does not reach the cutoff"
                  if not own_covers_window else
                  f"own node errored on {len(cohort) - own_ok_accounts} account(s)")
        log.info("witness pass: %s", reason)
        for url in full_history_nodes():
            host = url.split("//")[-1].split("/")[0].split(":")[0]
            source = f"witness:{host}"
            wclient = JsonRpcClient(url)
            wrows, wok_all = [], True
            for addr in cohort:
                rows, ok = _fetch_account_escrow_txs(
                    wclient, addr, cutoff, cohort, source)
                wrows.extend(rows)
                if not ok:
                    wok_all = False
                    break
            if wok_all:
                all_rows.extend(wrows)
                witness_used = source
                log.info("witness pass complete via %s (+%d rows)", source, len(wrows))
                break
            log.warning("witness %s incomplete; trying next", source)
        if witness_used is None:
            findings.append(
                f"lookback window ({LOOKBACK_DAYS}d) not fully covered: "
                f"own node short and every full-history witness failed")

    # --- Write ------------------------------------------------------------
    written = db.record_escrow_unlock_events(all_rows)
    if written is None:
        return False, 0, f"record_escrow_unlock_events returned None (DB write error), {len(all_rows)} rows pending"

    # --- Gap check across the display window ------------------------------
    gaps = missing_months(db.read_escrow_unlock_months_present(HISTORY_MONTHS),
                          HISTORY_MONTHS, now=now)
    if gaps:
        findings.append(
            f"{len(gaps)} month(s) in the {HISTORY_MONTHS}-month window have "
            f"no observed rows: {', '.join(gaps)} — run the 12-month backfill "
            f"(ops/backfill_escrow_unlock_history.py) if this is not a "
            f"genuinely release-free month")

    for f in findings:
        log.warning("FINDING: %s", f)

    src_desc = db.SOURCE_OWN_NODE if witness_used is None else f"own-node+{witness_used}"
    msg = (
        f"cohort={len(cohort)} · seen={len(all_rows)} · written={written} · "
        f"source={src_desc} · own_covers_{LOOKBACK_DAYS}d={own_covers_window} · "
        f"gaps={len(gaps)}"
    )
    return True, len(findings), msg


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
