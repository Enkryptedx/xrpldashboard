"""supply_fast_walker — 60-second ledger(validated) → xrp_supply_block.

Charlie ruling 2026-09-24 16:08 ET (cadence c): pair of walkers.
This one runs every 60s on the Mac (Lenovo systemd port later), makes
ONE ledger request, upserts the total_* half of the singleton row.
`burned_since_genesis` is derived at render time from total_xrp so no
extra column is needed here.

Fail modes: any RPC or PG error → walker_health.ok=False, do NOT
overwrite. Block continues to serve last-good with age_s > cadence.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import sys

from xrpl.models.requests import Ledger

import db
import supply_block

logging.basicConfig(
    format="%(asctime)s [supply_fast_walker] %(levelname)s %(message)s",
    level=logging.INFO,
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

WALKER_NAME = "supply_fast_walker"
CADENCE_SECONDS = int(os.environ.get("SUPPLY_FAST_CADENCE_SECONDS", "60"))
XRP_DROPS_PER_XRP = 1_000_000


def _ripple_close_to_iso(rc: int | None) -> str:
    if rc is None:
        return ""
    return dt.datetime.fromtimestamp(rc + 946_684_800, dt.timezone.utc)\
        .isoformat().replace("+00:00", "Z")


def main() -> int:
    if not db.pg_available():
        log.error("pg not available; exiting")
        return 78

    from xrpl_client import get_client
    client = get_client()

    started = dt.datetime.now(dt.timezone.utc)
    _ok = False
    _msg = "init"
    try:
        db.write_walker_health_start(WALKER_NAME, cadence_seconds=CADENCE_SECONDS)
        r = client.request(Ledger(ledger_index="validated")).result
        ledger = r.get("ledger") or {}
        if not ledger.get("ledger_index"):
            raise RuntimeError("ledger(validated) returned no ledger_index")
        total_drops = int(ledger.get("total_coins") or 0)
        if total_drops <= 0:
            raise RuntimeError(f"ledger.total_coins invalid: {ledger.get('total_coins')!r}")
        total_xrp = total_drops / XRP_DROPS_PER_XRP
        supply_block.upsert_total_side(
            fetched_at=started,
            ledger_index=int(ledger["ledger_index"]),
            ledger_hash=ledger.get("ledger_hash") or "",
            ledger_close_time_iso=_ripple_close_to_iso(ledger.get("close_time")),
            total_drops=total_drops,
            total_xrp=total_xrp,
        )
        log.info("wrote total: ledger#%s total_xrp=%.2f", ledger["ledger_index"], total_xrp)
        _ok = True
        _msg = f"ok ledger={ledger['ledger_index']} total_xrp={total_xrp:.2f}"
        return 0
    except Exception as e:  # noqa: BLE001
        _msg = f"{type(e).__name__}: {str(e)[:200]}"
        log.exception("walker failed")
        return 1
    finally:
        try:
            db.write_walker_health_end(WALKER_NAME, ok=_ok, message=_msg)
        except Exception:
            pass


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
