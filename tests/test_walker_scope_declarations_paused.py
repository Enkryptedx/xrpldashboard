"""seed_walker_scope_declarations — the paused-walker declaration must be in
the REPO, not just the database (2026-10-14).

`walker_scope_declarations` is the Phase 1b escrow-lesson enforcement table:
every XRPL-reading walker in `walker_health` must have a row, and the Coverage
Register renders UNDECLARED — its own alarm class — for any walker missing
one. `seed_walker_scope_declarations.py` is the repo-side source of truth for
that table, and it is idempotent precisely so it can be re-run to restate
every declaration after a scope change.

On 2026-10-14 `supply_escrow_walker` was paused (launchd job booted out,
`cadence_seconds` NULLed so `compute_walker_staleness()` stops paging a walker
that is not scheduled to run). The paused note was written STRAIGHT TO THE DB
and the walker had no entry in this seeder at all — it had never been declared
here, unlike the unrelated `escrow_walker`. So /coverage was correct while that
one hand-written row survived, and the next `python
seed_walker_scope_declarations.py` would have PURGED it: `main()` computes
`stale = existing - valid_names` from `SCOPES` and DELETEs every name not
listed. A declaration that exists only in the database is therefore not merely
undocumented, it is scheduled for deletion by the seeder's own purge arm — and
the walker would have silently reverted to UNDECLARED.

What this file pins:

1. `supply_escrow_walker` has an entry at all. This is the assertion that
   would have failed before this change, and the one that keeps the purge arm
   from eating the row.
2. The entry survives the purge: it is present in the same `valid_names` set
   `main()` builds, so the seeder can never classify it as stale.
3. It declares itself PAUSED and intentional in the scope line, so /coverage
   reads as deliberately-paused rather than broken or forgotten.
4. The note carries the three things a future reader needs and cannot
   reconstruct: WHY it is paused (the pages/s degradation), that the frozen
   figure is DISCLOSED on the page, and the restore path.
5. `honest_partial` is True — a paused walker is not serving full coverage,
   and declaring otherwise is the exact dishonesty this table exists to catch.
6. Walker names are unique, so adding an entry cannot shadow an existing
   declaration (the seeder keys on `walker_name`, so a duplicate would make
   the last one silently win).
7. Every entry is a well-formed 4-tuple of the right types — a malformed row
   would blow up `main()`'s unpacking mid-run, after it had already purged.
8. Guard-the-guard: the module really exposes a populated `SCOPES`, so none
   of the above can pass vacuously against an empty or renamed list.

Hermetic: imports the seeder module and inspects `SCOPES` in memory. No DB, no
network, `main()` is never called.
"""
import importlib.util
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SEEDER_PATH = os.path.join(REPO_ROOT, "seed_walker_scope_declarations.py")

PAUSED_WALKER = "supply_escrow_walker"


@pytest.fixture(scope="module")
def scopes():
    """Load SCOPES from the seeder without running its main()."""
    if REPO_ROOT not in sys.path:
        sys.path.insert(0, REPO_ROOT)
    spec = importlib.util.spec_from_file_location(
        "_seed_walker_scope_declarations_under_test", SEEDER_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.SCOPES


@pytest.fixture(scope="module")
def paused_entry(scopes):
    matches = [row for row in scopes if row[0] == PAUSED_WALKER]
    assert len(matches) == 1, (
        f"expected exactly one {PAUSED_WALKER} declaration, got {len(matches)}"
    )
    return matches[0]


# ── 8. guard-the-guard ───────────────────────────────────────────────────
def test_scopes_is_populated(scopes):
    """A renamed or emptied SCOPES must fail loudly, not pass vacuously."""
    assert isinstance(scopes, list)
    assert len(scopes) >= 30, f"SCOPES collapsed to {len(scopes)} entries"


# ── 1. the entry exists ──────────────────────────────────────────────────
def test_paused_walker_is_declared(scopes):
    """The assertion that would have failed before 2026-10-14."""
    names = [row[0] for row in scopes]
    assert PAUSED_WALKER in names, (
        f"{PAUSED_WALKER} is paused but has no declaration in the seeder; "
        "/coverage would render it UNDECLARED after the next seeder run"
    )


# ── 2. it survives the seeder's own purge arm ────────────────────────────
def test_paused_walker_survives_purge(scopes):
    """main() DELETEs any DB row whose name is absent from SCOPES."""
    valid_names = {row[0] for row in scopes}
    assert PAUSED_WALKER in valid_names


# ── 3. declared paused and intentional ───────────────────────────────────
def test_scope_line_declares_paused_and_intentional(paused_entry):
    declared_scope = paused_entry[1]
    assert "PAUSED" in declared_scope
    assert "intentional" in declared_scope.lower()


# ── 4. the note keeps what cannot be reconstructed ───────────────────────
def test_note_records_why_paused(paused_entry):
    """The pages/s degradation is the reason; without it the pause is a mystery."""
    note = paused_entry[2]
    assert "90.9" in note and "23.7" in note
    assert "pages/s" in note


def test_note_records_that_frozen_figure_is_disclosed(paused_entry):
    """A frozen number presented as current is the trust defect; say it is stamped."""
    note = paused_entry[2]
    assert "refresh" in note.lower() and "paused" in note.lower()
    assert "disclosed" in note.lower()


def test_note_records_restore_path(paused_entry):
    note = paused_entry[2]
    assert "Restore by" in note
    assert "launchd" in note
    assert "cadence_seconds" in note


def test_note_records_staleness_suppression(paused_entry):
    """Why the canary stops paging — the NULLed cadence, with its code path."""
    note = paused_entry[2]
    assert "compute_walker_staleness()" in note
    assert "NULL" in note


# ── 5. honest about partial coverage ─────────────────────────────────────
def test_paused_walker_is_honest_partial(paused_entry):
    assert paused_entry[3] is True, (
        "a paused walker is not serving full coverage; honest_partial must be True"
    )


# ── 6. names unique ──────────────────────────────────────────────────────
def test_walker_names_are_unique(scopes):
    names = [row[0] for row in scopes]
    dupes = sorted({n for n in names if names.count(n) > 1})
    assert not dupes, f"duplicate walker declarations would silently shadow: {dupes}"


# ── 7. every entry well-formed ───────────────────────────────────────────
def test_every_entry_is_a_wellformed_4_tuple(scopes):
    for row in scopes:
        assert isinstance(row, tuple), f"not a tuple: {row!r}"
        assert len(row) == 4, f"expected 4 fields, got {len(row)}: {row[0]!r}"
        walker_name, declared_scope, filter_note, honest_partial = row
        assert isinstance(walker_name, str) and walker_name.strip()
        assert isinstance(declared_scope, str) and declared_scope.strip()
        assert isinstance(filter_note, str) and filter_note.strip()
        assert isinstance(honest_partial, bool), (
            f"{walker_name}: honest_partial must be a bool, got "
            f"{type(honest_partial).__name__}"
        )
