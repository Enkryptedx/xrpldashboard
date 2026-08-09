#!/usr/bin/env python3
"""L2 inspector — nightly LIVE-site verification tier.

Runs on Lenovo every night via systemd timer. Fetches the PRODUCTION site
(never the test client) and re-executes the Red Team #1 (Grok 2026-08-04)
checklist against what actually renders. Silent on green; Telegram on
deviation; weekly heartbeat so the inspector's own silence is visible.

Design law (from project_autonomous_oversight_tiers_plan.md):
  1. Read-only against prod. This process observes; it never writes,
     never mutates, never remediates. Its only side effect is Telegram.
  2. Weekly alive+all-green heartbeat. Silence from the inspector is
     itself an alarm — the who-watches-the-watchman answer for L2.
     Runs Monday 09:00-10:00 ET (differentiates from L1's Sunday
     heartbeat window).

Checks (v1 — subset per amendment 2026-08-08 to keep the build shippable):
  A. claims_url_guard   — fetch /claims/index.json, resolve every URL
                          it emits; any non-2xx = alert. Successor to
                          the tests/test_claim_envelope_urls_resolve.py
                          guard, run against LIVE not Flask test client.
  B. snapshot_signature — fetch /.well-known/snapshots/chain.json +
                          today's signed snapshot + published pubkey;
                          verify Ed25519 signature + chain-link to
                          yesterday's snapshot. This is the trust story
                          re-executed nightly.

Backlogged for L2 v2 (documented in project_autonomous_oversight_tiers_plan.md):
  - LAST_VERIFIED freshness stamps vs declared cadences
  - Surface reachability (llms.txt, agents.json, MCP handshake)
  - On-ledger anchor tx resolution (Clio query)
  - Drift diff vs yesterday's inspection

Dedup rules (same shape as L1):
  - New alert       → send immediately, remember first_fired.
  - Same alert live → reminder every 4h (L2_REMINDER_INTERVAL_SEC).
  - Alert cleared   → send RECOVERED once, then forget.

Env vars:
  L2_TELEGRAM_BOT_TOKEN    — bot token (dry-run if unset).
  L2_TELEGRAM_CHAT_ID      — target chat id (dry-run if unset).
  L2_STATE_PATH            — state json (default ~/.l2_inspector_state.json).
  L2_REMINDER_INTERVAL_SEC — active-alert reminder cadence (default 14400 = 4h).
  L2_SITE_URL              — base URL (default https://xrpldashboard.com).

Exit code: always 0 unless a hard exception escapes. Same policy as L1.

Invocation:
  Manual: python3 tools/l2_inspector.py [--dry-run] [--test-message]
  Timer:  /etc/systemd/system/xrpld-l2-inspector.timer (nightly)
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import traceback
import urllib.parse
import urllib.request
import zoneinfo
from pathlib import Path
from urllib.error import HTTPError, URLError

try:
    from nacl.signing import VerifyKey
    from nacl.exceptions import BadSignatureError
    from cryptography.hazmat.primitives import serialization
except ImportError:
    VerifyKey = None
    BadSignatureError = Exception
    serialization = None


# ── thresholds ──────────────────────────────────────────────────────
DEFAULT_REMINDER_INTERVAL_SEC = 4 * 3600
WEEKLY_HEARTBEAT_WEEKDAY = 0                # Monday (differentiates from L1's Sunday=6)
WEEKLY_HEARTBEAT_HOUR_MIN = 9
WEEKLY_HEARTBEAT_HOUR_MAX = 10
ET_ZONE = zoneinfo.ZoneInfo("America/New_York")
HTTP_TIMEOUT_SEC = 15
DEFAULT_SITE_URL = "https://xrpldashboard.com"
USER_AGENT = "xrpld-l2-inspector/1.0 (read-only LIVE-site verifier)"


# ── delivery (same shape as L1) ─────────────────────────────────────
def send_telegram(text: str, dry_run: bool = False) -> tuple[bool, str]:
    """POST to Telegram Bot API. Returns (delivered, note)."""
    token = os.environ.get("L2_TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("L2_TELEGRAM_CHAT_ID", "").strip()
    if dry_run or not token or not chat:
        reason = "dry-run" if dry_run else "credentials unset"
        print(f"[l2 dry {reason}] {text}", flush=True)
        return False, reason
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = urllib.parse.urlencode({
        "chat_id": chat,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    try:
        with urllib.request.urlopen(url, data=body, timeout=HTTP_TIMEOUT_SEC) as resp:
            resp.read()
        return True, "ok"
    except Exception as e:
        return False, f"telegram send failed: {e}"


# ── state ───────────────────────────────────────────────────────────
def state_path() -> Path:
    override = os.environ.get("L2_STATE_PATH", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".l2_inspector_state.json"


def load_state() -> dict:
    p = state_path()
    if not p.exists():
        return {"active_alerts": {}, "last_heartbeat_sent": None}
    try:
        data = json.loads(p.read_text())
        data.setdefault("active_alerts", {})
        data.setdefault("last_heartbeat_sent", None)
        return data
    except Exception:
        return {"active_alerts": {}, "last_heartbeat_sent": None}


def save_state(state: dict) -> None:
    p = state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, default=str))
    tmp.replace(p)


# ── HTTP helper (READ-ONLY — never uses methods other than GET/HEAD) ─
def _fetch(url: str, method: str = "GET") -> tuple[int, bytes, dict]:
    """Fetch a URL. Returns (status_code, body_bytes, headers).
    READ-ONLY by contract — only GET/HEAD permitted. Raises on other."""
    if method not in ("GET", "HEAD"):
        raise ValueError(f"L2 is read-only; method={method} forbidden")
    req = urllib.request.Request(
        url,
        method=method,
        headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SEC) as resp:
            body = resp.read() if method == "GET" else b""
            return resp.status, body, dict(resp.headers)
    except HTTPError as e:
        body = e.read() if hasattr(e, "read") else b""
        return e.code, body, dict(e.headers or {})


# ── check A: claims URL guard against LIVE ──────────────────────────
def _collect_urls(node, out: list[str]) -> None:
    """Same URL-collection walk as tests/test_claim_envelope_urls_resolve.py.
    Recursively pull every string that starts with http:// or https://."""
    if isinstance(node, dict):
        for v in node.values():
            _collect_urls(v, out)
    elif isinstance(node, list):
        for v in node:
            _collect_urls(v, out)
    elif isinstance(node, str) and node.startswith(("http://", "https://")):
        out.append(node)


def check_claims_url_guard(site_url: str) -> list[dict]:
    """Fetch LIVE /claims/index.json, extract every URL emitted anywhere
    in the response, and confirm each one resolves (2xx OR 3xx). Any
    non-resolving URL is a broken citation contract — the Grok red-team
    #1 finding class, re-run nightly against production."""
    alerts = []
    index_url = f"{site_url.rstrip('/')}/claims/index.json"
    try:
        status, body, _ = _fetch(index_url)
    except Exception as e:
        alerts.append({
            "id": "claims_url_guard:index_fetch_failed",
            "url": index_url,
            "detail": f"{type(e).__name__}: {e}",
        })
        return alerts
    if status != 200:
        alerts.append({
            "id": "claims_url_guard:index_non_200",
            "url": index_url,
            "status": status,
        })
        return alerts
    try:
        index = json.loads(body)
    except Exception as e:
        alerts.append({
            "id": "claims_url_guard:index_not_json",
            "detail": f"{type(e).__name__}: {e}",
        })
        return alerts

    # Follow every claim endpoint referenced from index, collect URLs
    urls: list[str] = []
    _collect_urls(index, urls)

    # Also expand the per-claim envelopes for deeper URL coverage
    for entry in (index.get("claims") or []):
        claim_url = entry.get("endpoint") or entry.get("url")
        if not claim_url:
            continue
        if claim_url.startswith("/"):
            claim_url = f"{site_url.rstrip('/')}{claim_url}"
        try:
            cst, cbody, _ = _fetch(claim_url)
            if cst == 200:
                try:
                    _collect_urls(json.loads(cbody), urls)
                except Exception:
                    pass
        except Exception:
            pass  # per-claim fetch errors surface via the URL check below

    # Deduplicate and check every URL that points into this site
    seen: set[str] = set()
    failures: list[dict] = []
    for u in urls:
        if u in seen:
            continue
        seen.add(u)
        if site_url.rstrip("/") not in u:
            continue  # external URLs — out of scope for a nightly resolve
        try:
            st, _, _ = _fetch(u, method="HEAD")
            # Some routes may not honor HEAD; fall back to GET on 405.
            if st == 405:
                st, _, _ = _fetch(u)
            if st >= 400:
                failures.append({"url": u, "status": st})
        except Exception as e:
            failures.append({"url": u, "error": f"{type(e).__name__}: {e}"})

    if failures:
        alerts.append({
            "id": "claims_url_guard:broken_urls",
            "count": len(failures),
            "checked": len(seen),
            "sample": failures[:5],
        })
    return alerts


# ── check B: signed snapshot Ed25519 + chain link ───────────────────
def _load_pubkey(pem_bytes: bytes):
    """Parse Ed25519 public key from PEM. Returns VerifyKey."""
    if serialization is None or VerifyKey is None:
        raise RuntimeError("cryptography + pynacl required for signature verify")
    key_obj = serialization.load_pem_public_key(pem_bytes)
    raw = key_obj.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return VerifyKey(raw)


def check_snapshot_signature(site_url: str, now_utc: dt.datetime) -> list[dict]:
    """Fetch today's (or newest available) signed snapshot from LIVE,
    fetch the published pubkey.pem, verify the Ed25519 signature over
    the canonical payload, and confirm chain-link (prev_hash) matches
    yesterday's entry in chain.json. Any failure = alert."""
    alerts = []
    base = site_url.rstrip("/")
    pubkey_url = f"{base}/.well-known/snapshots/pubkey.pem"
    chain_url = f"{base}/.well-known/snapshots/chain.json"

    # 1. Pubkey
    try:
        st, body, _ = _fetch(pubkey_url)
        if st != 200:
            alerts.append({
                "id": "snapshot_signature:pubkey_non_200",
                "url": pubkey_url, "status": st,
            })
            return alerts
        verify_key = _load_pubkey(body)
    except Exception as e:
        alerts.append({
            "id": "snapshot_signature:pubkey_load_failed",
            "detail": f"{type(e).__name__}: {e}",
        })
        return alerts

    # 2. Chain
    try:
        st, body, _ = _fetch(chain_url)
        if st != 200:
            alerts.append({
                "id": "snapshot_signature:chain_non_200",
                "url": chain_url, "status": st,
            })
            return alerts
        chain = json.loads(body)
    except Exception as e:
        alerts.append({
            "id": "snapshot_signature:chain_fetch_failed",
            "detail": f"{type(e).__name__}: {e}",
        })
        return alerts

    leaves = chain.get("leaves") or []
    if not leaves:
        alerts.append({
            "id": "snapshot_signature:chain_empty",
            "detail": "chain.json has no leaves",
        })
        return alerts

    leaves = sorted(leaves, key=lambda e: e.get("date") or "")
    newest_leaf = leaves[-1]
    prev_leaf = leaves[-2] if len(leaves) >= 2 else None
    newest_date = newest_leaf.get("date") or ""

    def _snap_url(date_str: str) -> str:
        return f"{base}/.well-known/snapshots/{date_str}.json"

    # 3. Fetch newest snapshot artifact
    snap_url = _snap_url(newest_date)
    try:
        st, snap_body, _ = _fetch(snap_url)
        if st != 200:
            alerts.append({
                "id": "snapshot_signature:snapshot_non_200",
                "url": snap_url, "status": st,
            })
            return alerts
        snapshot = json.loads(snap_body)
    except Exception as e:
        alerts.append({
            "id": "snapshot_signature:snapshot_fetch_failed",
            "url": snap_url,
            "detail": f"{type(e).__name__}: {e}",
        })
        return alerts

    # 4. Verify Ed25519 signature.
    # Signed envelope shape (from signed_snapshot.py:574-583): exactly these
    # 8 keys, sorted-key canonical JSON, signature is HEX not base64.
    envelope_keys = (
        "signing_domain", "schema_version", "snapshot_date_utc",
        "leaf_hash", "leaf_index", "leaves_total",
        "chain_root", "previous_root",
    )
    missing = [k for k in envelope_keys if k not in snapshot]
    if missing:
        alerts.append({
            "id": "snapshot_signature:envelope_incomplete",
            "url": snap_url,
            "detail": f"missing keys: {missing}",
        })
        return alerts
    sig_hex = snapshot.get("signature_ed25519")
    if not sig_hex:
        alerts.append({
            "id": "snapshot_signature:signature_absent",
            "url": snap_url,
        })
        return alerts

    envelope = {k: snapshot[k] for k in envelope_keys}
    canonical = json.dumps(
        envelope, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    try:
        sig = bytes.fromhex(sig_hex)
        verify_key.verify(canonical, sig)
    except BadSignatureError:
        alerts.append({
            "id": "snapshot_signature:invalid_signature",
            "url": snap_url,
        })
        return alerts
    except Exception as e:
        alerts.append({
            "id": "snapshot_signature:verify_error",
            "detail": f"{type(e).__name__}: {e}",
        })
        return alerts

    # 5. Chain link — newest.previous_root should equal previous snapshot's chain_root.
    if prev_leaf is not None:
        prev_date = prev_leaf.get("date") or ""
        prev_url = _snap_url(prev_date)
        try:
            pst, pbody, _ = _fetch(prev_url)
            if pst == 200:
                prev_snap = json.loads(pbody)
                newest_prev_root = snapshot.get("previous_root") or ""
                prev_chain_root = prev_snap.get("chain_root") or ""
                if newest_prev_root != prev_chain_root:
                    alerts.append({
                        "id": "snapshot_signature:chain_link_mismatch",
                        "newest_date": newest_date,
                        "prev_date": prev_date,
                        "newest_previous_root": newest_prev_root[:32] + "…",
                        "prev_chain_root": prev_chain_root[:32] + "…",
                    })
        except Exception as e:
            alerts.append({
                "id": "snapshot_signature:prev_snapshot_fetch_failed",
                "url": prev_url,
                "detail": f"{type(e).__name__}: {e}",
            })

    return alerts


# ── formatting ──────────────────────────────────────────────────────
def format_alert(alert: dict) -> str:
    aid = alert["id"]
    kind = aid.split(":", 1)[0]
    if kind == "claims_url_guard":
        if aid.endswith("broken_urls"):
            sample = "\n".join(
                f"   • {s.get('url')} → {s.get('status') or s.get('error')}"
                for s in alert.get("sample", [])
            )
            return (
                f"🟥 <b>L2: broken citation URLs</b> — "
                f"{alert['count']}/{alert['checked']} URLs failed to resolve.\n"
                f"{sample}"
            )
        return f"🟥 <b>L2: claims index unavailable</b>: <code>{aid}</code> — {alert.get('detail') or alert.get('url')}"
    if kind == "snapshot_signature":
        detail = alert.get("detail") or alert.get("url") or ""
        return f"🟥 <b>L2: snapshot verify failed</b>: <code>{aid}</code>\n{detail}"
    if kind == "pager_check_error":
        return f"🟥 <b>L2: check crashed</b>: <code>{aid}</code>\n{alert.get('error', '')}"
    return f"🟥 <b>L2 ALERT</b>: {aid}"


def format_recovered(alert_id: str) -> str:
    return f"🟩 <b>L2 RECOVERED</b>: <code>{alert_id}</code>"


def format_heartbeat(alerts: list[dict]) -> str:
    if not alerts:
        return (
            "🟢 <b>L2 weekly heartbeat</b>\n"
            "All LIVE-site checks green (claims URL guard, snapshot "
            "signature + chain). Inspector is alive."
        )
    lines = ["🟡 <b>L2 weekly heartbeat</b>", "Active alerts:"]
    for a in alerts:
        lines.append(f" • {format_alert(a)}")
    return "\n".join(lines)


# ── main loop ───────────────────────────────────────────────────────
def gather_alerts(site_url: str, now_utc: dt.datetime) -> list[dict]:
    alerts: list[dict] = []
    checks = (
        (check_claims_url_guard, {"site_url": site_url}),
        (check_snapshot_signature, {"site_url": site_url, "now_utc": now_utc}),
    )
    for fn, kwargs in checks:
        try:
            alerts.extend(fn(**kwargs))
        except Exception as e:
            alerts.append({
                "id": f"pager_check_error:{fn.__name__}",
                "error": f"{type(e).__name__}: {e}",
            })
    return alerts


def should_send_heartbeat(state: dict, now_utc: dt.datetime) -> bool:
    now_et = now_utc.astimezone(ET_ZONE)
    if now_et.weekday() != WEEKLY_HEARTBEAT_WEEKDAY:
        return False
    if not (WEEKLY_HEARTBEAT_HOUR_MIN <= now_et.hour < WEEKLY_HEARTBEAT_HOUR_MAX):
        return False
    last = state.get("last_heartbeat_sent")
    if not last:
        return True
    last_dt = dt.datetime.fromisoformat(last)
    if last_dt.tzinfo is None:
        last_dt = last_dt.replace(tzinfo=dt.timezone.utc)
    return (now_utc - last_dt).total_seconds() > 6 * 86400


def reconcile(state: dict, current: list[dict], now_utc: dt.datetime,
              reminder_interval: int, dry_run: bool) -> None:
    current_by_id = {a["id"]: a for a in current}
    active = state["active_alerts"]

    for aid, alert in current_by_id.items():
        prev = active.get(aid)
        if prev is None:
            send_telegram(format_alert(alert), dry_run=dry_run)
            active[aid] = {
                "first_fired": now_utc.isoformat(),
                "last_reminder": now_utc.isoformat(),
                "snapshot": alert,
            }
            continue
        last_reminder = dt.datetime.fromisoformat(prev["last_reminder"])
        if last_reminder.tzinfo is None:
            last_reminder = last_reminder.replace(tzinfo=dt.timezone.utc)
        if (now_utc - last_reminder).total_seconds() > reminder_interval:
            send_telegram(
                "🔁 <b>L2 still active</b>\n" + format_alert(alert),
                dry_run=dry_run,
            )
            prev["last_reminder"] = now_utc.isoformat()
            prev["snapshot"] = alert

    for aid in list(active.keys()):
        if aid not in current_by_id:
            send_telegram(format_recovered(aid), dry_run=dry_run)
            del active[aid]


def main() -> int:
    p = argparse.ArgumentParser(description="L2 inspector — see module docstring.")
    p.add_argument("--dry-run", action="store_true",
                   help="Print messages to stdout instead of sending Telegram.")
    p.add_argument("--force-heartbeat", action="store_true",
                   help="Send a heartbeat regardless of day/hour (for testing).")
    p.add_argument("--test-message", action="store_true",
                   help="Send one 'L2 inspector online' test message and exit 0.")
    args = p.parse_args()

    if args.test_message:
        ok, note = send_telegram(
            "🟢 <b>L2 inspector online</b> — test message. If you see this, "
            "the delivery channel works.",
            dry_run=args.dry_run,
        )
        print(f"test-message delivered={ok} note={note}", flush=True)
        return 0

    site_url = os.environ.get("L2_SITE_URL", DEFAULT_SITE_URL).strip() or DEFAULT_SITE_URL
    now_utc = dt.datetime.now(dt.timezone.utc)
    reminder_interval = int(os.environ.get(
        "L2_REMINDER_INTERVAL_SEC", DEFAULT_REMINDER_INTERVAL_SEC))

    state = load_state()
    try:
        current = gather_alerts(site_url, now_utc)
    except Exception as e:
        traceback.print_exc()
        send_telegram(
            f"🟥 <b>L2 inspector EXCEPTION</b>: {type(e).__name__}: {e}",
            dry_run=args.dry_run,
        )
        return 0

    reconcile(state, current, now_utc, reminder_interval, args.dry_run)

    if args.force_heartbeat or should_send_heartbeat(state, now_utc):
        send_telegram(format_heartbeat(current), dry_run=args.dry_run)
        state["last_heartbeat_sent"] = now_utc.isoformat()

    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
