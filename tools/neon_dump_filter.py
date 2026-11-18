"""Strip Neon-only attoptions from a pg_dump SQL stream.

WHY
---
Neon stamps a per-column `pg_attver` into `pg_attribute.attoptions` for
columns added through its metadata-only ADD COLUMN fast path. pg_dump then
faithfully emits:

    ALTER TABLE ONLY public.page_view_bot_hashes
        ALTER COLUMN pattern_owned SET (pg_attver=2739598001400);

`pg_attver` is not an upstream attoption, so ANY vanilla Postgres rejects it:

    ERROR:  unrecognized parameter "pg_attver"

That broke pg_restore_test on 2026-11-17 (the Nov 14 provenance columns on
page_view_bot_hashes were the first to carry it) and it would break a REAL
disaster restore to vanilla Postgres identically -- which is why the same
step is written into the restore runbook, not just this script.

DESIGN
------
Generic by statement, not by column: anything setting `pg_attver` is dropped,
so the next Neon-added column needs no code change. Everything else is passed
through untouched and applied under `psql -v ON_ERROR_STOP=1`, so every OTHER
error still fails the restore loudly. We are removing Neon bookkeeping with
no data meaning -- never a constraint, default, index or row.
"""

from __future__ import annotations

import re

# Matches the attoption whether pg_dump emits it inline or wrapped, and
# whether or not whitespace surrounds the '='. Case-insensitive because
# SQL keywords are, even though pg_dump emits upper-case SET.
PG_ATTVER_RE = re.compile(r"SET\s*\(\s*pg_attver\s*=", re.IGNORECASE)

# A dropped line must look like a complete, self-contained ALTER ... ; so we
# never silently swallow half of a multi-line statement and leave the rest to
# apply as a syntax error.
_TERMINATED = re.compile(r";\s*$")


class UnterminatedAttverStatement(RuntimeError):
    """Raised when a pg_attver statement is not self-contained on one line.

    pg_dump emits these on a single line. If that ever changes, failing
    closed is correct: silently dropping one line of a multi-line statement
    would corrupt the stream, and silently passing it through would
    reintroduce the restore error this module exists to remove.
    """


def filter_stream(lines, dropped: list[str] | None = None):
    """Yield `lines` with every `SET (pg_attver=...)` statement removed.

    `dropped` collects the removed statements for reporting, so a run can
    say exactly what it stripped instead of filtering invisibly.
    """
    for line in lines:
        if PG_ATTVER_RE.search(line):
            if not _TERMINATED.search(line):
                raise UnterminatedAttverStatement(line.strip()[:200])
            if dropped is not None:
                dropped.append(line.strip())
            continue
        yield line
