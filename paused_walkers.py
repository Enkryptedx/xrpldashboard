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
PAUSED_WALKERS: dict[str, dict[str, str | None]] = {
    "supply_escrow_walker": {
        "paused_since": "2026-10-15",
        "reason": (
            "escrow incremental redesign; full ledger_data(type=escrow) "
            "enumeration stopped finishing inside its 2700s hard cap "
            "(~78,279 pages at ~27 pages/s)"
        ),
        "tracking": "escrow stage 4 (owner-gated)",
        "replacement": "escrow_seed_walk",
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
    if d.get("replacement"):
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
