"""
Long-lived XRPL WebSocket subscriber. One connection, multiple handlers.

Active handlers:
    amm_create_handler   — AMMCreate txns → amm_index_incremental.json
                           (separate from bootstrap scan output to avoid
                           write races with scan_all_amms.py)
    whale_handler        — large XRP transfers + watchlisted accounts →
                           events.db (see WHALE_WATCH.md)
    token_event_handler  — token Payments + AMM deposit/withdraw →
                           volumes.db hourly buckets (see TOKEN_ECONOMY.md
                           Phase 0; volume_xrp is a 0.0 placeholder until
                           token_prices.py exists)
    new_token_handler    — TrustSet to an unseen (currency, issuer) →
                           log + state["seen_tokens"]

Run as a long-running background process. Reconnects with exponential
backoff on disconnect.

Usage:
    SSL_CERT_FILE=$(python -m certifi) python xrpl_stream.py

Outputs (next to this script):
    amm_index_incremental.json   — AMMs created since stream start
    events.db / volumes.db       — sqlite stores (init via init_dbs.py)
    xrpl_stream.log              — progress log + reconnect events
    xrpl_stream_state.json       — last seen ledger_index, counters,
                                   seen-tokens index
"""

from xrpl.clients import WebsocketClient
from xrpl.models.requests import Subscribe, StreamParameter
from datetime import datetime, timezone
import certifi
import json
import os
import sqlite3
import sys
import threading
import time

import db as pgbridge

# Default to certifi's CA bundle if the env doesn't already point somewhere.
# Without this, wss:// connections fail on stock macOS Python with a cert
# verification error.
os.environ.setdefault("SSL_CERT_FILE", certifi.where())

XRPL_WS_NODE = "wss://s2.ripple.com"
RECONNECT_BACKOFF_BASE = 2.0
RECONNECT_BACKOFF_MAX = 60.0
HEARTBEAT_EVERY_SECONDS = 300   # write a heartbeat log line every 5 min
STATE_SAVE_EVERY_SECONDS = 60   # persist counters to disk every minute
IDLE_KILL_SECONDS = 90          # force-close session if no msg in this window
WATCHDOG_TICK_SECONDS = 10      # how often the watchdog checks for idle

HERE = os.path.dirname(os.path.abspath(__file__))
INCREMENTAL_AMM_PATH = os.path.join(HERE, "amm_index_incremental.json")
STATE_PATH = os.path.join(HERE, "xrpl_stream_state.json")
LOG_PATH = os.path.join(HERE, "xrpl_stream.log")
EVENTS_DB_PATH = os.path.join(HERE, "events.db")
VOLUMES_DB_PATH = os.path.join(HERE, "volumes.db")
NAMED_ACCOUNTS_PATH = os.path.join(HERE, "named_accounts.json")
AMM_RANKED_PATH = os.path.join(HERE, "amm_ranked.json")

# Conservative threshold locked in WHALE_WATCH.md. Override via env if the
# feed is too sparse — never tighten without a public note.
WHALE_XRP_THRESHOLD_DROPS = int(
    float(os.environ.get("WHALE_XRP_THRESHOLD_XRP", "50000")) * 1_000_000
)

# Long-lived sqlite connections. WAL mode lets the dashboard read these
# files while the stream is writing. autocommit (isolation_level=None) keeps
# writes flushing immediately so a crash loses at most the in-flight tx.
def _open_db(path):
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


_events_conn = _open_db(EVENTS_DB_PATH)
_volumes_conn = _open_db(VOLUMES_DB_PATH)


def _load_named_accounts():
    if not os.path.exists(NAMED_ACCOUNTS_PATH):
        return {}
    try:
        with open(NAMED_ACCOUNTS_PATH) as f:
            return json.load(f) or {}
    except Exception as e:
        print(f"WARN named_accounts load failed: {e}", file=sys.stderr)
        return {}


NAMED_ACCOUNTS = _load_named_accounts()
_SEEN_TOKEN_SET = None  # lazily populated from state["seen_tokens"]


def log(msg):
    line = f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as e:
        log(f"WARN failed to load {path}: {e}")
        return default


def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, path)


def extract_tx(msg):
    """
    Stream messages have shifted shape across xrpl-py / rippled api versions.
    The tx body might live under 'transaction' or 'tx_json'. Normalise.
    """
    return msg.get("transaction") or msg.get("tx_json") or {}


# ---------------------------------------------------------------------------
# Handlers — pure functions of (message, state). Each returns None.
# Add new ones to HANDLERS at the bottom.
# ---------------------------------------------------------------------------

def amm_create_handler(msg, state):
    tx = extract_tx(msg)
    if tx.get("TransactionType") != "AMMCreate":
        return
    if msg.get("validated") is False:
        # Only count validated txns. Belt-and-suspenders — the 'transactions'
        # stream is supposed to be validated-only, but be explicit.
        return

    asset = tx.get("Amount") or tx.get("Asset")
    asset2 = tx.get("Amount2") or tx.get("Asset2")
    fee = tx.get("TradingFee")
    account = tx.get("Account")
    tx_hash = msg.get("hash") or tx.get("hash")
    ledger = msg.get("ledger_index") or tx.get("ledger_index")

    # Resolve the new AMM's account from transaction metadata where available.
    # (The AMM gets its own account; the tx 'Account' is the creator, not the
    # AMM. We may not have the AMM account directly in the stream message —
    # it ends up in metadata.AffectedNodes. Capture what we can and mark
    # for later resolution.)
    new_amm = {
        "tx_hash": tx_hash,
        "ledger_index": ledger,
        "creator_account": account,
        "asset": asset,
        "asset2": asset2,
        "trading_fee": fee,
        "detected_at": datetime.now(timezone.utc).isoformat(),
    }

    incremental = load_json(INCREMENTAL_AMM_PATH, [])
    # Dedup by tx_hash (in case the stream re-delivers).
    if any(a.get("tx_hash") == tx_hash for a in incremental):
        return
    incremental.append(new_amm)
    save_json(INCREMENTAL_AMM_PATH, incremental)

    state["amm_creates_seen"] = state.get("amm_creates_seen", 0) + 1
    log(f"new AMM detected: tx={tx_hash[:12]}... ledger={ledger} "
        f"by {account} (total since start: {state['amm_creates_seen']})")


def whale_handler(msg, state):
    """Large XRP transfers + any tx involving a watchlisted account → events.db.

    Three event categories per WHALE_WATCH.md:
      'large_xfer' — XRP Payment ≥ WHALE_XRP_THRESHOLD_DROPS
      'tagged'    — any tx whose Account or Destination is in NAMED_ACCOUNTS
      'trustset'  — TrustSet from a watchlisted account (large-balance filter
                    deferred — needs an extra balance lookup per source)
    """
    tx = extract_tx(msg)
    ttype = tx.get("TransactionType")
    if not ttype:
        return
    tx_hash = msg.get("hash") or tx.get("hash")
    ledger = msg.get("ledger_index") or tx.get("ledger_index")
    if not tx_hash or not ledger:
        return

    from_addr = tx.get("Account")
    to_addr = tx.get("Destination")
    event_type = None
    amount_drops = None
    currency = None
    issuer = None

    # Resolve "what actually moved" for a Payment.
    #   - delivered_amount (in meta) is the post-tx truth when present:
    #       * string  → XRP delivered, in drops
    #       * dict    → token delivered (currency/issuer/value)
    #   - When delivered_amount is absent, that means either:
    #       * pure same-currency XRP payment → Amount/DeliverMax IS the truth
    #       * non-Payment tx                  → no amount applies
    #   - Critically: if SendMax is present, this is a cross-currency payment
    #     and Amount/DeliverMax is only the upper bound — never use it as the
    #     amount. delivered_amount is the only valid source in that case.
    def _resolved_amount():
        delivered_in_meta = (msg.get("meta") or {}).get("delivered_amount")
        if delivered_in_meta is not None:
            return delivered_in_meta
        if "SendMax" in tx:
            return None
        return tx.get("Amount") or tx.get("DeliverMax")

    payment_amount = _resolved_amount() if ttype == "Payment" else None
    tx_succeeded = msg.get("engine_result") == "tesSUCCESS"

    if ttype == "Payment" and tx_succeeded and isinstance(payment_amount, str):
        try:
            drops = int(payment_amount)
        except ValueError:
            drops = 0
        if drops >= WHALE_XRP_THRESHOLD_DROPS:
            event_type = "large_xfer"
            amount_drops = drops
            currency = "XRP"

    if event_type is None and (
        from_addr in NAMED_ACCOUNTS or to_addr in NAMED_ACCOUNTS
    ):
        event_type = "tagged"
        if isinstance(payment_amount, str):
            try:
                amount_drops = int(payment_amount)
                currency = "XRP"
            except ValueError:
                pass
        elif isinstance(payment_amount, dict):
            currency = payment_amount.get("currency")
            issuer = payment_amount.get("issuer")
            # Token amount is a decimal string; not convertible to drops.
            # Leave amount_drops NULL and let the renderer pull `value` from
            # raw_json. We still record currency + issuer for indexing.

    if event_type is None and ttype == "TrustSet" and from_addr in NAMED_ACCOUNTS:
        limit = tx.get("LimitAmount") or {}
        if isinstance(limit, dict):
            currency = limit.get("currency")
            issuer = limit.get("issuer")
        event_type = "trustset"

    if event_type is None:
        return

    now_ts = int(time.time())
    raw_json = json.dumps(msg, default=str)
    try:
        _events_conn.execute(
            "INSERT OR IGNORE INTO events "
            "(tx_hash, ledger_index, ts, type, from_addr, to_addr, "
            " amount_drops, currency, issuer, raw_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                tx_hash, ledger, now_ts, event_type,
                from_addr, to_addr, amount_drops, currency, issuer,
                raw_json,
            ),
        )
    except Exception as e:
        log(f"whale_handler db error: {e}")
        return

    pgbridge.write_event(
        tx_hash, ledger, now_ts, event_type,
        from_addr, to_addr, amount_drops, currency, issuer, raw_json,
    )

    state["whale_events_seen"] = state.get("whale_events_seen", 0) + 1


def token_event_handler(msg, state):
    """Token Payments + AMM deposit/withdraw → volumes.db hourly buckets.

    Phase 0 of TOKEN_ECONOMY.md: count trades per (currency, issuer, hour).
    volume_xrp is recorded as 0.0 placeholder until token_prices.py can
    derive XRP-equivalents from the AMM index. trade_count alone is
    already useful for the new-token activity surface.
    """
    tx = extract_tx(msg)
    ttype = tx.get("TransactionType")

    pairs = []
    if ttype == "Payment":
        amount = tx.get("Amount") or tx.get("DeliverMax")
        if isinstance(amount, dict):
            cur = amount.get("currency")
            iss = amount.get("issuer")
            if cur and iss:
                pairs.append((cur, iss))
    elif ttype in ("AMMDeposit", "AMMWithdraw"):
        for key in ("Asset", "Asset2"):
            asset = tx.get(key) or {}
            if isinstance(asset, dict):
                cur = asset.get("currency")
                iss = asset.get("issuer")
                if cur and cur != "XRP" and iss:
                    pairs.append((cur, iss))

    if not pairs:
        return

    hour_bucket = int(time.time() // 3600)
    try:
        for cur, iss in pairs:
            _volumes_conn.execute(
                "INSERT INTO token_volume "
                "(currency, issuer, hour_bucket, volume_xrp, trade_count) "
                "VALUES (?, ?, ?, 0.0, 1) "
                "ON CONFLICT(currency, issuer, hour_bucket) DO UPDATE SET "
                "trade_count = trade_count + 1",
                (cur, iss, hour_bucket),
            )
    except Exception as e:
        log(f"token_event_handler db error: {e}")
        return

    for cur, iss in pairs:
        pgbridge.upsert_token_volume(cur, iss, hour_bucket, trade_delta=1)

    state["token_events_seen"] = state.get("token_events_seen", 0) + len(pairs)


def new_token_handler(msg, state):
    """First-seen detection for (currency, issuer) pairs via TrustSet.

    Maintains an in-memory set across the run, persisted as a list in
    state["seen_tokens"] so restarts don't re-fire on every known token.
    Persistence to a real token_index.json is deferred to token_catalog.py.
    """
    global _SEEN_TOKEN_SET
    tx = extract_tx(msg)
    if tx.get("TransactionType") != "TrustSet":
        return
    limit = tx.get("LimitAmount") or {}
    if not isinstance(limit, dict):
        return
    cur = limit.get("currency")
    iss = limit.get("issuer")
    if not cur or not iss:
        return
    key = f"{cur}:{iss}"

    if _SEEN_TOKEN_SET is None:
        _SEEN_TOKEN_SET = set(state.get("seen_tokens", []))

    if key in _SEEN_TOKEN_SET:
        return
    _SEEN_TOKEN_SET.add(key)
    state.setdefault("seen_tokens", []).append(key)
    log(f"new token detected: {key}")
    state["new_tokens_seen"] = state.get("new_tokens_seen", 0) + 1


_PULSE_INSERTS_SINCE_PRUNE = 0
PULSE_CAP_ROWS = 1500
PULSE_PRUNE_EVERY = 200


def pulse_handler(msg, _state):
    """Every successful Payment → tx_pulse ring buffer in events.db.

    Powers the homepage globe. Captures any size, any currency — the globe
    represents the whole ledger, not just whale activity. Periodically
    prunes the table to PULSE_CAP_ROWS so growth stays bounded.
    """
    global _PULSE_INSERTS_SINCE_PRUNE

    tx = extract_tx(msg)
    if tx.get("TransactionType") != "Payment":
        return
    if msg.get("engine_result") != "tesSUCCESS":
        return

    delivered = (msg.get("meta") or {}).get("delivered_amount")
    if delivered is None and "SendMax" not in tx:
        delivered = tx.get("Amount") or tx.get("DeliverMax")

    amount_drops = None
    currency = None
    if isinstance(delivered, str):
        try:
            amount_drops = int(delivered)
            currency = "XRP"
        except ValueError:
            return
    elif isinstance(delivered, dict):
        currency = delivered.get("currency")
    else:
        return

    try:
        _events_conn.execute(
            "INSERT INTO tx_pulse (ts, amount_drops, currency) VALUES (?, ?, ?)",
            (int(time.time()), amount_drops, currency),
        )
    except Exception as e:
        log(f"pulse_handler db error: {e}")
        return

    _PULSE_INSERTS_SINCE_PRUNE += 1
    if _PULSE_INSERTS_SINCE_PRUNE >= PULSE_PRUNE_EVERY:
        _PULSE_INSERTS_SINCE_PRUNE = 0
        try:
            _events_conn.execute(
                "DELETE FROM tx_pulse WHERE id <= "
                "(SELECT MAX(id) - ? FROM tx_pulse)",
                (PULSE_CAP_ROWS,),
            )
        except Exception as e:
            log(f"pulse_handler prune error: {e}")


# ---------------------------------------------------------------------------
# AMM-pool activity → events.db / amm_pool_events
# Powers the live constellation comets on /pools. We watch any tx whose
# metadata touches a known AMM account (loaded from amm_ranked.json) and
# label it deposit / withdraw / swap. Caps at AMM_POOL_CAP_ROWS like tx_pulse.
# ---------------------------------------------------------------------------

_AMM_ACCOUNT_SET = None
_AMM_POOL_INSERTS_SINCE_PRUNE = 0
AMM_POOL_CAP_ROWS = 5000
AMM_POOL_PRUNE_EVERY = 250


def _load_amm_account_set():
    global _AMM_ACCOUNT_SET
    pools = load_json(AMM_RANKED_PATH, [])
    _AMM_ACCOUNT_SET = {
        p.get("amm_account") for p in pools if p.get("amm_account")
    }
    log(f"amm_pool_event_handler: tracking {len(_AMM_ACCOUNT_SET)} AMM accounts")


def amm_pool_event_handler(msg, state):
    """Per-pool AMM activity → amm_pool_events ring buffer in events.db.

    Watches AMMDeposit, AMMWithdraw, Payment (swap-through-AMM), and
    OfferCreate (offer-filled-by-AMM). Matches by walking AffectedNodes
    for an AccountRoot whose Account is in our known AMM set. Any tx that
    doesn't touch a tracked AMM is ignored.
    """
    global _AMM_ACCOUNT_SET, _AMM_POOL_INSERTS_SINCE_PRUNE

    if _AMM_ACCOUNT_SET is None:
        _load_amm_account_set()
    if not _AMM_ACCOUNT_SET:
        return
    if msg.get("engine_result") != "tesSUCCESS":
        return

    tx = extract_tx(msg)
    ttype = tx.get("TransactionType")
    if ttype not in ("AMMDeposit", "AMMWithdraw", "Payment", "OfferCreate"):
        return

    affected = (msg.get("meta") or {}).get("AffectedNodes") or []
    matched_account = None
    for node in affected:
        wrapper = (node.get("ModifiedNode")
                   or node.get("CreatedNode")
                   or node.get("DeletedNode"))
        if not wrapper:
            continue
        if wrapper.get("LedgerEntryType") != "AccountRoot":
            continue
        fields = (wrapper.get("FinalFields")
                  or wrapper.get("NewFields")
                  or {})
        acct = fields.get("Account")
        if acct and acct in _AMM_ACCOUNT_SET:
            matched_account = acct
            break

    if not matched_account:
        return

    if ttype == "AMMDeposit":
        event_type = "deposit"
    elif ttype == "AMMWithdraw":
        event_type = "withdraw"
    else:
        event_type = "swap"

    now_ts = int(time.time())
    try:
        _events_conn.execute(
            "INSERT INTO amm_pool_events (ts, amm_account, event_type) "
            "VALUES (?, ?, ?)",
            (now_ts, matched_account, event_type),
        )
    except Exception as e:
        log(f"amm_pool_event_handler db error: {e}")
        return

    pgbridge.write_amm_pool_event(now_ts, matched_account, event_type)

    state["amm_pool_events_seen"] = state.get("amm_pool_events_seen", 0) + 1
    _AMM_POOL_INSERTS_SINCE_PRUNE += 1
    if _AMM_POOL_INSERTS_SINCE_PRUNE >= AMM_POOL_PRUNE_EVERY:
        _AMM_POOL_INSERTS_SINCE_PRUNE = 0
        try:
            _events_conn.execute(
                "DELETE FROM amm_pool_events WHERE id <= "
                "(SELECT MAX(id) - ? FROM amm_pool_events)",
                (AMM_POOL_CAP_ROWS,),
            )
        except Exception as e:
            log(f"amm_pool_event_handler prune error: {e}")
        pgbridge.prune_amm_pool_events(AMM_POOL_CAP_ROWS)


HANDLERS = [
    amm_create_handler,
    whale_handler,
    token_event_handler,
    new_token_handler,
    pulse_handler,
    amm_pool_event_handler,
]


def run_session(state):
    """
    One websocket session. Returns when the connection drops; caller
    reconnects.

    A watchdog thread monitors `last_msg_at` and force-closes the client
    if no message arrives for IDLE_KILL_SECONDS. Without this, a silently
    dead socket can stall the iterator indefinitely (no exception raised),
    bypassing the reconnect loop entirely.
    """
    log(f"connecting to {XRPL_WS_NODE}")
    last_heartbeat = time.time()
    last_state_save = time.time()
    txns_at_last_heartbeat = state.get("txns_seen", 0)

    with WebsocketClient(XRPL_WS_NODE) as client:
        client.send(Subscribe(streams=[StreamParameter.TRANSACTIONS]))
        log("subscribed to transactions stream")

        # Stamp an immediate heartbeat so prod /health doesn't wait the full
        # HEARTBEAT_EVERY_SECONDS window to learn the worker is back up.
        pgbridge.write_heartbeat(
            "xrpl_stream",
            txns_seen=state.get("txns_seen"),
            last_ledger=state.get("last_ledger_index"),
            extra={"event": "session_start"},
        )

        last_msg_at = [time.time()]
        watchdog_stop = threading.Event()

        def watchdog():
            while not watchdog_stop.wait(WATCHDOG_TICK_SECONDS):
                idle = time.time() - last_msg_at[0]
                if idle > IDLE_KILL_SECONDS:
                    log(f"watchdog: no msg in {idle:.0f}s — closing socket and exiting for launchd restart")
                    try:
                        client.close()
                    except Exception as e:
                        log(f"watchdog close error: {e}")
                    # client.close() alone doesn't unblock the iterator on a
                    # silently dead socket; hard-exit so launchd KeepAlive restarts.
                    os._exit(1)

        wd = threading.Thread(target=watchdog, daemon=True, name="ws-watchdog")
        wd.start()

        try:
            for msg in client:
                last_msg_at[0] = time.time()
                # `for msg in client` yields every incoming message. The first
                # one is usually the subscribe ack ('response' type) — skip.
                if msg.get("type") == "response":
                    continue

                state["txns_seen"] = state.get("txns_seen", 0) + 1

                tx = extract_tx(msg)
                ledger = msg.get("ledger_index") or tx.get("ledger_index")
                if ledger:
                    state["last_ledger_index"] = ledger

                for handler in HANDLERS:
                    try:
                        handler(msg, state)
                    except Exception as e:
                        log(f"handler {handler.__name__} error: {e}")

                now = time.time()
                if now - last_state_save >= STATE_SAVE_EVERY_SECONDS:
                    save_json(STATE_PATH, state)
                    last_state_save = now

                if now - last_heartbeat >= HEARTBEAT_EVERY_SECONDS:
                    window_txns = state["txns_seen"] - txns_at_last_heartbeat
                    rate = window_txns / (now - last_heartbeat)
                    log(f"heartbeat: txns_seen={state['txns_seen']:,} "
                        f"last_ledger={state.get('last_ledger_index')} "
                        f"amm_creates={state.get('amm_creates_seen', 0)} "
                        f"whales={state.get('whale_events_seen', 0)} "
                        f"token_evts={state.get('token_events_seen', 0)} "
                        f"new_tokens={state.get('new_tokens_seen', 0)} "
                        f"rate={rate:.1f} tx/s")
                    # Cross-machine liveness signal: Render reads this row
                    # to know the Mac-hosted worker is alive (local file
                    # mtimes don't cross hosts).
                    pgbridge.write_heartbeat(
                        "xrpl_stream",
                        txns_seen=state.get("txns_seen"),
                        last_ledger=state.get("last_ledger_index"),
                        extra={"rate_tx_s": round(rate, 2)},
                    )
                    last_heartbeat = now
                    txns_at_last_heartbeat = state["txns_seen"]
        finally:
            watchdog_stop.set()
            wd.join(timeout=2)


def main():
    state = load_json(STATE_PATH, {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "txns_seen": 0,
        "amm_creates_seen": 0,
        "last_ledger_index": None,
    })
    log(f"xrpl_stream starting — txns_seen so far: {state['txns_seen']:,}")

    backoff = RECONNECT_BACKOFF_BASE
    while True:
        try:
            run_session(state)
            log("session ended cleanly (unexpected) — reconnecting")
            backoff = RECONNECT_BACKOFF_BASE
        except KeyboardInterrupt:
            log("interrupted — saving state and exiting")
            save_json(STATE_PATH, state)
            return 130
        except Exception as e:
            log(f"session error: {e} — reconnect in {backoff:.1f}s")
            save_json(STATE_PATH, state)
            time.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_BACKOFF_MAX)


if __name__ == "__main__":
    sys.exit(main())
