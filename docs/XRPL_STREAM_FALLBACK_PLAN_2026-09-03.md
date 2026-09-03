# xrpl_stream fallback + watchdog plan (2026-09-03)

**Verdict: NOT building s2 fallback — 98% uptime, sub-minute recovery. Real issue: watchdog-driven restarts (~53/day), tracked separately.**

## Background

xrpl_stream is a Lenovo-side subscription client that opens a websocket to the LAN rippled at `ws://127.0.0.1:6007` and consumes the `transactions` stream, writing every tx into `events.db` and various derived tables (`amm_pool_events`, `token_events`, etc.). It has no fallback at all: if the local rippled restarts, xrpl_stream retries locally until the node returns.

Whether that lack of fallback is a problem depends on how often the connection actually goes down and how long each outage lasts.

## Uptime measurement (2026-08-11 → 2026-09-03, 23-day span)

Log: `/home/charlie/xrpldashboard/logs/xrpl_stream.log` (13 MB, no rotation, spans full 23 days).

Measured against the actual `xrpl_stream starting` markers (each = one systemd auto-restart cycle), NOT against raw line-gap noise:

| Metric | Value |
|---|---|
| Restarts (systemd auto-recovered) | 1,238 (~53/day) |
| Watchdog-triggered closes | 1,218 (98% of restarts) |
| Median offline per restart | **31 seconds** |
| Total offline (all restarts summed) | 54.2 hr |
| Total offline excluding 4 setup-day outliers | ~10.6 hr / 23d ≈ **1.9% downtime, 98.1% uptime** |
| Longest single outage post-Aug 15 | ~1m16s (Sep 2) |
| Longest outage all-time (setup day Aug 11-12) | 27.4 hr |
| Anomaly day | Aug 30: 158 restarts in one day (mostly clustered 00-07 UTC) |

## Decision: no s2 fallback

Uptime is already ~98%. Every "outage" is a ~31s window where systemd bounces the process. Public s2 fallback would:
- Add cascade-selection logic (sovereign → s2)
- Add per-event `sourcing` tagging
- Add public-RPC leak surface to disclose
- Add a `walker_node_fallback` write path for xrpl_stream

...all to reduce a ~1.9% downtime by some fraction that assumes s2 is up during our local outages. That's not the right cost/benefit trade.

If uptime ever meaningfully degrades, the correct path is a **second sovereign node** — mirror the Lenovo rig, second rippled sync (~2 weeks), teach xrpl_stream to peer-elect. Never route to public s2 for xrpl_stream — the sovereignty covenant applies to writes as much as reads.

## Real issue: watchdog-driven restarts

The problem isn't outages. It's the watchdog killing healthy connections.

**Current code** (`xrpl_stream.py:54-55, 1123-1140`):
```python
IDLE_KILL_SECONDS = 90          # force-close session if no msg in this window
WATCHDOG_TICK_SECONDS = 10      # how often the watchdog checks for idle
```

**What it measures**: `last_msg_at` is updated on EVERY message from the WS iterator. The subscription is **transactions-only** (`Subscribe(streams=[StreamParameter.TRANSACTIONS])`). If no tx message arrives for 90 seconds, the watchdog calls `client.close()` + `os._exit(1)` to force a launchd-KeepAlive restart.

**Why it fires ~53/day**: 90 seconds of tx-only silence on the XRPL is rare but not impossible — a slow ledger, a WS internal buffer hiccup, or (most likely) a **silently-dead WS connection where ledger closes are still happening at the node but the WS iterator has stalled**. The watchdog can't distinguish "no tx activity" from "connection dead" because it only listens on the tx stream.

**Correlated pattern**: Aug 30 had 158 restarts clustered 00-07 UTC (128 of 158, one every ~3 minutes). Every one was watchdog-triggered. That's consistent with the WS client entering a "healthy iterator, no tx frames" state repeatedly over ~7 hours — probably a Lenovo rippled disk/IO/network hiccup during that specific window.

## Proposed fix (not building yet — awaiting ruling)

**Two changes:**

1. **Subscribe to `LEDGER` in addition to `TRANSACTIONS`**. Ledger closes fire every 3-5 seconds on the XRPL — a much more reliable liveness signal than tx activity. The watchdog's `last_msg_at` gets updated by both streams; 90s without ANY message from a live rippled is a true dead-connection signal.

   ```python
   client.send(Subscribe(streams=[
       StreamParameter.TRANSACTIONS,
       StreamParameter.LEDGER,
   ]))
   ```

   Plus a `msg.get("type") == "ledgerClosed"` skip in the handler dispatch so ledger messages count as liveness proof but don't get counted as `txns_seen`.

2. **Optionally raise `IDLE_KILL_SECONDS` from 90 → 180**. With ledger closes as the keepalive, 90s is still safe (30 missed closes = definitely dead). 180s gives margin for a truly slow validation round.

**Expected reduction**: from ~53/day to **<5/day** — only real WS disconnects would fire the watchdog. Watchdog stays as insurance against silently-dead sockets, doesn't fire on healthy-but-quiet ones.

**Rollout**: single edit to `xrpl_stream.py` on Lenovo, restart the service. Zero schema change. Zero downstream impact (the `LEDGER` messages type-filter out before `HANDLERS` dispatch, so `events.db` writes are unchanged).

**Risk**: if the WS client library treats a multi-stream subscribe differently from single-stream (batching, message ordering), the watchdog could see a different pattern of `last_msg_at` updates. Low probability but worth confirming with a 24h canary before making it permanent.

## Restart-rate metric (built + running)

**Walker `xrpl_stream_restart_rate`** (Mac-side, hourly SSH to Lenovo, `xrpl_stream_restart_rate_walker.py`) writes a walker_health row every hour with:
- `findings_count` = restart count in rolling 24h
- `message` = `24h=N restarts (watchdog=M) · 7d=X (Y.Y/day avg) · host=rippled-node`

First run: `24h=10 restarts (watchdog=7) · 7d=354 (50.6/day avg)`. The 7d confirms the ~53/day baseline holds.

Now the watchdog impact is visible in `/walker_health` instead of buried in a 250MB log. If the LEDGER-stream fix lands, the metric will drop and stay dropped — that's the measurable proof.

## Not built (per ruling)

- No s2 fallback code
- No LEDGER-stream fix yet (proposal above, awaiting greenlight)
- No second-sovereign-node planning (revisit if uptime slips)
