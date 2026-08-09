#!/usr/bin/env python3
"""Deterministic coverage checks: does xrpldashboard reflect what XRPL is showing?

Read-only. Compares authoritative XRPL sources against what xrpldashboard
serves (live HTML and DB snapshots). Prints a short structured report.

Exit code 0 = all green, 1 = anything flagged.

Usage:
  set -a; source ~/.config/xrpldashboard/env; set +a
  venv/bin/python ops/coverage_check.py
"""
from __future__ import annotations
import base64
import json
import os
import re
import sys
import time

import httpx

import db
import amendments_state

PROD = "https://xrpldashboard.com"
USER_AGENT = "xrpldashboard-coverage-check/0.1"

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NAMED_ACCOUNTS_PATH = os.path.join(HERE, "named_accounts.json")
WHALE_XRP_THRESHOLD = 100_000

RIPPLED_NODES = ("https://s1.ripple.com:51234", "https://s2.ripple.com:51234")


def _rpc(method: str, params: dict, timeout: int = 15):
    body = {"method": method, "params": [params]}
    last_err = None
    for url in RIPPLED_NODES:
        try:
            r = httpx.post(url, json=body, timeout=timeout, headers={"User-Agent": USER_AGENT})
            r.raise_for_status()
            j = r.json()
            return j.get("result") or {}
        except Exception as e:
            last_err = e
    raise RuntimeError(f"all rippled nodes failed: {last_err!r}")


def _http_get(url: str, timeout: int = 15) -> bytes:
    r = httpx.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout, follow_redirects=True)
    r.raise_for_status()
    return r.content


def _http_json(url: str, timeout: int = 15):
    return json.loads(_http_get(url, timeout))


def _decode_unl_manifest(manifest_b64: str):
    """Returns the inner UNL JSON (with validator list) from a v1/v2 manifest."""
    raw = base64.b64decode(manifest_b64)
    # v2 UNL payloads are JSON-shaped directly; v1 wraps the blob.
    try:
        return json.loads(raw)
    except Exception:
        return None


def fetch_live_unl(url: str):
    """Returns list of validator pubkeys from the publisher URL.

    Each UNL publisher serves a manifest with a base64 payload listing validators.
    Parse defensively across v1 and v2 shapes."""
    blob = _http_json(url, timeout=20)
    inner_b64 = blob.get("blob")
    if not inner_b64:
        return [], blob.get("sequence")
    inner = _decode_unl_manifest(inner_b64)
    if not inner:
        return [], blob.get("sequence")
    seq = inner.get("sequence") or blob.get("sequence")
    vlist = inner.get("validators") or []
    pubkeys = []
    for v in vlist:
        pk = v.get("validation_public_key") or v.get("public_key")
        if pk:
            pubkeys.append(pk)
    return pubkeys, seq


# ---- check 1: amendments freshness ---------------------------------------
def check_amendments():
    findings = []
    state = amendments_state.fetch_amendments_state()
    if not state.get("ok"):
        findings.append(("RED", "amendments", "fetch_amendments_state returned not-ok — node unreachable"))
        return findings, {}

    node_enabled_count = state.get("enabled_count") or 0
    in_flight_now = {a["name"] for a in state.get("in_flight", []) if a.get("name")}
    in_dev_now = {a["name"] for a in state.get("in_development", []) if a.get("name")}
    superseded_now = {a["name"] for a in state.get("superseded", []) if a.get("name")}
    unrecognized = state.get("unrecognized_enabled_count") or 0

    cb = int(time.time() * 1000)
    html = _http_get(f"{PROD}/amendments?cb={cb}", timeout=20).decode("utf-8", "replace")

    # Page-side hero-count gut check: look for "N enabled" near the hero
    m = re.search(r">\s*(\d{2,3})\s*<[^>]*>\s*</?\w+>\s*[^<]*enabled", html, re.IGNORECASE)
    if not m:
        m = re.search(r"(\d{2,3})\s+enabled", html, re.IGNORECASE)
    page_enabled = int(m.group(1)) if m else None

    if page_enabled is not None and abs(page_enabled - node_enabled_count) > 1:
        findings.append((
            "YELLOW", "amendments",
            f"hero-count mismatch: page shows ~{page_enabled} enabled, node ledger reports {node_enabled_count}"
        ))

    # Name-by-name check for the categories we DO have names for.
    named_now = in_flight_now | in_dev_now | superseded_now
    missing_from_page = {n for n in named_now if n and n not in html}
    if missing_from_page:
        findings.append((
            "YELLOW", "amendments",
            f"{len(missing_from_page)} known-named amendment(s) not on /amendments: "
            + ", ".join(sorted(missing_from_page))
        ))

    if unrecognized > 0:
        findings.append((
            "YELLOW", "amendments",
            f"{unrecognized} enabled amendment(s) unrecognized by responding node — manual review on /amendments may be due"
        ))

    summary = {
        "enabled_count_node": node_enabled_count,
        "in_flight_count_node": len(in_flight_now),
        "in_development_count_node": len(in_dev_now),
        "superseded_count_node": len(superseded_now),
        "unrecognized_enabled_count": unrecognized,
        "page_hero": page_enabled,
        "named_amendments_missing_from_page": len(missing_from_page),
    }
    if not findings:
        findings.append((
            "GREEN", "amendments",
            f"{node_enabled_count} enabled on-chain, all {len(named_now)} named (in-flight/in-dev/superseded) present on /amendments"
        ))
    return findings, summary


# ---- check 2: validator UNL freshness ------------------------------------
UNL_SOURCES = {
    "ripple": "https://vl.ripple.com/",
    "xrplf":  "https://unl.xrpl.foundation/",
}


def check_unl():
    findings = []
    summary = {}
    with db.pg_connect() as con:
        cur = con.cursor()
        for source, url in UNL_SOURCES.items():
            try:
                live_pubkeys, live_seq = fetch_live_unl(url)
            except Exception as e:
                findings.append(("RED", f"unl/{source}", f"fetch failed: {e!r}"))
                continue
            cur.execute(
                "SELECT payload->'validators', payload->>'sequence', fetched_at_iso "
                "FROM unl_snapshots WHERE source=%s ORDER BY fetched_at_iso DESC LIMIT 1",
                (source,),
            )
            row = cur.fetchone()
            if not row:
                findings.append(("YELLOW", f"unl/{source}", "no snapshot row in DB yet"))
                continue
            db_validators, db_seq, db_when = row
            db_pubkeys = set()
            for v in (db_validators or []):
                pk = v.get("pubkey") or v.get("validation_public_key") or v.get("public_key")
                if pk:
                    db_pubkeys.add(pk)
            live_set = set(live_pubkeys)
            added = live_set - db_pubkeys
            removed = db_pubkeys - live_set
            summary[source] = {
                "live_seq": live_seq, "db_seq": db_seq,
                "live_count": len(live_set), "db_count": len(db_pubkeys),
                "added_since_snapshot": len(added),
                "removed_since_snapshot": len(removed),
                "db_fetched_at": str(db_when),
            }
            if added or removed:
                detail = []
                if added: detail.append(f"+{len(added)} new validators")
                if removed: detail.append(f"-{len(removed)} dropped")
                findings.append(("YELLOW", f"unl/{source}", "snapshot stale: " + ", ".join(detail)))
            elif str(live_seq) != str(db_seq) and live_seq is not None:
                findings.append(("YELLOW", f"unl/{source}", f"sequence drifted (live={live_seq} db={db_seq}) — same validator set"))
            else:
                findings.append(("GREEN", f"unl/{source}", f"snapshot current ({len(db_pubkeys)} validators, seq={db_seq})"))
    return findings, summary


# ---- check 3: whales (events freshness + render sanity) ------------------
def check_whales():
    findings = []
    with db.pg_connect() as con:
        cur = con.cursor()
        cur.execute("SELECT MAX(ts), COUNT(*) FROM events WHERE type='large_xfer' AND ts > extract(epoch from now())::bigint - 86400")
        last_ts, n_24h = cur.fetchone()
        cur.execute("""
          SELECT COUNT(DISTINCT addr) FROM (
            SELECT from_addr AS addr FROM events WHERE type='large_xfer' AND ts > extract(epoch from now())::bigint - 7*86400
            UNION
            SELECT to_addr AS addr FROM events WHERE type='large_xfer' AND ts > extract(epoch from now())::bigint - 7*86400
          ) s
        """)
        n_7d_addrs = cur.fetchone()[0]

    cb = int(time.time() * 1000)
    try:
        html = _http_get(f"{PROD}/whales?cb={cb}", timeout=20).decode("utf-8", "replace")
    except Exception as e:
        findings.append(("RED", "whales", f"could not curl /whales: {e!r}"))
        return findings, {}

    # Count distinct r-addresses rendered on the page (loose proxy for "rendered")
    page_addrs = set(re.findall(r"\br[1-9A-HJ-NP-Za-km-z]{24,34}\b", html))
    summary = {
        "events_in_24h": int(n_24h or 0),
        "distinct_addrs_in_7d_events": int(n_7d_addrs or 0),
        "addresses_rendered_on_page": len(page_addrs),
        "last_large_xfer_ts": last_ts,
        "whale_threshold_xrp": WHALE_XRP_THRESHOLD,
    }
    if (n_24h or 0) == 0:
        findings.append(("YELLOW", "whales", "no large_xfer events captured in last 24h — xrpl_stream may be stale"))
    if len(page_addrs) == 0:
        findings.append(("YELLOW", "whales", "/whales rendered 0 r-addresses — page may be broken or cached cold"))
    if not findings:
        findings.append(("GREEN", "whales", f"{n_24h} whale events in 24h, {len(page_addrs)} addresses rendered on /whales"))
    return findings, summary


# ---- check 4: AMM pools (snapshot freshness + page sanity) ---------------
def check_amm_pools():
    findings = []
    with db.pg_connect() as con:
        cur = con.cursor()
        cur.execute("SELECT MAX(snapshot_ts) FROM amm_ranked_pools")
        latest_ts = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*), COUNT(tvl_usd) FROM amm_ranked_pools WHERE snapshot_ts=%s", (latest_ts,))
        n_pools, n_with_tvl = cur.fetchone()
        cur.execute("SELECT pair, tvl_usd FROM amm_ranked_pools WHERE snapshot_ts=%s ORDER BY tvl_usd DESC NULLS LAST LIMIT 1", (latest_ts,))
        top_row = cur.fetchone()
        top_pair, top_tvl = (top_row if top_row else (None, None))

    snapshot_age_h = (time.time() - (latest_ts or 0)) / 3600 if latest_ts else None

    cb = int(time.time() * 1000)
    try:
        html = _http_get(f"{PROD}/pools?cb={cb}", timeout=20).decode("utf-8", "replace")
    except Exception as e:
        findings.append(("RED", "amm_pools", f"could not curl /pools: {e!r}"))
        return findings, {}

    top_pair_on_page = bool(top_pair and top_pair in html)
    summary = {
        "latest_snapshot_ts": latest_ts,
        "snapshot_age_hours": round(snapshot_age_h, 2) if snapshot_age_h is not None else None,
        "pools_in_snapshot": int(n_pools or 0),
        "pools_with_tvl_usd": int(n_with_tvl or 0),
        "top_pool_pair": top_pair,
        "top_pool_tvl_usd": top_tvl,
        "top_pool_visible_on_page": top_pair_on_page,
    }
    if snapshot_age_h is None or snapshot_age_h > 8:
        findings.append(("YELLOW", "amm_pools", f"latest snapshot is {snapshot_age_h:.1f}h old (cadence is 4h)" if snapshot_age_h else "no snapshot ever"))
    if top_pair and not top_pair_on_page:
        findings.append(("YELLOW", "amm_pools", f"top pool '{top_pair}' (TVL ${top_tvl:,.0f}) is not visible on /pools"))
    if (n_pools or 0) < 50:
        findings.append(("YELLOW", "amm_pools", f"latest snapshot has only {n_pools} pools — expected 100+"))
    if not findings:
        findings.append(("GREEN", "amm_pools", f"{n_pools} pools in snapshot ({n_with_tvl} priced), top '{top_pair}' shown on page"))
    return findings, summary


# ---- check 5: MPTs (snapshot count vs on-chain reality) ------------------
def check_mpts():
    findings = []
    with db.pg_connect() as con:
        cur = con.cursor()
        cur.execute("SELECT payload->'total', payload->'walk_complete', written_at FROM mpt_snapshot ORDER BY written_at DESC LIMIT 1")
        row = cur.fetchone()
    if not row:
        findings.append(("YELLOW", "mpts", "no mpt_snapshot rows in DB"))
        return findings, {}
    db_total, walk_complete, written_at = row
    snapshot_age_h = (time.time() - (written_at or 0)) / 3600 if written_at else None

    cb = int(time.time() * 1000)
    try:
        html = _http_get(f"{PROD}/mpts?cb={cb}", timeout=20).decode("utf-8", "replace")
    except Exception as e:
        findings.append(("RED", "mpts", f"could not curl /mpts: {e!r}"))
        return findings, {}

    # Try to find a hero count on the page
    m = re.search(r">\s*(\d{2,5})\s*<[^>]*>\s*</?\w+>\s*[^<]*MPT", html, re.IGNORECASE)
    if not m:
        m = re.search(r"(\d{2,5})\s+MPT", html, re.IGNORECASE)
    page_count = int(m.group(1)) if m else None

    summary = {
        "db_total_mpts": db_total,
        "walk_complete": walk_complete,
        "snapshot_age_hours": round(snapshot_age_h, 2) if snapshot_age_h is not None else None,
        "page_count_extracted": page_count,
    }
    if snapshot_age_h is None or snapshot_age_h > 36:
        findings.append(("YELLOW", "mpts", f"snapshot {snapshot_age_h:.1f}h old (cadence is daily)" if snapshot_age_h else "no snapshot"))
    if walk_complete is False:
        findings.append(("YELLOW", "mpts", "latest walk reported walk_complete=False — partial enumeration"))
    if page_count is not None and db_total is not None and abs(page_count - int(db_total)) > 2:
        findings.append(("YELLOW", "mpts", f"page hero ~{page_count} differs from DB {db_total}"))
    if not findings:
        findings.append(("GREEN", "mpts", f"{db_total} MPTs in snapshot, walk_complete={walk_complete}, page-hero={page_count}"))
    return findings, summary


# ---- check 6: named_accounts (each entry still funded on-chain) ----------
def check_named_accounts():
    findings = []
    try:
        with open(NAMED_ACCOUNTS_PATH) as f:
            named = json.load(f)
    except Exception as e:
        findings.append(("RED", "named_accounts", f"could not read named_accounts.json: {e!r}"))
        return findings, {}

    addrs = list(named.keys())
    summary = {"total_entries": len(addrs), "verified_active": 0, "dead": [], "errors": 0}
    for addr in addrs:
        try:
            res = _rpc("account_info", {"account": addr, "ledger_index": "validated"})
        except Exception:
            summary["errors"] += 1
            continue
        if res.get("error") == "actNotFound":
            label = (named[addr].get("name") if isinstance(named.get(addr), dict) else None) or "?"
            summary["dead"].append({"addr": addr, "label": label})
        elif res.get("account_data"):
            summary["verified_active"] += 1
        else:
            summary["errors"] += 1

    if summary["dead"]:
        for d in summary["dead"]:
            findings.append(("YELLOW", "named_accounts", f"dead address: {d['addr']} ({d['label']}) — actNotFound"))
    if summary["errors"] > 0:
        findings.append(("YELLOW", "named_accounts", f"{summary['errors']}/{len(addrs)} entries unverifiable (RPC errors)"))
    if not findings:
        findings.append(("GREEN", "named_accounts", f"all {summary['verified_active']}/{len(addrs)} named accounts verified active on-chain"))
    return findings, summary


# ---- main ----------------------------------------------------------------
def main():
    all_findings = []
    print("=== xrpldashboard coverage check ===")
    print(f"prod = {PROD}")
    print()

    checks = (
        ("amendments", check_amendments),
        ("validator UNL", check_unl),
        ("whales", check_whales),
        ("amm pools", check_amm_pools),
        ("MPTs", check_mpts),
        ("named accounts", check_named_accounts),
    )
    for label, fn in checks:
        print(f"--- {label} ---")
        sys.stdout.flush()
        try:
            findings, summary = fn()
        except Exception as e:
            findings = [("RED", label, f"check raised: {e!r}")]
            summary = {}
        for sev, area, msg in findings:
            print(f"  [{sev}] {area}: {msg}")
        if summary:
            print(f"  details: {json.dumps(summary, default=str)}")
        all_findings.extend(findings)
        print()

    reds = [f for f in all_findings if f[0] == "RED"]
    yellows = [f for f in all_findings if f[0] == "YELLOW"]
    print(f"=== summary: {len(reds)} RED, {len(yellows)} YELLOW, {len(all_findings) - len(reds) - len(yellows)} GREEN ===")
    sys.exit(0 if not (reds or yellows) else 1)


if __name__ == "__main__":
    main()
