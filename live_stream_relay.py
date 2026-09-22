"""
Lenovo-side live-stream WSS relay for xrpldashboard.com browser panels.

One upstream connection to the local rippled ledger stream (ws://127.0.0.1:6006).
Fan-out to N browser clients over WebSockets. Subscribe-only whitelist.
Per-client rate limit + send timeout backpressure. Separate HTTP healthz.

Charlie ruling 2026-09-22 Tue PM (LIVE_STREAM_WSS_PRIMARY finish, not revert):
browser live feed migrates from public XRPL cluster to our own-node relay in a
staged rollout. This process is that relay. Deployed as a systemd unit alongside
rippled on the Lenovo IdeaCentre Mini. Exposed via cloudflared as
wss.xrpldashboard.com (new public hostname; rpc.xrpldashboard.com is
CF-Access-gated and browsers cannot authenticate transparently).

Contract:
  - Clients connect, then send exactly ONE valid message:
      {"id":"subscribe","command":"subscribe","streams":["ledger"]}
  - Anything else -> close 1008 policy_violation.
  - Server sends ledgerClosed events as they arrive from rippled.
  - Server replays the last cached ledgerClosed to a new client immediately
    on connect (matches xrplcluster first-paint behavior).

Ports:
  6007  WSS relay (browser-facing via cloudflared)
  6008  HTTP /healthz (localhost monitoring)

Env:
  LIVE_STREAM_RELAY_UPSTREAM         default ws://127.0.0.1:6006
  LIVE_STREAM_RELAY_HOST             default 127.0.0.1
  LIVE_STREAM_RELAY_PORT             default 6007
  LIVE_STREAM_RELAY_HEALTHZ_PORT     default 6008
  LIVE_STREAM_RELAY_MAX_MSGS_PER_MIN default 30   (per-client incoming ceiling)

Run:
  python3 live_stream_relay.py
"""
from __future__ import annotations
import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import time

try:
    import websockets
    from websockets.exceptions import ConnectionClosed
    from websockets.server import serve as ws_serve
except ImportError:
    print("live_stream_relay: missing 'websockets' (pip install 'websockets>=12')", file=sys.stderr)
    raise

UPSTREAM_URL = os.environ.get("LIVE_STREAM_RELAY_UPSTREAM", "ws://127.0.0.1:6006")
LISTEN_HOST = os.environ.get("LIVE_STREAM_RELAY_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("LIVE_STREAM_RELAY_PORT", "6007"))
HEALTHZ_PORT = int(os.environ.get("LIVE_STREAM_RELAY_HEALTHZ_PORT", "6008"))
MAX_MSGS_PER_MIN = int(os.environ.get("LIVE_STREAM_RELAY_MAX_MSGS_PER_MIN", "30"))
UPSTREAM_RECONNECT_BASE_S = 2.0
UPSTREAM_RECONNECT_MAX_S = 60.0
UPSTREAM_PING_INTERVAL_S = 20.0
CLIENT_PING_INTERVAL_S = 25.0
SEND_TIMEOUT_S = 1.0

log = logging.getLogger("live_stream_relay")


class RelayState:
    def __init__(self) -> None:
        self.clients: set = set()
        self.last_ledger: dict | None = None
        self.last_ledger_at: float | None = None
        self.upstream_connected: bool = False
        self.upstream_reconnects: int = 0
        self.broadcast_total: int = 0
        self.reject_total: int = 0
        self.dropped_slow: int = 0
        self.boot_at: float = time.time()

    def snapshot(self) -> dict:
        now = time.time()
        return {
            "ok": self.upstream_connected,
            "clients": len(self.clients),
            "last_ledger_index": (self.last_ledger or {}).get("ledger_index"),
            "last_ledger_age_s": round(now - self.last_ledger_at, 2) if self.last_ledger_at else None,
            "upstream_connected": self.upstream_connected,
            "upstream_reconnects": self.upstream_reconnects,
            "broadcast_total": self.broadcast_total,
            "reject_total": self.reject_total,
            "dropped_slow": self.dropped_slow,
            "uptime_s": round(now - self.boot_at, 1),
        }


def is_valid_subscribe(data) -> bool:
    """Whitelist: only subscribe:ledger is accepted."""
    if not isinstance(data, dict):
        return False
    if data.get("command") != "subscribe":
        return False
    streams = data.get("streams")
    if not isinstance(streams, list) or not streams:
        return False
    for s in streams:
        if s != "ledger":
            return False
    return True


async def upstream_loop(state: RelayState, url: str) -> None:
    """Maintain the single upstream subscription with backoff reconnect."""
    backoff = UPSTREAM_RECONNECT_BASE_S
    while True:
        try:
            log.info("upstream: connecting to %s", url)
            async with websockets.connect(url, ping_interval=UPSTREAM_PING_INTERVAL_S) as ws:
                state.upstream_connected = True
                backoff = UPSTREAM_RECONNECT_BASE_S
                await ws.send(json.dumps({
                    "id": "relay_subscribe",
                    "command": "subscribe",
                    "streams": ["ledger"],
                }))
                log.info("upstream: subscribed to ledger stream")
                async for raw in ws:
                    try:
                        data = json.loads(raw)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if data.get("type") == "ledgerClosed":
                        state.last_ledger = data
                        state.last_ledger_at = time.time()
                        await _broadcast(state, raw)
                    elif (data.get("type") == "response"
                          and isinstance(data.get("result"), dict)
                          and data["result"].get("ledger_index")):
                        # Subscribe response — cache as last_ledger so new
                        # clients get an immediate replay.
                        r = data["result"]
                        state.last_ledger = {
                            "type": "ledgerClosed",
                            "ledger_index": r["ledger_index"],
                            "ledger_hash": r.get("ledger_hash"),
                            "ledger_time": r.get("ledger_time"),
                            "txn_count": r.get("txn_count", 0),
                            "fee_base": r.get("fee_base"),
                            "reserve_base": r.get("reserve_base"),
                            "reserve_inc": r.get("reserve_inc"),
                            "validated_ledgers": r.get("validated_ledgers"),
                        }
                        state.last_ledger_at = time.time()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("upstream: connection error: %s", e)
        state.upstream_connected = False
        state.upstream_reconnects += 1
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, UPSTREAM_RECONNECT_MAX_S)


async def _broadcast(state: RelayState, raw: str) -> None:
    """Fan-out one message to all connected clients. Drop slow ones."""
    state.broadcast_total += 1
    dead = []
    for client in list(state.clients):
        try:
            await asyncio.wait_for(client.send(raw), timeout=SEND_TIMEOUT_S)
        except asyncio.TimeoutError:
            dead.append((client, "send_timeout"))
        except Exception:
            dead.append((client, "send_error"))
    for client, reason in dead:
        state.clients.discard(client)
        state.dropped_slow += 1
        try:
            await client.close(code=1011, reason=reason[:120])
        except Exception:
            pass


async def client_handler_impl(websocket, state: RelayState) -> None:
    """Per-client: enforce subscribe-only + rate limit; replay last ledger."""
    state.clients.add(websocket)
    rate_window: list[float] = []
    try:
        if state.last_ledger is not None:
            try:
                await websocket.send(json.dumps(state.last_ledger))
            except Exception:
                return
        async for msg in websocket:
            now = time.time()
            rate_window = [t for t in rate_window if now - t < 60.0]
            rate_window.append(now)
            if len(rate_window) > MAX_MSGS_PER_MIN:
                state.reject_total += 1
                await websocket.close(code=1008, reason="rate_limit")
                return
            if len(msg) > 2048:
                state.reject_total += 1
                await websocket.close(code=1008, reason="msg_too_large")
                return
            try:
                data = json.loads(msg)
            except (json.JSONDecodeError, TypeError):
                state.reject_total += 1
                await websocket.close(code=1008, reason="bad_json")
                return
            if not is_valid_subscribe(data):
                state.reject_total += 1
                await websocket.close(code=1008, reason="subscribe_only")
                return
            # Valid subscribe — reply with current ledger (idempotent).
            if state.last_ledger is not None:
                try:
                    await websocket.send(json.dumps({
                        "id": data.get("id", "subscribe"),
                        "type": "response",
                        "status": "success",
                        "result": {
                            "ledger_index": state.last_ledger.get("ledger_index"),
                            "ledger_hash": state.last_ledger.get("ledger_hash"),
                            "ledger_time": state.last_ledger.get("ledger_time"),
                            "fee_base": state.last_ledger.get("fee_base"),
                            "reserve_base": state.last_ledger.get("reserve_base"),
                            "reserve_inc": state.last_ledger.get("reserve_inc"),
                            "validated_ledgers": state.last_ledger.get("validated_ledgers"),
                        },
                    }))
                except Exception:
                    return
    except ConnectionClosed:
        pass
    finally:
        state.clients.discard(websocket)


def _make_ws_handler(state: RelayState):
    """Return a handler compatible with both 1-arg (ws) and 2-arg (ws, path) forms."""
    async def handler(*args):
        websocket = args[0]
        await client_handler_impl(websocket, state)
    return handler


async def healthz_server(state: RelayState, host: str, port: int) -> asyncio.AbstractServer:
    """Tiny asyncio HTTP server serving GET /healthz."""
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=2.0)
            if not request_line:
                writer.close()
                return
            # Drain headers (until blank line)
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=2.0)
                if line in (b"\r\n", b"\n", b""):
                    break
            parts = request_line.decode("latin-1", "replace").split()
            if len(parts) < 2 or parts[0] != "GET":
                _write_http(writer, 405, b"method not allowed")
                return
            path = parts[1]
            if path == "/healthz":
                body = json.dumps(state.snapshot()).encode()
                _write_http(writer, 200, body, "application/json")
            else:
                _write_http(writer, 404, b"not found")
        except Exception:
            try:
                _write_http(writer, 500, b"server error")
            except Exception:
                pass
        finally:
            try:
                await writer.drain()
            except Exception:
                pass
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    return await asyncio.start_server(handle, host, port)


def _write_http(writer: asyncio.StreamWriter, status: int, body: bytes,
                content_type: str = "text/plain") -> None:
    reasons = {200: "OK", 404: "Not Found", 405: "Method Not Allowed", 500: "Server Error"}
    reason = reasons.get(status, "OK")
    hdr = (
        f"HTTP/1.1 {status} {reason}\r\n"
        f"Content-Type: {content_type}\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode()
    writer.write(hdr + body)


async def run(host: str = LISTEN_HOST, port: int = LISTEN_PORT,
              healthz_port: int = HEALTHZ_PORT, upstream: str = UPSTREAM_URL) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    state = RelayState()
    upstream_task = asyncio.create_task(upstream_loop(state, upstream))
    ws_server = await ws_serve(
        _make_ws_handler(state),
        host,
        port,
        ping_interval=CLIENT_PING_INTERVAL_S,
        max_size=4096,
    )
    health_server = await healthz_server(state, host, healthz_port)
    log.info("relay: ws=%s:%d healthz=%s:%d upstream=%s",
             host, port, host, healthz_port, upstream)
    stop = asyncio.get_running_loop().create_future()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            asyncio.get_running_loop().add_signal_handler(sig, lambda: stop.set_result(None))
        except (NotImplementedError, RuntimeError):
            pass
    try:
        await stop
    finally:
        upstream_task.cancel()
        ws_server.close()
        health_server.close()
        try:
            await upstream_task
        except (asyncio.CancelledError, Exception):
            pass
        await ws_server.wait_closed()
        await health_server.wait_closed()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--host", default=LISTEN_HOST)
    p.add_argument("--port", type=int, default=LISTEN_PORT)
    p.add_argument("--healthz-port", type=int, default=HEALTHZ_PORT)
    p.add_argument("--upstream", default=UPSTREAM_URL)
    args = p.parse_args()
    try:
        asyncio.run(run(args.host, args.port, args.healthz_port, args.upstream))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
