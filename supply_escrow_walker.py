"""supply_escrow_walker — 15-minute full-ledger escrow enumeration.

Charlie ruling 2026-09-24 15:37 ET (Interpretation B) + 16:08 ET
(cadence c): walker enumerates every Escrow object on the validated
ledger via `ledger_data(type=escrow)` paginated to completion, then
upserts the escrow_* half of the `xrp_supply_block` singleton row.

The wrapper enforces single-instance execution via a flock-guarded
lockfile so a launchd re-fire on a slow enumeration is a clean no-op
(exit 0) rather than a stampede.

Empirically the walk takes 5-15 min on our LAN rippled (2026-09-24
sampling: 77,906 pages at ~93 pages/s → 13 min 54 s for the full walk).
Cadence 900s (15 min) is a small overlap buffer; the wrapper lock is
what actually prevents concurrent runs.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import sys
import time

from xrpl.models.requests import LedgerData

import db
import supply_block

logging.basicConfig(
    format="%(asctime)s [supply_escrow_walker] %(levelname)s %(message)s",
    level=logging.INFO,
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

WALKER_NAME = "supply_escrow_walker"
# 2026-10-13 (Charlie): cadence 900 -> 3600, cap 1200 -> 2700, changed TOGETHER.
# The old pair was incoherent: cadence (900s) was SHORTER than the hard cap
# (1200s), so any run that hit the cap overran its own interval and launchd
# relaunched it the instant it exited. Result on 2026-10-12/13 was 44
# consecutive capped failures with zero idle time, continuously hammering our
# own rippled node. The cap must always be LESS than the cadence so a
# worst-case run still leaves a gap.
#
# Why 2700: the last successful walk did 78,215 pages in 860s (90.9 pages/s,
# matching the 2026-09-24 design note). Throughput has since fallen to ~37
# pages/s (44,512 pages before the 1200s cap), so the same walk now needs
# ~2,100s. 2700 clears that with ~30% headroom; 3600 cadence keeps >= 900s
# idle even in the worst case. If the node returns to ~91 pages/s the walk is
# back to ~860s and the extra ceiling simply goes unused.
# Both remain env-overridable; keep plist StartInterval == CADENCE_SECONDS.
CADENCE_SECONDS = int(os.environ.get("SUPPLY_ESCROW_CADENCE_SECONDS", "3600"))
ENUM_HARD_CAP_SECONDS = int(os.environ.get("SUPPLY_ESCROW_HARD_CAP_SECONDS", "2700"))
LEDGER_DATA_PAGE_LIMIT = 400
XRP_DROPS_PER_XRP = 1_000_000


def _ripple_cohort_accounts() -> set[str]:
    """Ripple monthly-release cohort — same accounts the existing
    escrow_supply_walker + /cold-storage read."""
    try:
        from cold_storage import _load_named
        named = _load_named() or {}
        return {
            addr for addr, meta in named.items()
            if isinstance(meta, dict)
            and meta.get("category") == "ripple"
            and meta.get("name") != "RLUSD issuer"
        }
    except Exception as e:
        log.warning("cohort load failed: %s: %s", type(e).__name__, e)
        return set()


def _fetch_ledger_stamp(client) -> tuple[int, str, str]:
    """One ledger(validated) call captures the ledger stamp we associate
    with this enum. Returns (ledger_index, ledger_hash, close_time_iso)."""
    from xrpl.models.requests import Ledger
    r = client.request(Ledger(ledger_index="validated")).result
    ledger = r.get("ledger") or {}
    li = ledger.get("ledger_index")
    if not li:
        raise RuntimeError("ledger(validated) returned no ledger_index")
    return int(li), ledger.get("ledger_hash") or "", _ripple_close_to_iso(ledger.get("close_time"))


def _ripple_close_to_iso(rc: int | None) -> str:
    if rc is None:
        return ""
    return dt.datetime.fromtimestamp(rc + 946_684_800, dt.timezone.utc)\
        .isoformat().replace("+00:00", "Z")


def _enumerate_escrows(client) -> tuple[list[dict], int, float]:
    escrows: list[dict] = []
    marker = None
    page = 0
    t0 = time.time()
    while True:
        kw = dict(
            ledger_index="validated",
            type="escrow",
            binary=False,
            limit=LEDGER_DATA_PAGE_LIMIT,
        )
        if marker:
            kw["marker"] = marker
        r = client.request(LedgerData(**kw)).result
        for e in r.get("state", []) or []:
            if e.get("LedgerEntryType") == "Escrow":
                escrows.append(e)
        marker = r.get("marker")
        page += 1
        if not marker:
            break
        if time.time() - t0 > ENUM_HARD_CAP_SECONDS:
            raise RuntimeError(
                f"escrow enumeration exceeded {ENUM_HARD_CAP_SECONDS}s hard cap "
                f"at page {page}; aborting."
            )
    return escrows, page, time.time() - t0


def _aggregate(escrows: list[dict], cohort: set[str]) -> dict:
    all_total_drops = 0
    ripple_total_drops = 0
    ripple_count = 0
    all_accounts: set[str] = set()
    for e in escrows:
        amt = e.get("Amount")
        drops = 0
        if isinstance(amt, str):
            try:
                drops = int(amt)
            except ValueError:
                drops = 0
        all_total_drops += drops
        acc = e.get("Account")
        if acc:
            all_accounts.add(acc)
            if acc in cohort:
                ripple_total_drops += drops
                ripple_count += 1
    return {
        "all_drops": all_total_drops,
        "all_xrp": all_total_drops / XRP_DROPS_PER_XRP,
        "all_object_count": len(escrows),
        "all_account_count": len(all_accounts),
        "all_account_list": sorted(all_accounts),
        "ripple_drops": ripple_total_drops,
        "ripple_xrp": ripple_total_drops / XRP_DROPS_PER_XRP,
        "ripple_object_count": ripple_count,
    }


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
        ledger_index, ledger_hash, ledger_close_time_iso = _fetch_ledger_stamp(client)
        log.info("enumerating escrows (ledger#%s, hard_cap=%ss)…",
                 ledger_index, ENUM_HARD_CAP_SECONDS)
        escrows, pages, elapsed = _enumerate_escrows(client)
        log.info("enum done: pages=%d escrows=%d elapsed=%.1fs",
                 pages, len(escrows), elapsed)

        cohort = _ripple_cohort_accounts()
        agg = _aggregate(escrows, cohort)
        supply_block.upsert_escrow_side(
            fetched_at=started,
            ledger_index=ledger_index,
            ledger_hash=ledger_hash,
            ledger_close_time_iso=ledger_close_time_iso,
            all_drops=agg["all_drops"],
            all_xrp=agg["all_xrp"],
            all_object_count=agg["all_object_count"],
            all_account_count=agg["all_account_count"],
            all_account_list_json=json.dumps(agg["all_account_list"]),
            ripple_drops=agg["ripple_drops"],
            ripple_xrp=agg["ripple_xrp"],
            ripple_object_count=agg["ripple_object_count"],
            enum_pages=pages,
            enum_duration_s=round(elapsed, 2),
        )
        log.info(
            "wrote escrow: ledger#%s all=%.3fB (%d objs, %d accts) ripple=%.3fB (%d objs)",
            ledger_index, agg["all_xrp"] / 1e9,
            agg["all_object_count"], agg["all_account_count"],
            agg["ripple_xrp"] / 1e9, agg["ripple_object_count"],
        )
        _ok = True
        _msg = (f"ok pages={pages} escrows={len(escrows)} accts={agg['all_account_count']} "
                f"ledger={ledger_index} enum_s={elapsed:.1f}")
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
