"""XRPL node selection helper for xrpldashboard walkers.

Local rippled node primary, cascading public fallback. The probe uses
the same JSON-RPC POST path the actual requests use, so a green health
check can't come from a path that differs from the walker's real call
route.

TTL cache on health is load-bearing for long-running processes (Flask
workers, persistent walkers). For launchd one-shot walkers it is inert
— each invocation does one fresh probe per run. That's acceptable for
daily-cadence one-shots (one extra server_info per run is nothing); it
becomes essential when the batch cutover hits any persistent walker.

── 2026-09-02: retry-before-cascade + connection pooling ──────────────
Prior version created a fresh xrpl-py JsonRpcClient per call — each call
opened a new httpx.AsyncClient, i.e. a new TCP handshake per request.
Under cold_storage's tight loops (21 addresses at ~7 rps) that produced
21+ new TCP connections in ~3s and rippled's accept path dropped some.
Result: 48,641 fallback events / 7 days, 99.79% ConnectError, cascading
to Ripple's s1/s2. Diagnosis is in a report from that date; live
`load_factor=1` proved the node itself wasn't overloaded.

Two changes here:
  1. RETRY LOCAL BEFORE CASCADING. On a local transport error OR a
     result-level node-health error, retry up to LOCAL_RETRY_ATTEMPTS
     total with jittered backoff before giving up on local. Retry
     recoveries are visible via logger.info "local_retry_recovered"
     lines — grep launchd_logs to count them. Not written to
     walker_node_fallback because they're the opposite of a fallback.
  2. KEEP-ALIVE HTTPX CLIENT per URL. Sequential calls reuse the same
     TCP connection instead of handshaking fresh every time. That
     eliminates the burst-of-new-TCP pressure that caused the drops
     in the first place. Client is thread-safe (httpx pool has its
     own locking) and shares one pool across walkers in the same
     process.

Failure classification unchanged: transport exceptions and result-level
node-health errors (notSynced, tooBusy, lgrNotFound, …) are retryable;
regular application errors (actNotFound, …) return normally.
"""

import logging
import os
import random
import time

import httpx

from xrpl.asyncio.clients.utils import request_to_json_rpc, json_to_response
from xrpl.models.requests import ServerInfo

import db

LOCAL_NODE = os.environ.get("XRPL_LOCAL_NODE", "http://localhost:5005")
PUBLIC_NODES = [
    os.environ.get("XRPL_PUBLIC_PRIMARY",   "https://s1.ripple.com:51234"),
    os.environ.get("XRPL_PUBLIC_SECONDARY", "https://s2.ripple.com:51234"),
]
HEALTH_TTL_SECONDS = 45

# Retry against LOCAL_NODE before cascading to public. Absorbs the
# transient connection-refused window caused by walker bursts opening
# many TCPs against rippled's accept path in a short span. Base × attempt
# gives 100ms / 200ms; jitter avoids thundering-herd re-sync on retry.
LOCAL_RETRY_ATTEMPTS = 3
LOCAL_RETRY_BASE_MS = 100
LOCAL_RETRY_JITTER_MS = 100

REQUEST_TIMEOUT_SECONDS = 20.0

logger = logging.getLogger(__name__)
_health = {"checked_at": 0.0, "ok": False, "reason": "uninitialized"}

# ── Connection pooling ────────────────────────────────────────────────
# One shared httpx.Client per URL, with keep-alive. max_keepalive kept
# modest so an idle process doesn't hold many sockets open; keepalive_expiry
# recycles them after 30s of idle. httpx.Client is thread-safe.
_HTTP_LIMITS = httpx.Limits(
    max_keepalive_connections=8,
    max_connections=16,
    keepalive_expiry=30.0,
)
_http_clients: dict[str, httpx.Client] = {}


def _client_for(url: str) -> httpx.Client:
    c = _http_clients.get(url)
    if c is None:
        c = httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS, limits=_HTTP_LIMITS)
        _http_clients[url] = c
    return c


def _post_rpc(url, req):
    """One JSON-RPC POST via the shared keep-alive client.
    Raises httpx.* on transport failure; returns xrpl-py Response on success.
    """
    payload = request_to_json_rpc(req)
    r = _client_for(url).post(url, json=payload)
    return json_to_response(r.json())


def _probe_local():
    try:
        resp = _post_rpc(LOCAL_NODE, ServerInfo())
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


def _local_backoff_seconds(attempt: int) -> float:
    """Backoff for attempt N (1-indexed, only called for N>=2 i.e. between
    retries). 100ms * (attempt-1) + jitter 0..100ms.
    attempt=2 → ~100-200ms; attempt=3 → ~200-300ms.
    """
    base_ms = LOCAL_RETRY_BASE_MS * (attempt - 1)
    jitter_ms = random.uniform(0, LOCAL_RETRY_JITTER_MS)
    return (base_ms + jitter_ms) / 1000.0


class XrplClient:
    """Drop-in for xrpl.clients.JsonRpcClient. .request(req) tries local
    first with retry-before-cascade; on health-fail, transport error, or
    a result-level node-health error, retries LOCAL_NODE up to
    LOCAL_RETRY_ATTEMPTS with jittered backoff before cascading through
    PUBLIC_NODES in order. Non-health application errors (e.g. actNotFound)
    return normally — those are valid answers, not a node problem.
    A cascade to public writes one walker_node_fallback row; a retry
    recovery (would-have-been fallback that succeeded on retry) does NOT
    write to walker_node_fallback — it's logged at INFO with the tag
    'local_retry_recovered'. Grep launchd_logs to count recoveries."""

    def __init__(self, walker_name="unknown"):
        self.walker_name = walker_name

    def _try_local_with_retry(self, req):
        """Try LOCAL_NODE up to LOCAL_RETRY_ATTEMPTS. Returns (resp, None)
        on success, or (None, last_failure_reason_str) on total local
        failure. On success after retries, emits an INFO log line for
        measurability."""
        last_reason = None
        for attempt in range(1, LOCAL_RETRY_ATTEMPTS + 1):
            try:
                resp = _post_rpc(LOCAL_NODE, req)
            except Exception as e:
                last_reason = f"unreachable:{type(e).__name__}"
                logger.debug(
                    "local_attempt_failed walker=%s attempt=%d/%d err=%s",
                    self.walker_name, attempt, LOCAL_RETRY_ATTEMPTS, last_reason,
                )
            else:
                if not _is_node_health_error(resp):
                    if attempt > 1:
                        logger.info(
                            "local_retry_recovered walker=%s attempt=%d",
                            self.walker_name, attempt,
                        )
                    return resp, None
                # result-level node-health error — retryable
                err = (resp.result or {}).get("error")
                last_reason = f"local_result_error:{err}"
                logger.debug(
                    "local_attempt_health_err walker=%s attempt=%d/%d err=%s",
                    self.walker_name, attempt, LOCAL_RETRY_ATTEMPTS, err,
                )
            # If not last attempt, sleep and try again
            if attempt < LOCAL_RETRY_ATTEMPTS:
                time.sleep(_local_backoff_seconds(attempt + 1))
        return None, last_reason

    def request(self, req):
        ok, reason = _get_health()
        if ok:
            resp, local_failure_reason = self._try_local_with_retry(req)
            if resp is not None:
                return resp
            # All local attempts failed — force fresh health probe next time
            _health["checked_at"] = 0.0
            _log_fallback(self.walker_name, local_failure_reason or "unreachable:Unknown")
        else:
            # Health cached as bad — one log row per call, same as before.
            _log_fallback(self.walker_name, reason)

        # Cascade to public
        last = None
        for url in PUBLIC_NODES:
            try:
                return _post_rpc(url, req)
            except Exception as e:
                last = e
                logger.warning("public_request_failed url=%s err=%s", url, e)
        raise last or RuntimeError("all xrpl endpoints failed")


def get_client(walker_name="unknown"):
    return XrplClient(walker_name)
