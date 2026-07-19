"""XRPL RLUSD net-supply-change aggregates via gateway_balances snapshot-diff.

Option A per project_xrpldashboard_rlusd_false_flat_2026-07-17. Replaces
the previously-disabled _fetch_xrpl_24h_aggregates() Payment-only sweep
that produced 53 days of silent-fabricate $0 (2026-05-25 → 2026-07-17).

Shape mirrors rlusd_etherscan.py deliberately. One HTTP surface, one
boundary-resolution rule, one place to change. Two aggregators return
the same shape their ETH counterparts do so the walker + writer + template
diff cleanly.

Why gateway_balances rather than a transaction sweep:
  * `gateway_balances` returns the issuer's total obligations
    (currency_hex → total issued) at a specific ledger_index. That IS
    circulating supply — subtract two snapshots at UTC-day-boundary
    ledgers and you get exact net supply change on that day, with no
    dependence on which transaction primitive minted/burned it.
  * The 2026-05-25 → 2026-07-17 outage happened because the old sweep
    assumed the supply-change path was "Payment from issuer = mint,
    Payment to issuer = burn." That's one path among several
    (Escrow{Create,Finish,Cancel}, MPT ops, AMM operations, PaymentChannel,
    Clawback, etc.). Snapshot-diff bypasses the enumeration problem —
    whatever primitive changes issued balance, the obligation total moves.
  * gateway_balances at an arbitrary historical ledger_index works on
    s2.ripple.com (full-history rippled) for the entire supply history.
    Fixture pull 2026-07-18 verified 4 event days across a 23-day span —
    see artifacts/rlusd_option_a_fixtures.json.

Semantic exposed to the page: "Net supply change · 24h." NOT labeled
"mints" or "burns" — we don't have them, and the ledger doesn't prove
them cleanly for this issuer. The tooltip says "mints minus redemptions —
we show the net because it's what the ledger proves directly."

Shape:
  * `aggregate_calendar_day(day)` → float net change (RLUSD, signed) for
    the UTC calendar day `day`. Positive = net mint, negative = net redeem.
  * `aggregate_rolling_24h(now_unix)` → float net change (RLUSD, signed)
    over the trailing 24h ending at `now_unix`.
  * Both raise XrplOptionAError on failure; walker records `error` on the
    xrpl payload and the affected row stays unwritten. Same discipline as
    rlusd_etherscan: unavailable is not zero.
"""

from __future__ import annotations

import json
import os
import ssl
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any

from urllib import request as urlrequest
from urllib.error import URLError

try:
    import certifi
    _SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:  # pragma: no cover
    _SSL_CONTEXT = ssl.create_default_context()


# --- Constants ---------------------------------------------------------------

# RLUSD issuer on the XRPL.
XRPL_ISSUER = "rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De"
# 40-char hex form (5-char ticker "RLUSD" doesn't fit the 3-byte
# native slot, so the wire form is hex).
XRPL_CURRENCY_HEX = "524C555344000000000000000000000000000000"
# Some serializers echo "RLUSD" back as a convenience.
XRPL_CURRENCY_NAMES = {"RLUSD", XRPL_CURRENCY_HEX}

# Ripple ledger close_time epoch (2000-01-01 UTC).
XRPL_EPOCH_OFFSET = 946_684_800

# Full-history rippled — required for historical gateway_balances
# (s1.ripple.com is retention-limited; s2 keeps genesis-to-current).
# XRPL_OPTION_A_RPC lets prod pick a different node without a code change.
XRPL_RPC = os.environ.get("XRPL_OPTION_A_RPC", "https://s2.ripple.com:51234")

# Approximate ledger cadence in seconds — used for binary-search step
# sizing only. Actual close_time is verified on every candidate.
XRPL_SECONDS_PER_LEDGER = 3.9

# Boundary drift tolerance. Ledger closes are ~3.9s apart, so the last
# ledger with close_time <= target UTC boundary can drift up to ~4s
# before that boundary. Well inside daily-aggregate semantics.
BOUNDARY_DRIFT_TOLERANCE_S = 5

HTTP_TIMEOUT = 15.0
USER_AGENT = "xrpldashboard/1.0 (+https://xrpldashboard.com)"

# Retry policy for transient network / rate-limit hiccups. Snapshot-diff
# is only 4-6 RPC calls per aggregate walk (find start + find end +
# gateway_balances at each), so a small retry budget is fine.
_MAX_RETRIES = 3
_RETRY_BACKOFF_S = 1.0


class XrplOptionAError(RuntimeError):
    """Raised when any snapshot-diff step fails. Walker surfaces as `error`
    on the xrpl payload; row stays unwritten rather than shipping a $0."""


# --- Low-level JSON-RPC ------------------------------------------------------

def _rpc_call(method: str, params: dict) -> dict:
    """POST a JSON-RPC request to XRPL_RPC. Returns the `result` object.

    Raises XrplOptionAError on transport failure or non-success status
    envelope. XRPL JSON-RPC embeds status inside result — check for both
    HTTP-level and result-level failure.
    """
    payload = json.dumps({
        "method": method,
        "params": [params],
    }).encode("utf-8")
    last_err: str | None = None
    for attempt in range(_MAX_RETRIES):
        if attempt:
            time.sleep(_RETRY_BACKOFF_S * attempt)
        req = urlrequest.Request(
            XRPL_RPC,
            data=payload,
            headers={
                "User-Agent": USER_AGENT,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urlrequest.urlopen(req, timeout=HTTP_TIMEOUT, context=_SSL_CONTEXT) as resp:
                body = resp.read()
        except URLError as e:
            last_err = f"{type(e).__name__}: {e}"
            continue
        try:
            data = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError as e:
            last_err = f"json decode: {e}"
            continue
        result = data.get("result") or {}
        status = result.get("status")
        if status == "success":
            return result
        # rippled encodes rate-limit and load errors inside result. Retry
        # on `error`=noNetwork / lgrIdxMalformed transient shapes; fail
        # loud on schema-level errors like `error`=invalidParams.
        err_code = result.get("error", data.get("error", ""))
        err_msg = result.get("error_message", data.get("error_message", ""))
        transient = err_code in {"noNetwork", "noCurrent", "tooBusy"}
        if transient:
            last_err = f"{err_code}: {err_msg}"
            continue
        raise XrplOptionAError(f"{method}: {err_code} {err_msg}".strip())
    raise XrplOptionAError(f"{method}: retries exhausted: {last_err}")


# --- Ledger boundary resolution ---------------------------------------------

def _ledger_close_time(ledger_index: int) -> int | None:
    """Return close_time_unix for `ledger_index`, or None if the ledger
    doesn't exist yet (target > latest). Returns unix seconds, NOT
    ripple-epoch seconds."""
    try:
        result = _rpc_call("ledger", {
            "ledger_index": ledger_index,
            "transactions": False,
            "expand": False,
        })
    except XrplOptionAError as e:
        # ledgerNotFound / lgrIdxMalformed for future ledgers is normal
        # during boundary search; propagate as None so the caller can
        # clamp its search window.
        if "NotFound" in str(e) or "Malformed" in str(e):
            return None
        raise
    ledger = result.get("ledger") or {}
    if "close_time" not in ledger:
        return None
    return int(ledger["close_time"]) + XRPL_EPOCH_OFFSET


def _latest_validated_ledger() -> tuple[int, int]:
    """(ledger_index, close_time_unix) of the most recent validated ledger.
    Used as the search-window ceiling for boundary resolution."""
    result = _rpc_call("ledger", {
        "ledger_index": "validated",
        "transactions": False,
        "expand": False,
    })
    ledger = result.get("ledger") or {}
    idx = int(ledger.get("ledger_index") or result.get("ledger_index") or 0)
    close_time = int(ledger["close_time"]) + XRPL_EPOCH_OFFSET
    return idx, close_time


def find_boundary_ledger(target_unix: int) -> int:
    """Return the last ledger_index whose close_time <= target_unix.

    Binary search over the ledger index space, seeded from an
    XRPL_SECONDS_PER_LEDGER-based estimate. Terminates when the candidate
    close_time is within BOUNDARY_DRIFT_TOLERANCE_S of target_unix OR the
    search window collapses to <=1 ledger.

    Partial-day handling: if `target_unix` is in the future (mid-day
    partial-day query with endpoint at "tomorrow 00:00Z"), the caller
    should clamp target_unix to `latest.close_time` before calling — this
    function clamps too as a defense-in-depth, but callers should treat
    the "end" of a partial day as the latest validated ledger explicitly.
    """
    latest_idx, latest_close = _latest_validated_ledger()
    if target_unix >= latest_close:
        return latest_idx

    # Seed lo/hi around an estimate. Delta-seconds → delta-ledgers via
    # ~3.9s/ledger, then widen ±2000 ledgers (~2h) either side.
    seconds_back = latest_close - target_unix
    est_delta_ledgers = int(seconds_back / XRPL_SECONDS_PER_LEDGER)
    est = latest_idx - est_delta_ledgers
    lo = max(1, est - 2000)
    hi = min(latest_idx, est + 2000)

    # Expand `lo` down if the estimate + margin still overshoots (very
    # far historical targets, unlikely at these dates but cheap to
    # handle). Cap at 20 doublings so a broken RPC can't spin forever.
    for _ in range(20):
        lo_close = _ledger_close_time(lo)
        if lo_close is not None and lo_close <= target_unix:
            break
        span = hi - lo
        lo = max(1, lo - max(span, 1))

    # Binary search — find the last ledger with close_time <= target.
    best = lo
    while lo <= hi:
        mid = (lo + hi) // 2
        mid_close = _ledger_close_time(mid)
        if mid_close is None:
            # Future / missing ledger — search left.
            hi = mid - 1
            continue
        if mid_close <= target_unix:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
        # Early exit — inside drift tolerance and we've been strictly
        # under the target at some point.
        if mid_close <= target_unix and (target_unix - mid_close) <= BOUNDARY_DRIFT_TOLERANCE_S:
            best = mid
            # One more probe upward to see if best+1 is still <= target.
            next_close = _ledger_close_time(mid + 1)
            if next_close is not None and next_close <= target_unix:
                best = mid + 1
            break

    return best


# --- Snapshot ----------------------------------------------------------------

def _obligations_at(ledger_index: int) -> float:
    """Return the issuer's total RLUSD obligations at `ledger_index`. Raises
    XrplOptionAError if the RLUSD currency isn't in the obligations map
    (should never happen for a live issuer — but louder-than-silent)."""
    result = _rpc_call("gateway_balances", {
        "account": XRPL_ISSUER,
        "ledger_index": ledger_index,
        "strict": True,
    })
    obligations = result.get("obligations") or {}
    for cur, amt in obligations.items():
        if cur in XRPL_CURRENCY_NAMES:
            try:
                return float(amt)
            except (TypeError, ValueError) as e:
                raise XrplOptionAError(
                    f"gateway_balances: RLUSD amount unparseable at "
                    f"ledger {ledger_index}: {amt!r} ({e})"
                )
    raise XrplOptionAError(
        f"gateway_balances: no RLUSD obligation at ledger {ledger_index} "
        f"(obligations keys: {list(obligations.keys())})"
    )


def snapshot_diff(start_ledger: int, end_ledger: int) -> float:
    """Net supply change (RLUSD, signed) between two ledgers.

    Positive = net mint over the interval; negative = net redemption.
    Two gateway_balances calls, no pagination, no transaction walk.
    """
    if end_ledger < start_ledger:
        raise XrplOptionAError(
            f"snapshot_diff: end_ledger {end_ledger} < start_ledger {start_ledger}"
        )
    start_supply = _obligations_at(start_ledger)
    end_supply = _obligations_at(end_ledger)
    return end_supply - start_supply


# --- Aggregators -------------------------------------------------------------

def aggregate_calendar_day(day: date) -> float:
    """Net supply change (signed float RLUSD) for UTC calendar day `day`.

    Boundaries:
      * start = last ledger with close_time <= `day 00:00:00Z`
      * end   = last ledger with close_time <= `(day+1) 00:00:00Z`

    For the current day (partial), `end` naturally clamps to the latest
    validated ledger, matching the ETH `aggregate_rolling_24h` semantics
    that already ship in production.
    """
    day_start = int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())
    next_day_start = day_start + 86_400
    start_ledger = find_boundary_ledger(day_start)
    end_ledger = find_boundary_ledger(next_day_start)
    return snapshot_diff(start_ledger, end_ledger)


def aggregate_rolling_24h(now_unix: int | None = None) -> float:
    """Net supply change (signed float RLUSD) across trailing 24h ending
    at `now_unix` (defaults to time.time())."""
    now = int(now_unix if now_unix is not None else time.time())
    start_ledger = find_boundary_ledger(now - 86_400)
    end_ledger = find_boundary_ledger(now)
    return snapshot_diff(start_ledger, end_ledger)


def current_supply() -> float:
    """Live obligations total — cheap sanity call. Mirrors
    rlusd_etherscan.current_supply() so debug surfaces on both chains
    look the same."""
    latest_idx, _ = _latest_validated_ledger()
    return _obligations_at(latest_idx)
