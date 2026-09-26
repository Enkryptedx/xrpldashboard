"""wss_relay_canary --all-feeds probes against a loopback Milestone-2 relay.

Proves the per-feed verdict logic end to end (handshake, event wait,
OK_SILENT for traffic-dependent feeds, FAIL on a missing REQUIRED event,
FAIL on a rejected sub) without DB or internet. The canary's main() is not
exercised here (it writes walker_health); only the probe layer is.
"""
from __future__ import annotations
import asyncio
import contextlib
import json
import os
import sys

import websockets

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
import live_stream_relay as R  # noqa: E402
import wss_relay_canary as C  # noqa: E402
from test_live_stream_relay_feeds import FakeUpstream, _free_port, _tx, AMM, OTHER, TOTAL_COINS  # noqa: E402


@contextlib.asynccontextmanager
async def _relay():
    up_port, ws_port = _free_port(), _free_port()
    up = FakeUpstream(up_port)
    await up.start()
    state = R.RelayState()
    state.refs["pool_accounts"] = {AMM}
    up_task = asyncio.create_task(R.upstream_loop(state, f"ws://127.0.0.1:{up_port}"))
    srv = await websockets.serve(R._make_ws_handler(state), "127.0.0.1", ws_port,
                                 ping_interval=25, max_size=4096)
    for _ in range(100):
        if state.last_ledger is not None and state.refs.get("total_coins") is not None:
            break
        await asyncio.sleep(0.05)
    try:
        yield state, up, f"ws://127.0.0.1:{ws_port}"
    finally:
        up_task.cancel()
        with contextlib.suppress(BaseException):
            await up_task
        srv.close(); await srv.wait_closed()
        await up.stop()


def test_supply_updates_probe_ok_and_carries_total_coins():
    async def run():
        async with _relay() as (state, up, url):
            verdict, detail = await C._probe_feed(url, "supply_updates", None, 3.0, True)
            assert verdict == "OK", (verdict, detail)
            assert detail["handshake"] is True and detail["events"] == 1
            assert detail["total_coins"] == int(TOTAL_COINS)
    asyncio.run(run())


def test_silent_feed_is_ok_silent_not_fail():
    async def run():
        async with _relay() as (state, up, url):
            verdict, detail = await C._probe_feed(url, "whale_transactions", None, 0.5, False)
            assert verdict == "OK_SILENT" and detail["handshake"] is True
    asyncio.run(run())


def test_required_event_missing_is_fail():
    async def run():
        async with _relay() as (state, up, url):
            verdict, detail = await C._probe_feed(url, "wallet_transactions", [C.RLUSD_ISSUER], 0.5, True)
            assert verdict == "FAIL_no_event" and detail["handshake"] is True
    asyncio.run(run())


def test_wallet_probe_sees_issuer_traffic():
    async def run():
        async with _relay() as (state, up, url):
            async def feed_traffic():
                for _ in range(6):
                    await asyncio.sleep(0.15)
                    if up.ws is not None:
                        await up.inject(_tx(OTHER, dest=C.RLUSD_ISSUER))
            t = asyncio.create_task(feed_traffic())
            verdict, detail = await C._probe_feed(url, "wallet_transactions", [C.RLUSD_ISSUER], 3.0, True)
            t.cancel()
            with contextlib.suppress(BaseException):
                await t
            assert verdict == "OK" and detail["events"] == 1
    asyncio.run(run())


def test_rejected_sub_is_fail_close():
    async def run():
        async with _relay() as (state, up, url):
            # Malformed address -> relay closes 1008 bad_address regardless of mode
            verdict, detail = await C._probe_feed(url, "wallet_transactions", ["rNope"], 1.0, True)
            assert verdict.startswith("FAIL_close_1008"), verdict
    asyncio.run(run())


def test_all_feeds_flag_parsing(monkeypatch):
    assert C._all_feeds_enabled(["--all-feeds"]) is True
    monkeypatch.delenv("WSS_RELAY_CANARY_ALL_FEEDS", raising=False)
    assert C._all_feeds_enabled([]) is False
    monkeypatch.setenv("WSS_RELAY_CANARY_ALL_FEEDS", "1")
    assert C._all_feeds_enabled([]) is True


def test_verdict_dict_shape():
    # The message payload L1 parses: FEEDS={"feed":"VERDICT",...}
    d = {"ledger": "OK", "amm_transactions": "OK_SILENT", "supply_updates": "FAIL_no_event"}
    s = json.dumps(d, sort_keys=True, separators=(",", ":"))
    assert s == '{"amm_transactions":"OK_SILENT","ledger":"OK","supply_updates":"FAIL_no_event"}'


# ── Served-homepage freshness gate (incident 2026-09-26, frozen pre-render) ──

_HP = ('<html><head></head><body><span id="cached-ts" data-iso="{iso}">x</span>'
       '<script>var u="wss://wss.xrpldashboard.com";</script></body></html>')


def test_homepage_fresh_ok_within_budget():
    import datetime as dt
    now = dt.datetime(2026, 9, 26, 15, 0, 0, tzinfo=dt.timezone.utc)
    body = _HP.format(iso="2026-09-26T14:50:00Z")  # 600 s old
    assert C._classify_homepage_body(body, now=now) == ("ok", "ok", 600)


def test_homepage_stale_beyond_budget_is_flagged():
    # The 25.5 h freeze shape: relay URL present (URL check green), body
    # baked long ago. The freshness axis must trip on its own.
    import datetime as dt
    now = dt.datetime(2026, 9, 26, 14, 0, 0, tzinfo=dt.timezone.utc)
    body = _HP.format(iso="2026-09-25T12:35:39Z")
    url, fresh, age = C._classify_homepage_body(body, now=now)
    assert (url, fresh) == ("ok", "stale")
    assert age > C.HOMEPAGE_SERVED_MAX_BAKED_AGE_S


def test_homepage_no_ts_is_flagged_not_ok():
    body = '<html><body>wss.xrpldashboard.com</body></html>'
    assert C._classify_homepage_body(body) == ("ok", "no_ts", None)


def test_homepage_budget_exact_edge():
    import datetime as dt
    now = dt.datetime(2026, 9, 26, 15, 0, 0, tzinfo=dt.timezone.utc)
    edge = now - dt.timedelta(seconds=C.HOMEPAGE_SERVED_MAX_BAKED_AGE_S)
    body = _HP.format(iso=edge.strftime("%Y-%m-%dT%H:%M:%SZ"))
    assert C._classify_homepage_body(body, now=now)[1] == "ok"
    body = _HP.format(iso=(edge - dt.timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    assert C._classify_homepage_body(body, now=now)[1] == "stale"
