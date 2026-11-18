#!/usr/bin/env python3
"""stdin -> stdout filter dropping Neon's `SET (pg_attver=...)` statements.

The pipe-friendly form of neon_dump_filter, for the hand-run disaster
restore in docs/RESTORE_RUNBOOK.md:

    pg_restore --no-owner --no-acl -f - dump.dump \\
      | python3 tools/neon_dump_filter_cli.py \\
      | psql -d target -v ON_ERROR_STOP=1 -q -f -

Dropped statements are reported on stderr so the restore is auditable, and
stderr never pollutes the SQL on stdout. Exits non-zero if a pg_attver
statement is not self-contained on one line, rather than corrupting the
stream.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from neon_dump_filter import UnterminatedAttverStatement, filter_stream


def main() -> int:
    dropped: list[str] = []
    try:
        for line in filter_stream(sys.stdin, dropped):
            sys.stdout.write(line)
    except UnterminatedAttverStatement as e:
        print(f"neon_dump_filter: refusing to filter a multi-line "
              f"pg_attver statement: {e}", file=sys.stderr)
        return 2
    sys.stdout.flush()
    print(f"neon_dump_filter: dropped {len(dropped)} pg_attver statement(s)",
          file=sys.stderr)
    for d in dropped:
        print(f"  {d[:160]}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
