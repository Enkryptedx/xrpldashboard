"""Milestone 2 tests for live_stream_relay.py — named feeds, log_only vs
enforce policy, per-client bounded queues, supply_updates decoration,
healthz fields. Companion to tests/test_live_stream_relay.py (legacy path).

Fake upstream here answers the `ledger` command (total_coins) and lets the
test inject `transaction` envelopes on demand. No DB, loopback only.
"""
from __future__ import annotations
import asyncio
import contextlib
import json
import os
import socket
import sys
from urllib.request import urlopen

import pytest
import websockets

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)
import live_stream_relay as R  # noqa: E402

RLUSD_ISSUER = "rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De"
RLUSD_CUR = "524C555344000000000000000000000000000000"
AMM = "rN7n7otQDd6FczFgLdSqtcsAUxDkw6fzRH"      # any valid classic address
OTHER = "rHb9CJAWyB4rj91VRWn96DkukG4bwdtyTh"    # genesis account (valid)
TOTAL_COINS = "99987000000000000"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class FakeUpstream:
    """rippled stand-in: subscribe response, ledgerClosed ticks, answers the
    `ledger` command with total_coins, and forwards injected transactions."""

    def __init__(self, port: int):
        self.port = port
        self.server = None
        self.ws = None
        self.subscribed_streams = None
        self.tick_s = 0.2
        self.ledger_i = 0
        self.ledger_cmds = 0

    async def start(self):
        self.server = await websockets.serve(self._handler, "127.0.0.1", self.port)

    async def stop(self):
        self.server.close()
        await self.server.wait_closed()

    async def _handler(self, ws):
        self.ws = ws
        tick = asyncio.create_task(self._ticker(ws))
        try:
            async for raw in ws:
                req = json.loads(raw)
                if req.get("command") == "subscribe":
                    self.subscribed_streams = req.get("streams")
                    await ws.send(json.dumps({
                        "id": req.get("id", "s"), "type": "response", "status": "success",
                        "result": {"ledger_index": 100000, "ledger_hash": "AA",
                                   "ledger_time": 1000000000, "fee_base": 10,
                                   "reserve_base": 10000000, "reserve_inc": 2000000,
                                   "validated_ledgers": "1-100000"},
                    }))
                elif req.get("command") == "ledger":
                    self.ledger_cmds += 1
                    await ws.send(json.dumps({
                        "id": req.get("id"), "type": "response", "status": "success",
                        "result": {"ledger_index": 100000 + self.ledger_i,
                                   "ledger": {"total_coins": TOTAL_COINS,
                                              "ledger_index": str(100000 + self.ledger_i)},
                                   "validated": True},
                    }))
        except Exception:
            pass
        finally:
            tick.cancel()
            with contextlib.suppress(BaseException):
                await tick

    async def _ticker(self, ws):
        while True:
            await asyncio.sleep(self.tick_s)
            self.ledger_i += 1
            i = self.ledger_i
            await ws.send(json.dumps({
                "type": "ledgerClosed", "ledger_index": 100000 + i,
                "ledger_hash": f"BB{i:04d}", "ledger_time": 1000000000 + i,
                "txn_count": i, "fee_base": 10, "reserve_base": 10000000,
                "reserve_inc": 2000000, "validated_ledgers": f"1-{100000+i}",
            }))

    async def inject(self, envelope: dict):
        await self.ws.send(json.dumps(envelope))


def _tx(account, dest=None, amount="1000000", tx_type="Payment", ok=True, validated=True):
    tx = {"TransactionType": tx_type, "Account": account, "hash": "C" * 64, "Sequence": 1}
    if dest:
        tx["Destination"] = dest
    if amount is not None:
        tx["Amount"] = amount
    return {"type": "transaction", "validated": validated,
            "meta": {"TransactionResult": "tesSUCCESS" if ok else "tecPATH_DRY",
                     "AffectedNodes": []},
            "tx_json": tx, "ledger_index": 100001}


@contextlib.asynccontextmanager
async def _boot(mode="log_only", **overrides):
    saved = {k: getattr(R, k) for k in list(overrides) + ["RELAY_MODE"]}
    for k, v in overrides.items():
        setattr(R, k, v)
    R.RELAY_MODE = mode
    up_port, ws_port, hz_port = _free_port(), _free_port(), _free_port()
    up = FakeUpstream(up_port)
    await up.start()
    state = R.RelayState()
    state.refs["pool_accounts"] = {AMM}
    state.refs["top100"] = {(RLUSD_CUR, RLUSD_ISSUER)}
    state.refs["whale_tier_drops"] = 100_000 * 1_000_000
    up_task = asyncio.create_task(R.upstream_loop(state, f"ws://127.0.0.1:{up_port}"))
    ws_srv = await websockets.serve(R._make_ws_handler(state), "127.0.0.1", ws_port,
                                    ping_interval=25, max_size=4096)
    hz_srv = await R.healthz_server(state, "127.0.0.1", hz_port)
    for _ in range(100):
        if state.last_ledger is not None and state.refs.get("total_coins") is not None:
            break
        await asyncio.sleep(0.05)
    try:
        yield state, up, ws_port, hz_port
    finally:
        up_task.cancel()
        with contextlib.suppress(BaseException):
            await up_task
        ws_srv.close(); await ws_srv.wait_closed()
        hz_srv.close(); await hz_srv.wait_closed()
        await up.stop()
        for k, v in saved.items():
            setattr(R, k, v)


async def _recv_until(ws, pred, timeout=3.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            return None
        try:
            m = json.loads(await asyncio.wait_for(ws.recv(), timeout=remaining))
        except asyncio.TimeoutError:
            return None
        if pred(m):
            return m


async def _sub(ws, feed, **extra):
    msg = {"id": f"sub-{feed}", "command": "subscribe", "stream": feed}
    msg.update(extra)
    await ws.send(json.dumps(msg))
    return await _recv_until(ws, lambda m: m.get("type") == "response" and m.get("id") == f"sub-{feed}")


# ── parse_subscribe ──

def test_parse_subscribe_shapes():
    assert R.parse_subscribe({"command": "subscribe", "streams": ["ledger"]}) == ("ledger", None, [])
    assert R.parse_subscribe({"command": "subscribe", "streams": ["transactions"]})[1] == "subscribe_only"
    for feed in ("ledger", "amm_transactions", "token_top100_transactions",
                 "whale_transactions", "supply_updates"):
        assert R.parse_subscribe({"command": "subscribe", "stream": feed}) == (feed, None, [])
    assert R.parse_subscribe({"command": "subscribe", "stream": "transactions"})[1] == "subscribe_only"
    assert R.parse_subscribe({"command": "subscribe", "stream": "wallet_transactions"})[1] == "bad_address"
    assert R.parse_subscribe({"command": "subscribe", "stream": "wallet_transactions",
                              "accounts": ["rNotAnAddress"]})[1] == "bad_address"
    assert R.parse_subscribe({"command": "subscribe", "stream": "wallet_transactions",
                              "accounts": [RLUSD_ISSUER, RLUSD_ISSUER]}) == (
        "wallet_transactions", None, [RLUSD_ISSUER])
    assert R.parse_subscribe({"command": "subscribe", "stream": "amm_transactions",
                              "accounts": [RLUSD_ISSUER]})[1] == "subscribe_only"
    assert R.is_valid_subscribe({"command": "subscribe", "stream": "supply_updates"}) is True


# ── upstream sub shape ──

def test_upstream_subscribes_transactions_and_ledger():
    async def run():
        async with _boot() as (state, up, _, _):
            assert up.subscribed_streams == ["transactions", "ledger"]
            assert state.refs["total_coins"] == int(TOTAL_COINS)
            assert up.ledger_cmds >= 1
    asyncio.run(run())


# ── named feeds ──

def test_amm_feed_delivers_only_matching_tx():
    async def run():
        async with _boot() as (state, up, ws_port, _):
            async with websockets.connect(f"ws://127.0.0.1:{ws_port}") as c:
                resp = await _sub(c, "amm_transactions")
                assert resp["status"] == "success" and resp["result"]["stream"] == "amm_transactions"
                assert state.feeds["amm_transactions"] and len(state.clients) == 1
                await up.inject(_tx(OTHER, dest=RLUSD_ISSUER))        # not AMM
                await up.inject(_tx(OTHER, dest=AMM))                 # AMM hit
                m = await _recv_until(c, lambda m: m.get("type") == "transaction")
                assert m["tx_json"]["Destination"] == AMM
                # No further transaction arrives (the non-AMM one was filtered)
                extra = await _recv_until(c, lambda m: m.get("type") == "transaction", timeout=0.6)
                assert extra is None
            assert state.feed_broadcast["amm_transactions"] == 1
    asyncio.run(run())


def test_token_and_whale_feeds():
    async def run():
        async with _boot() as (state, up, ws_port, _):
            async with websockets.connect(f"ws://127.0.0.1:{ws_port}") as tok, \
                    websockets.connect(f"ws://127.0.0.1:{ws_port}") as wh:
                await _sub(tok, "token_top100_transactions")
                await _sub(wh, "whale_transactions")
                await up.inject(_tx(OTHER, dest=AMM, amount={"currency": RLUSD_CUR,
                                                             "issuer": RLUSD_ISSUER, "value": "5"}))
                await up.inject(_tx(OTHER, dest=AMM, amount=str(200_000 * 1_000_000)))
                await up.inject(_tx(OTHER, dest=AMM, amount=str(1_000_000)))  # below tier
                t = await _recv_until(tok, lambda m: m.get("type") == "transaction")
                assert isinstance(t["tx_json"]["Amount"], dict)
                w = await _recv_until(wh, lambda m: m.get("type") == "transaction")
                assert w["tx_json"]["Amount"] == str(200_000 * 1_000_000)
                assert await _recv_until(tok, lambda m: m.get("type") == "transaction", 0.5) is None
                assert await _recv_until(wh, lambda m: m.get("type") == "transaction", 0.5) is None
    asyncio.run(run())


def test_unvalidated_or_failed_tx_never_relayed():
    async def run():
        async with _boot() as (state, up, ws_port, _):
            async with websockets.connect(f"ws://127.0.0.1:{ws_port}") as c:
                await _sub(c, "amm_transactions")
                await up.inject(_tx(OTHER, dest=AMM, ok=False))
                await up.inject(_tx(OTHER, dest=AMM, validated=False))
                assert await _recv_until(c, lambda m: m.get("type") == "transaction", 0.6) is None
            assert state.tx_seen == 2
    asyncio.run(run())


def test_wallet_feed_per_subscriber_addresses():
    async def run():
        async with _boot() as (state, up, ws_port, _):
            async with websockets.connect(f"ws://127.0.0.1:{ws_port}") as a, \
                    websockets.connect(f"ws://127.0.0.1:{ws_port}") as b:
                ra = await _sub(a, "wallet_transactions", accounts=[RLUSD_ISSUER])
                rb = await _sub(b, "wallet_transactions", accounts=[OTHER])
                assert ra["status"] == "success" and rb["status"] == "success"
                assert set(state.wallet_watch) == {RLUSD_ISSUER, OTHER}
                await up.inject(_tx(AMM, dest=RLUSD_ISSUER))
                m = await _recv_until(a, lambda m: m.get("type") == "transaction")
                assert m["tx_json"]["Destination"] == RLUSD_ISSUER
                assert await _recv_until(b, lambda m: m.get("type") == "transaction", 0.5) is None
            await asyncio.sleep(0.1)
            assert state.wallet_watch == {}  # cleaned on disconnect
    asyncio.run(run())


def test_wallet_bad_address_rejected_in_both_modes():
    for mode in ("log_only", "enforce"):
        async def run():
            async with _boot(mode=mode) as (state, up, ws_port, _):
                async with websockets.connect(f"ws://127.0.0.1:{ws_port}") as c:
                    await c.send(json.dumps({"id": "w", "command": "subscribe",
                                             "stream": "wallet_transactions",
                                             "accounts": ["rBogusBogusBogusBogusBogusBog"]}))
                    with pytest.raises(Exception):
                        while True:
                            await asyncio.wait_for(c.recv(), timeout=2)
                    assert c.close_code == 1008 and c.close_reason == "bad_address"
                assert state.reject_total == 1 and state.wallet_watch == {}
        asyncio.run(run())


# ── policy: log_only vs enforce ──

def test_addr_cap_log_only_allows_and_records_shadow_trip(caplog):
    async def run():
        async with _boot(mode="log_only", PER_CLIENT_ADDR_CAP=1) as (state, up, ws_port, _):
            async with websockets.connect(f"ws://127.0.0.1:{ws_port}") as c:
                resp = await _sub(c, "wallet_transactions", accounts=[RLUSD_ISSUER, OTHER])
                assert resp["status"] == "success"
                assert set(state.wallet_watch) == {RLUSD_ISSUER, OTHER}
            assert state.shadow_trips.get("addr_cap") == 1
            assert state.reject_total == 0
    import logging
    with caplog.at_level(logging.WARNING, logger="live_stream_relay"):
        asyncio.run(run())
    assert any("SHADOW_TRIP addr_cap" in r.getMessage() for r in caplog.records)


def test_addr_cap_enforce_closes_1008():
    async def run():
        async with _boot(mode="enforce", PER_CLIENT_ADDR_CAP=1) as (state, up, ws_port, _):
            async with websockets.connect(f"ws://127.0.0.1:{ws_port}") as c:
                await c.send(json.dumps({"id": "w", "command": "subscribe",
                                         "stream": "wallet_transactions",
                                         "accounts": [RLUSD_ISSUER, OTHER]}))
                with pytest.raises(Exception):
                    while True:
                        await asyncio.wait_for(c.recv(), timeout=2)
                assert c.close_code == 1008 and c.close_reason == "addr_cap"
            assert state.wallet_watch == {} and state.reject_total == 1
            assert state.shadow_trips.get("addr_cap") == 1
    asyncio.run(run())


def test_addr_change_rate_enforce_vs_log_only():
    addrs = [RLUSD_ISSUER, OTHER, AMM]

    async def churn(mode):
        async with _boot(mode=mode, ADDR_CHANGE_PER_MIN=2, MAX_MSGS_PER_MIN=100) as (state, up, ws_port, _):
            async with websockets.connect(f"ws://127.0.0.1:{ws_port}") as c:
                closed = False
                for i in range(6):
                    try:
                        await c.send(json.dumps({"id": f"w{i}", "command": "subscribe",
                                                 "stream": "wallet_transactions",
                                                 "accounts": [addrs[i % 3]]}))
                        await asyncio.sleep(0.05)
                    except Exception:
                        closed = True
                        break
                await asyncio.sleep(0.3)
                if c.close_code is not None:
                    closed = True
                return closed, c.close_reason, dict(state.shadow_trips)

    closed, reason, trips = asyncio.run(churn("enforce"))
    assert closed and reason == "addr_change_rate"
    closed, reason, trips = asyncio.run(churn("log_only"))
    assert not closed and trips.get("addr_change_rate", 0) >= 1


def test_agg_addr_cap():
    async def run():
        async with _boot(mode="enforce", AGG_WALLET_ADDR_CAP=1) as (state, up, ws_port, _):
            async with websockets.connect(f"ws://127.0.0.1:{ws_port}") as a, \
                    websockets.connect(f"ws://127.0.0.1:{ws_port}") as b:
                assert (await _sub(a, "wallet_transactions", accounts=[RLUSD_ISSUER]))["status"] == "success"
                await b.send(json.dumps({"id": "w", "command": "subscribe",
                                         "stream": "wallet_transactions", "accounts": [OTHER]}))
                with pytest.raises(Exception):
                    while True:
                        await asyncio.wait_for(b.recv(), timeout=2)
                assert b.close_code == 1008 and b.close_reason == "agg_addr_cap"
                assert set(state.wallet_watch) == {RLUSD_ISSUER}
    asyncio.run(run())


# ── bounded queue (drop-oldest) ──

def test_queue_drop_oldest_is_mechanical_and_counted():
    async def run():
        async with _boot(mode="log_only", SLOW_CONSUMER_CAP=3) as (state, up, ws_port, _):
            async with websockets.connect(f"ws://127.0.0.1:{ws_port}") as c:
                await _sub(c, "amm_transactions")
                client = next(iter(state.clients))
                # Simulate a stalled sender: enqueue directly past the cap.
                for i in range(10):
                    R._enqueue(state, client, json.dumps({"n": i}))
                assert client.queue.qsize() == 3
                kept = [json.loads(client.queue.get_nowait())["n"] for _ in range(3)]
                assert kept == [7, 8, 9]                    # oldest dropped
                assert state.slow_consumer_drops == 7
                assert client.slow_drops == 7
                assert state.shadow_trips.get("queue_drop") == 1  # first drop logged
    asyncio.run(run())


# ── supply_updates ──

def test_supply_updates_decorated_with_total_coins():
    async def run():
        async with _boot() as (state, up, ws_port, _):
            async with websockets.connect(f"ws://127.0.0.1:{ws_port}") as c:
                await _sub(c, "supply_updates")
                m = await _recv_until(c, lambda m: m.get("type") == "supply_update")
                assert m["total_coins"] == int(TOTAL_COINS)
                assert isinstance(m["ledger_index"], int)
                assert m["total_coins_age_s"] is not None
                # A supply_updates subscriber does NOT also get raw ledgerClosed
                raw = await _recv_until(c, lambda m: m.get("type") == "ledgerClosed", 0.5)
                assert raw is None or raw["ledger_index"] <= 100000 + 1  # only the connect replay
    asyncio.run(run())


# ── legacy + named on one socket; healthz ──

def test_legacy_ledger_and_named_feed_coexist_and_healthz_fields():
    async def run():
        async with _boot() as (state, up, ws_port, hz_port):
            async with websockets.connect(f"ws://127.0.0.1:{ws_port}") as c:
                await c.send(json.dumps({"id": "legacy", "command": "subscribe", "streams": ["ledger"]}))
                r = await _recv_until(c, lambda m: m.get("type") == "response" and m.get("id") == "legacy")
                assert r["result"]["stream"] == "ledger" and r["result"]["relay_mode"] == "log_only"
                await _sub(c, "wallet_transactions", accounts=[RLUSD_ISSUER])
                lc = await _recv_until(c, lambda m: m.get("type") == "ledgerClosed" and m["ledger_index"] > 100001)
                assert lc is not None
                loop = asyncio.get_running_loop()
                body = await loop.run_in_executor(
                    None, lambda: urlopen(f"http://127.0.0.1:{hz_port}/healthz", timeout=2).read())
                snap = json.loads(body)
                assert snap["ok"] is True and snap["upstream_connected"] is True
                assert snap["relay_mode"] == "log_only"
                assert snap["feeds"]["ledger"] == 1 and snap["feeds"]["wallet_transactions"] == 1
                assert snap["feeds"]["amm_transactions"] == 0
                assert snap["slow_consumer_drops"] == 0
                assert snap["wallet_addr_count"] == 1
                assert snap["total_coins"] == int(TOTAL_COINS)
                assert snap["total_coins_age_s"] is not None
                assert snap["refs_pool_accounts"] == 1 and snap["refs_top100"] == 1
                assert "shadow_trips" in snap and "tx_seen" in snap
    asyncio.run(run())


def test_refs_file_loader(tmp_path):
    p = tmp_path / "refs.json"
    p.write_text(json.dumps({"pool_accounts": [AMM], "top100": [[RLUSD_CUR, RLUSD_ISSUER]]}))
    refs = R.load_refs_from_file(str(p))
    assert refs["pool_accounts"] == {AMM}
    assert refs["top100"] == {(RLUSD_CUR, RLUSD_ISSUER)}


# ── upstream read queue (incident 2026-09-26: rippled 1008 "client is too slow") ──

def test_upstream_read_queue_absorbs_bursts(monkeypatch):
    """Milestone 2 subscribes upstream to transactions+ledger; rippled dumps a
    ledger's transactions in one loopback burst. websockets' default
    max_queue=16 applied back-pressure at 16 frames and rippled closed the
    socket 531 times in 7 h. The connect kwargs must carry a burst-sized
    queue, env-tunable."""
    kw = R.upstream_connect_kwargs()
    assert kw["max_queue"] >= 1024
    assert kw["max_size"] is None
    monkeypatch.setattr(R, "UPSTREAM_MAX_QUEUE", 777)
    assert R.upstream_connect_kwargs()["max_queue"] == 777


def test_upstream_loop_passes_queue_kwargs_to_connect(monkeypatch):
    seen = {}

    def fake_connect(url, **kwargs):
        seen.update(kwargs)
        raise asyncio.CancelledError()

    monkeypatch.setattr(R.websockets, "connect", fake_connect)
    state = R.RelayState()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(R.upstream_loop(state, "ws://127.0.0.1:1"))
    assert seen["max_queue"] == R.UPSTREAM_MAX_QUEUE


def test_default_upstream_is_port_ws_public_6007(monkeypatch):
    """2026-09-26: the admin ws (6006) keeps rippled's default
    send_queue_limit=100 and closes a transactions subscriber with 1008
    "client is too slow" on every ledger burst; 6007 ([port_ws_public])
    carries the raised limit. The code default must never regress to 6006."""
    import importlib
    monkeypatch.delenv("LIVE_STREAM_RELAY_UPSTREAM", raising=False)
    mod = importlib.reload(R)
    try:
        assert mod.UPSTREAM_URL == "ws://127.0.0.1:6007"
    finally:
        importlib.reload(R)
