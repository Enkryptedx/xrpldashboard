"""Mac tunnel health canary.

Charlie ruling 2026-09-07 evening: the Mac-side cloudflared connector
(tunnel `xrpld-mac`, id `df8a4f98-...`, carries sig.xrpldashboard.com →
127.0.0.1:8842) needs the same drift-guard coverage the Lenovo
xrpldashboard-mcp connector already has via its systemd watcher +
l1_pager. Any Mac-side outage silently drops sig-receipt signing —
/check.json fails open (sig_status=sig_unreachable), which is not
paging-worthy on a single call but IS paging-worthy on any sustained
gap. This walker fires on:

  * launchd job for the tunnel is not `running`, OR
  * `cloudflared tunnel list` shows zero edge connections for xrpld-mac.

Cadence: 5 min (StartInterval 300). Cheap: one `launchctl print` grep +
one `cloudflared tunnel list` call, both local subprocess. Writes to
walker_health; L1 pager reads findings_count / consecutive_failures.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys


LAUNCHD_LABEL = "com.charliebruce.xrpldashboard.mac_tunnel"
TUNNEL_NAME = "xrpld-mac"


def _uid() -> str:
    return str(os.getuid())


def check_launchd() -> tuple[bool, str]:
    """Return (ok, reason). Ok when launchctl reports running state."""
    try:
        result = subprocess.run(
            ["launchctl", "print", f"gui/{_uid()}/{LAUNCHD_LABEL}"],
            capture_output=True, text=True, timeout=10,
        )
    except subprocess.TimeoutExpired:
        return False, "launchctl_print_timeout"
    except FileNotFoundError:
        return False, "launchctl_missing"

    if result.returncode != 0:
        return False, f"launchctl_print_rc_{result.returncode}"
    if "state = running" not in result.stdout:
        return False, "launchd_state_not_running"
    return True, "running"


def check_edge_connections() -> tuple[bool, str, int]:
    """Return (ok, reason, connection_count). Ok when >0 edge connections."""
    try:
        result = subprocess.run(
            ["cloudflared", "tunnel", "list"],
            capture_output=True, text=True, timeout=15,
        )
    except subprocess.TimeoutExpired:
        return False, "cloudflared_list_timeout", 0
    except FileNotFoundError:
        return False, "cloudflared_missing", 0
    if result.returncode != 0:
        return False, f"cloudflared_list_rc_{result.returncode}", 0

    for line in result.stdout.splitlines():
        if TUNNEL_NAME not in line:
            continue
        # Column format: ID  NAME  CREATED  CONNECTIONS
        # CONNECTIONS is the last whitespace-separated field, empty when
        # no edge registrations. Empty → the trailing field disappears.
        parts = line.split()
        if len(parts) < 4:
            return False, "no_connections_column", 0
        connections_str = " ".join(parts[3:])
        # Each connector adds one like "1xiad07" or "2xind01"; count
        # commas + 1, or just check non-empty. Use non-empty check.
        connections_str = connections_str.strip()
        if not connections_str:
            return False, "zero_edge_connections", 0
        # Count comma-separated entries as edge locations
        edge_count = len([c for c in connections_str.split(",") if c.strip()])
        return True, f"connected_{edge_count}_locations", edge_count
    return False, "tunnel_not_in_list", 0


def _write_walker_health(ok: bool, findings: list[dict], connection_count: int) -> None:
    try:
        sys.path.insert(0, "/Users/charliebruce/xrpl_test")
        import db
        if not db.pg_available():
            print("[mac_tunnel_canary] pg unavailable; skipping walker_health write",
                  file=sys.stderr, flush=True)
            return
        with db.pg_connect() as conn:
            with conn.cursor() as cur:
                msg = json.dumps({
                    "ok": ok, "connection_count": connection_count,
                    "findings": findings,
                })
                now = dt.datetime.now(dt.timezone.utc)
                if ok:
                    cur.execute(
                        """
                        INSERT INTO walker_health
                            (walker_name, last_run_started, last_run_completed,
                             last_run_ok, last_run_message, last_success_at,
                             consecutive_failures, cadence_seconds, findings_count)
                        VALUES (%s, %s, %s, TRUE, %s, %s, 0, 300, 0)
                        ON CONFLICT (walker_name) DO UPDATE
                        SET last_run_started = EXCLUDED.last_run_started,
                            last_run_completed = EXCLUDED.last_run_completed,
                            last_run_ok = TRUE,
                            last_run_message = EXCLUDED.last_run_message,
                            last_success_at = EXCLUDED.last_success_at,
                            consecutive_failures = 0,
                            cadence_seconds = 300,
                            findings_count = 0
                        """,
                        ("mac_tunnel_health_canary", now, now, msg, now),
                    )
                else:
                    cur.execute(
                        """
                        INSERT INTO walker_health
                            (walker_name, last_run_started, last_run_completed,
                             last_run_ok, last_run_message, last_failure_at,
                             cadence_seconds, findings_count)
                        VALUES (%s, %s, %s, FALSE, %s, %s, 300, %s)
                        ON CONFLICT (walker_name) DO UPDATE
                        SET last_run_started = EXCLUDED.last_run_started,
                            last_run_completed = EXCLUDED.last_run_completed,
                            last_run_ok = FALSE,
                            last_run_message = EXCLUDED.last_run_message,
                            last_failure_at = EXCLUDED.last_failure_at,
                            consecutive_failures = walker_health.consecutive_failures + 1,
                            cadence_seconds = 300,
                            findings_count = EXCLUDED.findings_count
                        """,
                        ("mac_tunnel_health_canary", now, now, msg, now, len(findings)),
                    )
                conn.commit()
    except Exception as e:
        print(f"[mac_tunnel_canary] walker_health write failed: {type(e).__name__}: {e}",
              file=sys.stderr, flush=True)


def main() -> int:
    launchd_ok, launchd_reason = check_launchd()
    edge_ok, edge_reason, edge_count = check_edge_connections()
    findings = []
    if not launchd_ok:
        findings.append({"axis": "launchd", "reason": launchd_reason})
    if not edge_ok:
        findings.append({"axis": "edge", "reason": edge_reason})

    ok = launchd_ok and edge_ok
    print(f"[mac_tunnel_canary] {'OK' if ok else 'FAIL'} launchd={launchd_reason} "
          f"edge={edge_reason} edge_count={edge_count} @ "
          f"{dt.datetime.now(dt.timezone.utc).isoformat()}")
    _write_walker_health(ok, findings, edge_count)
    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
