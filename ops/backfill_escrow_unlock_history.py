"""One-shot 12-month backfill of escrow_unlock_events.

Run ONCE, by hand, after migrations/2026_11_01_escrow_unlock_events.sql is
applied. The daily walker (scripts/escrow_unlock_walker.py) only looks back
3 days, so without this the /cold-storage release history starts empty and
fills in one month at a time.

    # see exactly what would be written, touch nothing:
    python3 ops/backfill_escrow_unlock_history.py --dry-run

    # then, to write:
    python3 ops/backfill_escrow_unlock_history.py --commit

--dry-run is the DEFAULT. Nothing is written unless --commit is passed, so a
bare invocation or a mistyped flag cannot mutate production.

WHY THIS READS FROM WITNESSES, NOT OUR OWN NODE
-----------------------------------------------
Our own rippled holds a ~54k-ledger window (~2.2 days). It physically cannot
answer a 12-month question. So this script reads the shared full-history
witness list from tools/anchor_nodes.py (Clio genesis-to-tip, then the
independent xrplcluster) and tags every row source='witness:<host>'.

That tagging is the whole safety story for re-running this script. The UPSERT
in db.record_escrow_unlock_events() enforces own-node-over-witness
precedence: it upgrades a witness row when the sovereign daily walker later
sees the same tx_hash, and it NEVER downgrades an own-node row to a witness
read. So this backfill is idempotent and safe to re-run at any time, in any
order relative to the walker. It cannot overwrite a sovereign read with a
third-party one, and it cannot double-count, because tx_hash is the PK.

SCOPE is identical to the walker's \u2014 the 20-account category=ripple cohort
minus the RLUSD issuer, scoped by escrow OWNER. It imports the walker's own
load_cohort() and row_from_tx() rather than restating either, so the backfill
and the daily job cannot drift on who is in the cohort or on how an
EscrowFinish amount is read out of metadata.

This script writes rows ONLY. It does not touch walker_health (it is not a
walker and must not look like one to the L1 pager), and it never writes to
escrows_snapshot or any supply total.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

from xrpl.clients import JsonRpcClient

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import db  # noqa: E402
from tools.anchor_nodes import full_history_nodes  # noqa: E402

sys.path.insert(0, os.path.join(HERE, "scripts"))
from escrow_unlock_walker import (  # noqa: E402
    _fetch_account_escrow_txs,
    load_cohort,
)

logging.basicConfig(
    format="%(asctime)s [backfill_escrow_unlock] %(levelname)s %(message)s",
    level=logging.INFO,
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

UTC = timezone.utc
BACKFILL_MONTHS = 12
# 12 months with a few days of slack so the oldest month is complete rather
# than clipped mid-month at whatever day-of-month this is run on.
BACKFILL_DAYS = 372


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--commit", action="store_true",
                    help="actually write rows (default is a dry run)")
    ap.add_argument("--days", type=int, default=BACKFILL_DAYS,
                    help=f"lookback in days (default {BACKFILL_DAYS})")
    args = ap.parse_args()
    dry_run = not args.commit

    cohort = load_cohort()
    if not cohort:
        log.error("no category=ripple addresses in named_accounts.json")
        return 1

    cutoff = datetime.now(UTC) - timedelta(days=args.days)
    log.info("mode=%s cohort=%d cutoff=%s",
             "DRY-RUN" if dry_run else "COMMIT", len(cohort), cutoff.isoformat())

    if not dry_run:
        if not db.ensure_escrow_unlock_events_table():
            log.error("ensure_escrow_unlock_events_table returned False "
                      "(DB unavailable / DDL failed) \u2014 refusing to continue")
            return 1

    # Walk the witness list in order; the first witness that answers for
    # EVERY cohort account wins. A partial read is discarded rather than
    # written, because a half-read witness would leave holes that look
    # exactly like release-free months to the page.
    rows = None
    used = None
    for url in full_history_nodes():
        host = url.split("//")[-1].split("/")[0].split(":")[0]
        source = f"witness:{host}"
        log.info("reading full history via %s", source)
        client = JsonRpcClient(url)
        collected, ok_all = [], True
        for addr, meta in cohort.items():
            got, ok = _fetch_account_escrow_txs(
                client, addr, cutoff, cohort, source)
            if not ok:
                log.warning("  %s (%s) FAILED on %s \u2014 discarding this witness",
                            addr, (meta.get("name") or "?"), source)
                ok_all = False
                break
            log.info("  %s (%s): %d escrow tx", addr, (meta.get("name") or "?"), len(got))
            collected.extend(got)
        if ok_all:
            rows, used = collected, source
            break

    if rows is None:
        log.error("every full-history witness failed \u2014 nothing written")
        return 1

    by_month = Counter()
    by_type = Counter()
    for r in rows:
        by_month[r["close_time"].strftime("%Y-%m")] += 1
        by_type[r["tx_type"]] += 1

    log.info("---- %d rows from %s ----", len(rows), used)
    for month in sorted(by_month, reverse=True):
        month_rows = [r for r in rows if r["close_time"].strftime("%Y-%m") == month]
        released = sum(r["amount_drops"] or 0 for r in month_rows
                       if r["tx_type"] == "EscrowFinish")
        re_escrowed = sum(r["amount_drops"] or 0 for r in month_rows
                          if r["tx_type"] == "EscrowCreate")
        log.info("  %s  tx=%-3d released=%15s  re-escrowed=%15s  net=%15s XRP",
                 month, by_month[month],
                 f"{released / 1_000_000:,.0f}",
                 f"{re_escrowed / 1_000_000:,.0f}",
                 f"{(released - re_escrowed) / 1_000_000:,.0f}")
    log.info("  types: %s", dict(by_type))
    log.info("  months covered: %d (expected ~%d)", len(by_month), BACKFILL_MONTHS)

    missing = BACKFILL_MONTHS - len(by_month)
    if missing > 0:
        log.warning("%d month(s) in the window produced no rows. That is only "
                    "legitimate if the cohort genuinely released nothing then "
                    "\u2014 check against the published schedule before accepting.",
                    missing)

    if dry_run:
        log.info("DRY-RUN \u2014 nothing written. Re-run with --commit to write.")
        return 0

    written = db.record_escrow_unlock_events(rows)
    if written is None:
        log.error("record_escrow_unlock_events returned None (DB write error)")
        return 1
    log.info("wrote %d row(s) (insert+update; own-node rows left untouched)", written)
    return 0


if __name__ == "__main__":
    sys.exit(main())
