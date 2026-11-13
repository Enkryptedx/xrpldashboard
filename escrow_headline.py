"""Shared composition of the escrow headline figure and its as-of stamp.

WHY THIS MODULE EXISTS
----------------------
Two surfaces publish "how much XRP is locked in the Ripple monthly-release
escrow cohort": the /cold-storage hero and the homepage "XRP locked in
escrows" pill. Until 2026-11-13 both read `xrp_supply_block`'s escrow side,
written by supply_escrow_walker -- which was PAUSED on 2026-10-14 pending the
incremental redesign. So both surfaces have been publishing a frozen figure
(31.4B XRP / 98 objects) for a month while `escrows_snapshot`, refreshed
every 30 min by escrow_walker from our own node, carried the live one.

This module is the single composition point for that headline, so the two
surfaces cannot drift apart again and so the reader-visible stamp is a pure
function that a test can lock without a node or a DB.

WHAT THE FIGURE COVERS
----------------------
`escrows_snapshot` is a full account_objects(type=escrow) walk of the
20-account Ripple monthly-release ESCROW cohort. The headline counts the
XRP-denominated escrow objects only: a TokenEscrow (IOU/MPT) row carries no
drops and must never be summed into an XRP total.

Two account counts live on this page and they are NOT the same number, which
is exactly why each one now carries its own label:

  * 21 -- the BALANCE cohort (cold_storage.py, `tracked_count`): every
    category=ripple account whose liquid balance we track, including the
    RLUSD issuer, which holds no XRP escrows.
  * 20 -- the ESCROW cohort (`escrow_account_count`): the accounts this
    walker scans for escrow objects. Of those, only some OWN an escrow right
    now (8 at the time of writing) -- that third number is the owner count
    below, not a cohort size.

Ledger-wide escrow totals (`escrow_all_*`, ~1,887 accounts) are a different
population again and stay visually separate on both surfaces.

THE STAMP
---------
`escrows_snapshot.snapshot_ledger_index` is the validated ledger the walk
observed. It was NULL for every row written before 2026-11-13 (the walker
discarded the ledger index the RPC echoed back), so the stamp degrades to a
fetch-time-only form rather than inventing a ledger number.
"""

from __future__ import annotations

import time
import zoneinfo
from datetime import datetime, timezone

UTC = timezone.utc
ET_ZONE = zoneinfo.ZoneInfo("America/New_York")

DROPS_PER_XRP = 1_000_000

# The site's stamp convention is the stable label "ET", not the %Z
# abbreviation (app.py's "%a %b %-d, %-I:%M %p ET", roll_call_card.py, and the
# five "ET (" stamps already in templates/). %Z would print EST/EDT and make
# this one surface read differently from every other. The CONVERSION is still
# a real America/New_York conversion, so the clock time is DST-correct -- only
# the printed abbreviation is fixed.
_ET_FMT = "%-I:%M %p"
_UTC_FMT = "%H:%M UTC"

_SEP = " \u00b7 "  # middot, matching the stamp separator used across the site


def format_as_of_stamp(snapshot_ledger_index, fetched_at):
    """Compose the reader-visible as-of stamp for an escrows_snapshot read.

    With a ledger index:
        "as of ledger 101,234,567 - 4:12 PM ET (21:12 UTC)"
    Without one (snapshot_ledger_index still NULL on pre-2026-11-13 rows):
        "as of 4:12 PM ET (21:12 UTC)"
    With neither (empty table / PG down):
        None -- the caller renders its own "no data yet" state rather than
        stamping a figure it does not have.

    `fetched_at` is epoch seconds (the column is written as an int).
    """
    ledger_part = None
    if snapshot_ledger_index:
        try:
            ledger_part = f"as of ledger {int(snapshot_ledger_index):,}"
        except (TypeError, ValueError):
            ledger_part = None

    time_part = None
    if fetched_at:
        try:
            utc_dt = datetime.fromtimestamp(float(fetched_at), tz=UTC)
        except (TypeError, ValueError, OSError, OverflowError):
            utc_dt = None
        if utc_dt is not None:
            et_dt = utc_dt.astimezone(ET_ZONE)
            time_part = (
                f"{et_dt.strftime(_ET_FMT)} ET "
                f"({utc_dt.strftime(_UTC_FMT)})"
            )

    if ledger_part and time_part:
        return ledger_part + _SEP + time_part
    if ledger_part:
        return ledger_part
    if time_part:
        return "as of " + time_part
    return None


def build_escrow_headline(snap, escrow_cohort_size=None):
    """Reduce an escrows_snapshot read to the headline both surfaces render.

    `snap` is the dict shape db.read_escrows_snapshot() returns (rows +
    fetched_at + snapshot_ledger_index). Returns None when there is nothing
    to publish, so a caller can fall back rather than print "0 XRP locked".
    """
    rows = (snap or {}).get("rows") or []
    if not rows:
        return None

    total_xrp = 0.0
    object_count = 0
    owners = set()
    token_objects = 0
    for r in rows:
        if (r.get("denom") or "XRP") != "XRP":
            token_objects += 1
            continue
        drops = r.get("amount_drops")
        if drops is None:
            continue
        try:
            total_xrp += int(drops) / DROPS_PER_XRP
        except (TypeError, ValueError):
            continue
        object_count += 1
        if r.get("owner"):
            owners.add(r["owner"])

    if object_count == 0:
        return None

    ledger_index = snap.get("snapshot_ledger_index")
    fetched_at = snap.get("fetched_at")
    age = (int(time.time()) - int(fetched_at)) if fetched_at else None

    return {
        "total_xrp": total_xrp,
        "object_count": object_count,
        "owner_count": len(owners),
        "token_object_count": token_objects,
        "escrow_cohort_size": escrow_cohort_size,
        "ledger_index": int(ledger_index) if ledger_index else None,
        "fetched_at": int(fetched_at) if fetched_at else None,
        "age_seconds": age,
        "as_of": format_as_of_stamp(ledger_index, fetched_at),
        # False while the walker's ledger-index capture has not yet written a
        # row: the stamp is honest but coarser, and the template says so.
        "has_ledger": bool(ledger_index),
    }
