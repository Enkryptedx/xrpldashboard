"""Etherscan V2 access for RLUSD calendar-day + rolling-24h aggregates.

Shared by rlusd_live.py (live walker) and ops/backfill_rlusd_eth_calendar_day.py
(one-shot history re-stamp). One HTTP surface, one pagination rule, one place
to change when Etherscan retires V2 like they just retired V1.

Reasoning for this API path over eth_getLogs (see
project_xrpldashboard_rlusd_false_flat_2026-07-17):
  * `tokentx` returns already-filtered ERC-20 transfers for the contract in
    a single call per block range, up to 10k rows across paginated pages.
  * Free tier: 5 calls/sec, 100k/day. A daily walker at 300s cadence uses
    <1% of the daily cap even after boundary + prev-day recomputation.
  * Same source Charlie spot-checks against in the Etherscan UI, so the
    numbers we render match what a reader sees when they click "verified
    on Etherscan" from the /rlusd correction note.

Semantics:
  * `aggregate_calendar_day(d)` sums mints (from=0x0) and burns (to=0x0)
    for the RLUSD contract across ALL blocks whose timestamps fall inside
    [d 00:00:00Z, d+1 00:00:00Z). Uses `getblocknobytime(closest='before')`
    to resolve the boundary block *before* each midnight, then walks the
    inclusive block range in between. Block-time drift is <2s at these
    heights, so day-boundary tx placement is exact to the block.
  * `aggregate_rolling_24h(now_unix)` sums the same across the trailing
    24h — [now-86400, now]. Used for the live "last 24 hours (rolling)"
    footer on /rlusd, which is deliberately a different semantic from the
    dated history rows and is labeled as such.

Both aggregators return (mints_usd, burns_usd) as floats (RLUSD is 1:1 USD,
18 decimals). Neither returns None — Etherscan V2 with a valid key is a
single reliable dependency, unlike the 4-endpoint public-RPC roulette the
old walker was running. Failures raise EtherscanError, which the walker
catches and records as `error` on the eth payload (rows stay unwritten
rather than shipping a fabricated value)."""

from __future__ import annotations

import os
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import httpx

# --- Constants ---------------------------------------------------------------

# RLUSD ERC-20 (checksummed) — the same address rlusd_live.py uses.
RLUSD_CONTRACT = "0x8292Bb45bf1Ee4d140127049757C2E0fF06317eD"
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
ETH_DECIMALS = 18
ETH_MAINNET_CHAINID = 1

# Etherscan V2 unified base URL. V1 (api.etherscan.io/api) was deprecated
# 2026 and now returns "V1 endpoint" error strings for all module calls,
# even with a valid key. V2 requires chainid=<n> per request.
ETHERSCAN_V2_BASE = "https://api.etherscan.io/v2/api"

# Free-tier rate limit is 5 req/sec. Sleep 0.25s between calls to stay
# under it under any concurrency (the walker refresh is single-threaded
# for the calendar-day path anyway).
_RATE_LIMIT_SLEEP = 0.25

# Etherscan caps `offset` at 10000 per page. tokentx for RLUSD averages
# ~1k tx/day, so single-page usually suffices; paginate defensively.
_PAGE_SIZE = 1000
_MAX_PAGES = 20  # 20k tx/day would be extraordinary; fail loud if hit

_HTTP_TIMEOUT = 25.0


class EtherscanError(RuntimeError):
    """Raised for any non-status=1 response from Etherscan V2, or transport
    failure. Callers surface as `error` on the eth payload; the affected
    row stays unwritten rather than shipping a $0."""


# --- Low-level ---------------------------------------------------------------

def _api_key() -> str:
    key = os.environ.get("ETHERSCAN_API_KEY", "").strip()
    if not key:
        raise EtherscanError("ETHERSCAN_API_KEY not set in environment")
    return key


def etherscan_call(**params: Any) -> dict:
    """Single Etherscan V2 request with rate-limit-aware retry. Returns the
    parsed JSON body. Raises EtherscanError on non-status=1 or transport
    failure."""
    params = dict(params)
    params["apikey"] = _api_key()
    params.setdefault("chainid", ETH_MAINNET_CHAINID)

    last_err: str | None = None
    for attempt in range(3):
        if attempt:
            time.sleep(1.0 * attempt)  # back off on retry
        try:
            r = httpx.get(ETHERSCAN_V2_BASE, params=params, timeout=_HTTP_TIMEOUT)
            body = r.json()
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            continue
        # V2 encodes rate-limit hits as status=0 with a "Max calls per sec"
        # message. Retry those; fail loud on anything else.
        if body.get("status") == "1":
            return body
        msg = str(body.get("result", body.get("message", "")))
        if "Max calls per sec" in msg or "rate limit" in msg.lower():
            last_err = f"rate-limit: {msg}"
            continue
        raise EtherscanError(f"etherscan {params.get('module')}/{params.get('action')}: {msg}")
    raise EtherscanError(f"etherscan retries exhausted: {last_err}")


# --- Block boundary resolution ----------------------------------------------

def block_number_at_or_before(unix_ts: int) -> int:
    """Etherscan `getblocknobytime(closest=before)` returns the last block
    whose timestamp <= unix_ts. Used to resolve calendar-day boundaries to
    the block level (drift ~2s at 12s block time is well within tolerance
    for daily aggregates).

    Pre-clamps `unix_ts` to `now - 15s` so a mid-day partial-day query
    (endpoint at "tomorrow 00:00Z" for today's row) doesn't hit Etherscan's
    "Block timestamp too far in the future" error — we want the latest block
    Etherscan will admit exists, not to fail hard on a partial day."""
    now = int(time.time())
    ts = min(int(unix_ts), now - 15)
    body = etherscan_call(
        module="block", action="getblocknobytime",
        timestamp=ts, closest="before",
    )
    time.sleep(_RATE_LIMIT_SLEEP)
    return int(body["result"])


def _calendar_day_block_range(day: date) -> tuple[int, int]:
    """(startblock, endblock) inclusive for the UTC calendar day `day`.

    startblock = first block on or after `day 00:00:00Z` (== block_before(midnight) + 1)
    endblock   = last block strictly before `day+1 00:00:00Z` (== block_before(next-midnight))

    A transfer whose block timestamp falls in [day 00:00Z, day+1 00:00Z) will
    always have `startblock <= transfer.block <= endblock`, because
    `getblocknobytime(closest=before)` returns strictly-preceding at the
    boundary tie."""
    day_start = int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())
    next_day_start = day_start + 86_400
    start_block = block_number_at_or_before(day_start - 1) + 1
    end_block = block_number_at_or_before(next_day_start - 1)
    return start_block, end_block


# --- Aggregation -------------------------------------------------------------

def _fetch_transfers(start_block: int, end_block: int) -> list[dict]:
    """Paginate through all RLUSD ERC-20 transfers in [start_block, end_block].
    Sorted ascending. Raises EtherscanError if _MAX_PAGES exceeded."""
    out: list[dict] = []
    for page in range(1, _MAX_PAGES + 1):
        body = etherscan_call(
            module="account", action="tokentx",
            contractaddress=RLUSD_CONTRACT,
            startblock=start_block, endblock=end_block,
            sort="asc", page=page, offset=_PAGE_SIZE,
        )
        rows = body.get("result", [])
        if not isinstance(rows, list) or not rows:
            break
        out.extend(rows)
        if len(rows) < _PAGE_SIZE:
            break
        time.sleep(_RATE_LIMIT_SLEEP)
    else:
        raise EtherscanError(
            f"tokentx pagination exceeded {_MAX_PAGES} pages for blocks "
            f"{start_block}-{end_block} — either an unusually active window "
            f"or an API misuse; refusing to under-count silently"
        )
    return out


def _sum_mints_burns(transfers: list[dict]) -> tuple[float, float]:
    """Split-and-sum by zero-address direction. Returns (mints_usd, burns_usd)."""
    mints_wei = Decimal(0)
    burns_wei = Decimal(0)
    for t in transfers:
        val = Decimal(t.get("value", "0"))
        frm = (t.get("from") or "").lower()
        to = (t.get("to") or "").lower()
        if frm == ZERO_ADDRESS:
            mints_wei += val
        if to == ZERO_ADDRESS:
            burns_wei += val
    divisor = Decimal(10) ** ETH_DECIMALS
    return float(mints_wei / divisor), float(burns_wei / divisor)


def aggregate_calendar_day(day: date) -> tuple[float, float]:
    """(mints_usd, burns_usd) for RLUSD on UTC calendar day `day`."""
    sb, eb = _calendar_day_block_range(day)
    txs = _fetch_transfers(sb, eb)
    return _sum_mints_burns(txs)


def aggregate_rolling_24h(now_unix: int | None = None) -> tuple[float, float]:
    """(mints_usd, burns_usd) for RLUSD across the trailing 24h ending at
    `now_unix` (defaults to time.time()). Used for the live /rlusd footer,
    NOT for history rows — history is calendar-day per snapshot_date."""
    now = int(now_unix if now_unix is not None else time.time())
    start_block = block_number_at_or_before(now - 86_400) + 1
    end_block = block_number_at_or_before(now)
    txs = _fetch_transfers(start_block, end_block)
    return _sum_mints_burns(txs)


def current_supply() -> float:
    """Current RLUSD ERC-20 totalSupply via Etherscan `tokensupply` — a
    cheap sanity call, useful for the backfill script's warmup and for
    verifying the API key works without touching the more expensive
    tokentx path."""
    body = etherscan_call(
        module="stats", action="tokensupply",
        contractaddress=RLUSD_CONTRACT,
    )
    return float(Decimal(body["result"]) / (Decimal(10) ** ETH_DECIMALS))
