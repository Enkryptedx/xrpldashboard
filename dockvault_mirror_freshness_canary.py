"""DockVault mirror freshness canary.

Charlie ruling 2026-09-07 evening: two days of silent mirror failure
on the only backup of the memory + deployment files is exactly what
the canary pattern is for. This walker checks the last_ok state files
for both mirror jobs and pages via walker_health.findings_count > 0
if either is stale.

Watched jobs (both run on the Mac via launchd):
  * dockvault_mirror        — daily rsync of ~/xrpl_test + ~/.ssh to
                              /Volumes/DockVault/xrpl_mirror.
                              Cadence: 24h (StartInterval 86400).
                              Grace: +2h before firing → 26h ceiling.
  * dockvault_memory_mirror — twice-daily rsync of ~/.openclaw/workspace
                              + JJ's on-Mac memory tree to
                              /Volumes/DockVault/memory_mirror.
                              Cadence: 12h (StartInterval 43200).
                              Grace: +2h before firing → 14h ceiling.

Each job writes a unix-timestamp state file when it completes ok:
  /Users/charliebruce/xrpl_test/launchd_state/<label>_last_ok

The canary reads those files, computes age vs now, and reports any
job whose last_ok is older than its ceiling (or whose state file is
missing entirely) as a walker_health finding. L1 pager reads
walker_health and escalates within the hour.

Cadence for this canary: 60 min (StartInterval 3600). Reading a file
+ one PG UPSERT — cheap. False-negative window is one cadence gap +
one pager cycle, which is well inside the ceilings above.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys


LAUNCHD_STATE_DIR = "/Users/charliebruce/xrpl_test/launchd_state"

# label → (ceiling_seconds, description for the finding)
JOBS: list[tuple[str, int, str]] = [
    ("dockvault_mirror", 26 * 3600,
     "daily 24h rsync ~/xrpl_test + ~/.ssh → /Volumes/DockVault/xrpl_mirror"),
    ("dockvault_memory_mirror", 14 * 3600,
     "twice-daily 12h rsync memory dirs → /Volumes/DockVault/memory_mirror"),
    # Charlie ruling 2026-09-08: added public_route_200_canary here
    # after it went silent for ~24h on Sunday night with no meta-watch
    # ("a watchdog nobody watches is the pattern we keep finding").
    # 15-min walker; 60 min ceiling = up to 4 missed cadences before
    # this meta-canary pages.
    ("public_route_200_canary", 60 * 60,
     "every-15min HTTP-200 sweep of all public + agent-tier + well-known routes on xrpldashboard.com"),
    # Charlie ruling 2026-09-09 morning: added route_5xx_rate_walker
    # alongside its sibling — same 15-min cadence, same meta-watch
    # ceiling. Reads page_views.status and pages any monitored route
    # with n>=20 and pct_5xx > 5% in the last 1h. Complements the
    # route canary: the canary catches a route breaking now (single
    # probe); this walker catches slow burns (route degrading to N%
    # 5xx over a rolling window).
    ("route_5xx_rate_walker", 60 * 60,
     "every-15min rolling-1h 5xx-rate check across monitored routes from page_views.status"),
    # Charlie ruling 2026-09-09 morning ("meta-watch all five"): after the
    # 09-08 StartCalendarInterval → StartInterval sweep, five plists were
    # in silent-failure state (RunAtLoad:false + no baseline stamp for
    # any existing canary to catch). dockvault_neon_dump went 27h stale,
    # DockVault mount-ok heartbeat was withheld, BetterStack paged
    # 2026-09-09 11:24 UTC. Meta-watch expanded here to catch the same
    # class next time. All four not-previously-watched jobs added, plus
    # dockvault_neon_dump (which had a mount-ok heartbeat withholder but
    # wasn't itself in this JOBS list). Wrappers stamp _last_ok on
    # genuine success only (SKIP/FAIL paths deliberately leave stale so
    # a persistent silent-skip surfaces here rather than looking healthy).
    ("dockvault_neon_dump", 26 * 3600,
     "daily 24h pull-back of latest Neon pg_dump from B2 → /Volumes/DockVault/neon_dumps"),
    ("b2_backup", 26 * 3600,
     "daily 24h rclone push of ~/xrpl_test + ~/.ssh → b2crypt bucket"),
    ("signed_registry_snapshot", 26 * 3600,
     "daily 24h sig-service signed registry snapshot → signed_registry_snapshots/YYYY-MM-DD.json"),
    ("refresh_ofac_sdn", 26 * 3600,
     "daily 24h fetch of OFAC SDN.XML → ofac_sdn_addresses.json + auto-commit+push if changed"),
    # Weekly cadence → 193h ceiling (168 + 25h grace, ~15% margin). Wider
    # than daily's 8% because a legitimately-delayed weekly can slip a day
    # without being an incident.
    ("pg_restore_test", 193 * 3600,
     "weekly restore-test: pull latest B2 dump → ephemeral PG :5439 → smoke queries → teardown"),
    # 2026-09-09: nft_activity_summary walker (--mode summary). 300s (5-min)
    # cadence; 1h ceiling = up to 12 missed cadences of grace. Wider ratio
    # than the 15-min canaries because summary staleness is NOT safety-
    # critical — the /nfts route already serves last-known-good on a stale
    # row (30-min banner) and never 500s. This meta-watch catches the walker
    # going fully silent (never stamping), which would eventually freeze the
    # public NFT counts. Wrapper stamps _last_ok on genuine success only.
    ("nft_activity_summary", 3600,
     "every-5min recompute of the single-row /nfts page cache from nft_activity → nft_activity_summary"),
    # Charlie ruling 2026-09-10 morning: added pg_backup_canary after
    # it went stale for ~35h during 09-09/09-10 with no meta-watch
    # (last stamp before the kick was 09-09 13:51 UTC — 35h > 25h
    # freshness threshold, canary reported FAIL but nothing paged on
    # the canary's own silence). Hourly canary; 26h ceiling matches the
    # daily-cadence jobs above. Wrapper (run_pg_backup_canary.sh) stamps
    # _last_ok on success only — same discipline as siblings.
    ("pg_backup_canary", 26 * 3600,
     "hourly freshness check for pg_backup B2 dump (age <= 25h threshold; stamps last_ok on success)"),
]


def check_one(label: str, ceiling_seconds: int) -> dict:
    """Read one job's last_ok file, compute age, return a result dict."""
    path = os.path.join(LAUNCHD_STATE_DIR, f"{label}_last_ok")
    now = dt.datetime.now(dt.timezone.utc)
    if not os.path.exists(path):
        return {
            "label": label,
            "ok": False,
            "reason": "missing_last_ok_file",
            "path": path,
            "age_seconds": None,
            "ceiling_seconds": ceiling_seconds,
        }
    try:
        with open(path) as f:
            ts_str = f.read().strip()
        ts = int(ts_str)
    except (OSError, ValueError) as e:
        return {
            "label": label,
            "ok": False,
            "reason": f"unreadable_last_ok:{type(e).__name__}",
            "path": path,
            "age_seconds": None,
            "ceiling_seconds": ceiling_seconds,
        }

    last_ok = dt.datetime.fromtimestamp(ts, dt.timezone.utc)
    age_seconds = int((now - last_ok).total_seconds())
    if age_seconds > ceiling_seconds:
        return {
            "label": label,
            "ok": False,
            "reason": f"stale_{age_seconds}s_gt_ceiling_{ceiling_seconds}s",
            "path": path,
            "age_seconds": age_seconds,
            "ceiling_seconds": ceiling_seconds,
            "last_ok_utc": last_ok.isoformat(),
        }
    return {
        "label": label,
        "ok": True,
        "reason": "fresh",
        "path": path,
        "age_seconds": age_seconds,
        "ceiling_seconds": ceiling_seconds,
        "last_ok_utc": last_ok.isoformat(),
    }


def _write_walker_health(ok_count: int, fail_count: int,
                         failures: list[dict], all_results: list[dict]) -> None:
    """Write one walker_health row. See public_route_200_canary for
    the same schema pattern."""
    try:
        sys.path.insert(0, "/Users/charliebruce/xrpl_test")
        import db
        if not db.pg_available():
            print("[dockvault_mirror_canary] pg not available; skipping walker_health write",
                  file=sys.stderr, flush=True)
            return
        with db.pg_connect() as conn:
            with conn.cursor() as cur:
                msg = json.dumps({
                    "ok": ok_count, "fail": fail_count,
                    "failures": [(f["label"], f["reason"], f["age_seconds"])
                                 for f in failures],
                    "all": [(r["label"], r["age_seconds"]) for r in all_results],
                })
                now = dt.datetime.now(dt.timezone.utc)
                if fail_count == 0:
                    cur.execute(
                        """
                        INSERT INTO walker_health
                            (walker_name, last_run_started, last_run_completed,
                             last_run_ok, last_run_message, last_success_at,
                             consecutive_failures, cadence_seconds, findings_count)
                        VALUES (%s, %s, %s, TRUE, %s, %s, 0, 3600, 0)
                        ON CONFLICT (walker_name) DO UPDATE
                        SET last_run_started = EXCLUDED.last_run_started,
                            last_run_completed = EXCLUDED.last_run_completed,
                            last_run_ok = TRUE,
                            last_run_message = EXCLUDED.last_run_message,
                            last_success_at = EXCLUDED.last_success_at,
                            consecutive_failures = 0,
                            cadence_seconds = 3600,
                            findings_count = 0
                        """,
                        ("dockvault_mirror_freshness_canary", now, now, msg, now),
                    )
                else:
                    cur.execute(
                        """
                        INSERT INTO walker_health
                            (walker_name, last_run_started, last_run_completed,
                             last_run_ok, last_run_message, last_failure_at,
                             cadence_seconds, findings_count)
                        VALUES (%s, %s, %s, FALSE, %s, %s, 3600, %s)
                        ON CONFLICT (walker_name) DO UPDATE
                        SET last_run_started = EXCLUDED.last_run_started,
                            last_run_completed = EXCLUDED.last_run_completed,
                            last_run_ok = FALSE,
                            last_run_message = EXCLUDED.last_run_message,
                            last_failure_at = EXCLUDED.last_failure_at,
                            consecutive_failures = walker_health.consecutive_failures + 1,
                            cadence_seconds = 3600,
                            findings_count = EXCLUDED.findings_count
                        """,
                        ("dockvault_mirror_freshness_canary", now, now, msg, now, fail_count),
                    )
                conn.commit()
    except Exception as e:
        print(f"[dockvault_mirror_canary] walker_health write failed: {type(e).__name__}: {e}",
              file=sys.stderr, flush=True)


def main() -> int:
    results = []
    failures = []
    ok_count = 0
    for label, ceiling, _desc in JOBS:
        r = check_one(label, ceiling)
        results.append(r)
        if r["ok"]:
            ok_count += 1
        else:
            failures.append(r)

    fail_count = len(failures)
    print(f"[dockvault_mirror_canary] {ok_count} fresh, {fail_count} stale @ "
          f"{dt.datetime.now(dt.timezone.utc).isoformat()}")
    for r in results:
        marker = " OK " if r["ok"] else "STALE"
        age = f"{r['age_seconds'] // 3600}h{(r['age_seconds'] % 3600) // 60:02d}m" \
              if r["age_seconds"] is not None else "n/a"
        ceiling_h = r["ceiling_seconds"] // 3600
        print(f"  {marker}  {r['label']:<28}  age {age:<8}  ceiling {ceiling_h}h  {r['reason']}")

    _write_walker_health(ok_count, fail_count, failures, results)
    return 0 if fail_count == 0 else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
