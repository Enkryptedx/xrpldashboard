#!/usr/bin/env python3
"""escrow_seed_walk — stage 1 of the escrow incremental redesign.

Builds the authoritative escrow set with ONE full `ledger_data(type=escrow)`
walk, pinned to a single ledger index, resumable, off-peak, and with NO hard
cap. See docs/ESCROW_INCREMENTAL_DESIGN_2026-10-14.md.

This is NOT wired to the site. It writes `escrow_set` and
`escrow_seed_progress` only. The homepage keeps its amber
`as of <UTC>` + `refresh paused` stamp until stage 4.

WHY THE PIN (owner requirement, 2026-10-14)
-------------------------------------------
Every `ledger_data` call passes a FIXED `ledger_index` N -- never
"validated". A walk that lets the ledger advance underneath it produces a
blend of several ledger states: an escrow finished mid-walk is missed or
double-counted depending on where the marker happened to be, and the
resulting total reconciles against nothing. Pinning makes the seed a true
snapshot of one ledger, which is the defined starting point the stage-2
delta stream needs -- it applies deltas only from N+1 onward.

The walk must finish while N is still in the node's retained history.
Measured 2026-10-14: `complete_ledgers = 107568362-107658759`, a span of
90,397 ledgers (~100.4 h at ~4 s/ledger) against a ~30 min walk at the
then-current 44.1 pages/s. Ample, but not guaranteed -- online_delete or a
restart can shrink the window -- so the pin is re-checked on resume and at
completion. If N has fallen out, the attempt is ABANDONED and a new one
starts. A set stitched across two pinned ledgers is the blended-state bug
above, so it is never patched.

WHY NO HARD CAP
---------------
`supply_escrow_walker` had ENUM_HARD_CAP_SECONDS, and that cap is exactly
what truncated walks mid-flight once throughput fell 90.9 -> 23.7 pages/s.
Rather than bounding the walk, this seed is made restartable: the marker is
checkpointed after every page, so a reap, crash or restart resumes against
the same N instead of starting over.

WHY OFF-PEAK
------------
At the degraded rate the old walker was enumerating almost continuously,
which is plausibly part of what degraded the node -- a feedback loop. So the
seed yields on node load instead, the way census_watcher gates its census:
proceed only while load_factor is low and server_state is full/proposing.
"""
import logging
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

WALKER_NAME = "escrow_seed_walk"
LEDGER_DATA_PAGE_LIMIT = 400

# Checkpoint every page by default. Cheap relative to an RPC round trip, and
# the whole point is that an interrupted walk loses at most one page.
CHECKPOINT_EVERY_PAGES = int(
    os.environ.get("ESCROW_SEED_CHECKPOINT_EVERY", "1")
)
# Off-peak gate. census_watcher uses load_factor <= 5.0 with server_state in
# {full, proposing}; match it rather than inventing a second standard.
MAX_LOAD_FACTOR = float(os.environ.get("ESCROW_SEED_MAX_LOAD_FACTOR", "5.0"))
GOOD_SERVER_STATES = frozenset({"full", "proposing"})
# How long to wait out a busy node before giving up for this invocation.
LOAD_WAIT_SECONDS = int(os.environ.get("ESCROW_SEED_LOAD_WAIT", "300"))
LOAD_POLL_SECONDS = int(os.environ.get("ESCROW_SEED_LOAD_POLL", "30"))

REQUIRED_DB_FUNCS = (
    "ensure_escrow_set_tables",
    "escrow_seed_start",
    "escrow_seed_checkpoint",
    "escrow_seed_finish",
    "escrow_seed_resumable",
    "upsert_escrow_objects",
    "escrow_set_totals",
    "write_walker_health_start",
    "write_walker_health_end",
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
# ledger_data is called tens of thousands of times; httpx logs every request
# URL at INFO. Silenced at IMPORT scope, not inside a function, so it holds
# regardless of entry point (rlusd_refresher lesson, 2026-10-14).
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger(WALKER_NAME)


def missing_db_funcs(db_module, required=REQUIRED_DB_FUNCS) -> list:
    """Names in `required` that `db_module` does not provide.

    Checked up front because a mistyped db helper inside a best-effort
    `except` is invisible: on 2026-10-14 daemon_log_capper called
    `write_walker_health_finish` (no such function) and reported rc=0 while
    its health row stayed pinned at 'run_in_progress'.
    """
    return [n for n in required if not hasattr(db_module, n)]


def parse_complete_ledgers(value) -> list:
    """Parse rippled's `complete_ledgers` into [(lo, hi), ...].

    The field is a comma-separated list of ranges and can also be "empty" or
    a bare single ledger. Anything unparseable yields [] so callers treat it
    as "cannot prove retention" rather than "retained".
    """
    if not value or not isinstance(value, str):
        return []
    if value.strip().lower() in {"empty", "none"}:
        return []
    out = []
    for chunk in value.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        m = re.fullmatch(r"(\d+)\s*-\s*(\d+)", chunk)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
            if lo <= hi:
                out.append((lo, hi))
            continue
        if chunk.isdigit():
            n = int(chunk)
            out.append((n, n))
    return out


def ledger_in_retained_history(complete_ledgers, ledger_index) -> bool:
    """True only when `ledger_index` provably sits in a retained range."""
    for lo, hi in parse_complete_ledgers(complete_ledgers):
        if lo <= int(ledger_index) <= hi:
            return True
    return False


def node_is_off_peak(info, max_load_factor=MAX_LOAD_FACTOR) -> bool:
    """True when the node is idle enough to walk.

    Mirrors census_watcher's gate. A missing load_factor is treated as 1.0
    (rippled omits it when unloaded); a missing/odd server_state is NOT
    treated as good, because an unknown state is not evidence of health.
    """
    if not isinstance(info, dict):
        return False
    state = info.get("server_state")
    if state not in GOOD_SERVER_STATES:
        return False
    try:
        load = float(info.get("load_factor", 1.0))
    except (TypeError, ValueError):
        return False
    return load <= max_load_factor


def escrow_row_from_state_entry(entry) -> dict:
    """Map one `ledger_data` state entry to an escrow_set row.

    Only entries whose LedgerEntryType is exactly "Escrow" are accepted;
    anything else returns None. TokenEscrow is a DIFFERENT ledger entry type
    (the 2026-07-12 census counted 404 of those) and must not be folded into
    XRP escrow supply.

    `Amount` on an Escrow is XRP drops as a string. A non-XRP (object) Amount
    means a token escrow and is rejected rather than coerced -- silently
    treating an IOU amount as drops would corrupt the supply figure, which is
    the exact class of error this redesign exists to avoid.
    """
    if not isinstance(entry, dict):
        return None
    if entry.get("LedgerEntryType") != "Escrow":
        return None
    amount = entry.get("Amount")
    if isinstance(amount, dict):
        return None
    try:
        drops = int(amount)
    except (TypeError, ValueError):
        return None
    index = entry.get("index") or entry.get("LedgerIndex")
    owner = entry.get("Account")
    if not index or not owner:
        return None
    return {
        "escrow_index": index,
        "owner": owner,
        "destination": entry.get("Destination"),
        "amount_drops": drops,
        "sequence_num": entry.get("Sequence"),
        "condition": entry.get("Condition"),
        "finish_after": entry.get("FinishAfter"),
        "cancel_after": entry.get("CancelAfter"),
        "destination_tag": entry.get("DestinationTag"),
        "source_tag": entry.get("SourceTag"),
    }


def rows_from_page(state) -> list:
    """Map a page's `state` list to escrow_set rows, dropping non-Escrow."""
    rows = []
    for entry in state or []:
        row = escrow_row_from_state_entry(entry)
        if row is not None:
            rows.append(row)
    return rows


def wait_for_off_peak(client, server_info_request,
                      wait_seconds=LOAD_WAIT_SECONDS,
                      poll_seconds=LOAD_POLL_SECONDS, sleep=time.sleep):
    """Block until the node is off-peak, or return False on timeout."""
    deadline = time.time() + wait_seconds
    while True:
        info = client.request(server_info_request()).result.get("info", {})
        if node_is_off_peak(info):
            return True
        if time.time() >= deadline:
            log.warning(
                "node busy (state=%s load_factor=%s); giving up for this "
                "invocation", info.get("server_state"),
                info.get("load_factor"),
            )
            return False
        sleep(poll_seconds)


def main() -> int:
    import db
    from xrpl_client import get_client
    from xrpl.models.requests import LedgerData, Ledger, ServerInfo

    missing = missing_db_funcs(db)
    if missing:
        log.error(
            "db API contract broken - missing %s; refusing to run",
            ", ".join(missing),
        )
        return 1

    if not db.ensure_escrow_set_tables():
        log.error("could not ensure escrow tables; refusing to walk")
        return 1

    db.write_walker_health_start(WALKER_NAME)
    client = get_client()

    if not wait_for_off_peak(client, ServerInfo):
        db.write_walker_health_end(
            WALKER_NAME, ok=True, message="deferred: node not off-peak"
        )
        return 0

    info = client.request(ServerInfo()).result.get("info", {})
    complete = info.get("complete_ledgers")

    # Resume an in-flight attempt, or pin a fresh ledger.
    resumable = db.escrow_seed_resumable()
    if resumable:
        seed_id, pinned, marker, pages_done, objects_seen = resumable
        if not ledger_in_retained_history(complete, pinned):
            msg = (
                f"pinned ledger {pinned} no longer in retained history "
                f"({complete}); abandoning rather than stitching"
            )
            log.warning(msg)
            db.escrow_seed_finish(seed_id, "abandoned", msg)
            resumable = None
        else:
            log.info(
                "resuming seed %s at pinned ledger %s (%s pages, %s objects)",
                seed_id, pinned, pages_done, objects_seen,
            )

    if not resumable:
        pinned = client.request(Ledger(ledger_index="validated")).result
        pinned = pinned.get("ledger_index") or pinned["ledger"]["ledger_index"]
        seed_id = db.escrow_seed_start(pinned)
        marker, pages_done, objects_seen = None, 0, 0
        log.info("starting seed %s pinned at ledger %s", seed_id, pinned)

    t0 = time.time()
    try:
        while True:
            kw = dict(ledger_index=pinned, type="escrow",
                      limit=LEDGER_DATA_PAGE_LIMIT)
            if marker:
                kw["marker"] = marker
            result = client.request(LedgerData(**kw)).result
            rows = rows_from_page(result.get("state"))
            if rows:
                db.upsert_escrow_objects(rows, pinned)
            pages_done += 1
            objects_seen += len(rows)
            marker = result.get("marker")

            if pages_done % CHECKPOINT_EVERY_PAGES == 0 or not marker:
                db.escrow_seed_checkpoint(
                    seed_id, marker, pages_done, objects_seen
                )
            if not marker:
                break
    except Exception as exc:  # noqa: BLE001 - checkpoint survives; report it
        db.escrow_seed_checkpoint(seed_id, marker, pages_done, objects_seen)
        log.error("seed interrupted at page %s: %s", pages_done, exc)
        db.write_walker_health_end(
            WALKER_NAME, ok=False,
            message=f"interrupted at page {pages_done}: {exc}",
        )
        return 1

    elapsed = time.time() - t0

    # Re-check the pin AFTER the walk: if N aged out mid-walk the set may be
    # a blend, which is exactly what must never be trusted.
    info = client.request(ServerInfo()).result.get("info", {})
    if not ledger_in_retained_history(info.get("complete_ledgers"), pinned):
        msg = (
            f"pinned ledger {pinned} aged out of retained history during the "
            f"walk; set may be blended - abandoning"
        )
        log.warning(msg)
        db.escrow_seed_finish(seed_id, "abandoned", msg)
        db.write_walker_health_end(WALKER_NAME, ok=False, message=msg)
        return 1

    count, total_drops = db.escrow_set_totals(pinned)
    msg = (
        f"seeded {count} escrow objects, {total_drops} drops at pinned "
        f"ledger {pinned} ({pages_done} pages, {elapsed:.0f}s, "
        f"{pages_done / elapsed if elapsed else 0:.1f} pages/s)"
    )
    log.info(msg)
    db.escrow_seed_finish(seed_id, "completed", msg)
    db.write_walker_health_end(WALKER_NAME, ok=True, message=msg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
