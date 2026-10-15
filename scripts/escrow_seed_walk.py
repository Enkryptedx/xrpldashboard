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
import atexit
import logging
import os
import re
import sys
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

WALKER_NAME = "escrow_seed_walk"
LEDGER_DATA_PAGE_LIMIT = 400

# Checkpoint every 50 pages.
#
# This said "every page" and called it "cheap relative to an RPC round trip".
# That was measured on 2026-10-15 and is false, by about 1.7x in the wrong
# direction:
#
#   Neon round-trip (persistent conn)   27.4 ms median  (min 23.5, max 417.3)
#   local ledger_data RPC                16.4 ms
#
# So the checkpoint cost MORE than the call it was being excused against.
# Over a full 78,258-page walk that is 78,258 x 27.4 ms = ~2,144 s -- about
# 36 minutes, ~23% of the whole walk, spent waiting on Postgres rather than
# reading the ledger. At 50 it is ~1,566 writes, about 43 s.
#
# Why 50 is safe rather than merely cheaper. The cost of a lost checkpoint is
# bounded by re-walking at most 49 pages, which is a few seconds at any rate
# we have observed (8.6 pages/s degraded, ~50 warm). It cannot corrupt
# anything: resumability is page-aligned and escrow_set UPSERTs on
# escrow_index, so re-reading an already-written page is idempotent rather
# than a primary-key error -- overlap at the resume boundary is expected, not
# exceptional (stage-1 property, tests/test_escrow_seed_walk.py §4). The
# marker and the rejection tally are written in ONE statement, so they rewind
# together and a replayed page cannot double-count. And `or not marker` in the
# loop means the FINAL page always checkpoints, so a completed walk is never
# left short of its own total.
#
# Only a hard kill (SIGKILL, power loss) can lose a window at all: the
# exception path checkpoints before it returns.
#
# max(1, ...) because `pages_done % 0` would raise ZeroDivisionError inside
# the walk loop -- a mis-set env var should not be able to kill the walker.
CHECKPOINT_EVERY_PAGES = max(1, int(
    os.environ.get("ESCROW_SEED_CHECKPOINT_EVERY", "50")
))

# Mutual exclusion. mkdir is atomic on APFS and is the house pattern
# (run_supply_escrow_walker.sh does the same); see acquire_lock for why the
# pid is recorded and why a stale lock is stolen rather than respected.
LOCK_DIR = os.environ.get("ESCROW_SEED_LOCK_DIR",
                          "/tmp/escrow_seed_walk.lock")

# Progress output. Page-based OR time-based, whichever fires first --
# see should_log_progress for why the time half is the one that matters.
PROGRESS_EVERY_PAGES = int(
    os.environ.get("ESCROW_SEED_PROGRESS_EVERY_PAGES", "2000")
)
PROGRESS_EVERY_SECONDS = float(
    os.environ.get("ESCROW_SEED_PROGRESS_EVERY_SECONDS", "120")
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


# Canonical rejection reasons. `other_type:` is a PREFIX — the concrete
# LedgerEntryType is appended so an unexpected entry type names itself
# instead of hiding in a catch-all bucket.
REJECTION_REASONS = (
    "not_a_dict",
    "token_escrow",
    "other_type",
    "iou_amount",
    "unparseable_amount",
    "missing_index_or_owner",
)


def map_state_entry(entry):
    """Return (row, None) when an entry maps, (None, reason) when it does not.

    SINGLE SOURCE OF TRUTH for both the mapping and the rejection reason.
    The 2026-10-14 stopgap wrapper carried its own `classify_rejection` that
    MIRRORED these rules, and the two had already drifted: the wrapper tested
    missing-index before unparseable-amount while the mapper does the
    opposite, so an Escrow with both faults was labelled differently by the
    counter than by the code that actually rejected it. Deriving the reason
    from the same branches that make the decision is why that class of bug
    cannot come back.
    """
    if not isinstance(entry, dict):
        return None, "not_a_dict"
    etype = entry.get("LedgerEntryType")
    if etype == "TokenEscrow":
        # A DIFFERENT ledger entry type (404 counted in the 2026-07-12
        # census). Surfaced separately from other_type because it is THE
        # confusable one — folding it into XRP escrow supply is the exact
        # error this redesign exists to prevent. In practice
        # ledger_data(type="escrow") filters server-side so this branch is
        # not expected to fire; it stays because "not expected" is not
        # "cannot".
        return None, "token_escrow"
    if etype != "Escrow":
        return None, f"other_type:{etype}"
    amount = entry.get("Amount")
    if isinstance(amount, dict):
        return None, "iou_amount"
    try:
        drops = int(amount)
    except (TypeError, ValueError):
        return None, "unparseable_amount"
    index = entry.get("index") or entry.get("LedgerIndex")
    owner = entry.get("Account")
    if not index or not owner:
        return None, "missing_index_or_owner"
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
    }, None


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

    Thin wrapper over map_state_entry, which is the single source of truth
    for both the mapping and the rejection reason. Kept because this is the
    documented public shape and the stage-1 tests assert against it.
    """
    row, _reason = map_state_entry(entry)
    return row


def rows_from_page(state, tally=None, entry_types=None) -> list:
    """Map a page's `state` list to escrow_set rows, dropping non-Escrow.

    `tally` and `entry_types` are optional Counters incremented in place:
    rejections by reason, and a histogram of every LedgerEntryType seen.
    Counting happens HERE, where entries are mapped, so a rejection cannot
    be dropped without being counted.
    """
    rows = []
    for entry in state or []:
        if entry_types is not None:
            if isinstance(entry, dict):
                entry_types[entry.get("LedgerEntryType") or "<none>"] += 1
            else:
                entry_types["<non_dict>"] += 1
        row, reason = map_state_entry(entry)
        if row is not None:
            rows.append(row)
        elif tally is not None:
            tally[reason] += 1
    return rows


def format_tally(tally) -> str:
    """Compact, deterministic one-line rendering for the health message."""
    total = sum(tally.values())
    if not total:
        return "rejected 0"
    detail = ", ".join(f"{k}={v}" for k, v in sorted(tally.items()))
    return f"rejected {total} ({detail})"


def _pid_alive(pid) -> bool:
    """True when a process with this pid currently exists."""
    try:
        os.kill(int(pid), 0)
    except (ProcessLookupError, ValueError, TypeError):
        return False
    except PermissionError:
        return True          # exists, owned by another user
    return True


def read_lock_pid(lock_dir=None):
    """The pid recorded in a held lock, or None if absent/unreadable."""
    lock_dir = LOCK_DIR if lock_dir is None else lock_dir
    try:
        with open(os.path.join(lock_dir, "pid")) as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        return None


def acquire_lock(lock_dir=None, pid=None, pid_alive=_pid_alive) -> bool:
    """mkdir-based mutual exclusion. True when this process holds the lock.

    WHY A LOCK AT ALL. On 2026-10-15 the 00:30 attempt was SIGKILLed by its
    7200s cap at ~06:30:22Z and the 02:30 retry began at 06:30:30Z — an
    8-second gap. Nothing prevented them from overlapping. Two walks against
    the same seed row both write the same `marker` column, so the resume
    point would ping-pong between two positions and neither walk's page
    count would mean anything.

    WHY STALE HANDLING IS MANDATORY, not a nicety. The failure mode this
    lock exists to survive is a KILL, and a killed process cannot remove its
    own lock directory. A plain mkdir lock would therefore be held forever by
    a dead pid after precisely the event it was added for — turning a
    recoverable interruption into a walker that never runs again. So the pid
    is recorded inside the lock, and a lock whose pid is gone is stolen with
    a loud warning rather than respected.

    mkdir is atomic on APFS, which is why it is the house pattern here
    (run_supply_escrow_walker.sh uses the same trick; BSD flock/shlock
    semantics on macOS are unreliable).

    RESIDUAL RACE, stated rather than hidden: two processes that both find a
    stale lock in the same instant can both steal it. The window is
    microseconds against a job that runs nightly, and the damage is bounded
    anyway — escrow_set UPSERTs on escrow_index, and marker+tally share one
    UPDATE so they cannot desynchronise. Serialising the steal would need a
    second lock, which has the same problem one level down.
    """
    lock_dir = LOCK_DIR if lock_dir is None else lock_dir
    pid = os.getpid() if pid is None else pid
    try:
        os.mkdir(lock_dir)
    except FileExistsError:
        holder = read_lock_pid(lock_dir)
        if holder == pid:
            # Re-entry by the same process is not a steal; say so plainly so
            # the log cannot be misread as a crash-recovery event.
            log.debug("lock %s already held by this process", lock_dir)
        elif holder is not None and pid_alive(holder):
            log.warning(
                "another seed walk holds %s (pid %s); no-op skip",
                lock_dir, holder,
            )
            return False
        else:
            log.warning(
                "stealing stale lock %s (recorded pid %s is gone — previous "
                "run was killed before it could clean up)", lock_dir, holder,
            )
    except OSError as exc:
        log.error("could not create lock %s: %s", lock_dir, exc)
        return False
    try:
        with open(os.path.join(lock_dir, "pid"), "w") as fh:
            fh.write(str(pid))
    except OSError as exc:
        # Holding a lock we cannot identify is worse than not holding one:
        # the next run could never tell live from stale.
        log.error("could not record pid in %s: %s", lock_dir, exc)
        release_lock(lock_dir)
        return False
    return True


def release_lock(lock_dir=None) -> None:
    """Best-effort release. Never raises.

    Best-effort is sufficient *because* staleness is handled on acquire: a
    release missed by SIGKILL or SIGTERM leaves a lock the next run steals.
    """
    lock_dir = LOCK_DIR if lock_dir is None else lock_dir
    try:
        os.unlink(os.path.join(lock_dir, "pid"))
    except OSError:
        pass
    try:
        os.rmdir(lock_dir)
    except OSError:
        pass


def should_log_progress(pages_since_last, seconds_since_last,
                        every_pages=None, every_seconds=None) -> bool:
    """True when the walk should emit a progress line. Either threshold fires.

    The TIME threshold is the half that defuses
    `--no-output-timeout-seconds`. A page threshold alone is a function of
    throughput, so the slower the node gets the longer the walk stays silent
    — and a walk slow enough to trip a no-output timer is exactly the walk
    that most needs to prove it is alive. On 2026-10-15 the seed emitted ONE
    line at 00:30:03 and nothing for the next two hours; both 7200s timers
    were armed against that silence, and the walk was indistinguishable from
    a hang to anything watching it.

    A non-positive threshold disables that half rather than firing on every
    page.
    """
    every_pages = PROGRESS_EVERY_PAGES if every_pages is None else every_pages
    every_seconds = (PROGRESS_EVERY_SECONDS if every_seconds is None
                     else every_seconds)
    if every_pages > 0 and pages_since_last >= every_pages:
        return True
    return every_seconds > 0 and seconds_since_last >= every_seconds


def should_checkpoint(pages_done, marker, every=None) -> bool:
    """True when the walk should persist its resume point after this page.

    The `not marker` half is the load-bearing one: the FINAL page always
    checkpoints regardless of cadence, so a completed walk can never report
    fewer pages than it actually walked. Raising the cadence from 1 to 50 is
    only safe because of this — otherwise a walk whose page count was not a
    multiple of the cadence would close with a stale total.
    """
    every = CHECKPOINT_EVERY_PAGES if every is None else max(1, int(every))
    return pages_done % every == 0 or not marker


def format_walk_progress(pages_done, pages_this_run, elapsed_seconds) -> str:
    """Pages and throughput, keeping CUMULATIVE and THIS-RUN distinct.

    `pages_done` is cumulative across every attempt on this seed — it is
    restored from the checkpoint on resume. `elapsed_seconds` covers only the
    CURRENT invocation. Dividing one by the other is the bug this function
    exists to prevent.

    Measured 2026-10-15: the resumed run reported "36.7 pages/s" by dividing
    78,258 cumulative pages by its own 2,133s, when it had actually walked
    19,110 pages in that time — 9.0 pages/s. That 4x overstatement is not a
    cosmetic log defect: it made a walk running ~5x below the previously
    measured 44.1 pages/s look *better* than healthy, and it was the figure
    that made a 2h cap appear generous when a cold walk at the true rate
    needs ~2h32m. A throughput number that cannot be wrong in the
    reassuring direction is the point.

    A fresh walk emits the short form, because "19110 pages total, 19110
    this run" is noise. elapsed <= 0 yields 0.0 rather than dividing.
    """
    rate = (pages_this_run / elapsed_seconds) if elapsed_seconds > 0 else 0.0
    if pages_this_run == pages_done:
        return (f"{pages_done} pages, {elapsed_seconds:.0f}s, "
                f"{rate:.1f} pages/s")
    return (f"{pages_done} pages total, {pages_this_run} this run, "
            f"{elapsed_seconds:.0f}s, {rate:.1f} pages/s")


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

    # Mutual exclusion BEFORE any walker_health write: a second instance must
    # not stamp run_in_progress over the holder's row, and must not start
    # paging the node alongside it. A skip is a clean no-op (return 0) rather
    # than a failure, matching run_supply_escrow_walker.sh's "no-op skip" --
    # an overlapping fire is the lock working, not an error worth paging.
    #
    # Release is registered with atexit, which covers normal return and
    # unhandled exceptions. SIGKILL cannot run it and SIGTERM does not by
    # default, so those leave a lock the NEXT run steals after finding its
    # pid dead -- which is exactly the 2026-10-15 scenario.
    if not acquire_lock():
        return 0
    atexit.register(release_lock)

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
    tally, entry_types = Counter(), Counter()
    resumable = db.escrow_seed_resumable()
    if resumable:
        (seed_id, pinned, marker, pages_done, objects_seen,
         stored_rejections, stored_entry_types) = resumable
        if not ledger_in_retained_history(complete, pinned):
            msg = (
                f"pinned ledger {pinned} no longer in retained history "
                f"({complete}); abandoning rather than stitching"
            )
            log.warning(msg)
            db.escrow_seed_finish(seed_id, "abandoned", msg)
            resumable = None
        else:
            # Seed the counters from what the interrupted attempt already
            # counted, so the final tally describes the WHOLE walk rather
            # than only this slice. Without this the 2026-10-15 report could
            # only account for 19,110 of 78,258 pages — its 261 rejections
            # covered the resumed portion while the first 59,148 pages' 675
            # were simply gone, and recovering them cost a full re-walk.
            tally.update(stored_rejections or {})
            entry_types.update(stored_entry_types or {})
            log.info(
                "resuming seed %s at pinned ledger %s (%s pages, %s objects, "
                "%s carried forward)",
                seed_id, pinned, pages_done, objects_seen,
                format_tally(tally),
            )

    if not resumable:
        pinned = client.request(Ledger(ledger_index="validated")).result
        pinned = pinned.get("ledger_index") or pinned["ledger"]["ledger_index"]
        seed_id = db.escrow_seed_start(pinned)
        marker, pages_done, objects_seen = None, 0, 0
        tally, entry_types = Counter(), Counter()
        log.info("starting seed %s pinned at ledger %s", seed_id, pinned)

    # Baseline for throughput: pages_done may already be non-zero (restored
    # from a checkpoint), while t0 starts now. Only the delta between them
    # happened during THIS invocation. See format_walk_progress.
    pages_at_start = pages_done
    t0 = time.time()
    last_log_pages, last_log_time = pages_done, t0
    try:
        while True:
            kw = dict(ledger_index=pinned, type="escrow",
                      limit=LEDGER_DATA_PAGE_LIMIT)
            if marker:
                kw["marker"] = marker
            result = client.request(LedgerData(**kw)).result
            rows = rows_from_page(result.get("state"), tally, entry_types)
            if rows:
                db.upsert_escrow_objects(rows, pinned)
            pages_done += 1
            objects_seen += len(rows)
            marker = result.get("marker")

            if should_checkpoint(pages_done, marker):
                db.escrow_seed_checkpoint(
                    seed_id, marker, pages_done, objects_seen,
                    rejections=tally, entry_types=entry_types,
                )

            now = time.time()
            if should_log_progress(pages_done - last_log_pages,
                                   now - last_log_time):
                log.info(
                    "seed %s progress: %s, %s objects, %s",
                    seed_id,
                    format_walk_progress(pages_done,
                                         pages_done - pages_at_start,
                                         now - t0),
                    objects_seen, format_tally(tally),
                )
                last_log_pages, last_log_time = pages_done, now

            if not marker:
                break
    except Exception as exc:  # noqa: BLE001 - checkpoint survives; report it
        db.escrow_seed_checkpoint(seed_id, marker, pages_done, objects_seen,
                                  rejections=tally, entry_types=entry_types)
        log.error("seed interrupted at page %s: %s", pages_done, exc)
        db.write_walker_health_end(
            WALKER_NAME, ok=False,
            message=(f"interrupted at page {pages_done}: {exc} "
                     f"[{format_tally(tally)}]"),
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
        db.escrow_seed_finish(seed_id, "abandoned", msg, rejections=tally,
                              entry_types=entry_types)
        db.write_walker_health_end(WALKER_NAME, ok=False, message=msg)
        return 1

    count, total_drops = db.escrow_set_totals(pinned)
    progress = format_walk_progress(
        pages_done, pages_done - pages_at_start, elapsed
    )
    msg = (
        f"seeded {count} escrow objects, {total_drops} drops at pinned "
        f"ledger {pinned} ({progress}, {format_tally(tally)})"
    )
    log.info(msg)
    db.escrow_seed_finish(seed_id, "completed", msg, rejections=tally,
                          entry_types=entry_types)
    db.write_walker_health_end(WALKER_NAME, ok=True, message=msg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
