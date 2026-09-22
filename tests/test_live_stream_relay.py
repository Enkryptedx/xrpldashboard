"""Smoke test for deploy/lenovo/live_stream_relay.py.

Spawns a fake upstream WS (mimicking rippled ledger stream), boots the relay
against it, and drives two fake browser clients through the golden path +
subscribe-only reject + rate-limit reject + /healthz.

Runs with plain pytest (no pytest-asyncio needed) via asyncio.run inside each
test function.
"""
from __future__ import annotations
import asyncio
import contextlib
import json
import os
import socket
import sys
import time
from urllib.request import urlopen

import pytest
import websockets

# Import the relay module from the repo root. Kept at root so it can be
# rsync'd to the Lenovo alongside xrpl_stream.py (same convention).
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)
import live_stream_relay as R  # noqa: E402


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


async def _fake_upstream(port: int, tx_count_start: int = 0):
    """Fake rippled: subscribe response then ledgerClosed events every 200ms."""
    async def handler(ws):
        try:
            first = await asyncio.wait_for(ws.recv(), timeout=2.0)
            req = json.loads(first)
            assert req.get("command") == "subscribe" and "ledger" in req.get("streams", [])
            # Subscribe response with current ledger
            await ws.send(json.dumps({
                "id": req.get("id", "s"),
                "type": "response",
                "status": "success",
                "result": {
                    "ledger_index": 100000,
                    "ledger_hash": "AA",
                    "ledger_time": 1000000000,
                    "fee_base": 10, "reserve_base": 10000000,
                    "reserve_inc": 2000000, "validated_ledgers": "1-100000",
                },
            }))
            i = 0
            while True:
                await asyncio.sleep(0.2)
                i += 1
                await ws.send(json.dumps({
                    "type": "ledgerClosed",
                    "ledger_index": 100000 + i,
                    "ledger_hash": f"BB{i:04d}",
                    "ledger_time": 1000000000 + i,
                    "txn_count": tx_count_start + i,
                    "fee_base": 10, "reserve_base": 10000000,
                    "reserve_inc": 2000000, "validated_ledgers": f"1-{100000+i}",
                }))
        except Exception:
            pass
    server = await websockets.serve(handler, "127.0.0.1", port)
    return server


@contextlib.asynccontextmanager
async def _boot(*, healthz: bool = True):
    up_port = _free_port()
    ws_port = _free_port()
    hz_port = _free_port()
    upstream = await _fake_upstream(up_port)
    state = R.RelayState()
    up_task = asyncio.create_task(R.upstream_loop(state, f"ws://127.0.0.1:{up_port}"))
    ws_srv = await websockets.serve(R._make_ws_handler(state), "127.0.0.1", ws_port,
                                    ping_interval=25, max_size=4096)
    hz_srv = await R.healthz_server(state, "127.0.0.1", hz_port) if healthz else None
    # Wait for upstream to connect + cache last_ledger
    for _ in range(50):
        if state.last_ledger is not None:
            break
        await asyncio.sleep(0.05)
    try:
        yield state, ws_port, hz_port
    finally:
        up_task.cancel()
        try:
            await up_task
        except BaseException:
            pass
        ws_srv.close(); await ws_srv.wait_closed()
        if hz_srv:
            hz_srv.close(); await hz_srv.wait_closed()
        upstream.close(); await upstream.wait_closed()


def test_subscribe_only_valid_check():
    assert R.is_valid_subscribe({"command": "subscribe", "streams": ["ledger"]}) is True
    assert R.is_valid_subscribe({"command": "subscribe", "streams": ["ledger", "transactions"]}) is False
    assert R.is_valid_subscribe({"command": "subscribe", "streams": []}) is False
    assert R.is_valid_subscribe({"command": "unsubscribe", "streams": ["ledger"]}) is False
    assert R.is_valid_subscribe({"command": "ledger_current"}) is False
    assert R.is_valid_subscribe({}) is False
    assert R.is_valid_subscribe("bogus") is False


def test_two_clients_receive_fanout():
    async def run():
        async with _boot() as (state, ws_port, _):
            uri = f"ws://127.0.0.1:{ws_port}"
            async with websockets.connect(uri) as c1, websockets.connect(uri) as c2:
                # Collect messages from both clients concurrently for 1.5s.
                # Fake upstream ticks ledgerClosed every 200ms — expect ~7 in that
                # window plus the 1 replayed on connect (types: ledgerClosed).
                sub = json.dumps({"id": "s", "command": "subscribe", "streams": ["ledger"]})
                await c1.send(sub); await c2.send(sub)
                got_ledger = {"c1": 0, "c2": 0}
                got_response = {"c1": 0, "c2": 0}
                async def collect(client, key):
                    while True:
                        try:
                            m = json.loads(await asyncio.wait_for(client.recv(), timeout=0.5))
                        except asyncio.CancelledError:
                            return
                        except asyncio.TimeoutError:
                            continue
                        except Exception:
                            return
                        t = m.get("type")
                        if t == "ledgerClosed":
                            got_ledger[key] += 1
                        elif t == "response":
                            got_response[key] += 1
                async def stop_after(sec):
                    await asyncio.sleep(sec)
                task_c1 = asyncio.create_task(collect(c1, "c1"))
                task_c2 = asyncio.create_task(collect(c2, "c2"))
                await stop_after(1.5)
                task_c1.cancel(); task_c2.cancel()
                for t in (task_c1, task_c2):
                    try:
                        await t
                    except BaseException:
                        pass
                assert got_ledger["c1"] >= 3, f"c1 ledgers={got_ledger['c1']}"
                assert got_ledger["c2"] >= 3, f"c2 ledgers={got_ledger['c2']}"
                assert got_response["c1"] >= 1
                assert got_response["c2"] >= 1
                assert state.broadcast_total >= 3
                assert len(state.clients) == 2
    asyncio.run(run())


def test_subscribe_only_rejects_ledger_current():
    async def run():
        async with _boot() as (state, ws_port, _):
            uri = f"ws://127.0.0.1:{ws_port}"
            async with websockets.connect(uri) as c:
                _ = await asyncio.wait_for(c.recv(), timeout=2)  # replay
                await c.send(json.dumps({"id": "bad", "command": "ledger_current"}))
                # Server should close 1008
                with pytest.raises(Exception):
                    await asyncio.wait_for(c.recv(), timeout=2)
                assert c.close_code == 1008
            assert state.reject_total >= 1
    asyncio.run(run())


def test_subscribe_only_rejects_non_ledger_stream():
    async def run():
        async with _boot() as (state, ws_port, _):
            uri = f"ws://127.0.0.1:{ws_port}"
            async with websockets.connect(uri) as c:
                _ = await asyncio.wait_for(c.recv(), timeout=2)
                await c.send(json.dumps({"id": "bad", "command": "subscribe",
                                         "streams": ["ledger", "transactions"]}))
                with pytest.raises(Exception):
                    await asyncio.wait_for(c.recv(), timeout=2)
                assert c.close_code == 1008
            assert state.reject_total >= 1
    asyncio.run(run())


def test_rate_limit_kicks_in():
    async def run():
        # Temporarily lower the rate ceiling for the test.
        orig = R.MAX_MSGS_PER_MIN
        R.MAX_MSGS_PER_MIN = 3
        try:
            async with _boot() as (state, ws_port, _):
                uri = f"ws://127.0.0.1:{ws_port}"
                async with websockets.connect(uri) as c:
                    _ = await asyncio.wait_for(c.recv(), timeout=2)
                    sub = json.dumps({"id": "s", "command": "subscribe", "streams": ["ledger"]})
                    for _ in range(5):
                        try:
                            await c.send(sub)
                        except Exception:
                            break
                    # Server should close 1008 after >3 msgs in a minute
                    await asyncio.sleep(0.5)
                    assert c.close_code == 1008, f"expected 1008, got {c.close_code}"
        finally:
            R.MAX_MSGS_PER_MIN = orig
    asyncio.run(run())


def test_healthz_returns_json():
    async def run():
        async with _boot() as (state, ws_port, hz_port):
            uri = f"ws://127.0.0.1:{ws_port}"
            async with websockets.connect(uri):
                # give the server a beat to register the client
                await asyncio.sleep(0.1)
                # urlopen is blocking — run it in the default executor so the
                # asyncio healthz server can service it.
                loop = asyncio.get_running_loop()
                body = await loop.run_in_executor(
                    None,
                    lambda: urlopen(f"http://127.0.0.1:{hz_port}/healthz", timeout=2).read(),
                )
                snap = json.loads(body)
                assert snap["ok"] is True
                assert snap["upstream_connected"] is True
                assert snap["clients"] >= 1
                assert snap["last_ledger_index"] is not None
    asyncio.run(run())
