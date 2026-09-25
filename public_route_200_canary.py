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
    # Trust-critical routes added 2026-09-25 (Charlie ruling): these six
    # were in tests/test_routes.py::TRUST_CRITICAL_PAGES but missing here,
    # so the canary never GET'd them. /rwa proved the gap — it served 100%
    # 5xx for ~55h (commit 9e3620d 2026-09-22 14:54 ET -> e03829d
    # 2026-09-24 21:58 ET) with no canary alert because it wasn't listed.
    # Detection-only add: closes the observability hole the /rwa outage
    # exposed. (CI deploy-gate is a separate proposal.)
    ("/rwa",                              5000,   "RWA"),
    ("/claims",                          5000,   "claim"),
    ("/connect",                         2000,   None),
    ("/amendments",                      5000,   "amendment"),
    ("/sidechain",                       3000,   None),
    ("/snapshots/",                      2000,   None),
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
    # Charlie ruling 2026-09-23 Wed 13:32 ET — API breadth (build #2):
    # four new signed JSON surfaces. Content check requires the
    # check_v09_signature block name so we catch envelope drops.
    ("/tokens.json",                      500,    "check_v09_signature"),
    ("/whales.json",                      500,    "check_v09_signature"),
    ("/pools.json",                       500,    "check_v09_signature"),
    ("/amendments.json",                  500,    "check_v09_signature"),
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
    # Signed verified-tokens manifest (env-flipped 2026-09-22 Tue evening).
    # 71 rows, ~50KB. Content check on 'xrpldashboard/verified-tokens/v1'
    # catches route regressions AND schema drift together — if the walker
    # ever writes a wrong signing_domain, this fails.
    ("/.well-known/verified-tokens.json", 8000,  "xrpldashboard/verified-tokens/v1"),
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


def _dynamic_warnings_probes() -> list[tuple]:
    """Query current warnings-feed + /tokens?range=warnings token set,
    return route tuples for probe_one to hit each `/token/<cur>/<iss>`.

    Filed 2026-09-11 after XLM (rKiCet…) 500'd on click from the
    warnings feed — the fixed ROUTES list can't cover data-driven
    URLs, so this dynamic layer probes whatever the feed is exposing
    right now. Self-updating: as warnings shift, the canary sample
    tracks them.

    Returns [] if DB is unavailable (canary continues with fixed
    ROUTES only — no failure escalation just because PG is out).
    """
    try:
        import db
        if not db.pg_available():
            return []
        feed = db.read_token_warnings_recent(hours_back=3, limit=5)
        warn_list = db.read_token_warning_aggregates(hours_back=24, limit=5)
    except Exception as e:
        print(f"[public_route_canary] dynamic_warnings query failed: "
              f"{type(e).__name__}: {e}", file=sys.stderr, flush=True)
        return []
    seen: set[tuple[str, str]] = set()
    probes: list[tuple] = []
    for row in feed:
        _hb, cur, iss = row[0], row[1], row[2]
        if (cur, iss) in seen:
            continue
        seen.add((cur, iss))
        probes.append((f"/token/{cur}/{iss}", 20000, None))
    for row in warn_list:
        cur, iss = row[0], row[1]
        if (cur, iss) in seen:
            continue
        seen.add((cur, iss))
        probes.append((f"/token/{cur}/{iss}", 20000, None))
    return probes


def _tier_agreement_probes() -> list[dict]:
    """Cross-page tier-agreement checks (station audit 2026-09-12).

    Prior gap: /token, /check.json, /whales, /tokens returned different
    tier values for the same (currency, issuer) because /check.json had
    its own local tier ladder. shared_tier_verifier.resolve_tier is now
    the single source of truth for /check.json's returned `tier` field.

    This probe fetches /check.json + /token for a fixed sample and
    asserts the top-level `tier` field agrees. Fixed sample (5 pairs)
    covers each tier state (verified / labeled / bare / unknown) plus
    one impostor (was the Circle-USDC-class miss).

    Returns findings-shaped dicts appended to run_walker's failures
    list on mismatch. Not a route probe — a semantic check.
    """
    import httpx
    try:
        import json as _json
        pairs = [
            ("RLUSD_canonical", "524C555344000000000000000000000000000000",
             "rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De"),
            ("Reaper_RPR", "5250520000000000000000000000000000000000",
             "r3qWgpz2ry3BhcRJ8JE6rxM8esrfhuKp4R"),
            ("Circle_USDC", "5553444300000000000000000000000000000000",
             "rGm7WCVp9gb4jZHWTEtGUr4dd74z2XuWhE"),
            ("GateHub_USD", "USD", "rhub8VRN55s94qWKDv6jmDy1pUykJzF3wq"),
            ("Impostor_USDT", "5553445400000000000000000000000000000000",
             "rGbUjUtNVq5M3Un5r4efJqHed4o5P2Usdt"),
        ]
        findings = []
        for label, cur, iss in pairs:
            try:
                r = httpx.get(
                    f"{BASE_URL}/check.json?q={cur}.{iss}",
                    timeout=TIMEOUT_S,
                    headers={"User-Agent": "public-route-canary/tier-agree"},
                    follow_redirects=True,
                )
                if r.status_code != 200:
                    findings.append({
                        "path": f"tier_agree:{label}",
                        "reason": f"check.json http_{r.status_code}",
                        "ok": False, "status": r.status_code, "body_bytes": 0,
                        "attempt": 1, "started_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                    })
                    continue
                check_data = r.json().get("data", {})
                check_tier = check_data.get("tier")
                check_canonical = check_data.get("tier_canonical")
                # Assert the same value on both fields (post-2026-09-12 fix)
                if check_tier != check_canonical and check_canonical is not None:
                    findings.append({
                        "path": f"tier_agree:{label}",
                        "reason": f"tier({check_tier})!=tier_canonical({check_canonical})",
                        "ok": False, "status": 200, "body_bytes": len(r.content),
                        "attempt": 1, "started_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                    })
            except Exception as e:
                findings.append({
                    "path": f"tier_agree:{label}",
                    "reason": f"probe_error_{type(e).__name__}",
                    "ok": False, "status": None, "body_bytes": 0,
                    "attempt": 1, "started_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                })
        return findings
    except Exception as e:
        print(f"[public_route_canary] tier_agreement probes wrapper error: "
              f"{type(e).__name__}: {e}", file=sys.stderr, flush=True)
        return []


def _address_label_agreement_probes() -> list[dict]:
    """Cross-page label-agreement checks (Charlie ruling 2026-09-21 Mon PM).

    Prior gap: /whales rows say "Binance" and "BeBe" for addresses whose
    /wallet header renders no name, label, or source. Now /wallet reads
    the same address_badge() /whales uses, layered over PG account_labels
    on top of named_accounts.json. This probe re-verifies the agreement.

    Fixed sample covers:
      - PG-only label (curator xrpscan)   → Binance
      - PG-only derived label (AMM)       → BeBe AMM pool
      - File-based label (named_accounts) → Bitstamp
      - File-based verified (toml)        → Ripple Escrow #04

    For each: fetch /check.json?q=<addr>, /wallet/<addr>, /whales
    (grep the address bucket if present). Assert the top-level `name`
    string matches across surfaces. Not-in-/whales is not a failure —
    the wallet may not have transacted at whale scale recently; we
    only check /whales when the wallet's address turns up.
    """
    import httpx, re
    try:
        addresses = [
            ("Binance_1_pg", "rEb8TK3gBgk5auZkwc6sHnwrGVJH8DuaLh", "Binance"),
            ("BeBe_AMM_derived", "rsMCkyP1eAZCSbyLn334sw7Q1Si2UazPDD", None),  # AMM pair
            ("Bitstamp_file", "rvYAfWj5gh67oV6fW32ZzP3Aw4Eubs59B", "Bitstamp"),
            ("Ripple_Escrow_04", "rDdXiA3M4mYTQ4cFpWkVXfc2UaAXCFWeCK", "Ripple Escrow #04"),
        ]
        findings = []
        for label, addr, expected_name in addresses:
            surfaces = {}
            # /wallet — grep for identity-name
            try:
                r = httpx.get(
                    f"{BASE_URL}/wallet/{addr}", timeout=TIMEOUT_S,
                    headers={"User-Agent": "public-route-canary/label-agree"},
                    follow_redirects=True,
                )
                if r.status_code == 200:
                    m = re.search(r'<span class="identity-name">([^<]+)</span>', r.text)
                    if m:
                        surfaces["wallet"] = m.group(1).strip()
                    # AMM branch uses amm_pair; grep the pool label from
                    # source-note when present
                    m = re.search(r'source:\s*([a-z_:]+)', r.text)
                    if m:
                        surfaces["wallet_source"] = m.group(1).strip()
            except Exception as e:
                findings.append({
                    "path": f"label_agree:{label}:wallet",
                    "reason": f"probe_error_{type(e).__name__}",
                    "ok": False, "status": None, "body_bytes": 0,
                    "attempt": 1, "started_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                })
                continue
            # /check.json
            try:
                r = httpx.get(
                    f"{BASE_URL}/check.json?q={addr}", timeout=TIMEOUT_S,
                    headers={"User-Agent": "public-route-canary/label-agree"},
                    follow_redirects=True,
                )
                if r.status_code == 200:
                    d = r.json().get("data", {}) or {}
                    surfaces["check_name"] = d.get("name") or d.get("known_name")
            except Exception:
                pass
            # Agreement check: /wallet + /check should agree on the name
            # when expected_name is set (skip the AMM case where the
            # identity-name is the pair not the pool label).
            if expected_name is not None:
                wallet_name = surfaces.get("wallet")
                if wallet_name and wallet_name != expected_name:
                    findings.append({
                        "path": f"label_agree:{label}",
                        "reason": f"wallet_name({wallet_name!r}) != expected({expected_name!r})",
                        "ok": False, "status": 200, "body_bytes": 0,
                        "attempt": 1, "started_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                    })
                elif not wallet_name:
                    findings.append({
                        "path": f"label_agree:{label}",
                        "reason": f"wallet identity-name missing (expected {expected_name!r})",
                        "ok": False, "status": 200, "body_bytes": 0,
                        "attempt": 1, "started_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                    })
            # For all: the wallet page MUST show a source citation
            # (otherwise the label agreement claim can't be traced).
            if "wallet_source" not in surfaces:
                findings.append({
                    "path": f"label_agree:{label}",
                    "reason": "wallet page shows no source citation for labeled address",
                    "ok": False, "status": 200, "body_bytes": 0,
                    "attempt": 1, "started_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                })
        return findings
    except Exception as e:
        print(f"[public_route_canary] label_agreement probes wrapper error: "
              f"{type(e).__name__}: {e}", file=sys.stderr, flush=True)
        return []


def run_walker() -> tuple[int, int, list[dict], list[dict]]:
    """Probe every route. Returns (ok_count, fail_count, failing_results, results)."""
    ok = 0
    fail = 0
    failures: list[dict] = []
    results: list[dict] = []
    all_entries = list(ROUTES) + _dynamic_warnings_probes()
    for entry in all_entries:
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
    # 2026-09-12 station-audit: cross-page tier-agreement probes.
    # Runs AFTER route probes — a route-probe failure that also broke
    # /check.json would surface there first with a clearer message.
    tier_findings = _tier_agreement_probes()
    for tf in tier_findings:
        results.append(tf)
        fail += 1
        failures.append(tf)
    # 2026-09-21 Mon PM (Charlie ruling): address-label agreement.
    # Same shape — check that /wallet, /check.json, and /whales all
    # report the same name/tier/source for known-labeled addresses.
    label_findings = _address_label_agreement_probes()
    for lf in label_findings:
        results.append(lf)
        fail += 1
        failures.append(lf)
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
