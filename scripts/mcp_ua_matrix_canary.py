#!/usr/bin/env python3
"""MCP UA-matrix regression canary.

Runs the same 16-client-class User-Agent matrix that the 2026-08-26
four-AI QUADFECTA audit used to prove the Cloudflare 1010 discrimination,
and alarms if any UA that should be allowed comes back with the 1010
"browser signature banned" body.

Why this exists:
  - CF Bot Fight / WAF Custom rules can be edited from the dashboard.
    A well-intentioned tweak elsewhere (e.g., a broader Managed Rule
    skip toggle, a new Custom Rule, or the WAF skip rule getting
    re-scoped) can silently reintroduce the 1010 block for the two
    UA strings we specifically carved out (Python-urllib, libwww-perl).
    Without a canary, we'd only find out on the next AI-agent audit —
    which is monthly at best and off-cadence to the ops timeline.
  - Success signal is walker_health row `mcp_ua_matrix_canary` staying
    green. answer_plausibility monitor pages on stale/red.

Design fence:
  - This canary sends 16 GET requests to https://mcp.xrpldashboard.com/mcp
    (unauthenticated; a bare GET is refused by design, which is the point).
  - Any UA that returns a body containing "Error 1010" OR "browser
    signature banned" OR HTTP status 403 with the CF error-page shape
    is a FAIL — that means the WAF skip rule leaked, reverted, or was
    re-scoped away from `http.host eq "mcp.xrpldashboard.com"`.
  - A PASS requires HTTP 406 *and* the FastMCP JSON-RPC body marker
    "Client must accept text/event-stream". Both, not either: the status
    alone could come from an intermediary, and that body can only be
    produced by FastMCP's streamable-http handler actually serving. So a
    pass proves BOTH that the UA was not WAF-banned (the request reached
    origin) and that MCP is alive.
  - Anything else — 404, 502, a 200 without the marker — is
    fail_unexpected. Until 2026-10-17 this canary probed /health, a path
    that has never existed, and treated the resulting 404 as a pass
    because the status was never compared. It would have stayed green with
    the server completely dead.
  - A transport-level error (timeout, DNS, TLS) is RETRIED once. If the
    retry also fails it is fail_transport, not a skip: a persistent
    timeout is a real outage. The retry is what keeps a single network
    flap from paging — same retry-before-negative rule as
    two_way_toml_verifier_walker.

Runs daily via launchd.
"""
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
import logging

import certifi

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db

logging.basicConfig(
    format="%(asctime)s [mcp_ua_matrix_canary] %(levelname)s %(message)s",
    level=logging.INFO,
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

WALKER_NAME = "mcp_ua_matrix_canary"
WALKER_CADENCE_SECONDS = 86400  # daily

MCP_PROBE_URL = "https://mcp.xrpldashboard.com/mcp"
REQUEST_TIMEOUT = 10

# A bare GET to the streamable-http endpoint is refused with 406 plus a
# FastMCP-specific JSON-RPC error body. Asserting both is what makes this a
# liveness probe rather than a "something answered" probe.
ALIVE_STATUS = 406
ALIVE_BODY_MARKER = "Client must accept text/event-stream"

# Retry a transport error once before calling it an outage, so a single
# network flap does not page. Matches the retry-before-negative rule.
TRANSPORT_RETRY_SLEEP_SECONDS = 1.0

# The 16 client-class UAs from the 2026-08-26 QUADFECTA external matrix.
# Two of these (Python-urllib, libwww-perl) were 1010-blocked pre-fix;
# after the 2026-08-27 WAF skip rule ships, all 16 must pass. Adding
# a UA here is safe — every UA in the list must NOT return 1010.
UA_MATRIX = [
    "Python-urllib/3.11",
    "libwww-perl/6.68",
    "python-requests/2.31.0",
    "curl/8.4.0",
    "Wget/1.21.4",
    "Go-http-client/1.1",
    "GPTBot/1.0",
    "ChatGPT-User/1.0",
    "ClaudeBot/1.0 (+https://www.anthropic.com/traffic)",
    "Anthropic-Agent/1.0",
    "PerplexityBot/1.0",
    "OAI-SearchBot/1.0",
    "Amazonbot/0.1",
    "GoogleOther/1.0",
    "Mozilla/5.0 (X11; Linux x86_64) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15.7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 "
    "Safari/605.1.15",
]

# CF 1010 signature strings. Presence of any in the response body =
# WAF skip rule is not covering this UA anymore.
CF_1010_MARKERS = ("Error 1010", "browser signature banned", "1010 IssueID")


def _fetch_once(ua: str):
    """One GET. Returns (status, body, transport_error_or_None). Never raises."""
    req = urllib.request.Request(
        MCP_PROBE_URL,
        headers={"User-Agent": ua, "Accept": "application/json"},
        method="GET",
    )
    ctx = ssl.create_default_context(cafile=certifi.where())
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT, context=ctx) as resp:
            return resp.status, resp.read(4096).decode("utf-8", errors="replace"), None
    except urllib.error.HTTPError as e:
        # Both the 406 we expect AND a CF 403 block arrive as HTTPError, so
        # the body still has to be read here.
        try:
            body = e.read(4096).decode("utf-8", errors="replace")
        except Exception:
            body = ""
        return e.code, body, None
    except (urllib.error.URLError, TimeoutError, ssl.SSLError, OSError) as e:
        return None, "", f"{type(e).__name__}: {e}"


def _probe_one(ua: str, fetcher=None, sleeper=None) -> dict:
    """Return {'ua', 'verdict', 'detail'}.

    verdict: 'pass' | 'fail_1010' | 'fail_unexpected' | 'fail_transport'
    fetcher/sleeper are injectable so the matrix is testable without network.
    """
    fetcher = _fetch_once if fetcher is None else fetcher
    sleeper = time.sleep if sleeper is None else sleeper

    status, body, terr = fetcher(ua)
    if terr is not None:
        sleeper(TRANSPORT_RETRY_SLEEP_SECONDS)
        status, body, terr = fetcher(ua)
    if terr is not None:
        return {
            "ua": ua,
            "verdict": "fail_transport",
            "detail": f"transport error twice: {terr}",
        }

    # 1010 first: a WAF ban is the thing this canary exists to catch, and it
    # must not be masked by the status check below.
    if any(marker in body for marker in CF_1010_MARKERS):
        return {
            "ua": ua,
            "verdict": "fail_1010",
            "detail": f"status={status} body-signature=1010 (WAF skip rule leaked)",
        }

    if status == ALIVE_STATUS and ALIVE_BODY_MARKER in body:
        return {
            "ua": ua,
            "verdict": "pass",
            "detail": f"status={status} fastmcp-alive",
        }

    return {
        "ua": ua,
        "verdict": "fail_unexpected",
        "detail": (f"status={status} (expected {ALIVE_STATUS} + FastMCP "
                   f"marker; got body[:80]={body[:80]!r})"),
    }


def run():
    db.write_walker_health_start(WALKER_NAME, cadence_seconds=WALKER_CADENCE_SECONDS)
    if not db.pg_available():
        log.error("DATABASE_URL not configured — exiting")
        db.write_walker_health_end(
            WALKER_NAME, ok=False, message="pg_unavailable at canary start"
        )
        sys.exit(1)

    results = []
    for ua in UA_MATRIX:
        result = _probe_one(ua)
        log.info(
            "%s → %s (%s)", ua[:40], result["verdict"], result["detail"]
        )
        results.append(result)
        time.sleep(0.25)  # gentle pacing, avoid tripping rate limits

    fails_1010 = [r for r in results if r["verdict"] == "fail_1010"]
    fails_unexp = [r for r in results if r["verdict"] == "fail_unexpected"]
    fails_trans = [r for r in results if r["verdict"] == "fail_transport"]
    fails = fails_1010 + fails_unexp + fails_trans
    passes = [r for r in results if r["verdict"] == "pass"]

    # Every fail_* verdict counts. Previously only fail_1010 did, which is
    # why a 404 from a nonexistent path read as a clean run.
    ok = len(fails) == 0
    summary = (
        f"probed {len(results)} UAs: "
        f"pass={len(passes)} fail_1010={len(fails_1010)} "
        f"fail_unexpected={len(fails_unexp)} "
        f"fail_transport={len(fails_trans)}"
    )
    if fails:
        blocked = ", ".join(f["ua"][:32] for f in fails)
        summary += f" | blocked: {blocked}"

    if ok:
        log.info("canary PASS: %s", summary)
    else:
        log.error("canary FAIL: %s", summary)

    db.write_walker_health_end(WALKER_NAME, ok=ok, message=summary)

    # BetterStack heartbeat — success-path ONLY. Visible-skip when the
    # URL isn't set (matches is_bot_canary discipline).
    if ok:
        url = os.environ.get("BETTERSTACK_MCP_UA_MATRIX_CANARY_URL")
        if url:
            try:
                ctx = ssl.create_default_context(cafile=certifi.where())
                with urllib.request.urlopen(url, timeout=10, context=ctx) as resp:
                    resp.read()
                log.info("betterstack ping ok")
            except Exception as e:  # noqa: BLE001 — ping is best-effort
                log.warning("betterstack ping failed (canary still PASS): %s", e)
        else:
            log.warning(
                "betterstack skip: URL not in environment "
                "(BETTERSTACK_MCP_UA_MATRIX_CANARY_URL) — external "
                "monitor will page within the 24h+2h grace window if "
                "this persists"
            )


if __name__ == "__main__":
    run()
