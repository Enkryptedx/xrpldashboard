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
  4. sovereignty_loss    — Class A walker has ≥12 `unreachable:*` or
                           `local_*` walker_node_fallback events in the
                           trailing 6h AND the earliest event in the
                           trailing 24h is ≥12h old (proves sustained,
                           not a blip). Would have caught the 2026-07
                           Mac walker fallback flood on day-of. Class B
                           walkers (check_page/token_page/unknown) are
                           the Render→Lenovo hairpin baseline — always
                           suppressed by allowlist, not blocklist.
  5. weekly_heartbeat    — Sunday 09:00-10:00 ET, once per week.

Dedup / suppression rules (see `reconcile`):
  - NEW alert (unseen id)                       → page immediately.
  - EXISTING alert, fingerprint CHANGED         → page immediately as
    a breakthrough; reset throttle clock.
  - EXISTING alert, fingerprint UNCHANGED,
      within 6h of last page                    → suppress; add to
      the per-tick digest line.
  - EXISTING alert, fingerprint UNCHANGED,
      past 6h                                   → full re-page + reset.
  - CLEARED alert                               → RECOVERED, immediate.
  - End of tick: if any suppressed alerts, one digest message with
    per-alert next-repage times so silence never means "gone."

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
DEFAULT_REMINDER_INTERVAL_SEC = 6 * 3600     # re-page throttle floor
# Once a specific (walker, reason) condition has paged, its "still-active"
# reminder does not re-page more often than this. Fingerprint changes,
# new alerts, and recoveries all bypass the throttle immediately (see
# reconcile). Escalation on materially-worsening rate (e.g. 2× recent_count
# in one throttle window) is a future refinement — not shipped in the
# first suppression cut.
# ── per-walker staleness mutes ──────────────────────────────────────
# Dated, pointed mutes for known-harmless stale walkers.
# Each entry: walker_name → ISO expiry date (inclusive).
# Must reference a doc + build slot — not a permanent blindfold.
# Restore-test lesson: silenced monitors carry expiry + paper trail.
WALKER_STALENESS_MUTES: dict[str, str] = {
    # Two power events (2026-08-13 + 2026-08-20) reset StartInterval;
    # launchd has rescheduled the run to 2026-08-27 (tomorrow).
    # Known harmless: token naming is additive, no data at risk.
    # Documented in docs/TOKEN_NAMING_DEEP_DIVE.md §1.
    # Post-cert rebuild slot confirmed (behind POOLS-COMETS).
    # Expiry: 2026-09-08 — two weeks post-cert to cover the rebuild window.
    "enrich_token_names": "2026-09-08",
}

WEEKLY_HEARTBEAT_WEEKDAY = 6                # Sunday (Mon=0)
WEEKLY_HEARTBEAT_HOUR_MIN = 9
WEEKLY_HEARTBEAT_HOUR_MAX = 10
ET_ZONE = zoneinfo.ZoneInfo("America/New_York")

# ── sovereignty-loss check ──────────────────────────────────────────
# Class A walkers are Mac-hosted and MUST reach the Lenovo sovereign
# rippled at 192.168.40.95:5005. Fallback to public s1/s2 means we lost
# sovereignty — the exact class of silent-failure that ran 43 days
# undetected mid-2026 (framework Python 3.14 + launchd + utun4 VPN routes
# → EHOSTUNREACH). Rule below would have caught it on hour ~12 of the
# flood instead of day 43. Class B walkers are Render-side; their
# hairpin back to Lenovo is a structural problem tracked separately —
# never in this pager's scope.
SOVEREIGNTY_CLASS_A_WALKERS = frozenset({
    "escrow_walker",
    "nft_activity",
    "oracle_walker",
    "rlusd_refresher",
})
# Documentation-only allowlist of things we EXPECT to see in
# walker_node_fallback for reasons unrelated to Mac-sovereignty loss.
# Query below uses Class A explicitly (allowlist), so this set is
# informational — a place for the on-call reader to see why Render-side
# hairpin noise doesn't fire the pager.
# cold_storage / escrow_supply are Flask modules imported by app.py on
# Render (cold_storage.py + escrow_supply.py → xrpl_client → local first
# → cascade). Render's AWS network cannot route to 192.168.40.95:5005,
# so every request hairpins to s1/s2 by design. Rate tracks visitor
# traffic, not walker cadence. Mis-shelved in Class A until 2026-08-17.
SOVEREIGNTY_CLASS_B_WALKERS = frozenset({
    "check_page", "token_page", "unknown",
    "cold_storage", "escrow_supply",
})
SOVEREIGNTY_WINDOW_HOURS = 6                # rate-check window
SOVEREIGNTY_MIN_EVENTS = 12                 # events in that window
SOVEREIGNTY_MIN_SUSTAIN_HOURS = 12          # earliest event must be ≥12h old


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
                mute_exp = WALKER_STALENESS_MUTES.get(name)
                if mute_exp and dt.date.today() <= dt.date.fromisoformat(mute_exp):
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


def _fetch_sovereignty_rows(now_utc: dt.datetime) -> list[tuple]:
    """Pull raw walker_node_fallback rows for Class A walkers in the
    trailing 24h where the reason indicates a sovereignty-loss (local
    node unreachable or degraded). Returns list of
    (walker_name, ts, reason) tuples. Split out from the check so the
    threshold logic below is pure-Python + unit-testable."""
    with _pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT walker_name, ts, reason
                  FROM walker_node_fallback
                 WHERE walker_name = ANY(%s)
                   AND (reason LIKE 'unreachable:%%'
                        OR reason LIKE 'local_%%')
                   AND ts > NOW() - INTERVAL '24 hours'
                """,
                (list(SOVEREIGNTY_CLASS_A_WALKERS),),
            )
            return list(cur.fetchall())


def _evaluate_sovereignty_rows(rows: list[tuple],
                               now_utc: dt.datetime) -> list[dict]:
    """Pure function: given raw (walker_name, ts, reason) rows, apply the
    Class A allowlist + threshold logic and return the alert list.

    Threshold: fire when a walker has ≥SOVEREIGNTY_MIN_EVENTS events in
    the trailing SOVEREIGNTY_WINDOW_HOURS AND its earliest event in the
    input window is ≥SOVEREIGNTY_MIN_SUSTAIN_HOURS old (sustain check —
    refuses to fire on a fresh blip).

    Class B walkers are excluded by the allowlist filter here (belt on
    top of the SQL WHERE braces — if the SQL is edited or the caller
    passes wider input, we still don't page on structural noise)."""
    by_walker: dict[str, list[tuple[dt.datetime, str]]] = {}
    for walker_name, ts, reason in rows:
        if walker_name not in SOVEREIGNTY_CLASS_A_WALKERS:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=dt.timezone.utc)
        by_walker.setdefault(walker_name, []).append((ts, reason))

    alerts = []
    window_cutoff = now_utc - dt.timedelta(hours=SOVEREIGNTY_WINDOW_HOURS)
    sustain_cutoff = now_utc - dt.timedelta(hours=SOVEREIGNTY_MIN_SUSTAIN_HOURS)
    for walker, events in by_walker.items():
        recent = [(t, r) for t, r in events if t > window_cutoff]
        if len(recent) < SOVEREIGNTY_MIN_EVENTS:
            continue
        earliest_ts = min(t for t, _ in events)
        latest_ts = max(t for t, _ in events)
        if earliest_ts > sustain_cutoff:
            continue
        alerts.append({
            "id": f"sovereignty_loss:{walker}",
            "walker": walker,
            "recent_count": len(recent),
            "window_hours": SOVEREIGNTY_WINDOW_HOURS,
            "earliest_age_s": int((now_utc - earliest_ts).total_seconds()),
            "latest_age_s": int((now_utc - latest_ts).total_seconds()),
            "sample_reason": (recent[0][1] or "")[:120],
        })
    return alerts


def check_sovereignty_loss(now_utc: dt.datetime) -> list[dict]:
    """Alert when a Class A (Mac-hosted) walker has been cascading past
    its sovereign local node for ≥SOVEREIGNTY_MIN_EVENTS events in the
    trailing SOVEREIGNTY_WINDOW_HOURS AND its earliest fallback event
    within the trailing 24h is ≥SOVEREIGNTY_MIN_SUSTAIN_HOURS old.

    See _evaluate_sovereignty_rows for the pure-Python threshold logic.
    Class B walkers (check_page/token_page/unknown) are Render→Lenovo
    hairpin noise — excluded by allowlist, not blocklist."""
    rows = _fetch_sovereignty_rows(now_utc)
    return _evaluate_sovereignty_rows(rows, now_utc)


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
    if kind == "sovereignty_loss":
        # Earliest AND latest age both surfaced so a reader can tell
        # "fresh fire" (latest ≈ minutes ago) from "echo of an already-
        # resolved incident" (latest ≈ hours ago) at a glance.
        latest_line = ""
        if "latest_age_s" in alert:
            latest_line = (
                f" · latest event {_human_age(alert['latest_age_s'])} ago"
            )
        return (
            f"🟥 <b>SOVEREIGNTY LOST</b>: <code>{alert['walker']}</code> "
            f"cascaded past sovereign local node "
            f"{alert['recent_count']}× in trailing {alert['window_hours']}h "
            f"(earliest event {_human_age(alert['earliest_age_s'])} old"
            f"{latest_line} — sustained, not a blip).\n"
            f"sample reason: <code>{alert['sample_reason']}</code>\n"
            f"Check Mac VPN routing / launchd Python interpreter / "
            f"192.168.40.95:5005 reachability from the walker's context."
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


# ── suppression fingerprint + digest ───────────────────────────────
def _alert_fingerprint(alert: dict) -> str:
    """Stable identity string used to decide whether a still-active
    alert has *materially changed* since we last paged. Only text/reason
    fields are included — pure count/age drift within the same reason
    does NOT re-page. Fingerprint change (reason string flips) DOES
    re-page immediately (see reconcile). Keep this list narrow: adding
    a volatile field here reintroduces the page-storm this patch was
    written to end."""
    return "|".join(str(alert.get(k, "")) for k in (
        "id",
        "sample_reason",       # sovereignty_loss
        "last_message",        # walker_failing
    ))


def _format_digest(entries: list[dict], now_utc: dt.datetime,
                   throttle_sec: int) -> str:
    """One-line-per-suppressed-alert summary for a tick where re-pages
    were held back. Silence is not "gone"; it is "known and unchanged."
    The next full re-page time per alert is printed so an operator can
    see when the throttle expires."""
    lines = ["🔇 <b>Still-active, unchanged</b> "
             f"(suppressed by {throttle_sec // 3600}h re-page throttle):"]
    for e in entries:
        first_fired = e["first_fired"]
        next_repage = e["next_repage"]
        lines.append(
            f" • <code>{e['id']}</code> — since "
            f"{first_fired.astimezone(ET_ZONE).strftime('%m-%d %H:%M ET')}, "
            f"next re-page at "
            f"{next_repage.astimezone(ET_ZONE).strftime('%H:%M ET')}"
        )
    return "\n".join(lines)


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
        (check_sovereignty_loss, {"now_utc": now_utc}),
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
    """Diff current alerts vs state; deliver new/reminder/recovered messages.

    Suppression rules (see module docstring "Dedup rules"):
      - NEW alert (unseen id) → page immediately.
      - EXISTING alert, fingerprint CHANGED (e.g. sample_reason flipped)
        → page immediately as a breakthrough; reset the throttle clock.
      - EXISTING alert, fingerprint UNCHANGED, within `reminder_interval`
        of last page → SUPPRESSED; row added to the digest.
      - EXISTING alert, fingerprint UNCHANGED, past `reminder_interval`
        → re-page and reset the throttle clock.
      - CLEARED alert → RECOVERED message, immediate.

    After the per-alert loop, if any suppressions happened this tick,
    one digest message summarizes them so silence never means "gone."
    """
    current_by_id = {a["id"]: a for a in current}
    active = state["active_alerts"]
    digest_entries: list[dict] = []

    for aid, alert in current_by_id.items():
        prev = active.get(aid)
        fp = _alert_fingerprint(alert)

        if prev is None:
            # New condition — always breaks through, no throttle applies.
            send_telegram(format_alert(alert), dry_run=dry_run)
            active[aid] = {
                "first_fired": now_utc.isoformat(),
                "last_reminder": now_utc.isoformat(),
                "fingerprint": fp,
                "snapshot": alert,
            }
            continue

        prev_fp = prev.get("fingerprint", "")
        last_reminder = dt.datetime.fromisoformat(prev["last_reminder"])
        if last_reminder.tzinfo is None:
            last_reminder = last_reminder.replace(tzinfo=dt.timezone.utc)
        age_since_page = (now_utc - last_reminder).total_seconds()

        if prev_fp and prev_fp != fp:
            # Materially changed (reason/message flipped) — breakthrough.
            send_telegram(
                "⚠️ <b>Changed</b>\n" + format_alert(alert),
                dry_run=dry_run,
            )
            prev["last_reminder"] = now_utc.isoformat()
            prev["fingerprint"] = fp
            prev["snapshot"] = alert
            continue

        if age_since_page > reminder_interval:
            # Throttle expired — full re-page.
            send_telegram(
                "🔁 <b>Still active</b>\n" + format_alert(alert),
                dry_run=dry_run,
            )
            prev["last_reminder"] = now_utc.isoformat()
            prev["fingerprint"] = fp
            prev["snapshot"] = alert
            continue

        # Suppressed — record for the digest, do not send full page.
        first_fired = dt.datetime.fromisoformat(prev["first_fired"])
        if first_fired.tzinfo is None:
            first_fired = first_fired.replace(tzinfo=dt.timezone.utc)
        next_repage = last_reminder + dt.timedelta(seconds=reminder_interval)
        digest_entries.append({
            "id": aid,
            "first_fired": first_fired,
            "next_repage": next_repage,
        })
        # Refresh the snapshot so operator-visible fields (counts, ages)
        # stay current in state even when not paged.
        prev["snapshot"] = alert

    # Cleared — recoveries always break through (good news travels fast).
    for aid in list(active.keys()):
        if aid not in current_by_id:
            send_telegram(format_recovered(aid), dry_run=dry_run)
            del active[aid]

    # Emit the digest last — one line per suppressed alert, one message.
    if digest_entries:
        send_telegram(
            _format_digest(digest_entries, now_utc, reminder_interval),
            dry_run=dry_run,
        )


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
