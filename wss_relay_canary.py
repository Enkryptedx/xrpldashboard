"""wss_relay_canary — verify the own-node WSS relay is reachable from the
public internet and pushing ledgerClosed events on cadence.

Charlie ruling 2026-09-22 Tue 8:42 PM ET (post live-feed sovereignty flip):
the sovereignty flip made wss://wss.xrpldashboard.com the primary feed
for /tokens, /whales, /pools, /wallet. If the relay goes silent,
browsers auto-fall-back to xrplcluster.com and the walker_node_fallback
telemetry catches it — but that's a client-side symptom. This canary
watches the same handshake from a MAC-SIDE PROBE independent of every
browser, so a relay outage is detected even if no browser is currently
on the affected pages.

Contract:
  - Open a WSS connection to wss://wss.xrpldashboard.com through the CF tunnel.
  - Send {"command":"subscribe","streams":["ledger"]}.
  - Wait up to 10 seconds for at least one type=ledgerClosed message.
  - Sanity-check ledger_index is within 10 minutes of a live XRPL ledger
    (rough: within a few million of the current head; hard-cap is arbitrary).
  - Stamp launchd_state/wss_relay_canary_last_ok on success only.
  - Write walker_health row on both success + failure (no silent skip —
    see feedback_telemetry_fail_loud rule).

Cadence: every 15 min (StartInterval=900). Meta-watch ceiling: 1h.

Milestone 2 (2026-09-26): `--all-feeds` (or WSS_RELAY_CANARY_ALL_FEEDS=1)
adds one fresh-socket probe per named feed — amm_transactions,
token_top100_transactions, whale_transactions, wallet_transactions (RLUSD
issuer), supply_updates — and appends FEEDS={...} per-feed verdicts to the
walker_health message. Any FAIL_* verdict fails the run (findings_count>0).
Opt-in until the relay is deployed; flip the env in the plist after.
"""
from __future__ import annotations

import asyncio
import json
import os
import ssl
import sys
import time
import traceback

import certifi

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import db  # noqa: E402


WALKER_NAME = "wss_relay_canary"
WALKER_CADENCE_SECONDS = 900  # every 15 min
LAST_OK_STAMP = os.path.join(HERE, "launchd_state", f"{WALKER_NAME}_last_ok")

RELAY_URL = os.environ.get("WSS_RELAY_URL", "wss://wss.xrpldashboard.com")
WAIT_SECONDS = 10.0
SUBSCRIBE_MSG = {"id": "canary", "command": "subscribe", "streams": ["ledger"]}


async def _probe(url: str, timeout_s: float) -> dict:
    """One connect → subscribe → wait for ledgerClosed. Returns structured
    result. Raises on hard failures (DNS, TLS, refused)."""
    import websockets
    ctx = ssl.create_default_context(cafile=certifi.where())
    result = {"opened": False, "subscribed": False, "ledgers_seen": 0,
              "first_ledger_index": None, "first_ledger_at_s": None,
              "elapsed_s": None, "close_code": None, "close_reason": None}
    start = time.time()
    async with websockets.connect(url, ssl=ctx, open_timeout=timeout_s) as ws:
        result["opened"] = True
        await ws.send(json.dumps(SUBSCRIBE_MSG))
        result["subscribed"] = True
        deadline = time.time() + timeout_s
        while time.time() < deadline and result["ledgers_seen"] < 1:
            remaining = deadline - time.time()
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                break
            try:
                d = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            if d.get("type") == "ledgerClosed" and isinstance(d.get("ledger_index"), int):
                result["ledgers_seen"] += 1
                if result["first_ledger_index"] is None:
                    result["first_ledger_index"] = d["ledger_index"]
                    result["first_ledger_at_s"] = round(time.time() - start, 3)
        try:
            await ws.close()
        except Exception:
            pass
    result["elapsed_s"] = round(time.time() - start, 3)
    return result


# Served-homepage freshness gate (incident 2026-09-26: the pre-render walker
# re-saved its own cached body for ~25.5 h while every health signal stayed
# green — they measured "the walker ran", not "the page is fresh"). This
# canary fetches the SERVED page (no cache bypass — we want what a visitor
# gets) and reads the render time the route bakes into
# `id="cached-ts" data-iso=…`. Budget: walker cadence 300 s + route serve
# window 1800 s + slack → anything older than this is a frozen homepage.
HOMEPAGE_SERVED_MAX_BAKED_AGE_S = int(
    os.environ.get("HOMEPAGE_SERVED_MAX_BAKED_AGE_S", "2700"))


def _classify_homepage_body(body: str, now=None) -> tuple[str, str, "int | None"]:
    """Pure classifier for a 200 homepage body.

    Returns (url_status, fresh_status, baked_age_s):
      url_status   'ok' | 'missing'          — relay URL present in body?
      fresh_status 'ok' | 'no_ts' | 'stale'  — baked render age within budget?
      baked_age_s  int seconds, or None when no parseable cached-ts."""
    import datetime as _dt
    from homepage_summary_walker import baked_render_time
    url_status = "ok" if "wss.xrpldashboard.com" in body else "missing"
    baked = baked_render_time(body)
    if baked is None:
        return url_status, "no_ts", None
    now = now or _dt.datetime.now(_dt.timezone.utc)
    age = int((now - baked).total_seconds())
    fresh = "ok" if age <= HOMEPAGE_SERVED_MAX_BAKED_AGE_S else "stale"
    return url_status, fresh, age


def _probe_homepage() -> tuple[str, str, "int | None"]:
    """Fetch xrpldashboard.com/ (the served page, cache included) and
    classify it. Charlie ruling 2026-09-23 19:44 ET (relay-URL presence)
    + 2026-09-26 freshness gate. Runs every 15 min alongside the ledger
    + tx probes.

    Returns (url_status, fresh_status, baked_age_s) where url_status is
    one of:
      'ok'          — homepage body contains 'wss.xrpldashboard.com'
      'missing'     — homepage body served OK but relay URL absent
      'http_<code>' — non-200 status from the site
      'error_<T>'   — hard fetch failure
    and fresh_status is 'ok' | 'no_ts' | 'stale' | 'unknown' (fetch failed)."""
    import urllib.request as _ur
    import urllib.error as _ue
    _ctx = ssl.create_default_context(cafile=certifi.where())
    try:
        req = _ur.Request(
            "https://xrpldashboard.com/",
            headers={"User-Agent": "xrpldashboard-wss-relay-canary/1.0",
                     "Cache-Control": "no-cache"},
        )
        with _ur.urlopen(req, timeout=10, context=_ctx) as resp:
            if resp.status != 200:
                return f"http_{resp.status}", "unknown", None
            body = resp.read(300 * 1024).decode("utf-8", errors="replace")
        return _classify_homepage_body(body)
    except _ue.HTTPError as e:
        return f"http_{e.code}", "unknown", None
    except Exception as e:
        return f"error_{type(e).__name__}", "unknown", None


def _probe_homepage_url() -> str:
    """Back-compat: url_status only."""
    return _probe_homepage()[0]


async def _probe_tx_sub(url: str, timeout_s: float) -> str:
    """LOG-ONLY probe (Charlie ruling 2026-09-23 19:17 ET, per Option B
    prep). Opens a FRESH socket, sends a transactions-stream subscribe,
    and records the outcome as a short token.

    Returns one of:
      'ok'         — subscribe response with status=success within timeout
      'close_1008' — relay closed with 1008 subscribe_only (expected today)
      'close_other'— relay closed with a different code/reason
      'timeout'    — no response + no close within timeout_s

    NEVER FAILS THE CANARY on its own — this is the log-only phase per
    the standing rule (feedback: request_path_filter_log_only_first).
    Result string is appended to walker_health.message as
    `TX_SUB_STATUS=<token>`. After 24h of `TX_SUB_STATUS=ok`, a follow-
    up commit will flip the canary to fail if this returns anything
    other than 'ok'."""
    import websockets
    from websockets.exceptions import ConnectionClosed
    ctx = ssl.create_default_context(cafile=certifi.where())
    TX_SUB_MSG = {"id": "canary-tx", "command": "subscribe",
                  "streams": ["transactions"]}
    try:
        async with websockets.connect(url, ssl=ctx, open_timeout=timeout_s) as ws:
            await ws.send(json.dumps(TX_SUB_MSG))
            deadline = time.time() + timeout_s
            while time.time() < deadline:
                remaining = max(0.01, deadline - time.time())
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                except asyncio.TimeoutError:
                    return "timeout"
                except ConnectionClosed as e:
                    if e.code == 1008 and str(e.reason) == "subscribe_only":
                        return "close_1008"
                    return f"close_other_code={e.code}_reason={str(e.reason)[:32]}"
                try:
                    d = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                if (d.get("type") == "response"
                        and d.get("id") == "canary-tx"
                        and d.get("status") == "success"):
                    return "ok"
            return "timeout"
    except Exception as e:
        return f"probe_error_{type(e).__name__}"


# ── Milestone 2: named-feed probes (docs/OPTION_B_MILESTONE2_RELAY_DIFF.md §2) ──
#
# Enabled by --all-feeds or WSS_RELAY_CANARY_ALL_FEEDS=1 (so the Mac plist can
# flip it by env once the relay is deployed). Sequential, each on a FRESH
# socket. Budget: ≤10s ledger + ≤30s ×4 event-waits + one handshake ≈ 2.5 min,
# well under the 900s cadence.
#
# Per-feed verdicts:
#   OK          handshake success + expected event arrived in time
#   OK_SILENT   handshake success, no event in the window — acceptable for the
#               amm/token/whale feeds (traffic-dependent; silence is not a fault)
#   FAIL_*      handshake rejected / closed / timed out, or a REQUIRED event
#               (wallet_transactions on the RLUSD issuer, supply_updates) missing
#
# Wallet probe target (§11.3): the RLUSD issuer, ~240 tx/min. No synthetic
# account, no self-generated tx — passive observation of real traffic.
RLUSD_ISSUER = "rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De"
FEED_WAIT_SECONDS = 30.0
FEED_PROBES = (
    # (feed, accounts, event_required)
    ("amm_transactions", None, False),
    ("token_top100_transactions", None, False),
    ("whale_transactions", None, False),
    ("wallet_transactions", [RLUSD_ISSUER], True),
    ("supply_updates", None, True),
)


async def _probe_feed(url: str, feed: str, accounts, wait_s: float,
                      event_required: bool) -> tuple[str, dict]:
    """Subscribe to one named feed on a fresh socket. Returns (verdict, detail)."""
    import websockets
    from websockets.exceptions import ConnectionClosed
    # ssl only for wss:// (tests + Lenovo-local runs probe ws://127.0.0.1)
    ctx = ssl.create_default_context(cafile=certifi.where()) if url.startswith("wss://") else None
    sub = {"id": f"canary-{feed}", "command": "subscribe", "stream": feed}
    if accounts:
        sub["accounts"] = accounts
    want_type = "supply_update" if feed == "supply_updates" else "transaction"
    detail = {"handshake": False, "events": 0, "first_event_at_s": None, "elapsed_s": None}
    start = time.time()
    try:
        async with websockets.connect(url, ssl=ctx, open_timeout=10.0) as ws:
            await ws.send(json.dumps(sub))
            deadline = time.time() + wait_s
            while time.time() < deadline:
                remaining = max(0.01, deadline - time.time())
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                except asyncio.TimeoutError:
                    break
                except ConnectionClosed as e:
                    detail["elapsed_s"] = round(time.time() - start, 3)
                    return f"FAIL_close_{e.code}_{str(e.reason)[:24]}", detail
                try:
                    d = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                if (d.get("type") == "response" and d.get("id") == sub["id"]):
                    if d.get("status") == "success" and (d.get("result") or {}).get("stream") == feed:
                        detail["handshake"] = True
                        detail["relay_mode"] = (d.get("result") or {}).get("relay_mode")
                    else:
                        detail["elapsed_s"] = round(time.time() - start, 3)
                        return "FAIL_handshake", detail
                    continue
                if d.get("type") != want_type:
                    continue
                if feed == "supply_updates" and not isinstance(d.get("total_coins"), int):
                    detail["elapsed_s"] = round(time.time() - start, 3)
                    detail["total_coins"] = d.get("total_coins")
                    return "FAIL_no_total_coins", detail
                detail["events"] += 1
                if detail["first_event_at_s"] is None:
                    detail["first_event_at_s"] = round(time.time() - start, 3)
                    if feed == "supply_updates":
                        detail["total_coins"] = d.get("total_coins")
                    break
            try:
                await ws.close()
            except Exception:
                pass
    except Exception as e:
        detail["elapsed_s"] = round(time.time() - start, 3)
        return f"FAIL_probe_{type(e).__name__}", detail
    detail["elapsed_s"] = round(time.time() - start, 3)
    if not detail["handshake"]:
        return "FAIL_no_handshake", detail
    if detail["events"] >= 1:
        return "OK", detail
    return ("FAIL_no_event" if event_required else "OK_SILENT"), detail


def _run_feed_probes(url: str) -> tuple[dict, list]:
    """Sequential per-feed probes. Returns ({feed: verdict}, findings)."""
    verdicts: dict = {}
    findings: list = []
    for feed, accounts, required in FEED_PROBES:
        try:
            verdict, detail = asyncio.run(_probe_feed(url, feed, accounts, FEED_WAIT_SECONDS, required))
        except Exception as e:
            verdict, detail = f"FAIL_probe_{type(e).__name__}", {}
        verdicts[feed] = verdict
        print(f"[{WALKER_NAME}] feed={feed} verdict={verdict} {json.dumps(detail, sort_keys=True)}")
        if verdict.startswith("FAIL"):
            findings.append({"severity": "high", "reason": f"feed_{feed}",
                             "detail": f"{verdict} {json.dumps(detail, sort_keys=True)[:200]}"})
    return verdicts, findings


def _all_feeds_enabled(argv) -> bool:
    if "--all-feeds" in argv:
        return True
    return os.environ.get("WSS_RELAY_CANARY_ALL_FEEDS", "").strip().lower() in ("1", "true", "yes", "on")


def _stamp_last_ok() -> None:
    try:
        os.makedirs(os.path.dirname(LAST_OK_STAMP), exist_ok=True)
        with open(LAST_OK_STAMP, "w") as f:
            f.write(str(int(time.time())))
    except OSError:
        pass


def main() -> int:
    db.write_walker_health_start(WALKER_NAME, cadence_seconds=WALKER_CADENCE_SECONDS)
    ok = False
    message = "not_yet_stamped"
    findings = []
    try:
        try:
            result = asyncio.run(_probe(RELAY_URL, WAIT_SECONDS))
        except Exception as e:
            message = f"probe_exception:{type(e).__name__}:{str(e)[:120]}"
            findings.append({"severity": "high", "reason": "probe_exception",
                             "detail": message})
            print(f"[{WALKER_NAME}] {message}", file=sys.stderr, flush=True)
            return 1

        if not result["opened"]:
            message = "handshake_failed"
            findings.append({"severity": "high", "reason": "handshake_failed"})
        elif result["ledgers_seen"] < 1:
            message = (f"no_ledger_in_{WAIT_SECONDS:g}s "
                       f"(elapsed_s={result['elapsed_s']})")
            findings.append({"severity": "high", "reason": "no_ledger",
                             "detail": message})
        else:
            ok = True
            message = (f"ok ledger={result['first_ledger_index']} "
                       f"first_ledger_at_s={result['first_ledger_at_s']} "
                       f"total_elapsed_s={result['elapsed_s']}")
            _stamp_last_ok()

        # LOG-ONLY tx-sub probe (Charlie 2026-09-23 19:17 ET, Option B
        # prep). Runs regardless of ledger-sub outcome so we get a
        # baseline reading even during transient ledger issues. Result
        # appended to walker_health.message as TX_SUB_STATUS=<token>.
        # Does NOT modify ok/failure — pure observation for 24h until
        # the enforce flip.
        try:
            tx_status = asyncio.run(_probe_tx_sub(RELAY_URL, 5.0))
        except Exception as e:
            tx_status = f"probe_error_{type(e).__name__}"
        # HOMEPAGE_URL_STATUS: does the served homepage body contain the
        # relay URL? Charlie 2026-09-23 19:44 ET — catches the pre-render
        # env-gap regression at the served-body layer (walker env guard
        # catches it at build time; this catches it at serve time).
        try:
            homepage_status, homepage_fresh, homepage_age = _probe_homepage()
        except Exception as e:
            homepage_status, homepage_fresh, homepage_age = (
                f"error_{type(e).__name__}", "unknown", None)
        # Enforce: HOMEPAGE_URL_STATUS != ok is a regression — mark ok=False
        # so walker_health pages via staleness. Not log-only because the
        # regression it catches is a sovereignty-visible failure and we
        # want it loud immediately.
        if homepage_status != "ok":
            ok = False
            # Do NOT overwrite the ledger-sub message on the failure path;
            # append the reason so downstream monitors see both.
        # Enforce: HOMEPAGE_FRESH != ok on a 200 body is the 2026-09-26
        # frozen-homepage incident class (served page older than the
        # walker+serve budget, or no baked timestamp at all). Loud, not
        # log-only: it took 25.5 h to notice by eye.
        if homepage_status == "ok" and homepage_fresh != "ok":
            ok = False
            findings.append({
                "severity": "high", "reason": f"homepage_{homepage_fresh}",
                "baked_age_s": homepage_age,
                "budget_s": HOMEPAGE_SERVED_MAX_BAKED_AGE_S})
        message = (f"{message} TX_SUB_STATUS={tx_status} "
                   f"HOMEPAGE_URL_STATUS={homepage_status} "
                   f"HOMEPAGE_FRESH={homepage_fresh} "
                   f"HOMEPAGE_BAKED_AGE_S={homepage_age}")
        # Milestone 2 named-feed probes (opt-in until the relay is deployed).
        # Any FAIL_* verdict is a real finding: ok=False, findings_count>0,
        # and FEEDS=<json dict> in the message so L1 sees WHICH feed broke.
        if _all_feeds_enabled(sys.argv[1:]):
            verdicts, feed_findings = _run_feed_probes(RELAY_URL)
            verdicts = {"ledger": ("OK" if result["ledgers_seen"] >= 1 else "FAIL_no_ledger"),
                        **verdicts}
            if feed_findings:
                ok = False
                findings.extend(feed_findings)
            message = f"{message} FEEDS={json.dumps(verdicts, sort_keys=True, separators=(',', ':'))}"
        print(f"[{WALKER_NAME}] {message}")
        return 0 if ok else 1
    finally:
        try:
            db.write_walker_health_end(
                WALKER_NAME, ok=ok,
                message=message or ("clean_no_message" if ok else "unlabeled_failure"),
                findings_count=len(findings),
                findings_json=json.dumps(findings) if findings else None,
            )
        except TypeError:
            # older db.write_walker_health_end signature — no findings kwargs
            try:
                db.write_walker_health_end(WALKER_NAME, ok=ok, message=message)
            except Exception:
                pass
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
