"""Monthly escrow release history — read + shape layer for /cold-storage.

Backs the "monthly release history" section on /cold-storage and its machine
twin /cold-storage/unlocks.json. The existing locked/liquid hero is untouched
and keeps reading escrows_snapshot via escrow_snapshot.py.

Everything in here that does arithmetic or time conversion is a PURE function
over db.read_escrow_unlock_events() output, so the Sept-2026 fixture and the
DST pair in tests/test_escrow_unlock_history.py run with no database. Only
fetch_unlock_history_from_db() touches Postgres.

WHY THE MONTH LABEL IS UTC-BASED
--------------------------------
The monthly cohort unlock lands at 00:00:10 UTC on the 1st, which is the
PREVIOUS EVENING in ET (8:00:10 PM EDT on the 31st/30th). Grouping by ET
would file a September unlock under August and disagree with every explorer
and with Ripple's own schedule. So the month bucket is the generated
`unlock_month` column (date_trunc over close_time AT TIME ZONE 'UTC') and the
ET time is shown per-transaction as a courtesy, with an explicit badge
wherever the two calendar dates disagree.

The ET abbreviation is DERIVED from the zone (%Z over America/New_York), not
chosen by month-number arithmetic: a 00:00 UTC finish in early November is
EDT or EST depending on which side of the DST switch it lands, and the
switch date moves year to year.
"""
from __future__ import annotations

import threading
import time
import zoneinfo
from datetime import datetime, timezone

import db

UTC = timezone.utc
ET_ZONE = zoneinfo.ZoneInfo("America/New_York")

DROPS_PER_XRP = 1_000_000

# How much history the page and the JSON twin show.
HISTORY_MONTHS = 12

CACHE_TTL = 300  # 5 min, matching the other /cold-storage read layers

# The two transaction types this surface understands. EscrowFinish releases
# locked XRP into circulation; EscrowCreate by a cohort account puts it back.
TX_RELEASED = "EscrowFinish"
TX_RE_ESCROWED = "EscrowCreate"

_cache_lock = threading.Lock()
_cache: dict = {"fetched_at": 0.0, "data": None}


def _as_utc(dt: datetime) -> datetime:
    """Normalise a close_time to aware UTC.

    psycopg hands back an aware datetime for TIMESTAMPTZ, but a naive value
    can arrive from a test fixture or a hand-built row. Treating naive as UTC
    is correct here because close_time is only ever written from ledger
    metadata, which is UTC by definition.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def format_tx_times(close_time: datetime) -> dict:
    """Return the UTC and ET renderings of one transaction's close time.

    `et_abbrev` is read off the zone (EDT or EST), never inferred from the
    month. `date_differs` is the data-driven badge flag: True exactly when
    the UTC calendar date and the ET calendar date are not the same day,
    which is what makes a 1st-of-the-month 00:00 UTC unlock show up as the
    previous evening for a US reader.
    """
    utc_dt = _as_utc(close_time)
    et_dt = utc_dt.astimezone(ET_ZONE)
    return {
        "close_time_utc": utc_dt.isoformat().replace("+00:00", "Z"),
        "utc_date": utc_dt.strftime("%Y-%m-%d"),
        "utc_time": utc_dt.strftime("%H:%M:%S"),
        "utc_display": utc_dt.strftime("%-d %b %Y, %H:%M:%S UTC"),
        "et_date": et_dt.strftime("%Y-%m-%d"),
        "et_time": et_dt.strftime("%H:%M:%S"),
        "et_abbrev": et_dt.strftime("%Z"),
        "et_display": et_dt.strftime("%-d %b %Y, %-I:%M:%S %p ") + et_dt.strftime("%Z"),
        "date_differs": utc_dt.strftime("%Y-%m-%d") != et_dt.strftime("%Y-%m-%d"),
    }


def shape_tx(row: dict) -> dict:
    """Shape one DB row into the per-transaction dict the page renders."""
    out = {
        "tx_hash": row["tx_hash"],
        "tx_type": row["tx_type"],
        "ledger_index": row["ledger_index"],
        "denom": row["denom"],
        "amount_drops": row.get("amount_drops"),
        "amount_xrp": (
            row["amount_drops"] / DROPS_PER_XRP
            if row.get("amount_drops") is not None
            else None
        ),
        "escrow_owner": row["escrow_owner"],
        "escrow_destination": row.get("escrow_destination"),
        "submitter": row.get("submitter"),
        "tx_result": row["tx_result"],
        "source": row["source"],
        "is_released": row["tx_type"] == TX_RELEASED,
        "is_re_escrowed": row["tx_type"] == TX_RE_ESCROWED,
    }
    out.update(format_tx_times(row["close_time"]))
    return out


def _month_key(row: dict) -> str:
    """UTC month bucket for a row, as 'YYYY-MM'.

    Prefers the generated `unlock_month` column when present so the page and
    the database agree by construction; falls back to deriving it from
    close_time for hand-built rows in tests.
    """
    um = row.get("unlock_month")
    if um is not None:
        return um.strftime("%Y-%m")
    return _as_utc(row["close_time"]).strftime("%Y-%m")


def group_by_month(rows: list[dict]) -> list[dict]:
    """Group observed rows into month blocks, NEWEST MONTH FIRST.

    Per month: released drops (EscrowFinish), re-escrowed drops
    (EscrowCreate by a cohort account) and net released = released minus
    re-escrowed. Net is reported as a signed number and is NOT clamped at
    zero: a month where the cohort re-locked more than it released is a real
    outcome and is labelled as a net re-escrow rather than hidden as "0".

    IOU/MPT escrows carry no amount_drops (the column is NULL by constraint),
    so they cannot enter the XRP sums. They are counted in `non_xrp_count`
    so the section can disclose their presence instead of dropping them.
    """
    buckets: dict[str, list[dict]] = {}
    for row in rows:
        buckets.setdefault(_month_key(row), []).append(row)

    months = []
    for key in sorted(buckets.keys(), reverse=True):
        txs = [shape_tx(r) for r in buckets[key]]
        # Newest transaction first within the month, hash as the tiebreak so
        # the ordering is total and the JSON twin is byte-stable between runs.
        txs.sort(key=lambda t: (t["close_time_utc"], t["tx_hash"]), reverse=True)

        released = sum(
            t["amount_drops"] for t in txs
            if t["is_released"] and t["amount_drops"] is not None
        )
        re_escrowed = sum(
            t["amount_drops"] for t in txs
            if t["is_re_escrowed"] and t["amount_drops"] is not None
        )
        non_xrp = sum(1 for t in txs if t["amount_drops"] is None)
        month_start = datetime.strptime(key + "-01", "%Y-%m-%d").replace(tzinfo=UTC)

        months.append({
            "month": key,
            "month_display": month_start.strftime("%B %Y"),
            "released_drops": released,
            "re_escrowed_drops": re_escrowed,
            "net_released_drops": released - re_escrowed,
            "released_xrp": released / DROPS_PER_XRP,
            "re_escrowed_xrp": re_escrowed / DROPS_PER_XRP,
            "net_released_xrp": (released - re_escrowed) / DROPS_PER_XRP,
            "is_net_re_escrow": (released - re_escrowed) < 0,
            "tx_count": len(txs),
            "non_xrp_count": non_xrp,
            "any_date_differs": any(t["date_differs"] for t in txs),
            "sources": sorted({t["source"] for t in txs}),
            "transactions": txs,
        })
    return months


def build_history(rows: list[dict]) -> dict:
    """Assemble the full section payload from raw DB rows. Pure."""
    months = group_by_month(rows)
    return {
        "months": months,
        "months_observed": len(months),
        "tx_count": sum(m["tx_count"] for m in months),
        "window_months": HISTORY_MONTHS,
        "newest_month": months[0]["month"] if months else None,
        "oldest_month": months[-1]["month"] if months else None,
        # A month label present with zero rows is impossible by construction
        # (buckets come from rows), so an empty list means "nothing observed
        # yet" and the section renders its empty state, never a 0 XRP claim.
        "has_data": bool(months),
    }


def fetch_unlock_history_from_db(months: int = HISTORY_MONTHS) -> dict:
    """Cached read of the observed release history. 5-min TTL, thread-safe.

    Never touches the XRPL node: /cold-storage reads rows the walker wrote
    from our own node, same as every other section on that page.
    """
    now = time.time()
    with _cache_lock:
        cached = _cache["data"]
        if cached is not None and (now - _cache["fetched_at"]) < CACHE_TTL:
            out = dict(cached)
            out["cached_age_seconds"] = round(now - _cache["fetched_at"], 1)
            return out

    rows = db.read_escrow_unlock_events(months=months)
    data = build_history(rows)
    data["cached_age_seconds"] = 0.0

    with _cache_lock:
        _cache["data"] = data
        _cache["fetched_at"] = time.time()
    return dict(data)
