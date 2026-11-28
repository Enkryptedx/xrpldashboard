"""mpt_supply_history freshness canary.

WHY THIS EXISTS (2026-11-27). The MPT holders walk was dead for 88 days and
nothing paged. The last successful walk wrote holder rows at 2026-08-31
05:36:17Z; by the 2026-09-01 06:07Z daily snapshot every issuance read
`reason: pending`, and it stayed that way until the Clio re-point shipped
today. Three separate things each looked fine throughout:

  * `mpt_holders_refresh` exited 0 on every single run, so walker_health's
    ok/consecutive_failures axis stayed green. The walk "succeeded" at doing
    nothing.
  * its own counters mislabelled the failure -- 527 issuances were reported
    as `holders_incomplete` (a PAGINATION outcome) while the stored sentinel
    was `pending` (an RPC ERROR), so the number that was visible described
    a page cap that had not fired.
  * `history_rows=0` was printed on every run and watched by nobody.

The common shape: every existing signal asked the walker how its run went,
and the walker's run went fine. Nothing asked whether the TABLE had grown.
This canary asks only that, because a supply/holder time series that stops
advancing is the one fact that cannot be true while the walk is working.

THRESHOLD. 13 hours, per owner instruction 2026-11-27. The writer
(`mpt_holders_refresh`) moved hourly -> 6h today, so 13h is two missed
cycles plus an hour of slack: a single skipped run -- a sleeping Mac, one
Clio timeout, a slow cycle -- is never a page, while two consecutive
failures always are. Keep this in step with
`mpt_holders_refresh.WALKER_CADENCE_SECONDS` (21600) if the cadence moves
again; a ceiling below 2x cadence flaps, and one far above it is how 88
days go unnoticed.

ALERT POSTURE matches xrpl_env_drift_guard and the other canaries:
`ok=True` as long as the canary itself completed, with `findings_count > 0`
as the alert axis. The L1 pager and /walker_health both escalate on
findings_count, and conflating "my check found something" with "my check
crashed" is what makes consecutive_failures useless.

WHAT A MISSING DATABASE MEANS. Unreadable PG is reported as a FINDING, not
as silence and not as fresh. A canary that cannot see its table knows
nothing, and the permissive reading -- treat unknown as fine -- is the exact
failure this file exists to end.
"""
from __future__ import annotations

import datetime as dt
import logging
import sys

import db

WALKER_NAME = "mpt_supply_history_canary"
WALKER_CADENCE_SECONDS = 3600  # hourly

# Owner instruction 2026-11-27: page when no new rows for 13 hours.
STALE_CEILING_SECONDS = 13 * 3600  # 46800

# Mirrors mpt_holders_refresh.WALKER_CADENCE_SECONDS. Carried here only so
# the message can say how many writer cycles were missed, which is what
# makes the page actionable rather than just alarming.
WRITER_CADENCE_SECONDS = 21600


def check(now_ts: int | None = None) -> dict:
    """Read the newest snapshot_ts in mpt_supply_history and judge its age.

    `now_ts` is injectable so tests can place the newest row either side of
    the ceiling without touching the clock or sleeping.

    Returns a result dict; `findings` is 1 when the series is stale,
    unreadable or empty, else 0.
    """
    base = {
        "ceiling_seconds": STALE_CEILING_SECONDS,
        "age_seconds": None,
        "newest_ts": None,
        "rows_in_window": None,
    }
    if not db.pg_available():
        # NOT "fresh". See the module note on the permissive reading.
        return {**base, "findings": 1, "reason": "pg_unavailable"}

    now = int(dt.datetime.now(dt.timezone.utc).timestamp()) \
        if now_ts is None else int(now_ts)
    try:
        with db.pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT max(snapshot_ts) FROM mpt_supply_history")
                row = cur.fetchone()
                newest = row[0] if row else None
                # Row count inside the window, for the message only. A
                # single advancing issuance would clear the staleness test
                # while 500 others went dark, so the count is what tells a
                # reader which of those two they are looking at.
                cur.execute(
                    "SELECT count(*) FROM mpt_supply_history "
                    "WHERE snapshot_ts >= %s",
                    (now - STALE_CEILING_SECONDS,),
                )
                cnt_row = cur.fetchone()
                rows_in_window = cnt_row[0] if cnt_row else None
    except Exception as e:  # noqa: BLE001 -- a canary must not die on its DB
        return {**base, "findings": 1,
                "reason": f"query_failed_{type(e).__name__}"}

    if newest is None:
        return {**base, "findings": 1, "rows_in_window": 0,
                "reason": "table_empty_no_rows_ever"}

    newest = int(newest)
    age = now - newest
    out = {**base, "newest_ts": newest, "age_seconds": age,
           "rows_in_window": rows_in_window}

    if age > STALE_CEILING_SECONDS:
        missed = age // WRITER_CADENCE_SECONDS
        return {**out, "findings": 1,
                "reason": (f"stale_{age}s_gt_ceiling_{STALE_CEILING_SECONDS}s"
                           f"_approx_{missed}_missed_writer_cycles")}
    return {**out, "findings": 0, "reason": f"fresh_{age}s"}


def _message(r: dict) -> str:
    """One line a human can act on without opening psql."""
    newest_iso = (
        dt.datetime.fromtimestamp(r["newest_ts"], dt.timezone.utc).isoformat()
        if r.get("newest_ts") else "never")
    if r["findings"] == 0:
        return (f"clean: mpt_supply_history newest row {r['age_seconds']}s old "
                f"({newest_iso}), {r['rows_in_window']} rows inside the "
                f"{STALE_CEILING_SECONDS}s window")
    return (f"STALE[{r['reason']}]: mpt_supply_history newest row "
            f"{newest_iso} (age {r['age_seconds']}s, ceiling "
            f"{STALE_CEILING_SECONDS}s); rows in window "
            f"{r['rows_in_window']}; writer is mpt_holders_refresh "
            f"(cadence {WRITER_CADENCE_SECONDS}s)")


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    db.write_walker_health_start(
        WALKER_NAME, cadence_seconds=WALKER_CADENCE_SECONDS)

    r = check()
    msg = _message(r)
    logging.info(msg)
    print(f"[mpt_supply_history_canary] {msg}")

    # ok=True even with findings: the canary ran to completion. findings_count
    # is the alert axis the pager reads.
    db.write_walker_health_end(
        WALKER_NAME,
        ok=True,
        message=msg,
        findings_count=r["findings"],
    )
    return 0 if r["findings"] == 0 else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
