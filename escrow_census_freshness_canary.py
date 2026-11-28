"""Freshness canary for the ESCROW half of the xrp_supply_block singleton.

The singleton has two halves written by different jobs, and they can rot
independently:

  total_*   written every ~60s by supply_fast_walker
  escrow_*  written by supply_escrow_walker -- PAUSED since 2026-10-15

Measured 2026-11-28: the total half was 0.01h old while the escrow half was
**45.3 days** old (escrow_fetched_at_utc 2026-10-14 07:41 UTC, ledger
107,645,974). Nothing paged for forty-five days, and the reason is worth
stating because it is a different fault class from the ones already covered:

  * walker_health says nothing, because supply_escrow_walker is correctly
    and deliberately PAUSED -- a paused walker is not a failing walker.
  * the generic walker staleness rule says nothing for the same reason:
    paused is an explicit exemption, as it should be.
  * the row itself looks healthy, because supply_fast_walker keeps the
    total half current and the table has data in every column.

So the gap is a DATA freshness question that no WALKER liveness signal can
answer: the job responsible is legitimately off, and the stale value it left
behind is still being read. The only honest watcher is one that looks at the
timestamp on the data itself.

This is the same lesson as the MPT holders walk sitting dead for 88 days,
one level further in: there, every signal asked the walker how its run went.
Here, every signal asks whether a walker is running. Neither asks whether
the NUMBER is still true.

CEILING. Default 26h, which pairs with resuming supply_escrow_walker on a
daily cadence (24h + ~2h grace), the shape proposed on 2026-11-28. While the
walker stays paused this canary reports a standing finding on purpose: the
census really is stale, the API now withholds it, and the finding is the
record that a reader-visible number is missing. Raise ESCROW_CENSUS_MAX_AGE_H
deliberately if the pause is expected to run long -- do not silence it by
deleting the check.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import sys

WALKER_NAME = "escrow_census_freshness_canary"
WALKER_CADENCE_SECONDS = 3600  # hourly; keep equal to the plist StartInterval

# THE shared threshold, not a local copy. Charlie ruling 2026-11-28: the page
# gate, the API redaction gate and this ceiling must be one number, or the
# page withholds a figure the API still serves and the canary disagrees with
# both. See supply_block.ESCROW_CENSUS_STALE_AFTER_SECONDS; a test asserts
# all three consumers read this exact value.
#
# Imported WITHOUT a hardcoded sys.path insert. A first draft did
# `sys.path.insert(0, "/Users/charliebruce/xrpl_test")` here at module level,
# which works when deployed but silently redirects any LATER import in the
# same process to the main checkout -- it made a verification script read
# app.py from main instead of the worktree and report the two constants as
# disagreeing when they did not. A module that rewrites global import state
# as a side effect of being imported is a trap for every other importer.
# supply_block sits in this same directory, so a plain import resolves it.
from supply_block import (  # noqa: E402
    ESCROW_CENSUS_STALE_AFTER_SECONDS as MAX_AGE_SECONDS,
)


def evaluate(fetched_at: dt.datetime | None,
             now: dt.datetime | None = None,
             max_age_seconds: int = MAX_AGE_SECONDS) -> tuple[int, str]:
    """(findings_count, message) for one escrow_fetched_at_utc value. Pure.

    A MISSING timestamp is a finding, not a pass: an empty escrow half means
    the census has never been written, which is strictly worse than stale.
    """
    now = now or dt.datetime.now(dt.timezone.utc)

    if fetched_at is None:
        return 1, (
            "FINDING: escrow half of xrp_supply_block has no "
            "escrow_fetched_at_utc at all -- the ledger-wide escrow census "
            "has never been written"
        )

    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=dt.timezone.utc)

    age = (now - fetched_at).total_seconds()
    age_h = age / 3600
    limit_h = max_age_seconds / 3600

    if age > max_age_seconds:
        return 1, (
            f"FINDING: escrow census is {age_h:.1f}h old "
            f"(limit {limit_h:.0f}h), as of {fetched_at.strftime('%Y-%m-%d %H:%M')} "
            f"UTC. supply_escrow_walker is paused; /api/xrp-distribution "
            f"withholds the escrow figures while this is true."
        )

    return 0, (
        f"clean: escrow census {age_h:.1f}h old (limit {limit_h:.0f}h), "
        f"as of {fetched_at.strftime('%Y-%m-%d %H:%M')} UTC"
    )


def read_escrow_fetched_at() -> tuple[dt.datetime | None, str | None]:
    """(fetched_at, error). Never raises."""
    try:
        sys.path.insert(0, "/Users/charliebruce/xrpl_test")
        import db
        if not db.pg_available():
            return None, "pg_unavailable"
        with db.pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT escrow_fetched_at_utc FROM xrp_supply_block LIMIT 1"
                )
                row = cur.fetchone()
        if not row:
            return None, "no_singleton_row"
        return row[0], None
    except Exception as e:  # noqa: BLE001
        return None, f"read_failed:{type(e).__name__}"


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    sys.path.insert(0, "/Users/charliebruce/xrpl_test")
    import db

    db.write_walker_health_start(WALKER_NAME,
                                 cadence_seconds=WALKER_CADENCE_SECONDS)

    fetched_at, err = read_escrow_fetched_at()
    if err:
        # A canary that cannot see must not report health.
        findings, message = 1, f"FINDING: could not read escrow half -- {err}"
    else:
        findings, message = evaluate(fetched_at)

    logging.info(message)
    print(f"[{WALKER_NAME}] {message}", flush=True)

    # ok=True whenever the check RAN; findings_count is the alert axis, same
    # discipline as xrpl_env_drift_guard and the mpt canaries.
    db.write_walker_health_end(
        WALKER_NAME, ok=True, message=message, findings_count=findings,
    )
    return 1 if findings else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
