"""nft_activity_gap_fill.py — one-shot Clio gap-fill for the outage-tail range.

Filed 2026-09-20 morning after the anchor-#7 pull-plug + Mac power outage
(2026-09-15 → 2026-09-19) left a 2,739-ledger gap in nft_activity:

  Old cursor (frozen when the walker went down): 107,015,623
  Lenovo's rolling-window low bound today:       107,018,362

Ledgers 107,015,624 → 107,018,361 fall INSIDE this gap. Sept-15-tail NFT
activity that Lenovo has already pruned from its cache. The walker's
forward cursor was jumped to Lenovo's low so post-outage catch-up runs at
own-node speed (~8,500 ledgers/hr); this script fills the skipped tail
via public Clio and inserts rows using the same on-conflict-tx_hash
dedup pattern as the walker's activity+backfill modes, so a re-run is
safe and forward-pass overlap is safe.

Charlie approved 2026-09-20 06:59 ET. Written for one run; guard the
range explicitly and refuse to run outside it without --allow-any-range.

Cost estimate: 2,739 ledgers × ~1 s/tx via s2-clio.ripple.com (steady
state on Sun morning US) ≈ 45–60 min wall. Runs inline, prints progress
every 250 ledgers.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xrpl.clients import JsonRpcClient  # type: ignore

import db  # noqa: E402
from nft_activity_walker import (  # noqa: E402
    _fetch_ledger_txs_from,
    _nft_rows_from_ledger,
)

# Fixed range for the Sept-15-tail outage gap. Refuse to run outside this
# range unless --allow-any-range is passed — this script has no business
# ranging beyond the incident it was filed for.
GAP_LO = 107015624   # first ledger to fetch (inclusive)
GAP_HI = 107018361   # last  ledger to fetch (inclusive)

PUBLIC_CLIO_URL = "https://s2-clio.ripple.com:51234"
PROGRESS_EVERY = 250
INSERT_BATCH = 128


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--from", dest="lo", type=int, default=GAP_LO,
        help=f"first ledger to fetch (default: {GAP_LO})",
    )
    p.add_argument(
        "--to", dest="hi", type=int, default=GAP_HI,
        help=f"last  ledger to fetch (default: {GAP_HI})",
    )
    p.add_argument(
        "--clio-url", default=os.environ.get("NFT_GAP_FILL_CLIO_URL", PUBLIC_CLIO_URL),
        help="public Clio JSON-RPC URL (default: s2-clio.ripple.com:51234)",
    )
    p.add_argument(
        "--allow-any-range", action="store_true",
        help="allow --from/--to outside the fixed 2026-09-15 gap range.",
    )
    p.add_argument("--dry-run", action="store_true",
                   help="fetch + parse; print row count per ledger; don't insert.")
    args = p.parse_args()

    if not args.allow_any_range and (args.lo != GAP_LO or args.hi != GAP_HI):
        print(
            f"REFUSE: range {args.lo}..{args.hi} differs from the fixed "
            f"gap {GAP_LO}..{GAP_HI}. Pass --allow-any-range if you're sure.",
            file=sys.stderr,
        )
        return 2

    if args.lo > args.hi:
        print(f"REFUSE: --from ({args.lo}) > --to ({args.hi})", file=sys.stderr)
        return 2

    if not db.pg_available():
        print("REFUSE: DATABASE_URL not configured", file=sys.stderr)
        return 3

    print(
        f"[gap-fill] ledgers {args.lo:,} → {args.hi:,} "
        f"({args.hi - args.lo + 1:,} total) via {args.clio_url}"
    )
    client = JsonRpcClient(args.clio_url)
    started = time.monotonic()

    rows_buf: list[dict] = []
    total_ledgers = 0
    total_rows = 0
    total_inserted = 0
    empty_ledgers = 0
    fetch_fails = 0

    def flush():
        nonlocal total_inserted, rows_buf
        if not rows_buf:
            return
        if not args.dry_run:
            total_inserted += db.insert_nft_activity_batch(rows_buf)
        rows_buf = []

    try:
        for seq in range(args.lo, args.hi + 1):
            close_time, txs = _fetch_ledger_txs_from(client, seq)
            total_ledgers += 1
            if close_time is None:
                fetch_fails += 1
                # Fetch failure was already logged by _fetch_ledger_txs_from;
                # skip this ledger — a re-run picks it up.
                continue
            if not txs:
                empty_ledgers += 1
                if total_ledgers % PROGRESS_EVERY == 0:
                    elapsed = time.monotonic() - started
                    rate = total_ledgers / elapsed if elapsed > 0 else 0
                    eta_min = (args.hi - seq) / rate / 60 if rate > 0 else 0
                    print(
                        f"[gap-fill] {total_ledgers:,}/{args.hi - args.lo + 1:,} "
                        f"ledgers · rows={total_rows} inserted={total_inserted} "
                        f"empty={empty_ledgers} fetch_fails={fetch_fails} "
                        f"rate={rate:.1f}/s ETA={eta_min:.0f} min"
                    )
                continue

            rows = _nft_rows_from_ledger(seq, close_time, txs)
            total_rows += len(rows)
            rows_buf.extend(rows)
            if len(rows_buf) >= INSERT_BATCH:
                flush()

            if total_ledgers % PROGRESS_EVERY == 0:
                elapsed = time.monotonic() - started
                rate = total_ledgers / elapsed if elapsed > 0 else 0
                eta_min = (args.hi - seq) / rate / 60 if rate > 0 else 0
                print(
                    f"[gap-fill] {total_ledgers:,}/{args.hi - args.lo + 1:,} "
                    f"ledgers · rows={total_rows} inserted={total_inserted} "
                    f"empty={empty_ledgers} fetch_fails={fetch_fails} "
                    f"rate={rate:.1f}/s ETA={eta_min:.0f} min"
                )
        flush()
    except KeyboardInterrupt:
        flush()
        elapsed = time.monotonic() - started
        print(
            f"\n[gap-fill] interrupted after {total_ledgers:,} ledgers "
            f"({elapsed:.0f} s). Progress preserved via ON CONFLICT dedup — "
            f"safe to re-run.",
            file=sys.stderr,
        )
        return 130

    elapsed = time.monotonic() - started
    print(
        f"[gap-fill] DONE · ledgers={total_ledgers:,} · rows={total_rows} · "
        f"inserted={total_inserted} · empty_ledgers={empty_ledgers} · "
        f"fetch_fails={fetch_fails} · elapsed={elapsed:.0f}s "
        f"({elapsed/60:.1f} min)"
    )
    if fetch_fails > 0:
        print(
            f"[gap-fill] {fetch_fails} ledgers had fetch failures — re-run "
            f"to retry those. Public Clio occasionally returns tooBusy under "
            f"load; the ON CONFLICT (tx_hash) DO NOTHING pattern makes retries "
            f"idempotent.",
            file=sys.stderr,
        )
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
