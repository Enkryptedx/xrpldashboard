"""XRPL node selection helper for xrpldashboard walkers.

Local rippled node primary, cascading public fallback. The probe uses
the same xrpl-py JsonRpcClient path the actual request uses, so a green
health check can't come from a path that differs from the walker's
real call route.

TTL cache on health is load-bearing for long-running processes (Flask
workers, persistent walkers). For launchd one-shot walkers it is inert
— each invocation does one fresh probe per run. That's acceptable for
daily-cadence one-shots (one extra server_info per run is nothing); it
becomes essential when the batch cutover hits any persistent walker.
"""

import logging
import os
import time

from xrpl.clients import JsonRpcClient
from xrpl.models.requests import ServerInfo

import db

LOCAL_NODE = os.environ.get("XRPL_LOCAL_NODE", "http://localhost:5005")
PUBLIC_NODES = [
    os.environ.get("XRPL_PUBLIC_PRIMARY",   "https://s1.ripple.com:51234"),
    os.environ.get("XRPL_PUBLIC_SECONDARY", "https://s2.ripple.com:51234"),
]
HEALTH_TTL_SECONDS = 45

logger = logging.getLogger(__name__)
_health = {"checked_at": 0.0, "ok": False, "reason": "uninitialized"}


def _probe_local():
    try:
        client = JsonRpcClient(LOCAL_NODE)
        resp = client.request(ServerInfo())
        info = (resp.result or {}).get("info", {})
        state = info.get("server_state")
        if state != "full":
            return False, f"state={state}"
        return True, "full"
    except Exception as e:
        return False, f"unreachable:{type(e).__name__}"


def _get_health():
    now = time.monotonic()
    if now - _health["checked_at"] >= HEALTH_TTL_SECONDS:
        ok, reason = _probe_local()
        _health.update({"checked_at": now, "ok": ok, "reason": reason})
    return _health["ok"], _health["reason"]


def _log_fallback(walker_name, reason):
    try:
        db.write_walker_node_fallback(walker_name, reason)
    except Exception:
        logger.exception("walker_node_fallback write failed")


# Result-level error strings that mean "local node can't answer this
# right now" rather than "the request itself is bad" (e.g. actNotFound).
# When any of these appear in resp.result.error, treat the local response
# as unusable and cascade to public. Surfaced 2026-07-01 during the
# escrow_walker rollout: the local node was reporting server_state=full
# via server_info but returning notSynced on every account_objects call
# (load_factor ~600). The wrapper previously only cascaded on transport
# exceptions, so walkers saw notSynced as a per-request failure and lost
# every response.
_NODE_HEALTH_ERRORS = frozenset({
    "notSynced",
    "noNetwork",
    "noCurrent",
    "noClosed",
    "tooBusy",
    "amendmentBlocked",
    "InsufficientNetworkMode",
    # Local rippled can develop gaps in complete_ledgers (observed 2026-07-03
    # during nft_activity_walker rollout: complete_ledgers reported
    # "…-105354442,105354497-…" and every request into the 54-ledger hole
    # returned lgrNotFound while server_state=full). Structurally this is a
    # "local node can't answer this ledger" case, not a "request is bad" case.
    # Public nodes with full history will return the ledger; if both fail,
    # the caller still receives lgrNotFound as the honest answer.
    "lgrNotFound",
})


def _is_node_health_error(resp):
    try:
        result = resp.result or {}
    except Exception:
        return False
    err = result.get("error")
    if not err:
        return False
    return err in _NODE_HEALTH_ERRORS


class XrplClient:
    """Drop-in for xrpl.clients.JsonRpcClient. .request(req) tries local
    first; on health-fail, transport error, or a result-level node-health
    error, cascades through PUBLIC_NODES in order. Non-health application
    errors (e.g. actNotFound) return normally — those are valid answers,
    not a node problem. Each fallback writes one walker_node_fallback
    row."""

    def __init__(self, walker_name="unknown"):
        self.walker_name = walker_name

    def request(self, req):
        ok, reason = _get_health()
        if ok:
            try:
                resp = JsonRpcClient(LOCAL_NODE).request(req)
            except Exception as e:
                _health["checked_at"] = 0.0
                _log_fallback(self.walker_name, f"local_request_error:{type(e).__name__}")
            else:
                if not _is_node_health_error(resp):
                    return resp
                _health["checked_at"] = 0.0
                _log_fallback(
                    self.walker_name,
                    f"local_result_error:{(resp.result or {}).get('error')}",
                )
        else:
            _log_fallback(self.walker_name, reason)
        last = None
        for url in PUBLIC_NODES:
            try:
                return JsonRpcClient(url).request(req)
            except Exception as e:
                last = e
                logger.warning("public_request_failed url=%s err=%s", url, e)
        raise last or RuntimeError("all xrpl endpoints failed")


def get_client(walker_name="unknown"):
    return XrplClient(walker_name)
