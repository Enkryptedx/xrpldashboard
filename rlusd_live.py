"""Live RLUSD treasury feed — Ethereum + XRP Ledger.

Both chains are queried directly from public nodes — no third-party
gateway, no API key, no secret to leak. This matches the "self-sourced"
framing the rest of the dashboard already uses for XRP pricing and
ledger state.

Ethereum side: public JSON-RPC (defaults to https://1rpc.io/eth).
  * totalSupply()    via eth_call            → circulating supply
  * Transfer events  via eth_getLogs         → recent mints/burns
    A Transfer with the zero address as `from` is a mint; with the
    zero address as `to` is a burn.

XRPL side: Ripple's public JSON-RPC (s1.ripple.com).
  * gateway_balances  → issuer obligations (= circulating supply)
  * account_tx        → recent Payment txs to/from the issuer
    Issuer as sender = mint (issuance out). Issuer as destination =
    burn (redemption back in).

The two are merged behind a single TTL-cached entry point so the API
route stays cheap regardless of how often clients poll.
"""

from __future__ import annotations

import datetime
import json
import os
import ssl
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib import request as urlrequest
from urllib.error import URLError

from xrpl.clients import JsonRpcClient
from xrpl.models.requests import AccountTx, GatewayBalances

import xrpl_client

# macOS Python installs ship without a populated system cert store, so
# urllib's default SSLContext can't verify HTTPS. certifi ships with the
# Mozilla CA bundle and is already a transitive dependency via xrpl-py →
# httpx → certifi, so we can rely on it without adding a new requirement.
try:
    import certifi
    _SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:  # pragma: no cover
    _SSL_CONTEXT = ssl.create_default_context()


# --- Constants ---------------------------------------------------------------

ETH_CONTRACT = "0x8292bb45bf1ee4d140127049757c2e0ff06317ed"
ETH_DECIMALS = 18
# `totalSupply()` selector — first 4 bytes of keccak256("totalSupply()").
ETH_TOTAL_SUPPLY_SELECTOR = "0x18160ddd"
# ERC-20 Transfer(address,address,uint256) event topic.
TRANSFER_TOPIC = (
    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
)
ZERO_TOPIC = "0x" + "0" * 64

XRPL_ISSUER = "rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De"
XRPL_CURRENCY_HEX = "524C555344000000000000000000000000000000"
# XRPL serializes a 3-char ticker like "USD" literally, but RLUSD is 5
# chars so its on-wire form is the 40-char hex above. Some serializers
# still echo "RLUSD" back as a convenience, so we accept either.
XRPL_CURRENCY_NAMES = {"RLUSD", XRPL_CURRENCY_HEX}

# XRPL timestamps are seconds since 2000-01-01 UTC. Add this to align
# with the standard Unix epoch.
XRPL_EPOCH_OFFSET = 946_684_800

# Ethereum log lookback. Public RPCs tightened eth_getLogs windows in
# mid-2026 (publicnode.com caps at 100, 1rpc.io at 50). Lowered
# 1000 → 100 on 2026-07-17 so the live-event feed's single query stays
# under the sole responding endpoint's ceiling. 100 blocks ≈ 20 min of
# recent activity — noticeable downgrade from the old 3.3h window, but
# the alternative is silent all-endpoint-failure and an empty feed.
ETH_LOOKBACK_BLOCKS = 100
ETH_MAX_EVENTS = 80
# Approximate average block time, used to back-fill timestamps from
# block numbers (logs don't carry timestamps over public RPC, and we
# don't want to fan out N+1 eth_getBlockByNumber calls).
ETH_SECONDS_PER_BLOCK = 12

XRPL_TX_LIMIT = 80
XRPL_MAX_EVENTS = 80
# 24h mint/burn aggregate walk. account_tx paginates with marker; we walk
# newest-first and stop as soon as a page's oldest tx falls past the
# cutoff. 400 is rippled's per-call max. The page cap is a safety net
# for a hyperactive issuer day.
XRPL_AGGREGATE_PAGE = 400
XRPL_AGGREGATE_MAX_PAGES = 20

# Refresher cadence. The launchd walker (rlusd_refresher_walker) invokes
# _refresh_cache_once on this interval and writes the result to Neon. The
# /rlusd route and /api/rlusd/state read from Postgres only — RPC latency
# stays off the hot path and the 19s+ refresh can never trip gunicorn's
# request timeout. 5 min is adequate for a treasury page where XRPL-side
# mints/burns are genuinely rare and ETH-side moves in seconds-to-minute
# clumps. Was 30s when a per-worker daemon thread kept the cache warm;
# that pattern died silently on every deploy (caught 2026-05-27).
REFRESH_INTERVAL = int(os.environ.get("RLUSD_REFRESH_INTERVAL_SECONDS", "300"))
# Multi-RPC fallback. The 24h aggregate walk fires ~17 calls per cache
# miss which blew past 1rpc.io's free-tier limits in production. We try
# each endpoint in order, falling through on rate-limit/auth/network
# failures so the page survives any single node going dark or throttling.
# ETH_RPC, when set, prepends to the list (lets prod override without a
# code change). publicnode.com has the most generous free limits we've
# tested; llamarpc and ankr are healthy backups.
_DEFAULT_ETH_RPCS = [
    "https://ethereum-rpc.publicnode.com",
    "https://eth.llamarpc.com",
    "https://rpc.ankr.com/eth",
    "https://1rpc.io/eth",
]
_env_eth_rpc = os.environ.get("ETH_RPC")
ETH_RPCS = ([_env_eth_rpc] if _env_eth_rpc else []) + _DEFAULT_ETH_RPCS
# Kept for backward compat / debug surfaces that read a single endpoint.
ETH_RPC = ETH_RPCS[0]
XRPL_NODE = os.environ.get("XRPL_NODE", "https://s1.ripple.com:51234")

HTTP_TIMEOUT = 9.0
USER_AGENT = "xrpldashboard/1.0 (+https://xrpldashboard.com)"


# --- Ethereum (public JSON-RPC) ---------------------------------------------

def _eth_rpc_one(endpoint: str, method: str, params: list) -> Any:
    """POST a JSON-RPC request to a single Ethereum endpoint. Returns the
    `result` field; raises RuntimeError on network failure or JSON-RPC
    error envelope (including rate-limit responses, which come back as
    error envelopes from most public nodes)."""
    payload = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    ).encode("utf-8")
    req = urlrequest.Request(
        endpoint,
        data=payload,
        headers={"User-Agent": USER_AGENT, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlrequest.urlopen(req, timeout=HTTP_TIMEOUT, context=_SSL_CONTEXT) as resp:
            body = resp.read()
    except URLError as e:
        raise RuntimeError(f"{endpoint}: {type(e).__name__}: {e}")
    data = json.loads(body.decode("utf-8"))
    if "error" in data:
        err = data["error"]
        msg = err.get("message", err) if isinstance(err, dict) else err
        raise RuntimeError(f"{endpoint}: {msg}")
    return data.get("result")


def _eth_rpc(method: str, params: list) -> Any:
    """Try each ETH_RPCS endpoint in order; return the first success. Rate
    limits, auth failures, and network errors fall through to the next
    node. Raises only if every endpoint fails — message includes the
    last error so the surfaced cause is the most-recent attempt."""
    last_err: Exception | None = None
    for endpoint in ETH_RPCS:
        try:
            return _eth_rpc_one(endpoint, method, params)
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(f"eth_rpc {method}: all endpoints failed: {last_err}")


def _decode_eth_amount(data_hex: str) -> float:
    if not data_hex:
        return 0.0
    raw = int(data_hex, 16)
    return raw / (10 ** ETH_DECIMALS)


# Aggregate mint/burn walks moved to rlusd_etherscan (Etherscan V2 tokentx).
# Reason: eth_getLogs over public RPCs shipped 28 days of false-flat $0 in
# summer 2026 — providers tightened block-range limits and the chunked
# fanout hit rate-limits it couldn't back off from. See
# project_xrpldashboard_rlusd_false_flat_2026-07-17. The Etherscan path
# uses one HTTP call per aggregate walk (up to a few for paginated days),
# rate-limits itself at 5 req/sec, and is the same source Charlie
# spot-checks against in the Etherscan UI.


def fetch_eth() -> dict:
    """Returns {supply, events, mints_24h, burns_24h, mints_calendar_today,
    burns_calendar_today, mints_calendar_prev, burns_calendar_prev, error}.

    `mints_24h` / `burns_24h` are the *rolling* trailing-24h totals used by
    the live /rlusd footer (labeled "last 24 hours (rolling)" on-page).

    `mints_calendar_today` / `burns_calendar_today` are today's UTC calendar
    day mints/burns *so far* — used by the history writer for today's row.
    `mints_calendar_prev` / `burns_calendar_prev` are yesterday's UTC calendar
    day, fully finalized — the writer also UPDATEs yesterday's row each cycle
    so the last-committed calendar day is always the true full-day total,
    even if the walker didn't run right at 00:00Z.

    All aggregate fields are None when the Etherscan V2 tokentx path can't
    complete; the page renders '—' with a correction note rather than a
    fabricated $0. See rlusd_etherscan and
    project_xrpldashboard_rlusd_false_flat_2026-07-17 for the anti-pattern
    this replaced."""
    out: dict[str, Any] = {
        "supply": None,
        "events": [],
        "mints_24h": None,
        "burns_24h": None,
        "mints_calendar_today": None,
        "burns_calendar_today": None,
        "mints_calendar_prev": None,
        "burns_calendar_prev": None,
        "error": None,
    }

    # Total supply via eth_call → totalSupply().
    try:
        result = _eth_rpc("eth_call", [
            {"to": ETH_CONTRACT, "data": ETH_TOTAL_SUPPLY_SELECTOR},
            "latest",
        ])
        if isinstance(result, str) and result.startswith("0x"):
            raw = int(result, 16)
            out["supply"] = raw / (10 ** ETH_DECIMALS)
    except Exception as e:
        out["error"] = f"eth_supply: {type(e).__name__}: {e}"

    # All Transfer events in the lookback window. Mints/burns are rare
    # (often zero per day), so showing only those leaves the live log
    # looking dead even when hundreds of holder-to-holder transfers are
    # happening. Classify by topic and emit all three event types.
    try:
        latest_hex = _eth_rpc("eth_blockNumber", [])
        latest = int(latest_hex, 16)
        from_block = max(0, latest - ETH_LOOKBACK_BLOCKS)
        now_unix = int(time.time())

        logs = _eth_rpc("eth_getLogs", [{
            "address": ETH_CONTRACT,
            "topics": [TRANSFER_TOPIC],
            "fromBlock": hex(from_block),
            "toBlock": "latest",
        }])

        if isinstance(logs, list):
            for log in logs[-ETH_MAX_EVENTS:]:
                topics = log.get("topics", [])
                if len(topics) < 3:
                    continue
                from_t = topics[1].lower()
                to_t = topics[2].lower()
                amount = _decode_eth_amount(log.get("data", "0x0"))
                tx_hash = log.get("transactionHash", "")
                blk_hex = log.get("blockNumber", "0x0")
                blk = int(blk_hex, 16) if isinstance(blk_hex, str) else int(blk_hex)
                # Approximate timestamp from latest block + average block time.
                ts = now_unix - max(0, latest - blk) * ETH_SECONDS_PER_BLOCK
                if from_t == ZERO_TOPIC:
                    ev_type = "mint"
                elif to_t == ZERO_TOPIC:
                    ev_type = "burn"
                else:
                    ev_type = "transfer"
                out["events"].append({
                    "type": ev_type, "chain": "eth",
                    "amount": amount, "tx": tx_hash, "ts": ts,
                    "from": "0x" + from_t[-40:],
                    "to": "0x" + to_t[-40:],
                })
    except Exception as e:
        prev = out["error"]
        out["error"] = (
            f"{prev} | eth_events: {type(e).__name__}: {e}"
            if prev else f"eth_events: {type(e).__name__}: {e}"
        )

    # Rolling 24h (for the live footer) + today's + yesterday's calendar-day
    # totals (for the history writer). Two distinct semantics rendered in two
    # distinct places, both derived from Etherscan V2 tokentx. Any failure
    # leaves the affected aggregate at None; history row + live footer render
    # '—' rather than a synthesized $0.
    try:
        import rlusd_etherscan as _rle
        now_unix = int(time.time())
        today = datetime.datetime.now(datetime.timezone.utc).date()
        prev_day = today - datetime.timedelta(days=1)

        # Rolling 24h — one boundary call + one tokentx paginate.
        try:
            r_m, r_b = _rle.aggregate_rolling_24h(now_unix)
            out["mints_24h"] = r_m
            out["burns_24h"] = r_b
        except Exception as e:
            prev = out["error"]
            msg = f"eth_rolling_24h: {type(e).__name__}: {e}"
            out["error"] = f"{prev} | {msg}" if prev else msg

        # Today calendar-day so-far.
        try:
            t_m, t_b = _rle.aggregate_calendar_day(today)
            out["mints_calendar_today"] = t_m
            out["burns_calendar_today"] = t_b
        except Exception as e:
            prev = out["error"]
            msg = f"eth_cal_today: {type(e).__name__}: {e}"
            out["error"] = f"{prev} | {msg}" if prev else msg

        # Previous calendar-day (fully closed).
        try:
            p_m, p_b = _rle.aggregate_calendar_day(prev_day)
            out["mints_calendar_prev"] = p_m
            out["burns_calendar_prev"] = p_b
        except Exception as e:
            prev = out["error"]
            msg = f"eth_cal_prev: {type(e).__name__}: {e}"
            out["error"] = f"{prev} | {msg}" if prev else msg
    except Exception as e:
        prev = out["error"]
        out["error"] = (
            f"{prev} | eth_aggregates: {type(e).__name__}: {e}"
            if prev else f"eth_aggregates: {type(e).__name__}: {e}"
        )

    return out


# --- XRPL --------------------------------------------------------------------

def _xrpl_currency_match(cur: str | None) -> bool:
    return cur in XRPL_CURRENCY_NAMES if cur else False


def _fetch_xrpl_24h_aggregates(client, now_unix: int) -> tuple[float, float]:
    """Walk account_tx on the issuer until we cross the 24h cutoff, summing
    Payment-based mints (issuer → other) and burns (other → issuer). Trades
    (OfferCreate) are ignored — they don't change supply. Stops as soon as
    a page's oldest tx is older than the cutoff; capped at
    XRPL_AGGREGATE_MAX_PAGES so a runaway day can't stall the request."""
    cutoff_ts = now_unix - 86_400
    mints = 0.0
    burns = 0.0
    marker = None
    for _ in range(XRPL_AGGREGATE_MAX_PAGES):
        kwargs = {
            "account": XRPL_ISSUER,
            "ledger_index_min": -1,
            "ledger_index_max": -1,
            "limit": XRPL_AGGREGATE_PAGE,
            "binary": False,
        }
        if marker is not None:
            kwargs["marker"] = marker
        try:
            resp = client.request(AccountTx(**kwargs))
        except Exception:
            break
        result = resp.result or {}
        txs = result.get("transactions", []) or []
        if not txs:
            break
        oldest_ts = None
        for entry in txs:
            tx = entry.get("tx") or entry.get("tx_json") or {}
            ts = int(tx.get("date", 0)) + XRPL_EPOCH_OFFSET
            if oldest_ts is None or ts < oldest_ts:
                oldest_ts = ts
            if ts < cutoff_ts:
                continue
            if tx.get("TransactionType") != "Payment":
                continue
            meta = entry.get("meta") or {}
            delivered = meta.get("delivered_amount")
            if not isinstance(delivered, dict):
                src = tx.get("Amount")
                delivered = src if isinstance(src, dict) else None
            if not isinstance(delivered, dict):
                continue
            if not _xrpl_currency_match(delivered.get("currency")):
                continue
            if delivered.get("issuer") != XRPL_ISSUER:
                continue
            try:
                amount = float(delivered.get("value", "0"))
            except (TypeError, ValueError):
                continue
            sender = tx.get("Account", "")
            destination = tx.get("Destination", "")
            if sender == XRPL_ISSUER and destination != XRPL_ISSUER:
                mints += amount
            elif destination == XRPL_ISSUER and sender != XRPL_ISSUER:
                burns += amount
        if oldest_ts is not None and oldest_ts < cutoff_ts:
            break
        marker = result.get("marker")
        if not marker:
            break
    return mints, burns


def fetch_xrpl() -> dict:
    """Returns {supply, events, mints_24h, burns_24h, error}. mints_24h and
    burns_24h are held at None pending the Step 3 detection rewrite — a
    Payment-based sweep against the issuer produced 53 days (2026-05-25 →
    2026-07-17) of false-flat $0. Root-cause + corrected logic land under
    Fable's Step 3 gate; until then the page shows '—' rather than a
    fabricated zero."""
    out: dict[str, Any] = {
        "supply": None,
        "events": [],
        "mints_24h": None,
        "burns_24h": None,
        "error": None,
    }
    client = xrpl_client.get_client(walker_name="rlusd_refresher")

    # gateway_balances → total RLUSD obligations (= circulating supply).
    try:
        resp = client.request(GatewayBalances(
            account=XRPL_ISSUER, ledger_index="validated"
        ))
        obligations = (resp.result or {}).get("obligations", {})
        for cur, amt in obligations.items():
            if _xrpl_currency_match(cur):
                try:
                    out["supply"] = float(amt)
                except (TypeError, ValueError):
                    pass
                break
    except Exception as e:
        out["error"] = f"xrpl_supply: {type(e).__name__}: {e}"

    # account_tx on the issuer → recent activity. Payments to/from the
    # issuer are mints/burns (rare). OfferCreates against RLUSD on the
    # XRPL DEX are constant and represent real RLUSD price discovery,
    # so we surface them as "trade" events. Without these the XRPL feed
    # would look dead even when the DEX is busy.
    try:
        resp = client.request(AccountTx(
            account=XRPL_ISSUER,
            ledger_index_min=-1,
            ledger_index_max=-1,
            limit=XRPL_TX_LIMIT,
            binary=False,
        ))
        for entry in (resp.result or {}).get("transactions", []):
            tx = entry.get("tx") or entry.get("tx_json") or {}
            meta = entry.get("meta") or {}
            tx_type = tx.get("TransactionType")
            ts = int(tx.get("date", 0)) + XRPL_EPOCH_OFFSET
            # Newer xrpl-py puts the hash on the entry, not inside tx_json.
            # Without this fallback every XRPL event ships tx="" and the
            # client's dedupe + non-empty-tx filter drops the whole feed.
            tx_hash = entry.get("hash") or tx.get("hash", "")

            if tx_type == "Payment":
                delivered = meta.get("delivered_amount")
                if not isinstance(delivered, dict):
                    src = tx.get("Amount")
                    delivered = src if isinstance(src, dict) else None
                if not isinstance(delivered, dict):
                    continue
                if not _xrpl_currency_match(delivered.get("currency")):
                    continue
                if delivered.get("issuer") != XRPL_ISSUER:
                    continue
                try:
                    amount = float(delivered.get("value", "0"))
                except (TypeError, ValueError):
                    continue
                sender = tx.get("Account", "")
                destination = tx.get("Destination", "")
                if sender == XRPL_ISSUER and destination != XRPL_ISSUER:
                    ev_type = "mint"
                elif destination == XRPL_ISSUER and sender != XRPL_ISSUER:
                    ev_type = "burn"
                else:
                    continue
                out["events"].append({
                    "type": ev_type, "chain": "xrpl",
                    "amount": amount, "tx": tx_hash, "ts": ts,
                })
            elif tx_type == "OfferCreate":
                # Find whichever side of the offer is denominated in RLUSD.
                rlusd_amount = None
                for side in (tx.get("TakerPays"), tx.get("TakerGets")):
                    if (isinstance(side, dict)
                            and _xrpl_currency_match(side.get("currency"))
                            and side.get("issuer") == XRPL_ISSUER):
                        try:
                            rlusd_amount = float(side.get("value", "0"))
                        except (TypeError, ValueError):
                            rlusd_amount = None
                        break
                if rlusd_amount is None:
                    continue
                out["events"].append({
                    "type": "trade", "chain": "xrpl",
                    "amount": rlusd_amount, "tx": tx_hash, "ts": ts,
                })
            else:
                continue

            if len(out["events"]) >= XRPL_MAX_EVENTS:
                break
    except Exception as e:
        prev = out["error"]
        out["error"] = (
            f"{prev} | xrpl_events: {type(e).__name__}: {e}"
            if prev else f"xrpl_events: {type(e).__name__}: {e}"
        )

    # 24h mint/burn aggregate DISABLED — the Payment-only sweep silently
    # returned 0.0 for 53 straight days (2026-05-25 → 2026-07-17) because
    # RLUSD supply changes on XRPL don't ride solely on issuer-side Payments
    # under the currently-monitored surface. Held None until Step 3 lands a
    # detection path anchored to actual supply-delta days (identified via
    # rlusd_supply_history) with sample mint tx pulled from Clio as
    # regression fixtures. Codified 2026-07-17.
    # Legacy call preserved below for reference; do not re-enable without
    # the Step 3 rewrite.
    # try:
    #     now_unix = int(time.time())
    #     mints_24h, burns_24h = _fetch_xrpl_24h_aggregates(client, now_unix)
    #     out["mints_24h"] = mints_24h
    #     out["burns_24h"] = burns_24h
    # except Exception as e:
    #     prev = out["error"]
    #     out["error"] = (
    #         f"{prev} | xrpl_24h: {type(e).__name__}: {e}"
    #         if prev else f"xrpl_24h: {type(e).__name__}: {e}"
    #     )
    prev = out["error"]
    msg = "xrpl_24h: disabled pending detection rewrite (Step 3)"
    out["error"] = f"{prev} | {msg}" if prev else msg

    return out


# --- Combined cached entry point --------------------------------------------


def _build_state() -> dict:
    """Pull fresh state from both chains and assemble the payload. Each chain
    fetch swallows its own RPC errors and reports them in its `error` field,
    so this never raises for a partial-outage."""
    now = time.time()
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="rlusd-state") as ex:
        eth_fut = ex.submit(fetch_eth)
        xrpl_fut = ex.submit(fetch_xrpl)
        eth = eth_fut.result()
        xrpl = xrpl_fut.result()

    events = sorted(
        list(eth.get("events", [])) + list(xrpl.get("events", [])),
        key=lambda e: e.get("ts", 0),
        reverse=True,
    )

    return {
        "eth": {
            "supply": eth["supply"],
            # Rolling — used by the live /rlusd footer.
            "mints_24h": eth.get("mints_24h"),
            "burns_24h": eth.get("burns_24h"),
            # Calendar-day (UTC) — used by write_rlusd_supply_history to
            # keep the history rows honest to their date labels. See
            # feedback_history_flip_cadence_rule (title-is-a-contract).
            "mints_calendar_today": eth.get("mints_calendar_today"),
            "burns_calendar_today": eth.get("burns_calendar_today"),
            "mints_calendar_prev": eth.get("mints_calendar_prev"),
            "burns_calendar_prev": eth.get("burns_calendar_prev"),
            "error": eth["error"],
        },
        "xrpl": {
            "supply": xrpl["supply"],
            "mints_24h": xrpl.get("mints_24h", 0.0),
            "burns_24h": xrpl.get("burns_24h", 0.0),
            "error": xrpl["error"],
        },
        "events": events[:120],
        "fetched_at": int(now),
        "ttl_seconds": REFRESH_INTERVAL,
        "sources": {
            "eth": {
                "contract": ETH_CONTRACT,
                "explorer": f"https://etherscan.io/token/{ETH_CONTRACT}",
                "via": ETH_RPC,
            },
            "xrpl": {
                "issuer": XRPL_ISSUER,
                "explorer": f"https://xrpscan.com/account/{XRPL_ISSUER}",
                "via": XRPL_NODE,
            },
        },
    }


def _refresh_cache_once() -> bool:
    """One refresh tick. Returns True on success. Invoked by the launchd
    walker (rlusd_refresher_walker) every REFRESH_INTERVAL seconds; also
    callable in-process via fetch_state(force=True). Defends against any
    unexpected raise so the walker never dies silently. Leaves the
    previous PG row in place on failure — last-good is always better
    than an empty payload on a treasury page."""
    try:
        data = _build_state()
    except Exception:
        return False

    # Gated on completeness — a partial RPC failure would persist
    # eth.supply=None and erase our previous last-known-good. Failures
    # stay silent: the walker process exits non-zero and launchd retries
    # on the next interval.
    try:
        if (data.get("eth", {}).get("supply") is not None
                and data.get("xrpl", {}).get("supply") is not None):
            import db
            db.write_rlusd_state_cache(data)
    except Exception:
        return False

    return True


def fetch_state(force: bool = False) -> dict:
    """Return the latest cross-chain RLUSD state from the Postgres mirror
    that the launchd walker (rlusd_refresher_walker) refreshes every
    REFRESH_INTERVAL seconds. Reading from PG keeps RPC latency off the
    request hot path. force=True triggers a synchronous refresh first
    (used by smoke tests). Cold-start fallthrough to _build_state only
    if the PG row is missing entirely — a true outage."""
    if force:
        _refresh_cache_once()
    import db
    payload, _written_at = db.read_rlusd_state_cache()
    if payload is not None:
        return payload
    return _build_state()
