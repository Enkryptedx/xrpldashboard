"""
Lenovo-side live-stream WSS relay for xrpldashboard.com browser panels.

One upstream connection to the local rippled (ws://127.0.0.1:6007, the
[port_ws_public] stanza with the raised send_queue_limit — never the admin
ws 6006, see UPSTREAM_URL), subscribed
to the `transactions` + `ledger` streams. Fan-out to N browser clients over
WebSockets as NAMED FEEDS, server-side filtered with the Milestone-1 filters in
relay_feed_filters.py (byte-for-byte ports of the browser JS). Subscribe-only
whitelist. Per-client bounded queue (drop-oldest). Separate HTTP healthz.

Charlie ruling 2026-09-22 Tue PM (LIVE_STREAM_WSS_PRIMARY finish, not revert):
browser live feed migrates from public XRPL cluster to our own-node relay.
Option B (docs/OPTION_B_RELAY_PROPOSAL.md, §11 answers 2026-09-25) turns the
subscribe-only-`ledger` relay into this named-feed relay. Milestone 2 diff:
docs/OPTION_B_MILESTONE2_RELAY_DIFF.md (Charlie GO 2026-09-25, log-only 72h).

Contract:
  - Clients connect, then send subscribe messages ONLY. Two accepted shapes:
      legacy  {"id":"subscribe","command":"subscribe","streams":["ledger"]}
      named   {"id":"x","command":"subscribe","stream":"<feed>"[,"accounts":[...]]}
    where <feed> is one of FEED_NAMES. `wallet_transactions` requires
    `accounts`: a list of well-formed classic addresses (validated BEFORE
    the sub is accepted; malformed -> close 1008 bad_address, always).
  - Anything else -> close 1008 policy_violation.
  - Server replays the last cached ledgerClosed to a new client on connect.
  - Every accepted subscribe gets a {"type":"response","status":"success"}
    reply carrying the current ledger fields + "stream".

Modes (LIVE_STREAM_RELAY_MODE):
  log_only (default, 72h after deploy): policy violations (address cap,
      aggregate address cap, address-change rate, send timeout, queue drop)
      are LOGGED as `SHADOW_TRIP <kind> ...` and otherwise allowed. Queue
      drops still drop-oldest (mechanical, not policy); the line records it.
  enforce: address-cap / aggregate-cap / addr-change-rate close 1008;
      two consecutive send timeouts close 1011. Queue drops stay drop-oldest.

Ports (defaults; the live unit overrides via env to 6011/6012):
  6007  WSS relay (browser-facing via cloudflared)
  6008  HTTP /healthz (localhost monitoring)

Env:
  LIVE_STREAM_RELAY_UPSTREAM               default ws://127.0.0.1:6007 (port_ws_public;
                                           6006 = admin ws with send_queue_limit 100
                                           → 1008 "client is too slow" on tx bursts)
  LIVE_STREAM_RELAY_HOST                   default 127.0.0.1
  LIVE_STREAM_RELAY_PORT                   default 6007
  LIVE_STREAM_RELAY_HEALTHZ_PORT           default 6008
  LIVE_STREAM_RELAY_MAX_MSGS_PER_MIN       default 30   (per-client incoming ceiling)
  LIVE_STREAM_RELAY_MODE                   default log_only   (log_only | enforce)
  LIVE_STREAM_RELAY_SLOW_CAP               default 256  (per-client queue depth)
  LIVE_STREAM_RELAY_PER_CLIENT_ADDR_CAP    default 1
  LIVE_STREAM_RELAY_AGG_ADDR_CAP           default 1000 (distinct watched addresses)
  LIVE_STREAM_RELAY_ADDR_CHANGE_PER_MIN    default 5
  LIVE_STREAM_RELAY_FEED_META_REFRESH_S    default 60
  LIVE_STREAM_RELAY_WHALE_TIER_DROPS       default 100000000000 (100K XRP, /whales default tier)
  DATABASE_URL                             optional; feed refs (pool accounts,
                                           top-100 tokens) are read from PG when
                                           set. Unset -> refs_source=none and the
                                           amm/token feeds stay silent (healthz
                                           shows it). total_coins never needs PG:
                                           it is read from the upstream node.
  LIVE_STREAM_RELAY_REFS_FILE              optional JSON fallback for refs
                                           ({"pool_accounts":[...],"top100":[[cur,iss],...]})

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

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import relay_feed_filters  # noqa: E402

# Upstream = rippled's [port_ws_public] 6007, NOT the admin ws 6006.
# INCIDENT 2026-09-26: 6006 keeps rippled's default send_queue_limit (100);
# a transactions+ledger subscriber gets closed with 1008 "client is too
# slow" on every ledger-close burst (531 closes 11:00Z–18:12Z; the client
# read-queue size makes no difference — rippled closed us 330 ms after
# subscribe). 6007 carries the raised send_queue_limit for exactly this
# subscriber class (2026-09-05 lesson, guarded by rippled_cfg_drift_guard)
# and is where xrpld-xrpl-stream and xrpld-roll-call already subscribe.
UPSTREAM_URL = os.environ.get("LIVE_STREAM_RELAY_UPSTREAM", "ws://127.0.0.1:6007")
LISTEN_HOST = os.environ.get("LIVE_STREAM_RELAY_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("LIVE_STREAM_RELAY_PORT", "6007"))
HEALTHZ_PORT = int(os.environ.get("LIVE_STREAM_RELAY_HEALTHZ_PORT", "6008"))
MAX_MSGS_PER_MIN = int(os.environ.get("LIVE_STREAM_RELAY_MAX_MSGS_PER_MIN", "30"))
RELAY_MODE = os.environ.get("LIVE_STREAM_RELAY_MODE", "log_only").strip().lower()
if RELAY_MODE not in ("log_only", "enforce"):
    RELAY_MODE = "log_only"
SLOW_CONSUMER_CAP = int(os.environ.get("LIVE_STREAM_RELAY_SLOW_CAP", "256"))
PER_CLIENT_ADDR_CAP = int(os.environ.get("LIVE_STREAM_RELAY_PER_CLIENT_ADDR_CAP", "1"))
AGG_WALLET_ADDR_CAP = int(os.environ.get("LIVE_STREAM_RELAY_AGG_ADDR_CAP", "1000"))
ADDR_CHANGE_PER_MIN = int(os.environ.get("LIVE_STREAM_RELAY_ADDR_CHANGE_PER_MIN", "5"))
FEED_META_REFRESH_S = int(os.environ.get("LIVE_STREAM_RELAY_FEED_META_REFRESH_S", "60"))
WHALE_TIER_DROPS = int(os.environ.get("LIVE_STREAM_RELAY_WHALE_TIER_DROPS", str(100_000 * 1_000_000)))
REFS_FILE = os.environ.get("LIVE_STREAM_RELAY_REFS_FILE") or None
TOP100_LIMIT = 100
UPSTREAM_RECONNECT_BASE_S = 2.0
UPSTREAM_RECONNECT_MAX_S = 60.0
UPSTREAM_PING_INTERVAL_S = 20.0
# Upstream read queue (frames buffered by the websockets client before it
# applies TCP back-pressure). INCIDENT 2026-09-26: Milestone 2 switched the
# upstream subscription from ledger-only to transactions+ledger; on loopback
# rippled delivers a whole ledger's transactions in one burst, websockets
# 17's default max_queue=16 pushed back after 16 frames, rippled's own send
# queue passed its limit (default 100) and it closed us with 1008 "client is
# too slow" — 531 times between 11:00Z and 18:12Z (24→119/h), each a ~2 s
# gap the browsers saw as a reconnect. 4096 frames × ~5 KB = ~20 MB worst
# case, absorbing any burst rippled can produce per ledger.
UPSTREAM_MAX_QUEUE = int(os.environ.get("LIVE_STREAM_RELAY_UPSTREAM_MAX_QUEUE", "4096"))
CLIENT_PING_INTERVAL_S = 25.0
SEND_TIMEOUT_S = 1.0
TOTAL_COINS_REQ_ID = "relay_total_coins"

FEED_NAMES = frozenset({
    "ledger",
    "amm_transactions",
    "token_top100_transactions",
    "whale_transactions",
    "wallet_transactions",
    "supply_updates",
})
# Feeds fed by classify() on each validated tx (not ledger/wallet/supply).
TX_FEEDS = ("amm_transactions", "token_top100_transactions", "whale_transactions")

log = logging.getLogger("live_stream_relay")


class Client:
    """One browser socket: bounded outbound queue + subscription state."""
    __slots__ = ("ws", "queue", "feeds", "addresses", "addr_changes",
                 "slow_drops", "send_timeouts", "id")
    _seq = 0

    def __init__(self, ws) -> None:
        Client._seq += 1
        self.id = Client._seq
        self.ws = ws
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=SLOW_CONSUMER_CAP)
        self.feeds: set[str] = set()
        self.addresses: set[str] = set()
        self.addr_changes: list[float] = []
        self.slow_drops = 0
        self.send_timeouts = 0


class RelayState:
    def __init__(self) -> None:
        self.clients: set[Client] = set()
        self.feeds: dict[str, set[Client]] = {name: set() for name in FEED_NAMES}
        self.wallet_watch: dict[str, set[Client]] = {}
        self.refs: dict = {"pool_accounts": set(), "top100": set(),
                           "whale_tier_drops": WHALE_TIER_DROPS, "total_coins": None}
        self.refs_at: float | None = None
        self.refs_source: str = "none"
        self.total_coins_at: float | None = None
        self.last_ledger: dict | None = None
        self.last_ledger_at: float | None = None
        self.upstream_connected: bool = False
        self.upstream_ws = None
        self.upstream_reconnects: int = 0
        self.broadcast_total: int = 0
        self.feed_broadcast: dict[str, int] = {name: 0 for name in FEED_NAMES}
        self.tx_seen: int = 0
        self.reject_total: int = 0
        self.dropped_slow: int = 0          # clients disconnected for slowness
        self.slow_consumer_drops: int = 0   # messages dropped (drop-oldest)
        self.shadow_trips: dict[str, int] = {}
        self.boot_at: float = time.time()

    # ── policy hook ──
    def trip(self, kind: str, client: Client | None, detail: str = "") -> bool:
        """Record a policy violation. Returns True when the caller must
        ENFORCE (close), False when log_only (allow)."""
        self.shadow_trips[kind] = self.shadow_trips.get(kind, 0) + 1
        cid = client.id if client is not None else "-"
        if RELAY_MODE == "enforce":
            log.warning("ENFORCE_TRIP %s client=%s %s", kind, cid, detail)
            return True
        log.warning("SHADOW_TRIP %s client=%s %s", kind, cid, detail)
        return False

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
            "tx_seen": self.tx_seen,
            "reject_total": self.reject_total,
            "dropped_slow": self.dropped_slow,
            "uptime_s": round(now - self.boot_at, 1),
            # Milestone 2 fields (§1i)
            "relay_mode": RELAY_MODE,
            "feeds": {name: len(subs) for name, subs in sorted(self.feeds.items())},
            "feed_broadcast": dict(sorted(self.feed_broadcast.items())),
            "slow_consumer_drops": self.slow_consumer_drops,
            "wallet_addr_count": len(self.wallet_watch),
            "total_coins": self.refs.get("total_coins"),
            "total_coins_age_s": round(now - self.total_coins_at, 1) if self.total_coins_at else None,
            "refs_source": self.refs_source,
            "refs_age_s": round(now - self.refs_at, 1) if self.refs_at else None,
            "refs_pool_accounts": len(self.refs.get("pool_accounts") or ()),
            "refs_top100": len(self.refs.get("top100") or ()),
            "whale_tier_drops": self.refs.get("whale_tier_drops"),
            "shadow_trips": dict(sorted(self.shadow_trips.items())),
        }


# ── subscribe validation ──

def parse_subscribe(data) -> tuple[str | None, str | None, list[str]]:
    """Classify a client message.

    Returns (feed, reject_reason, accounts):
      feed          feed name when the message is an acceptable subscribe
      reject_reason non-None when the message must be rejected (1008)
      accounts      address list for wallet_transactions (validated, deduped)

    Legacy streams:['ledger'] maps to feed 'ledger'. Address-COUNT caps are
    policy (mode-dependent) and are NOT checked here; malformed addresses are
    rejected here unconditionally (§11.1)."""
    if not isinstance(data, dict) or data.get("command") != "subscribe":
        return None, "subscribe_only", []
    streams = data.get("streams")
    if streams is not None:
        if not isinstance(streams, list) or not streams:
            return None, "subscribe_only", []
        if any(s != "ledger" for s in streams):
            return None, "subscribe_only", []
        if data.get("stream") is not None:
            return None, "subscribe_only", []
        return "ledger", None, []
    feed = data.get("stream")
    if not isinstance(feed, str) or feed not in FEED_NAMES:
        return None, "subscribe_only", []
    if feed != "wallet_transactions":
        if "accounts" in data:
            return None, "subscribe_only", []
        return feed, None, []
    accts = data.get("accounts")
    if not isinstance(accts, list) or not accts:
        return None, "bad_address", []
    seen: list[str] = []
    for a in accts:
        if not relay_feed_filters.is_valid_classic_address(a):
            return None, "bad_address", []
        if a not in seen:
            seen.append(a)
    return feed, None, seen


def is_valid_subscribe(data) -> bool:
    """Back-compat predicate (tests + canary): True when parse_subscribe accepts."""
    feed, reason, _ = parse_subscribe(data)
    return feed is not None and reason is None


# ── upstream ──

def upstream_connect_kwargs() -> dict:
    """websockets.connect kwargs for the rippled upstream. Kept as a function
    so the queue sizing is testable (see test_upstream_read_queue_absorbs_bursts)."""
    return {
        "ping_interval": UPSTREAM_PING_INTERVAL_S,
        "max_size": None,
        "max_queue": UPSTREAM_MAX_QUEUE,
    }


async def upstream_loop(state: RelayState, url: str) -> None:
    """Maintain the single upstream subscription with backoff reconnect."""
    backoff = UPSTREAM_RECONNECT_BASE_S
    while True:
        try:
            log.info("upstream: connecting to %s", url)
            async with websockets.connect(url, **upstream_connect_kwargs()) as ws:
                state.upstream_connected = True
                state.upstream_ws = ws
                backoff = UPSTREAM_RECONNECT_BASE_S
                await ws.send(json.dumps({
                    "id": "relay_subscribe",
                    "command": "subscribe",
                    "streams": ["transactions", "ledger"],
                }))
                log.info("upstream: subscribed to transactions+ledger streams")
                await _request_total_coins(state)
                async for raw in ws:
                    try:
                        data = json.loads(raw)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if not isinstance(data, dict):
                        continue
                    t = data.get("type")
                    if t == "ledgerClosed":
                        state.last_ledger = data
                        state.last_ledger_at = time.time()
                        _broadcast_feed(state, "ledger", raw)
                        _broadcast_supply_update(state, data)
                    elif t == "transaction":
                        state.tx_seen += 1
                        _route_transaction(state, data, raw)
                    elif t == "response" and data.get("id") == TOTAL_COINS_REQ_ID:
                        _absorb_total_coins(state, data)
                    elif (t == "response"
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
        state.upstream_ws = None
        state.upstream_reconnects += 1
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, UPSTREAM_RECONNECT_MAX_S)


async def _request_total_coins(state: RelayState) -> None:
    """Ask the upstream node for the validated ledger header (total_coins).
    Sovereign + loopback-only: no PG, no HTTP hop. Answer lands in the read
    loop via TOTAL_COINS_REQ_ID."""
    ws = state.upstream_ws
    if ws is None:
        return
    try:
        await ws.send(json.dumps({
            "id": TOTAL_COINS_REQ_ID,
            "command": "ledger",
            "ledger_index": "validated",
            "transactions": False,
            "expand": False,
        }))
    except Exception as e:
        log.warning("upstream: total_coins request failed: %s", e)


def _absorb_total_coins(state: RelayState, data: dict) -> None:
    result = data.get("result")
    if not isinstance(result, dict):
        return
    ledger = result.get("ledger")
    if not isinstance(ledger, dict):
        ledger = result
    tc = ledger.get("total_coins")
    try:
        tc_int = int(tc)
    except (TypeError, ValueError):
        log.warning("upstream: total_coins not integer-parseable: %r", tc)
        return
    state.refs["total_coins"] = tc_int
    state.total_coins_at = time.time()


# ── routing / fan-out ──

def _route_transaction(state: RelayState, data: dict, raw: str) -> None:
    if any(state.feeds[f] for f in TX_FEEDS):
        for feed in relay_feed_filters.classify(data, state.refs):
            _broadcast_feed(state, feed, raw)
    if state.wallet_watch and relay_feed_filters.is_validated_success(data):
        _broadcast_wallet(state, data, raw)


def _enqueue(state: RelayState, client: Client, raw: str) -> None:
    """Drop-oldest bounded enqueue (§11.2). Mechanical in both modes; the
    SHADOW_TRIP line records each client's first drop and every 100th."""
    q = client.queue
    if q.full():
        try:
            q.get_nowait()
        except asyncio.QueueEmpty:
            pass
        client.slow_drops += 1
        state.slow_consumer_drops += 1
        if client.slow_drops == 1 or client.slow_drops % 100 == 0:
            state.trip("queue_drop", client,
                       f"drops={client.slow_drops} feeds={sorted(client.feeds)}")
    try:
        q.put_nowait(raw)
    except asyncio.QueueFull:
        client.slow_drops += 1
        state.slow_consumer_drops += 1


def _broadcast_feed(state: RelayState, feed: str, raw: str) -> None:
    subs = state.feeds.get(feed)
    if not subs:
        return
    state.broadcast_total += 1
    state.feed_broadcast[feed] = state.feed_broadcast.get(feed, 0) + 1
    for client in list(subs):
        _enqueue(state, client, raw)


def _broadcast_wallet(state: RelayState, data: dict, raw: str) -> None:
    hit: set[Client] = set()
    for addr, clients in state.wallet_watch.items():
        if relay_feed_filters.matches_wallet(data, {addr}):
            hit.update(clients)
    if not hit:
        return
    state.broadcast_total += 1
    state.feed_broadcast["wallet_transactions"] += 1
    for client in hit:
        _enqueue(state, client, raw)


def _broadcast_supply_update(state: RelayState, ledger_data: dict) -> None:
    """§11.4: every ledger close, decorated with last-known total_coins."""
    if not state.feeds.get("supply_updates"):
        return
    msg = dict(ledger_data)
    msg["type"] = "supply_update"
    msg["total_coins"] = state.refs.get("total_coins")
    msg["total_coins_age_s"] = (round(time.time() - state.total_coins_at, 1)
                                if state.total_coins_at else None)
    _broadcast_feed(state, "supply_updates", json.dumps(msg))


# ── per-client send loop ──

async def _sender(state: RelayState, client: Client) -> None:
    """Drain the client's queue. Two consecutive send timeouts: enforce ->
    close 1011; log_only -> SHADOW_TRIP and keep going."""
    while True:
        raw = await client.queue.get()
        try:
            await asyncio.wait_for(client.ws.send(raw), timeout=SEND_TIMEOUT_S)
            client.send_timeouts = 0
        except asyncio.TimeoutError:
            client.send_timeouts += 1
            if client.send_timeouts >= 2:
                if state.trip("send_timeout", client, f"consecutive={client.send_timeouts}"):
                    state.dropped_slow += 1
                    await _close(client.ws, 1011, "send_timeout")
                    return
                client.send_timeouts = 0
        except Exception:
            return


async def _close(ws, code: int, reason: str) -> None:
    try:
        await ws.close(code=code, reason=reason[:120])
    except Exception:
        pass


# ── subscription bookkeeping ──

def _subscribe(state: RelayState, client: Client, feed: str) -> None:
    client.feeds.add(feed)
    state.feeds[feed].add(client)


def _set_addresses(state: RelayState, client: Client, addrs: list[str]) -> None:
    new = set(addrs)
    for a in client.addresses - new:
        s = state.wallet_watch.get(a)
        if s:
            s.discard(client)
            if not s:
                state.wallet_watch.pop(a, None)
    for a in new:
        state.wallet_watch.setdefault(a, set()).add(client)
    client.addresses = new


def _unregister(state: RelayState, client: Client) -> None:
    state.clients.discard(client)
    for feed in client.feeds:
        state.feeds[feed].discard(client)
    _set_addresses(state, client, [])


def _subscribe_response(state: RelayState, data: dict, feed: str) -> dict:
    ll = state.last_ledger or {}
    return {
        "id": data.get("id", "subscribe"),
        "type": "response",
        "status": "success",
        "result": {
            "stream": feed,
            "relay_mode": RELAY_MODE,
            "ledger_index": ll.get("ledger_index"),
            "ledger_hash": ll.get("ledger_hash"),
            "ledger_time": ll.get("ledger_time"),
            "fee_base": ll.get("fee_base"),
            "reserve_base": ll.get("reserve_base"),
            "reserve_inc": ll.get("reserve_inc"),
            "validated_ledgers": ll.get("validated_ledgers"),
        },
    }


async def client_handler_impl(websocket, state: RelayState) -> None:
    """Per-client: subscribe-only + rate limit; named feeds; replay last ledger."""
    client = Client(websocket)
    state.clients.add(client)
    sender = asyncio.create_task(_sender(state, client))
    rate_window: list[float] = []
    try:
        if state.last_ledger is not None:
            _enqueue(state, client, json.dumps(state.last_ledger))
        async for msg in websocket:
            now = time.time()
            rate_window = [t for t in rate_window if now - t < 60.0]
            rate_window.append(now)
            if len(rate_window) > MAX_MSGS_PER_MIN:
                state.reject_total += 1
                await _close(websocket, 1008, "rate_limit")
                return
            if len(msg) > 2048:
                state.reject_total += 1
                await _close(websocket, 1008, "msg_too_large")
                return
            try:
                data = json.loads(msg)
            except (json.JSONDecodeError, TypeError):
                state.reject_total += 1
                await _close(websocket, 1008, "bad_json")
                return
            feed, reason, accounts = parse_subscribe(data)
            if reason is not None:
                state.reject_total += 1
                await _close(websocket, 1008, reason)
                return
            if feed == "wallet_transactions":
                # §11.1 per-client cap (policy, mode-dependent)
                if len(accounts) > PER_CLIENT_ADDR_CAP:
                    if state.trip("addr_cap", client,
                                  f"requested={len(accounts)} cap={PER_CLIENT_ADDR_CAP}"):
                        state.reject_total += 1
                        await _close(websocket, 1008, "addr_cap")
                        return
                # §11.1 address-change rate (~5/min per socket)
                if set(accounts) != client.addresses:
                    client.addr_changes = [t for t in client.addr_changes if now - t < 60.0]
                    client.addr_changes.append(now)
                    if len(client.addr_changes) > ADDR_CHANGE_PER_MIN:
                        if state.trip("addr_change_rate", client,
                                      f"changes_60s={len(client.addr_changes)} cap={ADDR_CHANGE_PER_MIN}"):
                            state.reject_total += 1
                            await _close(websocket, 1008, "addr_change_rate")
                            return
                # aggregate watched-address cap
                new_addrs = [a for a in accounts if a not in state.wallet_watch]
                if new_addrs and len(state.wallet_watch) + len(new_addrs) > AGG_WALLET_ADDR_CAP:
                    if state.trip("agg_addr_cap", client,
                                  f"watched={len(state.wallet_watch)} adding={len(new_addrs)} cap={AGG_WALLET_ADDR_CAP}"):
                        state.reject_total += 1
                        await _close(websocket, 1008, "agg_addr_cap")
                        return
                _set_addresses(state, client, accounts)
            _subscribe(state, client, feed)
            _enqueue(state, client, json.dumps(_subscribe_response(state, data, feed)))
    except ConnectionClosed:
        pass
    finally:
        _unregister(state, client)
        sender.cancel()
        try:
            await sender
        except (asyncio.CancelledError, Exception):
            pass


def _make_ws_handler(state: RelayState):
    """Return a handler compatible with both 1-arg (ws) and 2-arg (ws, path) forms."""
    async def handler(*args):
        websocket = args[0]
        await client_handler_impl(websocket, state)
    return handler


# ── feed reference data (§1g) ──

def load_refs_from_pg(database_url: str) -> dict:
    """Blocking. Same rows the pages render from:
      pool_accounts  SELECT amm_account FROM amm_ranked_pools   (/pools)
      top100         token_volume, last 24h, top 100 by trade_count (/tokens)"""
    import psycopg
    with psycopg.connect(database_url, connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT amm_account FROM amm_ranked_pools WHERE amm_account IS NOT NULL")
            pools = {r[0] for r in cur.fetchall()}
            cutoff = int(time.time() // 3600) - 24
            cur.execute(
                "SELECT currency, issuer FROM token_volume WHERE hour_bucket >= %s "
                "GROUP BY currency, issuer ORDER BY SUM(trade_count) DESC LIMIT %s",
                (cutoff, TOP100_LIMIT),
            )
            top100 = {(r[0], r[1]) for r in cur.fetchall()}
    return {"pool_accounts": pools, "top100": top100}


def load_refs_from_file(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        d = json.load(f)
    pools = set(d.get("pool_accounts") or [])
    top100 = {tuple(x) for x in (d.get("top100") or []) if isinstance(x, (list, tuple)) and len(x) == 2}
    return {"pool_accounts": pools, "top100": top100}


def _apply_refs(state: RelayState, refs: dict, source: str) -> None:
    state.refs["pool_accounts"] = refs.get("pool_accounts", set())
    state.refs["top100"] = refs.get("top100", set())
    state.refs_at = time.time()
    state.refs_source = source


async def refs_refresh_loop(state: RelayState, interval_s: float,
                            database_url: str | None, refs_file: str | None) -> None:
    """Every interval: refresh pool/top100 refs (PG or file) + ask upstream
    for total_coins. Failures are logged and the previous refs are kept."""
    loop = asyncio.get_running_loop()
    while True:
        try:
            if database_url:
                refs = await loop.run_in_executor(None, load_refs_from_pg, database_url)
                _apply_refs(state, refs, "pg")
            elif refs_file:
                refs = await loop.run_in_executor(None, load_refs_from_file, refs_file)
                _apply_refs(state, refs, "file")
            else:
                state.refs_source = "none"
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("refs: refresh failed (%s); keeping previous set", e)
        await _request_total_coins(state)
        await asyncio.sleep(interval_s)


# ── healthz ──

async def healthz_server(state: RelayState, host: str, port: int) -> asyncio.AbstractServer:
    """Tiny asyncio HTTP server serving GET /healthz."""
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=2.0)
            if not request_line:
                writer.close()
                return
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


# ── main ──

async def run(host: str = LISTEN_HOST, port: int = LISTEN_PORT,
              healthz_port: int = HEALTHZ_PORT, upstream: str = UPSTREAM_URL) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    state = RelayState()
    database_url = os.environ.get("DATABASE_URL") or None
    upstream_task = asyncio.create_task(upstream_loop(state, upstream))
    refs_task = asyncio.create_task(
        refs_refresh_loop(state, FEED_META_REFRESH_S, database_url, REFS_FILE))
    ws_server = await ws_serve(
        _make_ws_handler(state),
        host,
        port,
        ping_interval=CLIENT_PING_INTERVAL_S,
        max_size=4096,
    )
    health_server = await healthz_server(state, host, healthz_port)
    log.info("relay: ws=%s:%d healthz=%s:%d upstream=%s mode=%s refs=%s",
             host, port, host, healthz_port, upstream, RELAY_MODE,
             "pg" if database_url else ("file" if REFS_FILE else "none"))
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
        refs_task.cancel()
        ws_server.close()
        health_server.close()
        for t in (upstream_task, refs_task):
            try:
                await t
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
