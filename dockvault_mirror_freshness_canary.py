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
import time as _time


LAUNCHD_STATE_DIR = "/Users/charliebruce/xrpl_test/launchd_state"

# ── per-tree source-vs-mirror freshness (added 2026-11-02) ──────────────────
# ABSOLUTE AGE IS THE WRONG TEST for an rclone mirror. rclone only copies
# CHANGED files, so a tree that genuinely has not changed looks ancient while
# being perfectly backed up. ~/.ssh is the standing example: its newest file
# is 2026-08-08 in BOTH source and mirror, which is correct, not cold. An
# absolute-age rule would have paged on it every single day, forever, and
# taught everyone to ignore this canary.
#
# The honest test is PER FILE: every source file that has had time to be
# copied must exist in the mirror and must not be older there than at the
# source. A file changed within the last cycle is exempt, because no mirror
# run has been due yet.
#
# Comparing the two trees' NEWEST files instead conflates two different
# things and misreports in both directions. A quiet tree that suddenly gains
# one much-newer file reads as catastrophically behind: on 2026-11-06
# xrpl_test_private_infra reported source_ahead_299581s (3.5 days) when the
# mirror was in fact complete as of its last run -- the source's newest file
# was minutes old, the mirror's newest was 3 days old because nothing had
# changed in between, and subtracting the two measured the quiet period, not
# the lag. In the other direction it hides real loss: a mirror missing a
# hundred files still passes as long as ONE recently-copied file is new
# enough, since a single newest mtime says nothing about coverage.
#
# Compares FILE mtimes, never DIRECTORY mtime. rclone updates files
# inside a directory without touching the directory's own mtime, so a dir
# mtime reads as a week stale while the contents are current -- that exact
# misreading sent this investigation down a wrong path on 2026-11-02.
MIRROR_ROOT = "/Volumes/DockVault/xrpl_mirror"
MIRROR_CYCLE_SECONDS = 25 * 3600  # 24h cadence + 1h slack

# (source_path, mirror_subdir) — must track SOURCES in run_dockvault_mirror.sh
MIRROR_TREES: list[tuple[str, str]] = [
    ("/Users/charliebruce/xrpl_test", "xrpl_test"),
    ("/Users/charliebruce/.ssh", "dot_ssh"),
    ("/Users/charliebruce/xrpl_test_private_infra", "xrpl_test_private_infra"),
]

# Bare `git clone --mirror` targets. Proof of arrival here is the HEAD COMMIT
# ID, not an mtime: a commit ID is the only honest evidence that HISTORY
# crossed, rather than just the working files beside it.
GIT_MIRRORS: list[tuple[str, str]] = [
    ("/Users/charliebruce/xrpl_test_private_infra",
     "xrpl_test_private_infra.git"),
]


# Anything rclone will not copy must not be counted on the SOURCE side, or the
# source looks permanently "ahead" of a mirror that is in fact complete. Kept
# in sync with b2_backup.excludes BY HAND and deliberately NOT read from it:
# that file is shared with run_b2_backup.sh and must not gain a
# canary-specific dependency.
#
# Two false positives this closes, both found on 2026-11-02:
#   * ~/.ssh holds a live ssh-agent SOCKET (s.*.agent.*). rclone skips it
#     correctly (12 entries in source, 11 in mirror), but os.walk lists it, so
#     dot_ssh read as 1010h behind forever. Sockets are not regular files --
#     hence the S_ISREG test below, which `find -type f` was doing implicitly
#     and os.walk was not.
#   * xrpl_test carries __pycache__/*.pyc and venv/, rewritten constantly and
#     excluded from the mirror. Counting them would make that tree flap.
_EXCLUDE_DIRS = {".git", "__pycache__", "venv", ".venv", "node_modules"}
_EXCLUDE_NAMES = {".DS_Store"}
_EXCLUDE_SUFFIXES = (".pyc", ".pyo", ".swp")


# Filesystems disagree about sub-second mtime, and rclone rounds, so a copy
# can land a hair "older" than its source while being a faithful copy. Only a
# gap wider than this counts as the mirror being behind.
MIRROR_MTIME_SLACK_SECONDS = 2


def _iter_files(root: str) -> dict[str, int]:
    """Map relative path -> mtime for every REGULAR file under `root`,
    honouring the mirror's exclusions.

    Walks files explicitly rather than stat'ing `root` -- see the module note
    above on why directory mtime is useless here.

    Only regular files count. Sockets, fifos and devices are skipped because
    rclone does not copy them, so including them guarantees a false positive
    that no amount of backing up can clear.
    """
    import stat as _stat
    out: dict[str, int] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDE_DIRS]
        for fn in filenames:
            if fn in _EXCLUDE_NAMES or fn.endswith(_EXCLUDE_SUFFIXES):
                continue
            full = os.path.join(dirpath, fn)
            try:
                st = os.stat(full)
            except OSError:
                continue
            if not _stat.S_ISREG(st.st_mode):
                continue
            out[os.path.relpath(full, root)] = int(st.st_mtime)
    return out


def check_tree(src: str, sub: str, now_ts: int | None = None) -> dict:
    """Compare one source tree against its mirror, FILE BY FILE. Same dict
    shape as check_one so the existing print + walker_health plumbing is
    unchanged.

    `now_ts` is injectable so tests can place files either side of the cycle
    boundary without sleeping or touching the clock.
    """
    label = f"mirror:{sub}"
    dst = os.path.join(MIRROR_ROOT, sub)
    base = {"label": label, "ceiling_seconds": MIRROR_CYCLE_SECONDS,
            "age_seconds": None}
    if not os.path.isdir(src):
        return {**base, "ok": False, "reason": "source_missing"}
    if not os.path.isdir(dst):
        return {**base, "ok": False, "reason": "mirror_missing"}
    src_files = _iter_files(src)
    dst_files = _iter_files(dst)
    if not src_files:
        # Nothing to back up is not a failure.
        return {**base, "ok": True, "age_seconds": 0, "reason": "source_empty"}
    if not dst_files:
        return {**base, "ok": False, "reason": "mirror_empty_but_source_has_files"}

    now = int(_time.time()) if now_ts is None else int(now_ts)
    missing: list[str] = []
    older: list[str] = []
    worst = 0
    for rel, s_mt in src_files.items():
        # Too fresh to expect: no mirror run has been due since it changed.
        if now - s_mt <= MIRROR_CYCLE_SECONDS:
            continue
        d_mt = dst_files.get(rel)
        if d_mt is None:
            missing.append(rel)
            worst = max(worst, now - s_mt)
        elif s_mt - d_mt > MIRROR_MTIME_SLACK_SECONDS:
            older.append(rel)
            worst = max(worst, s_mt - d_mt)

    if missing or older:
        # Name one concrete file: "3 missing" sends you looking, a path tells
        # you immediately whether it is real loss or an exclusion gap.
        example = sorted(missing or older)[0]
        return {**base, "ok": False, "age_seconds": worst,
                "reason": (f"stale_files_missing_{len(missing)}"
                           f"_older_{len(older)}_worst_{worst}s_eg_{example}")}
    return {**base, "ok": True, "age_seconds": 0,
            "reason": f"all_{len(src_files)}_files_mirrored"}


def _git_head(git_dir: str) -> str | None:
    import subprocess
    try:
        out = subprocess.run(["git", "--git-dir", git_dir, "rev-parse", "HEAD"],
                             capture_output=True, text=True, timeout=30)
        return out.stdout.strip() or None if out.returncode == 0 else None
    except Exception:  # noqa: BLE001 -- a canary must not crash on git
        return None


def _git_commit_ts(git_dir: str, rev: str) -> int | None:
    import subprocess
    try:
        out = subprocess.run(["git", "--git-dir", git_dir, "show", "-s",
                              "--format=%ct", rev],
                             capture_output=True, text=True, timeout=30)
        return int(out.stdout.strip()) if out.returncode == 0 else None
    except Exception:  # noqa: BLE001
        return None


def check_git_mirror(src: str, sub: str) -> dict:
    """Compare source HEAD against the bare mirror's HEAD by COMMIT ID.

    Matching IDs is the pass. A MISMATCH is only acceptable while the newer
    source commit is still inside one mirror cycle -- i.e. the nightly run
    has not had its turn yet. Beyond that the history genuinely is not backed
    up. Without the cycle allowance this would page on every daytime commit;
    without the ID check an mtime would call a working-tree copy 'history'.
    """
    label = f"gitmirror:{sub}"
    base = {"label": label, "ceiling_seconds": MIRROR_CYCLE_SECONDS,
            "age_seconds": None}
    src_git = os.path.join(src, ".git")
    dst_git = os.path.join(MIRROR_ROOT, sub)
    if not os.path.isdir(src_git):
        return {**base, "ok": False, "reason": "source_not_a_git_repo"}
    if not os.path.isdir(dst_git):
        return {**base, "ok": False, "reason": "git_mirror_missing"}
    s_head = _git_head(src_git)
    m_head = _git_head(dst_git)
    if s_head is None:
        return {**base, "ok": False, "reason": "source_head_unreadable"}
    if m_head is None:
        return {**base, "ok": False, "reason": "mirror_head_unreadable"}
    if s_head == m_head:
        return {**base, "ok": True, "age_seconds": 0,
                "reason": f"head_match_{s_head[:8]}"}
    ts = _git_commit_ts(src_git, s_head)
    if ts is None:
        return {**base, "ok": False,
                "reason": f"head_mismatch_src_{s_head[:8]}_mirror_{m_head[:8]}"}
    lag = int(dt.datetime.now(dt.timezone.utc).timestamp()) - ts
    if lag > MIRROR_CYCLE_SECONDS:
        return {**base, "ok": False, "age_seconds": lag,
                "reason": (f"head_mismatch_src_{s_head[:8]}_mirror_"
                           f"{m_head[:8]}_commit_{lag}s_old")}
    return {**base, "ok": True, "age_seconds": lag,
            "reason": (f"head_mismatch_pending_next_run_src_{s_head[:8]}_"
                       f"mirror_{m_head[:8]}")}

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
    # 2026-09-26: geoip_health_canary (15-min) — pages when /healthz.geoip is
    # unavailable or region_code goes NULL (MaxMind download-limit incident).
    # Same 60-min ceiling as public_route_200_canary.
    # 2026-09-26: weekly GeoLite2 refresher (StartInterval 604800). Ceiling
    # 9 days = one missed weekly slot + margin; geoip_health_canary also
    # pages if the store blob itself goes stale (>14 d) via /healthz.
    ("geoip_db_refresh_walker", 9 * 86400,
     "weekly GeoLite2-City download → Postgres geoip_db_blob"),
    ("geoip_health_canary", 60 * 60,
     "15-min /healthz.geoip + page_views region NULL-ratio canary"),
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
    # Charlie ruling 2026-09-21 Mon PM: /whales cold-render was 5-19s
    # on 3 of 12 tier×filter combos; walker precomputes all 12 cell
    # bodies into whales_summary so the route serves sub-ms. Same
    # meta-watch shape as nft_activity_summary: 5-min cadence, 1h
    # ceiling. Wrapper stamps _last_ok on success only.
    ("whales_summary_walker", 3600,
     "every-5min recompute of the 12 (tier, filter) cell bodies for /whales into whales_summary"),
    # Charlie ruling 2026-09-25 (item 3): homepage_summary_walker joins the
    # meta-watch. It pre-renders the `/` body per locale every 5 min into
    # the homepage_summary PG row; the route serves it if <30 min old else
    # falls to a slow live render. Same shape as whales_summary_walker
    # (5-min cadence, 1h ceiling = up to 12 missed cadences of grace before
    # paging). Motivating class: the 2026-09-23 relay-URL regression cached
    # a sovereignty-broken body and served it silently for 30 min — this
    # meta-watch catches the walker going fully silent (never stamping),
    # which would freeze the homepage on its last body. Wrapper stamps
    # _last_ok on success only.
    ("homepage_summary_walker", 3600,
     "every-5min per-locale pre-render of the / body into homepage_summary; route serves <30min-old else live-renders"),
    # Charlie ruling 2026-09-10 morning: added pg_backup_canary after
    # it went stale for ~35h during 09-09/09-10 with no meta-watch
    # (last stamp before the kick was 09-09 13:51 UTC — 35h > 25h
    # freshness threshold, canary reported FAIL but nothing paged on
    # the canary's own silence). Hourly canary; 26h ceiling matches the
    # daily-cadence jobs above. Wrapper (run_pg_backup_canary.sh) stamps
    # _last_ok on success only — same discipline as siblings.
    ("pg_backup_canary", 26 * 3600,
     "hourly freshness check for pg_backup B2 dump (age <= 25h threshold; stamps last_ok on success)"),
    # Charlie ruling 2026-09-10 evening: added token_elevation_walker
    # (Part C item 5). Hourly scan of labeled + self-described rows with
    # https .toml citations; runs two-way TOML verification via
    # shared_tier_verifier.resolve_tier(elevate=True); writes
    # source='two_way_toml' history rows on pass. Meta-watch ceiling 2h
    # (2× cadence, tight — a silent walker means self-described tokens
    # stop earning verified promotions and citation-only entries never
    # reach the two-way-proven signal).
    ("token_elevation_walker", 2 * 3600,
     "hourly two-way TOML verification of labeled/self-described tokens with .toml citations; elevates via source=two_way_toml history rows"),
    # Charlie ruling 2026-09-22 Tue 8:42 PM ET (post live-feed sovereignty
    # flip): browser live feed on /tokens, /whales, /pools, /wallet now
    # rides our own-node WSS relay at wss://wss.xrpldashboard.com. If the
    # relay flaps or the CF tunnel goes down, browsers fall back to
    # xrplcluster and walker_node_fallback fills — but that's a client-
    # side signal. This meta-watch catches the same class from an
    # independent Mac-side handshake probe every 15 min. Ceiling 1h (4x
    # cadence), same shape as the 15-min canaries above.
    ("wss_relay_canary", 3600,
     "every-15min WSS handshake to wss://wss.xrpldashboard.com + subscribe:ledger — waits ≤10s for one ledgerClosed; stamps last_ok on success"),
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

    # Per-tree source-vs-mirror checks. These catch the case last_ok cannot:
    # the job ran and exited clean, but a given tree did not actually arrive.
    for src, sub in MIRROR_TREES:
        r = check_tree(src, sub)
        results.append(r)
        if r["ok"]:
            ok_count += 1
        else:
            failures.append(r)

    for src, sub in GIT_MIRRORS:
        r = check_git_mirror(src, sub)
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
