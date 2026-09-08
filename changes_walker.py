"""Daily walker that computes /changes envelope + writes it to disk.

Runs on StartInterval after the 01:00 UTC signed_snapshot + registry
snapshot cycles land, so today's snapshot is already in PG when we
diff. Writes changes/YYYY-MM-DD.json + updates walker_health with the
run outcome.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import traceback

HERE = os.path.abspath(os.path.dirname(__file__))
sys.path.insert(0, HERE)

from changes_builder import build_changes_for_date  # noqa: E402

WALKER_NAME = "changes_walker"
CADENCE_SECONDS = 86400  # mirrors plist StartInterval


def run_for_date(date: dt.date) -> dict:
    import db
    envelope = build_changes_for_date(date)
    db.write_changes_envelope(date, envelope)
    print(f"wrote changes_envelopes[{date.isoformat()}]: "
          f"{len(envelope.get('changes', []))} changes")
    return envelope


def main(argv: list[str]) -> int:
    # Argv: [date_yyyy_mm_dd] or default to today (UTC)
    date = dt.datetime.now(dt.timezone.utc).date()
    if len(argv) > 1:
        try:
            date = dt.date.fromisoformat(argv[1])
        except ValueError:
            print(f"bad date: {argv[1]}", file=sys.stderr)
            return 2

    import db  # imported inside main so unit tests can stub
    db.write_walker_health_start(WALKER_NAME, cadence_seconds=CADENCE_SECONDS)
    try:
        env = run_for_date(date)
        n_changes = len(env.get("changes", []))
        db.write_walker_health_end(
            WALKER_NAME, ok=True,
            message=f"date={date.isoformat()} changes={n_changes}",
            findings_count=0,
        )
        return 0
    except Exception:  # noqa: BLE001
        tb = traceback.format_exc()
        print(tb, file=sys.stderr)
        db.write_walker_health_end(
            WALKER_NAME, ok=False,
            message=(f"date={date.isoformat()} "
                     f"exc={tb.strip().splitlines()[-1][:180]}"),
            findings_count=1,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
