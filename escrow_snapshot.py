"""Cached read layer over the escrows_snapshot Postgres table.

The walker (escrow_walker.py) writes to Postgres on a 30-min launchd
cadence. This module reads from Postgres and caches for 5 min so
reloads don't hammer PG when /cold-storage traffic bunches. Thread-safe.

Never touches the XRPL node — that's the walker's job. If PG is empty
or unreachable, returns an empty page shape so the template can render
a "no data yet" state instead of 500'ing.
"""

import os
import threading
import time
from datetime import datetime, timezone

import db

RIPPLE_EPOCH = 946684800  # 2000-01-01 UTC — XRPL "ripple time" offset

CACHE_TTL = int(os.environ.get("ESCROW_SNAPSHOT_CACHE_TTL", "300"))

_cache_lock = threading.Lock()
_cache = {"fetched_at": 0.0, "data": None}


def _short_addr(addr):
    if not addr or len(addr) < 12:
        return addr or ""
    return f"{addr[:6]}…{addr[-4:]}"


def _to_utc_iso(ripple_seconds):
    """Convert ripple-time seconds to UTC ISO 8601. Returns None on
    invalid input (defensive — callers may skip the field)."""
    if ripple_seconds is None:
        return None
    try:
        return datetime.fromtimestamp(
            int(ripple_seconds) + RIPPLE_EPOCH, tz=timezone.utc
        ).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _decorate_row(r):
    """Attach display-shaped fields the template renders directly. Keeps
    Jinja tidy and centralizes the ripple-time conversion in Python."""
    finish_iso = _to_utc_iso(r.get("finish_after"))
    cancel_iso = _to_utc_iso(r.get("cancel_after"))
    idx_hash = r.get("ledger_index_hash") or ""
    return {
        **r,
        "owner_short": _short_addr(r.get("owner")),
        "destination_short": _short_addr(r.get("destination")),
        "hash_short": (idx_hash[:6] + "…" + idx_hash[-4:]) if len(idx_hash) >= 12 else idx_hash,
        "finish_after_iso": finish_iso,
        "cancel_after_iso": cancel_iso,
        "amount_xrp": (r["amount_drops"] / 1_000_000
                       if r.get("amount_drops") is not None else None),
    }


def _month_bucket(ripple_seconds):
    """Group upcoming releases by YYYY-MM for the calendar panel."""
    if ripple_seconds is None:
        return None
    try:
        dt = datetime.fromtimestamp(
            int(ripple_seconds) + RIPPLE_EPOCH, tz=timezone.utc
        )
        return dt.strftime("%Y-%m")
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _build_page_data():
    snap = db.read_escrows_snapshot()
    rows = [_decorate_row(r) for r in snap.get("rows") or []]

    upcoming_raw = db.read_upcoming_escrow_releases(limit=200)
    upcoming = [_decorate_row(r) for r in upcoming_raw]

    # Month-grouped calendar for the template. Aggregates XRP totals and
    # object counts per month; per-escrow rows are also carried so the
    # template can expand a month into its releases without a second query.
    months = {}
    for r in upcoming:
        key = _month_bucket(r.get("finish_after"))
        if key is None:
            continue
        bucket = months.setdefault(key, {
            "month_key": key,
            "total_xrp": 0.0,
            "object_count": 0,
            "releases": [],
        })
        bucket["object_count"] += 1
        if r.get("amount_xrp") is not None:
            bucket["total_xrp"] += r["amount_xrp"]
        bucket["releases"].append(r)
    calendar = sorted(months.values(), key=lambda b: b["month_key"])

    return {
        "rows": rows,
        "upcoming": upcoming,
        "calendar": calendar,
        "token_escrow_observed": snap.get("token_escrow_observed", False),
        "snapshot_fetched_at": snap.get("fetched_at"),
        "snapshot_age_seconds": snap.get("snapshot_age_seconds"),
        "snapshot_ledger_index": snap.get("snapshot_ledger_index"),
        "row_count": len(rows),
        "tracked_owner_count": len({r["owner"] for r in rows}),
    }


def fetch_escrow_snapshot_cached(ttl=None):
    """Thread-safe cached read. Returns a page-shaped dict even when PG
    is empty (row_count == 0)."""
    ttl = ttl if ttl is not None else CACHE_TTL
    now = time.time()
    with _cache_lock:
        if _cache["data"] is not None and (now - _cache["fetched_at"]) < ttl:
            data = dict(_cache["data"])
            data["cache_age_seconds"] = round(now - _cache["fetched_at"], 1)
            return data
        fresh = _build_page_data()
        # Cache empty results too — an empty snapshot is a valid state
        # (walker hasn't run yet on first boot); recomputing every request
        # buys nothing.
        _cache["fetched_at"] = now
        _cache["data"] = fresh
        result = dict(fresh)
        result["cache_age_seconds"] = 0.0
        return result


if __name__ == "__main__":
    d = fetch_escrow_snapshot_cached(ttl=0)
    print(f"rows: {d['row_count']}")
    print(f"tracked owners with escrows: {d['tracked_owner_count']}")
    print(f"upcoming: {len(d['upcoming'])}")
    print(f"calendar months: {len(d['calendar'])}")
    print(f"token_escrow_observed: {d['token_escrow_observed']}")
    print(f"snapshot_age_seconds: {d['snapshot_age_seconds']}")
    for m in d["calendar"][:6]:
        print(f"  {m['month_key']}: {m['object_count']} objects, "
              f"{m['total_xrp']:,.0f} XRP")
