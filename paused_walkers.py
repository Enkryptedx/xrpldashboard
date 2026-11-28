"""Explicit register of DELIBERATELY PAUSED walkers, plus one classifier.

Why this file exists
--------------------
A paused walker and a broken walker look identical in `walker_health`.
The project's pause convention (xrpl-dashboard-walker/pause-walker.md) is:

  * set `cadence_seconds = NULL`  -> `compute_walker_staleness()` returns
    is_stale=False, the documented path for event-triggered / manual /
    paused walkers;
  * leave `last_run_ok`, `consecutive_failures` and `last_failure_at`
    ALONE, because closing the row via `write_walker_health_end` would
    either stamp a fake `last_success_at` (ok=True) or manufacture a
    failure (ok=False);
  * record a `'paused <date>: <reason>'` message.

That is deliberate and correct -- but it means a paused walker keeps the
`last_run_ok=false` / `consecutive_failures=3` it died with. Any reader
that treats those two columns as "failing" reports a deliberate pause as
a fresh outage, every run, forever. The 8 PM evening summary did exactly
that for `supply_escrow_walker` for 15 consecutive nights (Oct 14-29
2026) while the pause was intentional and tracked.

`cadence_seconds = NULL` is NOT usable as the paused marker on its own:
it is also NULL for event-triggered and manual walkers, and `l1_pager`
deliberately watches cadence-less rows precisely so a paused walker does
not become unwatchable (see tools/l1_pager.py -- it records
`supply_escrow_walker` sitting wedged 17.5 h with nothing legitimately
watching it). So "no cadence" cannot mean "do not report".

Hence an EXPLICIT register, not a string match on `last_run_message`.
A free-text match would silently reclassify any walker whose operator
happened to write "paused" in a message, and would break on rewording.
Membership here is a deliberate, reviewable act: adding a walker takes a
PR, so a pause can never be inferred from prose.

This register lives in the repo rather than in a `walker_health` column
on purpose: adding a column needs a migration and a prod UPDATE, both
owner-gated, and the pause state is an operator decision that belongs
under review. If a structured DB marker is added later, point `is_paused`
at it and keep this module's signature.

PAUSED IS NOT HIDDEN. `classify()` returns a distinct "paused" state so a
caller can report it as paused. Never filter these rows out entirely --
that is how a pause becomes a permanent blind spot.
"""
from __future__ import annotations

# walker_name -> declaration. Every field is for humans reading a report.
#   paused_since : ISO date the pause was decided
#   reason       : why it is paused
#   tracking     : what has to happen before it resumes
#   replacement  : walker carrying the load meanwhile, or None
# EMPTY as of 2026-11-28: nothing is currently paused.
#
# supply_escrow_walker was the only entry, paused 2026-10-15. It is RESUMED
# on a daily schedule -- see RESUMED below for what "stage 4" turned out to
# mean. The map is deliberately left in place rather than deleted: it is the
# declared register, and an empty register is a true statement, while no
# register at all would make the next pause undeclarable.
PAUSED_WALKERS: dict[str, dict[str, str | None]] = {}

# ── Resumed walkers, kept as the record of what unblocked them ─────────
#
# Not consulted by is_paused()/classify() -- these are no longer paused. It
# exists so the reasoning survives: a walker that comes back after six weeks
# leaves a question behind, and "stage 4" in particular was referenced for
# six weeks without ever being defined anywhere in the repo.
RESUMED: dict[str, dict[str, str]] = {
    "supply_escrow_walker": {
        "paused_since": "2026-10-15",
        "resumed_on": "2026-11-28",
        "original_reason": (
            "escrow incremental redesign; full ledger_data(type=escrow) "
            "enumeration stopped finishing inside its 2700s hard cap "
            "(~78,279 pages at ~27 pages/s)"
        ),
        # THE REDEFINITION. "escrow stage 4 (owner-gated)" appeared only
        # here and in tests asserting that string -- no spec, no acceptance
        # criteria, no named trigger anywhere in the repo. Measurement
        # replaced it: the walk never needed to be incremental, it needed
        # room.
        "stage_4": (
            "REDEFINED 2026-11-28, and it got SMALLER. Stage 4 is no longer "
            "'build incremental escrow enumeration'; it is 'run the existing "
            "full walk once a day'. The 2700s cap the walk kept missing only "
            "existed to fit an HOURLY cadence, because the cap must stay "
            "below the cadence or a capped run overruns its interval and "
            "launchd relaunches it instantly (2026-10-12/13: 44 consecutive "
            "capped failures, zero idle, continuously hammering our own "
            "node). At a daily cadence that constraint nearly vanishes. "
            "Measured one-off run 2026-11-28: 79,427 pages in 1,120s at "
            "71.2 pages/s sustained -- 41% of the old cap, 31% of the new "
            "one. So the redesign was never the blocker; the schedule was."
        ),
        "resumed_as": (
            "daily StartCalendarInterval Hour=6 Minute=30 local ET, NO "
            "RunAtLoad (a ~19-minute own-node walk must not fire at install "
            "or boot, outside the quiet slot). CADENCE_SECONDS=86400, "
            "ENUM_HARD_CAP_SECONDS=3600 -- 4% of cadence, so the worst case "
            "still leaves 23h idle, and the cap tolerates 22.1 pages/s, "
            "below every rate ever observed including the ~27 that caused "
            "the pause."
        ),
        "slot_rationale": (
            "06:30 ET is the only real gap in the fixed-slot map: 70 min "
            "after auto_pull_mac (05:20), 2.5h before regulation_hard_trigger "
            "(09:00), 8.5h after pg_backup (22:00) and clear of its whole "
            "chain (b2_backup 03:00, dockvault_neon_dump 04:15), and 2h10m "
            "after escrow_unlock_walker (04:20) so the two escrow jobs never "
            "overlap. The 6-hourly mpt_holders_refresh is StartInterval with "
            "no calendar hour, so it DRIFTS and cannot be dodged by clock -- "
            "but it reads public Clio while this walk reads our own node, so "
            "there is no contention to dodge."
        ),
        "coverage": (
            "full-ledger escrow census refreshed daily at 06:30 ET; "
            "staleness watched by escrow_census_freshness_canary against the "
            "shared 26h threshold in supply_block"
        ),
    },
}

# Classification states returned by classify().
OK = "ok"
PAUSED = "paused"
FAILING = "failing"
STALE = "stale"


def is_paused(walker_name: str) -> bool:
    """True only for walkers explicitly declared above."""
    return walker_name in PAUSED_WALKERS


def declaration(walker_name: str) -> dict[str, str | None] | None:
    """The paused declaration for `walker_name`, or None."""
    d = PAUSED_WALKERS.get(walker_name)
    return dict(d) if d is not None else None


def classify(
    walker_name: str,
    last_run_ok: bool | None = None,
    consecutive_failures: int | None = None,
    is_stale: bool = False,
) -> str:
    """Classify one walker_health row into OK / PAUSED / FAILING / STALE.

    PAUSED wins over FAILING and STALE -- that is the whole point: a
    deliberately paused walker keeps the failure columns it died with, and
    those columns must stop being read as a live outage.

    PAUSED is only ever reached via the explicit register, so a genuine
    failure on an undeclared walker can never be masked by this function.
    """
    if is_paused(walker_name):
        return PAUSED
    if last_run_ok is False or (consecutive_failures or 0) > 0:
        return FAILING
    if is_stale:
        return STALE
    return OK


def describe(walker_name: str) -> str:
    """One-line human summary for a paused walker, for report output."""
    d = PAUSED_WALKERS.get(walker_name)
    if d is None:
        return walker_name
    bits = [f"paused since {d.get('paused_since')}"]
    if d.get("tracking"):
        bits.append(f"tracking: {d['tracking']}")
    # `coverage` is rendered VERBATIM. `replacement` (legacy) still reads as
    # "covered by <walker>" -- only use it when a named walker genuinely does
    # the paused walker's job, which for supply_escrow_walker nothing does.
    if d.get("coverage"):
        bits.append(d["coverage"])
    elif d.get("replacement"):
        bits.append(f"covered by {d['replacement']}")
    return f"{walker_name} ({'; '.join(bits)})"


def summarise(rows) -> dict[str, list[str]]:
    """Bucket walker_health rows for a report.

    `rows` is an iterable of mappings or sequences carrying at least
    walker_name / last_run_ok / consecutive_failures, optionally is_stale.
    Returns {"failing": [...], "paused": [...], "stale": [...]} with
    display strings. OK rows are omitted (reports only name exceptions),
    but PAUSED rows are ALWAYS listed so a pause stays visible.
    """
    out: dict[str, list[str]] = {FAILING: [], PAUSED: [], STALE: []}
    for row in rows:
        if isinstance(row, dict):
            name = row.get("walker_name")
            ok = row.get("last_run_ok")
            cf = row.get("consecutive_failures")
            stale = bool(row.get("is_stale"))
        else:
            name, ok, cf = row[0], row[1], row[2]
            stale = bool(row[3]) if len(row) > 3 else False
        state = classify(name, ok, cf, stale)
        if state == PAUSED:
            out[PAUSED].append(describe(name))
        elif state == FAILING:
            n = cf or 0
            out[FAILING].append(
                f"{name} ({n} consecutive)" if n else f"{name} (last run failed)"
            )
        elif state == STALE:
            out[STALE].append(name)
    return out
