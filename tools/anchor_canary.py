#!/usr/bin/env python3
"""Anchor canary — the L1.5 tripwire no thief can silence (Shape C).

Discovers every anchor tx directly from the XRPL ledger via `account_tx`
against a full-history node (Clio), parses the v1 memo, and verifies the
LATEST anchor's freshness + root against the live-site `chain.json`. No
local registry file. No separate append step. The ledger IS the registry.

Why Shape C (2026-08-27 rewrite): the prior registry-driven design used a
locally-maintained `docs/anchor_registry.json` as source of truth. On
2026-08-22 ceremony Step 4 (`anchor_registry_append.py`) was skipped after
anchor #3 was stamped on-chain — the canary correctly fired against a
stale file. Shape C removes the writer/reader split entirely: the canary
reads state DIRECTLY from the chain, so no local file can drift.

Retention window solved at the query endpoint: the local rippled has
`online_delete=10000` (~13.5h retention). Weekly anchors fall outside
that window 6/7 days. Shape C queries a full-history Clio node instead
(default `s2-clio.ripple.com:51234`), which retains genesis-to-tip.

Failure semantics (the witness ladder):
  - Full-history node responds → check freshness + root
  - Full-history node responds but returns 0 anchors for account → FIRE
  - All full-history nodes unreachable → LOUD SKIP (no alert, no state
    mutation, log to stdout)
  - Full-history node returns partial history (`ledger_index_min` above
    threshold, meaning not genesis-anchored) → LOUD SKIP
  - Freshness fails → FIRE anchor_canary:anchor_stale
  - Root cross-check fails → FIRE anchor_canary:root_mismatch (the
    stolen-key / forged-site tripwire, preserved from prior design)

Design law:
  - Read-only end-to-end. No writes, no mutations, no side effects
    beyond Telegram delivery.
  - Refuse-to-run without credentials in production mode (loud exit 1
    to stderr, never a silent dry-run). --dry-run is exempt.
  - --dry-run skips state persistence entirely so it cannot consume
    triggers or pollute reconcile state.
  - Render-origin site fetch — ANCHOR_CANARY_SITE_URL default
    https://xrpldashboard.onrender.com, matching L2's hairpin workaround.
  - Weekly heartbeat Tuesday 09:00-10:00 ET (differentiates from L1
    Sunday and L2 Monday). Silence from the canary is itself an alarm.
  - Strip rule honored — decoded MemoData is `.rstrip()`ed, each
    pipe-delimited field is `.strip()`ed.

Env vars:
  ANCHOR_CANARY_FULL_HISTORY_NODES   — comma-separated full-history nodes for
                                       account_tx discovery (default
                                       s2-clio.ripple.com:51234).
  ANCHOR_CANARY_SITE_URL             — live site base (default onrender origin).
  ANCHOR_CANARY_ACCOUNT              — anchor account (default rL2y…NWQ).
  ANCHOR_CANARY_TELEGRAM_BOT_TOKEN   — bot token (REQUIRED unless --dry-run).
  ANCHOR_CANARY_TELEGRAM_CHAT_ID     — target chat id (REQUIRED unless --dry-run).
  ANCHOR_CANARY_STATE_PATH           — state json (default ~/.anchor_canary_state.json).
  ANCHOR_CANARY_REMINDER_INTERVAL_SEC — active-alert reminder cadence (default 4h).
  ANCHOR_CANARY_FRESHNESS_HOURS      — max age of most-recent anchor before alarm
                                       (default 192 = 8 days).

Exit code: 0 for normal cycles (including LOUD SKIP). 1 for missing
credentials in production mode.

Invocation:
  Manual: python3 tools/anchor_canary.py [--dry-run] [--test-message]
  Timer:  /etc/systemd/system/xrpld-anchor-canary.timer (daily)
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
from urllib.error import HTTPError


# ── constants + thresholds ───────────────────────────────────────────
DEFAULT_FULL_HISTORY_NODES = (
    "https://s2-clio.ripple.com:51234",
)
DEFAULT_SITE_URL = "https://xrpldashboard.onrender.com"
DEFAULT_ANCHOR_ACCOUNT = "rL2yMECEyUT94pLDrAcetMNMG1H4xqpNWQ"
DEFAULT_FRESHNESS_HOURS = 8 * 24  # 8 days — weekly cadence + 1-day slack
DEFAULT_REMINDER_INTERVAL_SEC = 4 * 3600
HTTP_TIMEOUT_SEC = 20
USER_AGENT = "xrpld-anchor-canary/3.0 (ledger-derived Shape C)"

# ledger_index_min above this means the witness is not full-history
# (Clio genesis is ~32570; anything above the threshold is a limited-
# history node that we cannot trust to enumerate weekly anchors).
PARTIAL_HISTORY_LEDGER_THRESHOLD = 1_000_000

# account_tx pagination cap per response. rippled/Clio typically cap at
# 400; 200 is a safe middle ground with predictable page counts.
ACCOUNT_TX_PAGE_LIMIT = 200

# Safety valve so a broken marker loop never spins forever.
ACCOUNT_TX_MAX_PAGES = 50

WEEKLY_HEARTBEAT_WEEKDAY = 1  # Tuesday (L1=Sun=6, L2=Mon=0)
WEEKLY_HEARTBEAT_HOUR_MIN = 9
WEEKLY_HEARTBEAT_HOUR_MAX = 10
ET_ZONE = zoneinfo.ZoneInfo("America/New_York")

ANCHOR_NAMESPACE_STANDARD = "xrpldashboard/anchor/v1"
ANCHOR_NAMESPACE_CORRECTION = "xrpldashboard/anchor/correction/v1"

# The bootstrap-hop tx (anchor→ops, no v1 memo) — allowlisted per
# ONLEDGER_ANCHOR_SPEC.md §Deviation history 2026-08-07. Registry never
# includes it; verify function still recognises it if ever passed by hand.
BOOTSTRAP_TX_HASH = "E94ADB8CF438EB94DCC00725572CBCC03ACC3084F12DE706AEB4D418B6A7438B"


# ── delivery ─────────────────────────────────────────────────────────
def send_telegram(text: str, dry_run: bool = False) -> tuple[bool, str]:
    """POST to Telegram Bot API. Returns (delivered, note).

    In production mode, missing credentials are a hard refuse enforced by
    `check_credentials_or_refuse()` at main() startup — they will never
    reach this function. In --dry-run mode we print to stdout unconditionally.
    """
    if dry_run:
        print(f"[anchor-canary dry-run] {text}", flush=True)
        return False, "dry-run"
    token = os.environ.get("ANCHOR_CANARY_TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("ANCHOR_CANARY_TELEGRAM_CHAT_ID", "").strip()
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


def check_credentials_or_refuse(dry_run: bool) -> None:
    """R5: at main() startup, in production mode, refuse to run without
    credentials. Silent-dry-run-with-missing-creds was the mode that
    generated 'why didn't the canary fire?' incidents — replace it with a
    loud stderr exit 1 that surfaces immediately in systemd status."""
    if dry_run:
        return
    token = os.environ.get("ANCHOR_CANARY_TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("ANCHOR_CANARY_TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        missing = []
        if not token:
            missing.append("ANCHOR_CANARY_TELEGRAM_BOT_TOKEN")
        if not chat:
            missing.append("ANCHOR_CANARY_TELEGRAM_CHAT_ID")
        print(
            "REFUSE_TO_RUN: anchor canary cannot deliver alerts without "
            "credentials. Missing: " + ", ".join(missing) + ".\n"
            "Fix: source ~/.config/xrpldashboard/env before invocation, "
            "or add env vars to the systemd unit EnvironmentFile.\n"
            "To exercise the checks without delivery, invoke with --dry-run.",
            file=sys.stderr,
            flush=True,
        )
        sys.exit(1)


# ── state ────────────────────────────────────────────────────────────
def state_path() -> Path:
    override = os.environ.get("ANCHOR_CANARY_STATE_PATH", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".anchor_canary_state.json"


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


# ── memo decoding (per ONLEDGER_ANCHOR_SPEC v1) ──────────────────────
def decode_memo_data(memo_data_hex: str) -> str | None:
    """Hex-decode the MemoData field. Returns the raw utf-8 string
    (pre-strip, pre-split). Returns None on malformed input."""
    if not memo_data_hex:
        return None
    try:
        return bytes.fromhex(memo_data_hex).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


def parse_anchor_memo(memo_data_raw: str) -> dict | None:
    """Parse a v1 anchor memo per ONLEDGER_ANCHOR_SPEC §Verifier
    requirements. Applies the strip rule: rstrip() the full string, then
    strip() each pipe-delimited field. Returns dict with type / date /
    chain_root (and correction_of for Type B), or None if not a v1
    anchor memo."""
    if not memo_data_raw:
        return None
    stripped = memo_data_raw.rstrip()
    parts = [p.strip() for p in stripped.split("|")]
    if len(parts) < 3:
        return None
    namespace = parts[0]
    if namespace == ANCHOR_NAMESPACE_STANDARD and len(parts) >= 3:
        return {
            "type": "standard",
            "namespace": namespace,
            "snapshot_date": parts[1],
            "chain_root_hex": parts[2].lower(),
        }
    if namespace == ANCHOR_NAMESPACE_CORRECTION and len(parts) >= 4:
        return {
            "type": "correction",
            "namespace": namespace,
            "snapshot_date": parts[1],
            "correction_of_tx": parts[2].upper(),
            "chain_root_hex": parts[3].lower(),
        }
    return None


def extract_v1_memo_from_tx(tx: dict) -> dict | None:
    """A v1 anchor tx carries exactly one Memo with MemoData holding the
    pipe-delimited payload. Returns the parsed memo dict or None if the
    tx has no v1 memo."""
    # Both tx shapes seen in practice: {"Memos":[...]} on the tx itself
    # or nested under {"tx":{"Memos":[...]}} for account_tx. The `tx`
    # RPC returns fields at the top level.
    memos = tx.get("Memos") or []
    if not memos and isinstance(tx.get("tx"), dict):
        memos = tx["tx"].get("Memos") or []
    for m in memos:
        inner = m.get("Memo") or m
        memo_data_hex = inner.get("MemoData") or ""
        raw = decode_memo_data(memo_data_hex)
        if raw is None:
            continue
        parsed = parse_anchor_memo(raw)
        if parsed is not None:
            return parsed
    return None


# ── HTTP (READ-ONLY — GET/POST for rippled JSON-RPC only) ────────────
def _http_get_json(url: str) -> tuple[int, dict | list | None]:
    req = urllib.request.Request(
        url,
        method="GET",
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SEC) as resp:
            body = resp.read()
            try:
                return resp.status, json.loads(body)
            except json.JSONDecodeError:
                return resp.status, None
    except HTTPError as e:
        body = e.read() if hasattr(e, "read") else b""
        try:
            return e.code, json.loads(body)
        except json.JSONDecodeError:
            return e.code, None


def _rippled_rpc(url: str, method: str, params: dict) -> dict | None:
    """POST a JSON-RPC method to rippled. Returns the .result payload or
    None on transport error. Read-only — only `tx` is invoked from this
    script. Result-level errors (txnNotFound, lgrNotFound) are returned
    as-is so the caller can distinguish 'no such tx here' from 'endpoint
    down'."""
    body = json.dumps({"method": method, "params": [params]}).encode()
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SEC) as resp:
            payload = json.loads(resp.read())
    except Exception:
        return None
    return (payload or {}).get("result") or {}


# rippled/Clio error codes on account_tx that mean "endpoint problem"
# (cascade to next full-history node). "actNotFound" is semantically
# different — the account itself doesn't exist per this witness — but on
# Clio full-history it would only occur if the account address is wrong,
# which we treat as an alertable condition rather than an endpoint skip.
_ACCOUNT_TX_ENDPOINT_ERRORS = frozenset({
    "notSynced",
    "noNetwork",
    "noCurrent",
    "noClosed",
    "tooBusy",
    "invalidParams",  # observed transiently from some Clio deployments
})


def full_history_nodes() -> list[str]:
    """R1: env-configurable full-history node list, default s2-clio."""
    override = os.environ.get("ANCHOR_CANARY_FULL_HISTORY_NODES", "").strip()
    if override:
        return [u.strip() for u in override.split(",") if u.strip()]
    return list(DEFAULT_FULL_HISTORY_NODES)


# XRPL epoch offset — ripple time is seconds since 2000-01-01 UTC.
_XRPL_EPOCH_OFFSET = 946684800


def _xrpl_time_to_iso(ripple_seconds: int) -> str:
    """Convert an XRPL `date`/close_time field (ripple epoch seconds) to
    an ISO-8601 UTC string. Returns empty string on invalid input."""
    try:
        return dt.datetime.fromtimestamp(
            _XRPL_EPOCH_OFFSET + int(ripple_seconds), tz=dt.timezone.utc
        ).isoformat()
    except (TypeError, ValueError):
        return ""


def _extract_close_time_iso(tx_wrapper: dict, tx_body: dict) -> str:
    """Pull an ISO close-time out of an account_tx entry. Modern Clio
    surfaces `close_time_iso` on the wrapper; older rippled puts a
    ripple-seconds `date` on the tx body. Try both."""
    iso = tx_wrapper.get("close_time_iso") or tx_body.get("close_time_iso")
    if isinstance(iso, str) and iso:
        return iso
    date = tx_wrapper.get("date")
    if date is None:
        date = tx_body.get("date")
    if isinstance(date, (int, float)):
        return _xrpl_time_to_iso(date)
    return ""


def fetch_account_tx_anchors(node_url: str, account: str
                             ) -> tuple[str, list[dict], str]:
    """Paginate `account_tx` on a full-history node, filter to v1 anchor
    memos, return anchors sorted by ledger_index ascending.

    Returns (status, anchors, detail):
      status ∈ {"ok", "partial_history", "unreachable", "invalid_response"}
      anchors : list of dicts with keys tx_hash, ledger_index,
                close_time_iso, snapshot_date, chain_root_hex, type,
                namespace, and correction_of_tx (for type=correction).
                Ordered by ledger_index ascending.
      detail  : human-readable explanation (empty on ok).
    """
    anchors: list[dict] = []
    marker: dict | str | None = None
    ledger_index_min_seen: int | None = None

    for _ in range(ACCOUNT_TX_MAX_PAGES):
        params: dict = {
            "account": account,
            "ledger_index_min": -1,
            "ledger_index_max": -1,
            "limit": ACCOUNT_TX_PAGE_LIMIT,
            "forward": False,
            "binary": False,
        }
        if marker is not None:
            params["marker"] = marker
        result = _rippled_rpc(node_url, "account_tx", params)
        if result is None:
            return "unreachable", [], (
                f"transport failure calling account_tx on {node_url}"
            )
        status = result.get("status")
        if status != "success":
            err = result.get("error") or "unknown"
            if err == "actNotFound":
                return "invalid_response", [], (
                    f"account_tx on {node_url}: actNotFound for {account}. "
                    f"Either the account is wrong or the witness has no "
                    f"history for it."
                )
            if err in _ACCOUNT_TX_ENDPOINT_ERRORS:
                return "unreachable", [], (
                    f"account_tx on {node_url}: error={err} — endpoint problem"
                )
            return "invalid_response", [], (
                f"account_tx on {node_url}: unexpected error={err}"
            )

        # Track the deepest ledger_index_min the witness reports across
        # pages. Genesis on Clio is ~32570 → anything above the threshold
        # means we're talking to a partial-history node.
        idx_min = result.get("ledger_index_min")
        if isinstance(idx_min, int):
            if ledger_index_min_seen is None or idx_min < ledger_index_min_seen:
                ledger_index_min_seen = idx_min

        transactions = result.get("transactions") or []
        if not isinstance(transactions, list):
            return "invalid_response", [], (
                f"account_tx on {node_url}: transactions field missing/wrong shape"
            )

        for entry in transactions:
            if not isinstance(entry, dict):
                continue
            tx_body = entry.get("tx") or entry.get("tx_json") or {}
            if not isinstance(tx_body, dict):
                continue
            memo = extract_v1_memo_from_tx(tx_body)
            if memo is None:
                continue
            tx_hash = (
                entry.get("hash")
                or tx_body.get("hash")
                or tx_body.get("Hash")
                or ""
            )
            if not isinstance(tx_hash, str) or not tx_hash:
                continue
            ledger_index = (
                entry.get("ledger_index")
                or tx_body.get("ledger_index")
                or tx_body.get("inLedger")
            )
            if not isinstance(ledger_index, int):
                continue
            anchor = {
                "tx_hash": tx_hash.upper(),
                "ledger_index": ledger_index,
                "close_time_iso": _extract_close_time_iso(entry, tx_body),
                "snapshot_date": memo.get("snapshot_date", ""),
                "chain_root_hex": memo.get("chain_root_hex", ""),
                "type": memo.get("type", "standard"),
                "namespace": memo.get("namespace", ""),
            }
            if memo.get("type") == "correction":
                anchor["correction_of_tx"] = memo.get("correction_of_tx", "")
            anchors.append(anchor)

        marker = result.get("marker")
        if not marker:
            break

    # Partial-history detection AFTER pagination completes so we've seen
    # the witness's actual reach.
    if (ledger_index_min_seen is not None
            and ledger_index_min_seen > PARTIAL_HISTORY_LEDGER_THRESHOLD):
        return "partial_history", [], (
            f"account_tx on {node_url}: ledger_index_min="
            f"{ledger_index_min_seen} exceeds full-history threshold "
            f"({PARTIAL_HISTORY_LEDGER_THRESHOLD}) — witness has limited "
            f"history, refusing to trust its anchor enumeration"
        )

    anchors.sort(key=lambda a: a["ledger_index"])
    return "ok", anchors, ""


# ── live-site chain.json fetch (for latest-anchor cross-check) ───────
def fetch_live_chain(site_url: str) -> dict | None:
    """Fetch /.well-known/snapshots/chain.json from Render origin.
    Returns parsed JSON or None on failure."""
    url = f"{site_url.rstrip('/')}/.well-known/snapshots/chain.json"
    try:
        st, payload = _http_get_json(url)
    except Exception:
        return None
    if st != 200 or not isinstance(payload, dict):
        return None
    return payload


def lookup_chain_root_for_date(chain: dict, snapshot_date: str) -> str | None:
    """Look up the chain_root recorded for `snapshot_date` in the live
    chain's root_history. Returns lowercase hex or None if not present."""
    for entry in chain.get("root_history") or []:
        if entry.get("date") == snapshot_date:
            root = entry.get("root")
            return root.lower() if isinstance(root, str) else None
    return None


# ── check: ledger-derived anchors × live-site chain.json ─────────────
def check_chain_anchors(site_url: str, full_history_urls: list[str],
                        account: str, now_utc: dt.datetime,
                        freshness_hours: int
                        ) -> tuple[list[dict], list[dict], dict]:
    """Derive the anchor chain directly from a full-history XRPL node,
    then check freshness + root against the live chain.json.

    Returns (alerts, anchors, meta) where:
      alerts  : list of alert dicts (empty when green or LOUD-SKIP)
      anchors : chain-derived anchor list (empty on LOUD SKIP)
      meta    : {"witness_url", "witness_status", "witness_detail",
                 "skipped"} for heartbeat + logging

    LOUD SKIP semantics (R2 + design pack §4):
      All full-history witnesses unreachable OR partial-history
      → no alert fired, no state mutation, meta["skipped"] = True
    """
    alerts: list[dict] = []
    tried: list[tuple[str, str, str]] = []
    anchors: list[dict] = []
    witness_url = ""
    witness_status = ""
    witness_detail = ""

    for url in full_history_urls:
        status, found, detail = fetch_account_tx_anchors(url, account)
        tried.append((url, status, detail))
        if status == "ok":
            witness_url = url
            witness_status = status
            witness_detail = detail
            anchors = found
            break
        if status == "invalid_response":
            # A definite semantic answer from the witness ("actNotFound",
            # unexpected error). Treat as alertable rather than cascade;
            # this is a config/state problem, not a network hiccup.
            witness_url = url
            witness_status = status
            witness_detail = detail
            break
        # "unreachable" or "partial_history" — try the next witness

    if witness_status not in ("ok", "invalid_response"):
        # Every witness was unreachable or partial-history.
        summary = "; ".join(
            f"{u}={s}" + (f" ({d})" if d else "") for u, s, d in tried
        )
        print(
            "[anchor-canary LOUD SKIP] all full-history witnesses unreachable "
            f"or partial-history — skipping this cycle. Tried: {summary}",
            flush=True,
        )
        return [], [], {
            "witness_url": "",
            "witness_status": "loud_skip",
            "witness_detail": summary,
            "skipped": True,
        }

    if witness_status == "invalid_response":
        # actNotFound or similar deterministic error — alertable.
        alerts.append({
            "id": "anchor_canary:witness_semantic_error",
            "severity": "critical",
            "detail": witness_detail,
        })
        return alerts, [], {
            "witness_url": witness_url,
            "witness_status": witness_status,
            "witness_detail": witness_detail,
            "skipped": False,
        }

    # Witness responded ok. Zero anchors is alertable — the account
    # should have at least the bootstrap + genesis pair by design.
    if not anchors:
        alerts.append({
            "id": "anchor_canary:no_anchors_on_chain",
            "severity": "critical",
            "account": account,
            "witness_url": witness_url,
            "detail": (
                f"full-history witness {witness_url} returned zero v1 "
                f"anchor memos for account {account}. Either no anchor has "
                f"ever been published or the witness has purged history "
                f"(impossible on Clio full-history)."
            ),
        })
        return alerts, [], {
            "witness_url": witness_url,
            "witness_status": witness_status,
            "witness_detail": witness_detail,
            "skipped": False,
        }

    latest = anchors[-1]

    # Freshness check on latest anchor's close time.
    if latest.get("close_time_iso"):
        try:
            close = dt.datetime.fromisoformat(latest["close_time_iso"])
            age_hours = (now_utc - close).total_seconds() / 3600
            if age_hours > freshness_hours:
                alerts.append({
                    "id": "anchor_canary:anchor_stale",
                    "severity": "critical",
                    "tx_hash": latest.get("tx_hash"),
                    "age_hours": round(age_hours, 1),
                    "threshold_hours": freshness_hours,
                    "close_time_iso": latest["close_time_iso"],
                    "snapshot_date": latest.get("snapshot_date"),
                })
        except ValueError:
            pass  # malformed on-chain timestamp — impossible in practice

    # Root-mismatch check against live chain.json (the stolen-key /
    # forged-site tripwire — preserved unchanged from prior design).
    chain = fetch_live_chain(site_url)
    if chain is None:
        alerts.append({
            "id": "anchor_canary:live_chain_unavailable",
            "severity": "warning",
            "detail": (
                f"could not fetch chain.json from {site_url} — check "
                f"Render origin reachability. Latest-anchor cross-check "
                f"cannot complete this cycle."
            ),
        })
    else:
        snapshot_date = latest.get("snapshot_date", "")
        anchored_root = (latest.get("chain_root_hex") or "").lower()
        live_root = lookup_chain_root_for_date(chain, snapshot_date)
        if live_root is None:
            alerts.append({
                "id": "anchor_canary:live_root_missing_for_anchored_date",
                "severity": "critical",
                "tx_hash": latest.get("tx_hash"),
                "snapshot_date": snapshot_date,
                "anchored_root": anchored_root,
                "detail": (
                    "on-ledger anchor names a date the live chain has no "
                    "root_history entry for — one side missed the snapshot "
                    "or the live chain was overwritten."
                ),
            })
        elif live_root != anchored_root:
            alerts.append({
                "id": "anchor_canary:root_mismatch",
                "severity": "critical",
                "tx_hash": latest.get("tx_hash"),
                "snapshot_date": snapshot_date,
                "anchored_root": anchored_root,
                "live_root": live_root,
                "detail": (
                    "on-ledger anchor memo and live chain.json disagree on "
                    "chain_root for the same date. This is the stolen-key / "
                    "forged-site tripwire."
                ),
            })

    return alerts, anchors, {
        "witness_url": witness_url,
        "witness_status": witness_status,
        "witness_detail": witness_detail,
        "skipped": False,
    }


# ── formatting ───────────────────────────────────────────────────────
def format_alert(alert: dict) -> str:
    aid = alert["id"]
    severity_prefix = "🚨" if alert.get("severity") == "critical" else "🟡"
    # split off any :seq suffix for canonical family match
    family = ":".join(aid.split(":")[:2])
    if family == "anchor_canary:root_mismatch":
        return (
            f"{severity_prefix} <b>ANCHOR MISMATCH — POSSIBLE FORGERY</b>\n"
            f"date: <code>{alert['snapshot_date']}</code>\n"
            f"on-ledger root: <code>{alert['anchored_root'][:32]}…</code>\n"
            f"live-site root: <code>{alert['live_root'][:32]}…</code>\n"
            f"anchor tx: <code>{alert['tx_hash']}</code>\n"
            f"This is the stolen-key / forged-site tripwire. "
            f"Investigate before publishing any further snapshots."
        )
    if family == "anchor_canary:anchor_stale":
        return (
            f"{severity_prefix} <b>Anchor stale</b> — last anchor is "
            f"{alert['age_hours']}h old (threshold {alert['threshold_hours']}h).\n"
            f"last tx: <code>{alert['tx_hash']}</code>\n"
            f"date: <code>{alert['snapshot_date']}</code>"
        )
    if family == "anchor_canary:live_root_missing_for_anchored_date":
        return (
            f"{severity_prefix} <b>Anchored date missing from live chain</b>\n"
            f"date: <code>{alert['snapshot_date']}</code>\n"
            f"anchored root: <code>{alert['anchored_root'][:32]}…</code>\n"
            f"anchor tx: <code>{alert['tx_hash']}</code>\n"
            f"{alert.get('detail', '')}"
        )
    if family == "anchor_canary:no_anchors_on_chain":
        return (
            f"{severity_prefix} <b>NO ANCHORS ON CHAIN</b>\n"
            f"account: <code>{alert.get('account')}</code>\n"
            f"witness: <code>{alert.get('witness_url')}</code>\n"
            f"{alert.get('detail', '')}\n"
            f"Either the anchor account never sent a v1 tx, the address "
            f"is misconfigured, or full-history has been purged — any of "
            f"those breaks provenance."
        )
    if family == "anchor_canary:witness_semantic_error":
        return (
            f"{severity_prefix} <b>Witness rejected discovery</b>\n"
            f"{alert.get('detail', '')}\n"
            f"Config check owed: is ANCHOR_CANARY_ACCOUNT correct, and is "
            f"ANCHOR_CANARY_FULL_HISTORY_NODES pointing at Clio nodes?"
        )
    return (
        f"{severity_prefix} <b>Anchor canary: {aid}</b>\n"
        f"{alert.get('detail', '')}"
    )


def format_recovered(alert_id: str) -> str:
    return f"🟩 <b>Anchor canary RECOVERED</b>: <code>{alert_id}</code>"


def format_heartbeat(alerts: list[dict], anchors: list[dict],
                     meta: dict) -> str:
    n = len(anchors)
    witness = meta.get("witness_url") or "?"
    if meta.get("skipped"):
        return (
            "🟡 <b>Anchor canary weekly heartbeat</b>\n"
            "LOUD SKIP this cycle — all full-history witnesses were "
            "unreachable or partial-history.\n"
            f"Detail: {meta.get('witness_detail', 'n/a')}"
        )
    if n > 0:
        latest = anchors[-1]
        latest_line = (
            f"latest: seq {n} · date <code>{latest.get('snapshot_date')}</code> "
            f"· ledger <code>{latest.get('ledger_index')}</code> "
            f"· tx <code>{(latest.get('tx_hash') or '')[:16]}…</code>"
        )
    else:
        latest_line = "latest: (none)"
    breakdown = (
        f"{n} anchor(s) discovered via full-history witness "
        f"<code>{witness}</code>. Latest anchor root matches live chain.json.\n"
        f"{latest_line}"
    )
    if not alerts:
        return (
            "🟢 <b>Anchor canary weekly heartbeat</b>\n"
            f"{breakdown}\n"
            "The chain IS the registry. The one check no thief can "
            "silence is alive."
        )
    lines = ["🟡 <b>Anchor canary weekly heartbeat</b>", breakdown, "Active alerts:"]
    for a in alerts:
        lines.append(f" • {format_alert(a)}")
    return "\n".join(lines)


# ── main loop ────────────────────────────────────────────────────────
def gather_alerts(site_url: str, full_history_urls: list[str],
                  account: str, now_utc: dt.datetime,
                  freshness_hours: int
                  ) -> tuple[list[dict], list[dict], dict]:
    """Shape C entry point. Returns (alerts, anchors, meta)."""
    try:
        return check_chain_anchors(
            site_url, full_history_urls, account, now_utc, freshness_hours,
        )
    except Exception as e:
        return [{
            "id": "anchor_canary:check_error",
            "severity": "warning",
            "detail": f"{type(e).__name__}: {e}",
        }], [], {"witness_url": "", "witness_status": "exception",
                 "witness_detail": str(e), "skipped": False}


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
                "🔁 <b>Still active</b>\n" + format_alert(alert),
                dry_run=dry_run,
            )
            prev["last_reminder"] = now_utc.isoformat()
            prev["snapshot"] = alert

    for aid in list(active.keys()):
        if aid not in current_by_id:
            send_telegram(format_recovered(aid), dry_run=dry_run)
            del active[aid]


def main() -> int:
    p = argparse.ArgumentParser(description="Anchor canary — see module docstring.")
    p.add_argument("--dry-run", action="store_true",
                   help="Print messages to stdout instead of sending Telegram. "
                        "Skips state persistence (read-only invocation).")
    p.add_argument("--force-heartbeat", action="store_true",
                   help="Send a heartbeat regardless of day/hour (for testing).")
    p.add_argument("--test-message", action="store_true",
                   help="Send one 'anchor canary online' test message and exit 0.")
    args = p.parse_args()

    # R5: refuse to run in production mode without credentials.
    check_credentials_or_refuse(args.dry_run)

    if args.test_message:
        ok, note = send_telegram(
            "🟢 <b>Anchor canary online</b> — test message. If you see "
            "this, the delivery channel works.",
            dry_run=args.dry_run,
        )
        print(f"test-message delivered={ok} note={note}", flush=True)
        return 0

    site_url = os.environ.get("ANCHOR_CANARY_SITE_URL", DEFAULT_SITE_URL).strip() \
               or DEFAULT_SITE_URL
    account = os.environ.get("ANCHOR_CANARY_ACCOUNT", DEFAULT_ANCHOR_ACCOUNT).strip() \
              or DEFAULT_ANCHOR_ACCOUNT
    freshness_hours = int(os.environ.get(
        "ANCHOR_CANARY_FRESHNESS_HOURS", DEFAULT_FRESHNESS_HOURS))
    reminder_interval = int(os.environ.get(
        "ANCHOR_CANARY_REMINDER_INTERVAL_SEC", DEFAULT_REMINDER_INTERVAL_SEC))
    full_history_urls = full_history_nodes()

    now_utc = dt.datetime.now(dt.timezone.utc)
    state = load_state()
    try:
        current, anchors, meta = gather_alerts(
            site_url, full_history_urls, account, now_utc, freshness_hours,
        )
    except Exception as e:
        traceback.print_exc()
        send_telegram(
            f"🚨 <b>Anchor canary EXCEPTION</b>: {type(e).__name__}: {e}",
            dry_run=args.dry_run,
        )
        return 0

    # LOUD SKIP: emit no alerts, mutate no state, log-only.
    if meta.get("skipped"):
        return 0

    reconcile(state, current, now_utc, reminder_interval, args.dry_run)

    if args.force_heartbeat or should_send_heartbeat(state, now_utc):
        send_telegram(
            format_heartbeat(current, anchors, meta),
            dry_run=args.dry_run,
        )
        state["last_heartbeat_sent"] = now_utc.isoformat()

    # R6: dry-run is read-only from state's perspective.
    if not args.dry_run:
        save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
