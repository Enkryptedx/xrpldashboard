"""Blank-failure guard on write_walker_health_end (Charlie ruling
2026-09-21).

A walker calling `db.write_walker_health_end(walker_name, ok=False)`
without a real message leaves the row with `last_run_ok=false` and
`last_run_message=NULL`. Readers can't tell why the run failed. Charlie
ruling 2026-09-21: a blank failure is a bug in the walker, not just
the run. `write_walker_health_end` must raise on `ok=False` with a
blank/None/whitespace message so the walker author sees the traceback
and names the failure before it can stamp a bad row.

`ok=True` with a blank/None message is fine — success runs don't need
a message.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _run():
    import db

    # ok=False + None → ValueError
    for msg in (None, "", "   ", "\n", "\t "):
        try:
            db.write_walker_health_end("test_walker_blank_failure", ok=False, message=msg)
        except ValueError as e:
            assert "blank failure" in str(e) or "non-blank message" in str(e), (
                f"Expected 'blank failure' / 'non-blank message' in error, got: {e}"
            )
            print(f"OK  ok=False + message={msg!r} → ValueError")
        else:
            raise AssertionError(
                f"FAIL: ok=False + message={msg!r} did NOT raise. "
                f"The blank-failure guard is missing."
            )

    # ok=False + real message → no exception raised in the guard step.
    # (We patch _writer_execute_with_retry so we don't actually hit PG.)
    from unittest.mock import patch
    calls = []
    with patch.object(db, "_writer_execute_with_retry",
                      side_effect=lambda label, fn: calls.append(label)):
        db.write_walker_health_end("test_walker_ok_msg", ok=False, message="upstream 502")
        assert len(calls) == 1, f"Expected one _writer_execute call, got {len(calls)}"
        print("OK  ok=False + real message → passes guard, calls PG writer")

    # ok=True + None message → no exception (success runs don't need a message).
    calls.clear()
    with patch.object(db, "_writer_execute_with_retry",
                      side_effect=lambda label, fn: calls.append(label)):
        db.write_walker_health_end("test_walker_ok_none", ok=True, message=None)
        assert len(calls) == 1
        print("OK  ok=True + message=None → passes (success needs no message)")

    print("\nALL PASS")


def test_blank_failure_is_rejected():
    _run()


if __name__ == "__main__":
    _run()
