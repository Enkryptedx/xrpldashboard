#!/usr/bin/env python3
"""Evernorth daily treasury snapshot.

Runs once a day AFTER the 21:00 ET signed snapshot. Reads the 13 attributed
wallets from our own node, writes one row to evernorth_daily_snapshot in
Neon, and mirrors the same payload to a local JSON file.

Two deliberate choices:

* The snapshot date is the **Eastern** calendar date, not UTC. The job runs
  at 21:30 ET, which is already the next UTC day, so keying on UTC would
  file every reading under tomorrow and the card's "daily total" history
  would be a day ahead of the heading it sits under.

* An unreadable wallet is recorded as ``None``, never 0. Counting a failed
  read as a zero balance would understate the total and render on the card
  as an outflow that never happened. ``total_xrp`` already excludes
  unreadable rows and reports ``readable_count`` beside the total so a
  partial read is visible rather than silently wrong.

Writing is best-effort on both legs: a missed day must not kill the job, and
the local JSON is written even when Postgres is unavailable so the reading
is never lost entirely.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import sys
import time
import zoneinfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db  # noqa: E402
import institutional_treasuries as T  # noqa: E402

logging.basicConfig(
    format="%(asctime)s [evernorth_daily_snapshot] %(levelname)s %(message)s",
    level=logging.INFO,
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

ET = zoneinfo.ZoneInfo("America/New_York")
XRPL_NODE = os.environ.get("XRPL_NODE", "https://s1.ripple.com:51234")
LOCAL_JSON = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "evernorth_daily_snapshot.json",
)


def node_reader(addresses):
    """(balances, last_moved) read from our own node, tunnel-first.

    Returns drops per address, or None for any address we could not read.
    SovereignFetcher.call never raises and returns None on total failure,
    so one dead wallet degrades that row instead of the whole snapshot.
    """
    from sovereign_tunnel_client import SovereignFetcher

    fetcher = SovereignFetcher(
        public_url=XRPL_NODE, walker_name="evernorth_daily_snapshot",
    )
    balances, last_moved = {}, {}
    for addr in addresses:
        res = fetcher.call(
            "account_info", {"account": addr, "ledger_index": "validated"}
        )
        acct = (res or {}).get("account_data") or {}
        bal = acct.get("Balance")
        balances[addr] = int(bal) if bal not in (None, "") else None
        if balances[addr] is None:
            log.warning("unreadable balance for %s", addr)
    return balances, last_moved


def run(reader=None, now=None):
    reader = reader or node_reader
    now = now or dt.datetime.now(dt.timezone.utc)
    snap = T.fetch_treasury_snapshot(reader=reader, now=now, force=True)

    # Eastern calendar date — see module docstring.
    snapshot_date = now.astimezone(ET).date().isoformat()
    balances = {r["address"]: r["balance_xrp"] for r in snap["rows"]}
    payload = {
        "snapshot_date": snapshot_date,
        "taken_at": int(now.timestamp()),
        "total_xrp": snap["total_xrp"],
        "readable_count": snap["readable_count"],
        "wallet_count": snap["wallet_count"],
        "balances": balances,
    }

    try:
        db.write_evernorth_daily_snapshot(
            snapshot_date, payload["taken_at"], payload["total_xrp"],
            payload["readable_count"], payload["wallet_count"], balances,
        )
        log.info("postgres row upserted for %s", snapshot_date)
    except Exception as e:  # noqa: BLE001 — local copy must still be written
        log.error("postgres write failed: %s", e)

    try:
        with open(LOCAL_JSON, "w") as fh:
            json.dump(payload, fh, indent=1, default=str)
        log.info("local copy written -> %s", LOCAL_JSON)
    except Exception as e:  # noqa: BLE001
        log.error("local copy failed: %s", e)

    log.info(
        "total=%s XRP readable=%s/%s date=%s",
        payload["total_xrp"], payload["readable_count"],
        payload["wallet_count"], snapshot_date,
    )
    return payload


if __name__ == "__main__":
    run()
