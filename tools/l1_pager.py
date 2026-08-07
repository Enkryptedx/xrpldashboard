#!/usr/bin/env python3
"""L1 pager — the "wrong stays wrong indefinitely" killer.

Runs on Lenovo every ~20min via systemd timer. Checks fleet
freshness, snapshot cadence, and canary state; delivers new
alerts to Charlie's Telegram; sends a weekly Sunday heartbeat
so silence from the pager itself becomes visible.

Design law (from project_autonomous_oversight_tiers_plan.md):
each oversight layer sends a periodic alive+all-green heartbeat.
Silence from a watchman is itself an alarm — this is the L1
answer to "who watches the watchman."

Checks:
  1. walker_stale        — walker_health row age > max(cadence*3, 24h)
                           AND age < 30d (skip fossil rows).
  2. walker_failing      — walker_health.consecutive_failures >= 3.
  3. snapshot_missed     — most recent signed_snapshots.snapshot_date
                           is not today or yesterday (26h window).
  4. weekly_heartbeat    — Sunday 09:00-10:00 ET, once per week.

Dedup rules:
  - New alert → send immediately, remember `first_fired`.
  - Same alert still active AND >4h since last_reminder → resend.
  - Alert cleared → send "RECOVERED" once, then forget.

Env vars:
  DATABASE_URL              — Neon connection string (required).
  L1_TELEGRAM_BOT_TOKEN     — bot token (optional; dry-run if unset).
  L1_TELEGRAM_CHAT_ID       — target chat id (optional; dry-run if unset).
  L1_STATE_PATH             — state json (default ~/.l1_pager_state.json).
  L1_REMINDER_INTERVAL_SEC  — active-alert reminder cadence (default 14400 = 4h).

Exit code: always 0 unless a hard exception escapes (systemd retry
policy is per-timer; the pager should never no-op-fail loudly).

Invocation:
  Manual: python3 tools/l1_pager.py [--dry-run]
  Timer:  /etc/systemd/system/xrpld-l1-pager.timer (every 20min)
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

try:
    import psycopg
except ImportError:
    psycopg = None


# ── thresholds ──────────────────────────────────────────────────────
STALE_FLOOR_SECONDS = 24 * 3600
STALE_CADENCE_MULTIPLIER = 3
STALE_MAX_AGE_SECONDS = 30 * 86400          # skip rows older than 30d
CONSECUTIVE_FAILURE_THRESHOLD = 3
SNAPSHOT_MAX_AGE_HOURS = 26
DEFAULT_REMINDER_INTERVAL_SEC = 4 * 3600
WEEKLY_HEARTBEAT_WEEKDAY = 6                # Sunday (Mon=0)
WEEKLY_HEARTBEAT_HOUR_MIN = 9
WEEKLY_HEARTBEAT_HOUR_MAX = 10
ET_ZONE = zoneinfo.ZoneInfo("America/New_York")


# ── delivery ────────────────────────────────────────────────────────
def send_telegram(text: str, dry_run: bool = False) -> tuple[bool, str]:
    """POST to Telegram Bot API. Returns (delivered, note).
    Dry-run mode prints to stdout and returns (False, 'dry-run')."""
    token = os.environ.get("L1_TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("L1_TELEGRAM_CHAT_ID", "").strip()
    if dry_run or not token or not chat:
        reason = "dry-run" if dry_run else "credentials unset"
        print(f"[l1 dry {reason}] {text}", flush=True)
        return False, reason
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = urllib.parse.urlencode({
        "chat_id": chat,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    try:
        with urllib.request.urlopen(url, data=body, timeout=15) as resp:
            resp.read()
        return True, "ok"
    except Exception as e:
        # Never crash the pager on delivery failure — log and continue.
        return False, f"telegram send failed: {e}"


# ── state ───────────────────────────────────────────────────────────
def state_path() -> Path:
    override = os.environ.get("L1_STATE_PATH", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".l1_pager_state.json"


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


# ── checks ──────────────────────────────────────────────────────────
def _pg_connect():
    if psycopg is None:
        raise RuntimeError("psycopg not importable")
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError("DATABASE_URL unset")
    return psycopg.connect(url)


def check_walker_stale(now_utc: dt.datetime) -> list[dict]:
    """Return list of active-stale alerts. Skips rows > 30d old
    (fossils — L1 shouldn't scream forever about decommissioned
    walkers)."""
    alerts = []
    with _pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT walker_name, cadence_seconds,
                       EXTRACT(EPOCH FROM (now() - last_success_at))::bigint AS age_s
                  FROM walker_health
                 WHERE cadence_seconds IS NOT NULL
                   AND last_success_at IS NOT NULL
            """)
            for name, cadence, age_s in cur.fetchall():
                if age_s is None or cadence is None:
                    continue
                threshold = max(int(cadence) * STALE_CADENCE_MULTIPLIER,
                                STALE_FLOOR_SECONDS)
                if age_s > threshold and age_s < STALE_MAX_AGE_SECONDS:
                    alerts.append({
                        "id": f"walker_stale:{name}",
                        "walker": name,
                        "cadence_s": int(cadence),
                        "age_s": int(age_s),
                        "threshold_s": threshold,
                    })
    return alerts


def check_walker_failing() -> list[dict]:
    """Alert when a walker has 3+ consecutive failures — real streak,
    not a race-window flicker."""
    alerts = []
    with _pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT walker_name, consecutive_failures,
                       last_failure_at, last_run_message
                  FROM walker_health
                 WHERE consecutive_failures >= %s
            """, (CONSECUTIVE_FAILURE_THRESHOLD,))
            for name, fails, last_fail, msg in cur.fetchall():
                alerts.append({
                    "id": f"walker_failing:{name}",
                    "walker": name,
                    "consecutive_failures": int(fails),
                    "last_failure_at": last_fail.isoformat() if last_fail else None,
                    "last_message": (msg or "")[:200],
                })
    return alerts


def check_snapshot_missed(now_utc: dt.datetime) -> list[dict]:
    """Alert when the newest signed snapshot is older than 26h. Daily
    signed snapshot is core to the sovereignty story — missing one
    day is worth waking Charlie."""
    alerts = []
    with _pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT snapshot_date, written_at
                  FROM signed_snapshots
                 ORDER BY snapshot_date DESC
                 LIMIT 1
            """)
            row = cur.fetchone()
    if not row:
        alerts.append({
            "id": "snapshot_missed:no_rows",
            "detail": "signed_snapshots table is empty",
        })
        return alerts
    _snap_date, written_at = row
    age_h = (now_utc - written_at).total_seconds() / 3600
    if age_h > SNAPSHOT_MAX_AGE_HOURS:
        alerts.append({
            "id": "snapshot_missed:latest",
            "age_hours": round(age_h, 1),
            "last_written_at": written_at.isoformat(),
        })
    return alerts


# ── formatting ──────────────────────────────────────────────────────
def _human_age(seconds: int) -> str:
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


def format_alert(alert: dict) -> str:
    kind = alert["id"].split(":", 1)[0]
    if kind == "walker_stale":
        return (
            f"🟥 <b>STALE walker</b>: <code>{alert['walker']}</code> — "
            f"no success in {_human_age(alert['age_s'])} "
            f"(cadence {_human_age(alert['cadence_s'])}, "
            f"threshold {_human_age(alert['threshold_s'])})"
        )
    if kind == "walker_failing":
        msg = alert.get("last_message") or ""
        tail = f"\nlast msg: <code>{msg}</code>" if msg else ""
        return (
            f"🟥 <b>FAILING walker</b>: <code>{alert['walker']}</code> — "
            f"{alert['consecutive_failures']} consecutive failures{tail}"
        )
    if kind == "snapshot_missed":
        if alert["id"].endswith("no_rows"):
            return "🟥 <b>SNAPSHOT missed</b>: signed_snapshots table is empty"
        return (
            f"🟥 <b>SNAPSHOT missed</b>: newest signed snapshot is "
            f"{alert['age_hours']}h old (last written {alert['last_written_at']})"
        )
    return f"🟥 <b>ALERT</b>: {alert['id']}"


def format_recovered(alert_id: str) -> str:
    return f"🟩 <b>RECOVERED</b>: <code>{alert_id}</code>"


def format_heartbeat(alerts: list[dict]) -> str:
    if not alerts:
        return (
            "🟢 <b>L1 weekly heartbeat</b>\n"
            "All checks green (walker freshness, consecutive failures, "
            "daily snapshot). Pager is alive."
        )
    lines = ["🟡 <b>L1 weekly heartbeat</b>", "Active alerts:"]
    for a in alerts:
        lines.append(f" • {format_alert(a)}")
    return "\n".join(lines)


# ── main loop ───────────────────────────────────────────────────────
def gather_alerts(now_utc: dt.datetime) -> list[dict]:
    alerts = []
    for fn, kwargs in (
        (check_walker_stale, {"now_utc": now_utc}),
        (check_walker_failing, {}),
        (check_snapshot_missed, {"now_utc": now_utc}),
    ):
        try:
            alerts.extend(fn(**kwargs))
        except Exception as e:
            # Pager check itself failed — surface, don't crash.
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
    """Diff current alerts vs state; deliver new/reminder/recovered messages."""
    current_by_id = {a["id"]: a for a in current}
    active = state["active_alerts"]

    # New + reminder
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

    # Cleared
    for aid in list(active.keys()):
        if aid not in current_by_id:
            send_telegram(format_recovered(aid), dry_run=dry_run)
            del active[aid]


def main() -> int:
    p = argparse.ArgumentParser(description="L1 pager — see module docstring.")
    p.add_argument("--dry-run", action="store_true",
                   help="Print messages to stdout instead of sending Telegram.")
    p.add_argument("--force-heartbeat", action="store_true",
                   help="Send a heartbeat regardless of day/hour (for testing).")
    p.add_argument("--test-message", action="store_true",
                   help="Send one 'L1 pager online' test message and exit 0.")
    args = p.parse_args()

    if args.test_message:
        ok, note = send_telegram(
            "🟢 <b>L1 pager online</b> — test message. If you see this, "
            "the delivery channel works.",
            dry_run=args.dry_run,
        )
        print(f"test-message delivered={ok} note={note}", flush=True)
        return 0

    now_utc = dt.datetime.now(dt.timezone.utc)
    reminder_interval = int(os.environ.get(
        "L1_REMINDER_INTERVAL_SEC", DEFAULT_REMINDER_INTERVAL_SEC))

    state = load_state()
    try:
        current = gather_alerts(now_utc)
    except Exception as e:
        # Total-failure fallback — always tell Charlie.
        traceback.print_exc()
        send_telegram(
            f"🟥 <b>L1 pager EXCEPTION</b>: {type(e).__name__}: {e}",
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
