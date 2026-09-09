"""Public-route 200 canary — verify every public surface returns 200
with a body of sane size on a 15-minute cadence.

Charlie ruling 2026-09-07 evening: /methodology 500 sat undetected in
prod for ~7 days because no monitor GET'd the route after deploys.
This walker closes the gap.

Coverage:
  - PUBLIC_ROUTES (imported from app.py — sitemap-worth routes)
  - Agent-tier machine surfaces (llms.txt, agents.json, sitemap.xml,
    robots.txt, x402, openapi.json)
  - Signed-artifact well-known surfaces (snapshots/*, anchors.json)
  - Weekly/registry public routes (/thisweek, /thisweek.xml,
    /registry/taxonomy)
  - /check.json with a known address (Ripple's cold wallet is stable +
    on the ledger since ledger 3)

Sanity rules per route:
  - HTTP 200 required
  - Body >= route-specific minimum size (avoids empty-shell responses
    that render 200 but ship a broken template — the exact class of
    failure /methodology exhibited before the fix)
  - No exception during fetch (timeout, DNS, TLS)

Any failure → row inserted into walker_health with findings_count > 0
and a specific route in findings_json. The existing L1 pager reads
walker_health and escalates to Charlie's page within the hour.

Cadence: 15 min. Runs on Lenovo (systemd timer, per Charlie's ruling —
Lenovo has independent network path from Render's AWS, so a Render
outage doesn't blind the canary).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import time

import httpx


BASE_URL = os.environ.get("PUBLIC_CANARY_BASE", "https://xrpldashboard.com").rstrip("/")
TIMEOUT_S = 15.0

# Route → (path, minimum_body_bytes, must_contain_substring_or_none,
#         [optional] set of acceptable HTTP statuses — defaults to {200})
# Minimum body sizes are lower bounds — templates always render at least
# their nav + footer, so genuine 200 responses are much larger. The
# optional 4th field lets a route legitimately return 404 (empty-state
# routes like /thisweek before any edition ships) without paging.
ROUTES: list[tuple] = [
    # Public human routes (from PUBLIC_ROUTES in app.py, kept in sync manually)
    ("/",                                 5000,   "xrpldashboard"),
    ("/whales",                           10000,  "whale"),
    ("/tokens",                           10000,  "token"),
    ("/pools",                            10000,  "pool"),
    ("/mpts",                             5000,   "MPT"),
    ("/nfts",                             5000,   "NFT"),
    ("/rlusd",                            5000,   "RLUSD"),
    ("/cold-storage",                     5000,   "cold"),
    ("/price-data",                       3000,   None),
    ("/health",                           1000,   None),
    ("/about",                            5000,   "About"),
    ("/institutional",                    5000,   None),
    ("/security",                         2000,   None),
    ("/subprocessors",                    3000,   "subprocessor"),
    # /thisweek returns HTTP 404 when no edition is currently published
    # (Charlie ruling 2026-09-08: Sunday's draft is gated public until
    # the published_at_utc lands). Accept 404 explicitly so the canary
    # doesn't page on the legitimate empty-list state. Once Sunday's
    # edition ships, the 200 path renders full content — this override
    # remains harmless (200 still qualifies).
    ("/thisweek",                         200,    None,   {200, 404}),
    ("/registry/taxonomy",                10000,  "taxonomy"),
    ("/changes",                          1000,   "What&#39;s new"),
    ("/changes.xml",                      500,    "xrpldashboard"),
    ("/api/changes/strip.json",           200,    "validated_ledger_index"),
    ("/methodology",                      50000,  "Event-derived metrics undercount"),
    ("/coverage",                         3000,   None),
    ("/analytics",                        3000,   None),
    ("/regulation",                       5000,   None),
    ("/glossary",                         3000,   None),
    ("/learn",                            3000,   None),
    # Agent-tier / machine surfaces
    ("/llms.txt",                         2000,   "xrpldashboard"),
    ("/.well-known/agents.json",          1000,   "agents"),
    ("/.well-known/x402",                 300,    None),
    ("/sitemap.xml",                      500,    "sitemap"),
    ("/robots.txt",                       50,     None),
    ("/openapi.json",                     1000,   None),
    # /thisweek.xml is a valid empty-feed shell (~387b) until Sunday
    # ships the first edition. Threshold lowered so the empty-state
    # feed passes; content check kept — "rss" is in the RSS wrapper
    # regardless of item count.
    ("/thisweek.xml",                     200,    "rss"),
    # Signed-artifact well-known
    ("/.well-known/snapshots/pubkey.pem",         100,   "PUBLIC KEY"),
    ("/.well-known/snapshots/pubkey.json",        200,   "Ed25519"),
    ("/.well-known/snapshots/receipt_pubkey.pem", 100,   "PUBLIC KEY"),
    ("/.well-known/snapshots/receipt_pubkey.json", 200,  "Ed25519"),
    ("/.well-known/anchors.json",         100,   None),
    # /check.json with a known-good address (Ripple's genesis cold wallet)
    ("/check.json?q=rrrrrrrrrrrrrrrrrrrrrhoLvTp", 200,   "kind"),
]


def _probe_once(path: str) -> tuple[int | None, bytes, str | None]:
    """One HTTP GET. Returns (status_or_None, body, err_reason_or_None)."""
    url = BASE_URL + path
    try:
        resp = httpx.get(url, timeout=TIMEOUT_S,
                         headers={"User-Agent": "xrpldashboard-public-route-canary/1.0"},
                         follow_redirects=True)
        return resp.status_code, resp.content[:1024 * 1024], None
    except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as e:
        return None, b"", f"network_{type(e).__name__}"


def probe_one(path: str, min_bytes: int, must_contain: str | None,
              acceptable_statuses: set[int] | None = None) -> dict:
    """GET one route with a single retry on failure. Never raises.

    Retry rationale: /nfts, /analytics, /tokens can occasionally exceed
    15s on the first request under load (10s+ observed at commit time).
    A single 1s-delayed retry filters transient stalls that would
    otherwise page L1 with no real regression.

    acceptable_statuses defaults to {200}. Passing {200, 404} lets a
    route legitimately return 404 without paging — used for empty-state
    routes like /thisweek before any edition is published. When the
    accepted status is not 200, the body-size / substring checks are
    skipped (a 404 error page's body isn't meaningfully bounded)."""
    if acceptable_statuses is None:
        acceptable_statuses = {200}
    started = dt.datetime.now(dt.timezone.utc)
    for attempt in (1, 2):
        status, body, net_err = _probe_once(path)
        if net_err is None and status in acceptable_statuses:
            # Body-size + substring checks only apply on the primary
            # success status (200). Alternate legitimate statuses (404
            # for empty-state routes) pass on status alone.
            if status != 200:
                return {
                    "path": path, "status": status, "body_bytes": len(body),
                    "ok": True, "reason": f"ok_alt_status_{status}",
                    "attempt": attempt, "started_utc": started.isoformat(),
                }
            if len(body) >= min_bytes and (
                must_contain is None or must_contain in body.decode("utf-8", errors="replace")
            ):
                return {
                    "path": path, "status": status, "body_bytes": len(body),
                    "ok": True, "reason": "ok", "attempt": attempt,
                    "started_utc": started.isoformat(),
                }
        if attempt == 1:
            time.sleep(1.0)
    if net_err is not None:
        return {
            "path": path, "status": None, "body_bytes": 0, "ok": False,
            "reason": net_err, "attempt": 2,
            "started_utc": started.isoformat(),
        }

    body_len = len(body)
    if status not in acceptable_statuses:
        reason = f"http_{status}"
    elif body_len < min_bytes:
        reason = f"body_short_{body_len}_lt_{min_bytes}"
    else:
        reason = f"missing_substring_{(must_contain or '')[:40]}"
    return {
        "path": path, "status": status, "body_bytes": body_len,
        "ok": False, "reason": reason, "attempt": 2,
        "started_utc": started.isoformat(),
    }


def run_walker() -> tuple[int, int, list[dict]]:
    """Probe every route. Returns (ok_count, fail_count, failing_results)."""
    ok = 0
    fail = 0
    failures: list[dict] = []
    results: list[dict] = []
    for entry in ROUTES:
        # 3-tuple = default acceptable_statuses={200}; 4-tuple allows
        # a per-route override like {200, 404} for empty-state routes.
        if len(entry) == 3:
            path, min_bytes, must_contain = entry
            acceptable_statuses = None
        else:
            path, min_bytes, must_contain, acceptable_statuses = entry
        r = probe_one(path, min_bytes, must_contain, acceptable_statuses)
        results.append(r)
        if r["ok"]:
            ok += 1
        else:
            fail += 1
            failures.append(r)
    return ok, fail, failures, results


def _write_walker_health(ok: int, fail: int, failures: list[dict]) -> None:
    """Write one walker_health row per run. Failing routes go into
    findings_json; the row's findings_count = fail is what the L1 pager
    watches. Falls back to stderr if PG is unavailable."""
    try:
        import db
        if not db.pg_available():
            print(f"[public_route_canary] pg not available; skipping walker_health write",
                  file=sys.stderr, flush=True)
            return
        with db.pg_connect() as conn:
            with conn.cursor() as cur:
                # walker_health schema (per db.py): walker_name PK,
                # last_run_started/completed, last_run_ok, last_run_message,
                # last_success_at/failure_at, consecutive_failures,
                # cadence_seconds, findings_count. Failing-route details
                # go into last_run_message (JSON string) since there's no
                # findings_json column today.
                msg = json.dumps({
                    "ok": ok, "fail": fail,
                    "failures": [(f["path"], f["reason"]) for f in failures],
                })
                now = dt.datetime.now(dt.timezone.utc)
                if fail == 0:
                    cur.execute(
                        """
                        INSERT INTO walker_health
                            (walker_name, last_run_started, last_run_completed,
                             last_run_ok, last_run_message, last_success_at,
                             consecutive_failures, cadence_seconds, findings_count)
                        VALUES (%s, %s, %s, TRUE, %s, %s, 0, 900, 0)
                        ON CONFLICT (walker_name) DO UPDATE
                        SET last_run_started = EXCLUDED.last_run_started,
                            last_run_completed = EXCLUDED.last_run_completed,
                            last_run_ok = TRUE,
                            last_run_message = EXCLUDED.last_run_message,
                            last_success_at = EXCLUDED.last_success_at,
                            consecutive_failures = 0,
                            cadence_seconds = 900,
                            findings_count = 0
                        """,
                        ("public_route_200_canary", now, now, msg, now),
                    )
                else:
                    cur.execute(
                        """
                        INSERT INTO walker_health
                            (walker_name, last_run_started, last_run_completed,
                             last_run_ok, last_run_message, last_failure_at,
                             cadence_seconds, findings_count)
                        VALUES (%s, %s, %s, FALSE, %s, %s, 900, %s)
                        ON CONFLICT (walker_name) DO UPDATE
                        SET last_run_started = EXCLUDED.last_run_started,
                            last_run_completed = EXCLUDED.last_run_completed,
                            last_run_ok = FALSE,
                            last_run_message = EXCLUDED.last_run_message,
                            last_failure_at = EXCLUDED.last_failure_at,
                            consecutive_failures = walker_health.consecutive_failures + 1,
                            cadence_seconds = 900,
                            findings_count = EXCLUDED.findings_count
                        """,
                        ("public_route_200_canary", now, now, msg, now, fail),
                    )
                conn.commit()
    except Exception as e:
        print(f"[public_route_canary] walker_health write failed: {type(e).__name__}: {e}",
              file=sys.stderr, flush=True)


def main() -> int:
    ok, fail, failures, results = run_walker()
    # Render a compact table for stdout / logs
    print(f"[public_route_canary] {ok} ok, {fail} failing @ {dt.datetime.now(dt.timezone.utc).isoformat()}")
    print(f"  {'PATH':<60} {'HTTP':<6} {'BYTES':<8} REASON")
    for r in results:
        marker = " " if r["ok"] else "!"
        print(f"  {marker}{r['path']:<59} {str(r['status'] or '-'):<6} {r['body_bytes']:<8} {r['reason']}")
    _write_walker_health(ok, fail, failures)
    return 0 if fail == 0 else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
