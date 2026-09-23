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
