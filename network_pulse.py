"""
XRPL Network Pulse

Lightweight, single-call network health snapshot. Designed to render at the
top of the dashboard as the editorial "what is XRPL doing right now" answer.

What this module does NOT cover (deferred to historical snapshot infra):
    - 24h transaction count
    - 24h fees burned
    - 24h active accounts
    - Ledgers closed in 24h
Those require iterating ~20k ledgers per render, which is not viable at
page-load latency. They belong to a background snapshot job.
"""

from datetime import datetime, timezone
import os
import threading
import time

from xrpl.clients import JsonRpcClient
from xrpl.models.requests import Ledger, ServerInfo


XRPL_NODE = "https://s1.ripple.com:51234"

# XRPL ledger close_time is seconds since Jan 1 2000 UTC (the "ripple epoch").
RIPPLE_EPOCH_OFFSET = 946684800

# How many recent ledgers to span when computing the moving avg close time.
# 1000 ledgers ≈ ~hour at 3.5s/ledger. Big enough to smooth jitter, small
# enough to still feel "current."
CLOSE_TIME_SAMPLE_LEDGERS = 1000

# Pulse cache TTL. Shorter than the AMM cache because the pulse is the
# "is it alive right now" panel — staleness here is more visible to users.
PULSE_CACHE_TTL_SECONDS = int(os.environ.get("PULSE_CACHE_TTL_SECONDS", "20"))
_cache_lock = threading.Lock()
_cache_state = {"data": None, "fetched_at": 0.0}


def _ripple_to_unix(close_time):
    return int(close_time) + RIPPLE_EPOCH_OFFSET


def _classify(load_factor, last_close_age, avg_close):
    """Rule-based status. Plain-language, no LLM, no hype.

    Primary signal: avg ledger close time.
      - Normal XRPL target: 3–4s. Above 5s = throughput genuinely degraded.
    Secondary signal: load_factor from server_info.
      - This is the *local* node's fee multiplier, not a network-wide figure.
        Public nodes like s1.ripple.com routinely show 100–500x because they
        absorb enormous client traffic. High load_factor alone does NOT mean the
        network is slow — close time is the honest congestion signal.
      - We only surface fee pressure when close time is also elevated, or when
        load is extreme (>256) AND close time is borderline (>4s), to avoid
        falsely labelling a 3.9s-close network as "CONGESTED".
    """
    if last_close_age is None or avg_close is None:
        return "unknown", "Network status unavailable."

    # Degraded: ledgers stalled or closing very slowly.
    if last_close_age > 30 or avg_close > 6.0:
        return (
            "degraded",
            "XRPL is degraded. Ledgers are closing slowly.",
        )

    # Congested: close time elevated AND fees are high — both signals together.
    if avg_close > 5.0 and load_factor and load_factor > 10.0:
        return (
            "congested",
            f"XRPL is congested. "
            f"Closing ledgers every {avg_close:.1f}s with elevated fee load.",
        )

    # Hot: close time slightly above target, or extreme local node load.
    if avg_close > 4.5 or (load_factor and load_factor > 256 and avg_close > 4.0):
        return (
            "running_hot",
            f"XRPL is operating normally. "
            f"Closing ledgers every {avg_close:.1f}s. Load slightly elevated.",
        )

    # Healthy.
    return (
        "operating_normally",
        f"XRPL is operating normally. "
        f"Closing ledgers every {avg_close:.1f}s. Network load is light.",
    )


def fetch_pulse():
    """Single-shot live pulse. No cache. Returns a dict with all panel fields."""
    timestamp = datetime.now(timezone.utc)
    client = JsonRpcClient(XRPL_NODE)

    try:
        si = client.request(ServerInfo()).result.get("info", {})
    except Exception as e:
        return {
            "error": f"server_info failed: {e}",
            "timestamp": timestamp,
            "node": XRPL_NODE,
        }

    validated = si.get("validated_ledger") or {}
    current_seq = validated.get("seq")
    last_close_age = validated.get("age")  # seconds since last close, per node clock
    base_fee_xrp = validated.get("base_fee_xrp")
    reserve_base_xrp = validated.get("reserve_base_xrp")
    reserve_inc_xrp = validated.get("reserve_inc_xrp")

    quorum = si.get("validation_quorum")
    load_factor = si.get("load_factor")
    complete_ledgers = si.get("complete_ledgers")

    # Avg close time: sample two ledger headers (current + N back) and divide
    # the close_time delta by the sequence delta. server_info doesn't return
    # close_time on Clio nodes, so we have to query the headers explicitly.
    avg_close = None
    if current_seq:
        target = current_seq - CLOSE_TIME_SAMPLE_LEDGERS
        try:
            current = client.request(
                Ledger(ledger_index=current_seq, transactions=False, expand=False)
            ).result.get("ledger") or {}
            old = client.request(
                Ledger(ledger_index=target, transactions=False, expand=False)
            ).result.get("ledger") or {}
            cur_close = current.get("close_time")
            cur_seq = current.get("ledger_index")
            old_close = old.get("close_time")
            old_seq = old.get("ledger_index")
            if all(v is not None for v in (cur_close, cur_seq, old_close, old_seq)):
                seq_delta = int(cur_seq) - int(old_seq)
                time_delta = int(cur_close) - int(old_close)
                if seq_delta > 0:
                    avg_close = time_delta / seq_delta
        except Exception:
            # Non-fatal — pulse panel still renders, status falls back to "unknown."
            pass

    status, status_text = _classify(load_factor, last_close_age, avg_close)

    return {
        "error": None,
        "timestamp": timestamp,
        "node": XRPL_NODE,
        "ledger_index": current_seq,
        "last_close_age_seconds": last_close_age,
        "avg_close_seconds": avg_close,
        "validation_quorum": quorum,
        "load_factor": load_factor,
        "base_fee_xrp": base_fee_xrp,
        "reserve_base_xrp": reserve_base_xrp,
        "reserve_inc_xrp": reserve_inc_xrp,
        "complete_ledgers": complete_ledgers,
        "status": status,
        "status_text": status_text,
    }


def fetch_pulse_cached(ttl_seconds=None):
    """Cached wrapper around fetch_pulse(). Same dict shape + cached_age_seconds.

    Never cache an errored pulse — a transient server_info hiccup at startup
    would otherwise pin the /health page to "currently unavailable" for the
    full TTL window even after the network recovers."""
    ttl = ttl_seconds if ttl_seconds is not None else PULSE_CACHE_TTL_SECONDS
    now = time.time()

    with _cache_lock:
        cached = _cache_state["data"]
        fetched_at = _cache_state["fetched_at"]
        age = now - fetched_at

        if cached is not None and age < ttl:
            result = dict(cached)
            result["cached_age_seconds"] = round(age, 1)
            return result

        fresh = fetch_pulse()
        if not fresh.get("error"):
            _cache_state["data"] = fresh
            _cache_state["fetched_at"] = now
        result = dict(fresh)
        result["cached_age_seconds"] = 0.0
        return result


def main():
    """CLI: print pulse to stdout. Useful for cron or smoke-testing."""
    p = fetch_pulse()
    if p.get("error"):
        print(f"ERROR: {p['error']}")
        return 1

    print("=" * 72)
    print("XRPL NETWORK PULSE")
    print("=" * 72)
    print(f"Node:    {p['node']}")
    print(f"Time:    {p['timestamp'].strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print()
    print(f"Status:  {p['status_text']}")
    print()
    print(f"  Ledger index:        {p['ledger_index']:,}" if p['ledger_index'] else "  Ledger index:        unknown")
    print(f"  Last close age:      {p['last_close_age_seconds']}s" if p['last_close_age_seconds'] is not None else "  Last close age:      unknown")
    print(f"  Avg close time:      {p['avg_close_seconds']:.2f}s (last {CLOSE_TIME_SAMPLE_LEDGERS} ledgers)" if p['avg_close_seconds'] is not None else "  Avg close time:      unknown")
    print(f"  Validation quorum:   {p['validation_quorum']}")
    print(f"  Load factor:         {p['load_factor']}")
    print(f"  Base fee:            {p['base_fee_xrp']} XRP")
    print(f"  Reserve (base):      {p['reserve_base_xrp']} XRP")
    print(f"  Reserve (per item):  {p['reserve_inc_xrp']} XRP")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
