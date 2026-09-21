"""Amendment tally backfill / reconstruction (Charlie ruling
2026-09-21 Mon PM, item 6).

For past dates that our signed leaves don't cover, reconstruct the
per-amendment tally by asking the Validator History Service (VHS)
what it can answer NOW about that date's state. Store labeled
`source=vhs_reconstructed` — NEVER as signed.

The signed leaves (from 2026-09-22 onward, once SCHEMA_VERSION 5
lands its first real leaf) carry the tally as of build time inside
`metrics[].name=='amendments_block'`. This script fills the gap for
dates BEFORE that.

Storage: `amendment_tally_reconstructions` PG table. One row per
(as_of_date, amendment_hash, source) tuple. Re-runs are idempotent
via ON CONFLICT DO NOTHING.

Source labels:
- **vhs_reconstructed**: VHS answered a historical query about that
  date. Confidence: as good as VHS's own historical record.
- **vhs_current_at_date_X**: VHS only returns "current" tallies, so
  we record what VHS says at time-of-reconstruction and stamp the
  date we captured it. Not a historical fact, just a snapshot with
  known provenance.

The public read path — a future `/amendments/history?date=YYYY-MM-DD`
or `/registry/amendments/YYYY-MM-DD.json` route — should surface the
`source` label prominently. Consumers must never treat these values
as if they were on a signed leaf.

Usage:
    python3 scripts/amendment_tally_reconstruct.py
    (grabs current VHS tally, stamps as_of_date=today,
     source=vhs_current_at_date_<today>. Re-run daily.)
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import db  # noqa: E402
import amendments_network_votes  # noqa: E402


def _write_row(as_of_date: str, amendment_hash: str, amendment_name: str,
               unl_votes_count: int, unl_validations: int,
               unl_threshold: int, source: str, source_url: str) -> None:
    if not db.pg_available():
        return

    def _do(conn):
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO amendment_tally_reconstructions "
                "  (as_of_date, amendment_hash, amendment_name, "
                "   unl_votes_count, unl_validations, unl_threshold, "
                "   source, source_url) "
                "VALUES (%s::date, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (as_of_date, amendment_hash, source) DO NOTHING",
                (as_of_date, amendment_hash, amendment_name,
                 unl_votes_count, unl_validations, unl_threshold,
                 source, source_url),
            )
    db._writer_execute_with_retry(
        f"amendment_tally_reconstruct[{as_of_date}][{amendment_hash[:8]}]",
        _do,
    )


def reconstruct_current(as_of_date: str | None = None) -> int:
    """Ask VHS for the current network tally and store one row per
    in-flight amendment labeled source=vhs_current_at_date_<today>."""
    if as_of_date is None:
        as_of_date = dt.date.today().isoformat()
    env = amendments_network_votes.fetch_network_vote_tallies_cached()
    data = (env or {}).get("data") or {}
    if not data:
        print(f"[reconstruct] no VHS data returned; nothing to write")
        return 0
    source = f"vhs_current_at_date_{as_of_date}"
    source_url = amendments_network_votes.ENDPOINT
    written = 0
    for h, entry in data.items():
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        count = entry.get("count")
        validations = entry.get("validations")
        threshold = entry.get("threshold")
        _write_row(
            as_of_date=as_of_date,
            amendment_hash=h.upper() if h else "",
            amendment_name=name,
            unl_votes_count=count,
            unl_validations=validations,
            unl_threshold=threshold,
            source=source,
            source_url=source_url,
        )
        written += 1
    print(
        f"[reconstruct] {as_of_date}: wrote {written} rows · source={source}"
    )
    return written


WALKER_NAME = "amendment_tally_reconstruct"
WALKER_CADENCE_SECONDS = 86400  # daily


def main() -> int:
    """Daily walker: reconstruct today's tally row via VHS. `0 rows`
    is treated as success (VHS may be temporarily silent — the walker
    stayed reachable and did what it was asked). A true failure raises
    and is caught below."""
    db.write_walker_health_start(WALKER_NAME, cadence_seconds=WALKER_CADENCE_SECONDS)
    ok = False
    message = "not_yet_stamped"
    try:
        written = reconstruct_current()
        message = f"wrote={written} rows source=vhs_current_at_date_{dt.date.today().isoformat()}"
        ok = True
        return 0
    except Exception as e:
        message = f"exception: {type(e).__name__}: {e}"
        ok = False
        raise
    finally:
        db.write_walker_health_end(
            WALKER_NAME, ok=ok,
            message=message or ("clean_no_message" if ok else "unlabeled_failure"),
        )


if __name__ == "__main__":
    sys.exit(main())
