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
import re
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
# source looks permanently "ahead" of a mirror that is in fact complete.
#
# This list is now READ FROM the same file rclone is given,
# launchd/b2_backup.excludes, rather than hand-copied. The hand-copied set
# held 5 directory names, 1 filename and 3 suffixes; the real file holds 40+
# patterns including *.log, launchd_logs/**, .env.*, *.prev, *.tmp and
# .openclaw/**. Every pattern missing from the copy became a phantom "missing
# from mirror" finding: on 2026-11-06 the per-file check reported 1429 stale
# files for xrpl_test, headed by .env.example, which `.env.*` excludes.
# Sharing the file is the point -- the canary must measure the same tree
# rclone was actually asked to copy, and a hand-synced duplicate of a
# security-relevant exclude list drifts silently in the dangerous direction.
# Nothing is written back to that file; the dependency is read-only.
#
# NOTE: launchd/ is gitignored (.gitignore "launchd/", public-repo boundary,
# 2026-09-07), so b2_backup.excludes exists on this Mac and in NO clone, CI
# runner or deploy. That is acceptable because the canary and the mirror are
# both Mac-local, but it means the file can be absent, so an unreadable
# exclude file is reported as a FAILED check rather than treated as "exclude
# nothing" -- the permissive reading would bury a real gap under thousands of
# phantom findings. Tests must point EXCLUDES_FILE at their own fixture and
# never at the real path, which does not exist where they run.
#
# SYMLINKS ARE SKIPPED. rclone is invoked with neither --links nor
# --copy-links, so it silently skips them. os.stat FOLLOWS a link, so a
# symlink to a regular file passes S_ISREG and reads as a real file the
# mirror is missing -- that is exactly how
# launchd/...permissioned_domains_walker.plist (a link into ~/xrpl_test)
# reported as 46 days absent from the private_infra mirror while nothing was
# wrong. Use lstat semantics, not stat.
#
# Two false positives closed earlier, both found on 2026-11-02:
#   * ~/.ssh holds a live ssh-agent SOCKET (s.*.agent.*). rclone skips it
#     correctly (12 entries in source, 11 in mirror), but os.walk lists it, so
#     dot_ssh read as 1010h behind forever. Sockets are not regular files --
#     hence the S_ISREG test below, which `find -type f` was doing implicitly
#     and os.walk was not.
#   * xrpl_test carries __pycache__/*.pyc and venv/, rewritten constantly and
#     excluded from the mirror. Counting them would make that tree flap.
_HERE = os.path.dirname(os.path.abspath(__file__))
EXCLUDES_FILE = os.path.join(_HERE, "launchd", "b2_backup.excludes")


def _rclone_glob_to_regex(pattern: str) -> str:
    """Translate one rclone --exclude-from glob into a full-match regex.

    rclone semantics implemented here: `**` spans directory separators, `*`
    and `?` do not, and a pattern NOT starting with `/` may match at any
    directory depth (so `.git/**` excludes `a/b/.git/config`, not just a
    top-level `.git`).
    """
    anchored = pattern.startswith("/")
    pat = pattern[1:] if anchored else pattern
    out: list[str] = []
    i = 0
    while i < len(pat):
        ch = pat[i]
        if ch == "*":
            if pat.startswith("**", i):
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
        elif ch == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(ch))
        i += 1
    body = "".join(out)
    prefix = "" if anchored else r"(?:.*/)?"
    return f"^{prefix}{body}$"


def load_exclude_matchers(path: str | None = None):
    """Compile every pattern in the rclone exclude file.

    `path` resolves at CALL time, not as a default argument: a default is
    bound once at import, so a caller (or a test) reassigning EXCLUDES_FILE
    would be silently ignored and the canary would keep reading the old path.

    Returns (matchers, prune_dirs, error). A read failure returns an EXPLICIT
    error rather than an empty list: an unreadable exclude file must not
    quietly turn into "exclude nothing", which would bury a real gap under
    thousands of phantom findings.
    """
    path = EXCLUDES_FILE if path is None else path
    try:
        with open(path) as fh:
            lines = fh.read().splitlines()
    except OSError as exc:
        return [], set(), f"excludes_unreadable_{type(exc).__name__}"
    patterns = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        patterns.append(line)
    if not patterns:
        return [], set(), "excludes_file_had_no_patterns"
    matchers = [re.compile(_rclone_glob_to_regex(p)) for p in patterns]
    return matchers, _prune_dirs(patterns), None


def _prune_dirs(patterns: list[str]) -> set[str]:
    """Directory names safe to skip wholesale, derived from `<name>/**`
    patterns. Pure speed: their contents are excluded by the regexes anyway,
    but descending into venv/ or .git/ to discard every file is wasteful."""
    names = set()
    for p in patterns:
        p = p.strip()
        if p.endswith("/**") and "/" not in p[:-3] and "*" not in p[:-3]:
            names.add(p[:-3])
    return names


# Filesystems disagree about sub-second mtime, and rclone rounds, so a copy
# can land a hair "older" than its source while being a faithful copy. Only a
# gap wider than this counts as the mirror being behind.
MIRROR_MTIME_SLACK_SECONDS = 2


def _iter_files(root: str, matchers=None, prune=None) -> dict[str, int]:
    """Map relative path -> mtime for every file under `root` that rclone
    would actually copy.

    Walks files explicitly rather than stat'ing `root` -- see the module note
    above on why directory mtime is useless here.

    Three kinds of entry are skipped, each because rclone skips it too, so
    counting them guarantees a finding no amount of backing up can clear:
    anything matching the exclude file, symlinks (no --links/--copy-links on
    the rclone call), and non-regular files such as sockets and fifos.
    """
    import stat as _stat
    if matchers is None:
        matchers, prune_from_file, _err = load_exclude_matchers()
        if prune is None:
            prune = prune_from_file
    prune = set() if prune is None else prune
    out: dict[str, int] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in prune]
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root)
            if any(m.match(rel) for m in matchers):
                continue
            try:
                lst = os.lstat(full)
            except OSError:
                continue
            # lstat, not stat: stat follows the link and reports the TARGET's
            # type, so a link to a regular file would pass S_ISREG below.
            if _stat.S_ISLNK(lst.st_mode):
                continue
            if not _stat.S_ISREG(lst.st_mode):
                continue
            out[rel] = int(lst.st_mtime)
    return out


FIRST_SEEN_PATH = os.path.join(LAUNCHD_STATE_DIR,
                               "dockvault_mirror_first_seen.json")


def _load_first_seen(path: str) -> dict:
    """{sub: {relpath: first_seen_epoch}}. Missing or corrupt reads as empty,
    which seeds silently rather than alarming on every path."""
    try:
        with open(path) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 -- a canary must not die on its own state
        return {}


def _save_first_seen(path: str, data: dict) -> None:
    """Atomic write. Best effort: failing to persist means paths get re-seeded
    next run (a missed alarm window), never a false alarm."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w") as fh:
            json.dump(data, fh)
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001
        try:
            os.unlink(tmp)
        except Exception:  # noqa: BLE001
            pass


def check_tree(src: str, sub: str, now_ts: int | None = None,
               state_path: str | None = None) -> dict:
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
    # Load once and share: measuring the two sides with different exclude
    # sets would invent differences that rclone never saw.
    matchers, prune, exc_err = load_exclude_matchers()
    if exc_err:
        # Fail loud. Carrying on with no excludes would report thousands of
        # files rclone was never asked to copy and bury any real gap.
        return {**base, "ok": False, "reason": exc_err}
    src_files = _iter_files(src, matchers, prune)
    dst_files = _iter_files(dst, matchers, prune)
    if not src_files:
        # Nothing to back up is not a failure.
        return {**base, "ok": True, "age_seconds": 0, "reason": "source_empty"}
    if not dst_files:
        return {**base, "ok": False, "reason": "mirror_empty_but_source_has_files"}

    now = int(_time.time()) if now_ts is None else int(now_ts)

    # ARRIVAL TIME, not mtime. mtime answers "when was this content last
    # written", which is not the question. The question is "how long has this
    # path existed HERE without reaching the mirror". Those differ whenever a
    # file arrives with an old timestamp: `mv` of a directory leaves the
    # files' mtime AND ctime untouched (only the directory's ctime moves),
    # and `cp -p` deliberately restores the source mtime. Both happened on
    # 2026-11-10 and produced 71 phantom "missing" findings for files that
    # were minutes old in their new home.
    #
    # Ancestor directory ctime was considered and rejected: log directories
    # change ctime constantly, so a genuinely missing file in launchd_logs
    # would be permanently exempt -- it would silence the alarm exactly
    # where loss is most likely to go unnoticed.
    #
    # So remember when we first SAW each path and judge against that. A path
    # absent from the mirror is only overdue once it has been seen here for
    # longer than one mirror cycle.
    seen_path = FIRST_SEEN_PATH if state_path is None else state_path
    all_state = _load_first_seen(seen_path)
    seen = dict(all_state.get(sub) or {})

    missing: list[str] = []
    older: list[str] = []
    worst = 0
    for rel, s_mt in src_files.items():
        first_seen = seen.get(rel)
        if first_seen is None:
            # First sighting: record it and do NOT alarm. This also makes the
            # very first run (no state file at all) a silent seeding pass,
            # rather than one alarm per file in the tree.
            seen[rel] = now
            continue
        d_mt = dst_files.get(rel)
        if d_mt is None:
            age_here = now - int(first_seen)
            if age_here <= MIRROR_CYCLE_SECONDS:
                # Seen, but no mirror run has been due since it appeared.
                continue
            missing.append(rel)
            worst = max(worst, age_here)
        elif s_mt - d_mt > MIRROR_MTIME_SLACK_SECONDS:
            # Content axis, still mtime: both sides exist, so arrival time is
            # irrelevant and the question is whether the copy is behind.
            #
            # But "behind" only becomes a FINDING once a mirror run has been
            # due SINCE the source last changed -- the same cycle grace the
            # absence axis above already gets, and the same rule the module
            # docstring has stated all along ("A file changed within the last
            # cycle is exempt, because no mirror run has been due yet"). It
            # was simply never implemented on this branch.
            #
            # Without it the check does not report, it FLAPS on a daily
            # cycle: green in the quiet hours after the 23:15 run, red from
            # the first morning walker write. Observed 2026-11-11 --
            # all_9907_files_mirrored at 10:25 UTC, then
            # stale_files_missing_0_older_29 at 11:25 UTC. Nothing had
            # broken in between. The 29 were launchd_state/*_last_ok stamps,
            # amm_rank.lock and snapshot JSON that the morning walkers
            # rewrite; rclone had copied every one of them correctly at
            # 23:15 and logged fails=0. The mirror held exactly the content
            # the run that actually happened was supposed to leave.
            #
            # Judge the SOURCE's age, not the size of the gap. A 9h gap on a
            # file written 9h ago is a 24h-cadence mirror doing its job; a
            # gap on a file untouched for two days is a real miss. Measuring
            # the gap alone guarantees a finding that no amount of backing
            # up can clear, which is how a canary gets ignored.
            if now - s_mt <= MIRROR_CYCLE_SECONDS:
                continue
            older.append(rel)
            worst = max(worst, s_mt - d_mt)

    # Drop paths that are gone from the source so the file cannot grow
    # without bound, then persist. Written even on a clean run: the seeding
    # of new paths is the whole point.
    seen = {r: t for r, t in seen.items() if r in src_files}
    all_state[sub] = seen
    _save_first_seen(seen_path, all_state)

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
    # the canary's own silence). Wrapper (run_pg_backup_canary.sh) stamps
    # _last_ok on success only — same discipline as siblings.
    #
    # CADENCE IS DAILY, NOT HOURLY (corrected 2026-11-28). The plist uses
    # StartCalendarInterval Hour=1 Minute=0 LOCAL (ET) — one run a day at
    # 01:00 ET — with RunAtLoad TRUE as the boot/login catch-up path. That
    # is deliberate per the 2026-09-09 ruling and explicitly supersedes the
    # 2026-09-08 conversion to StartInterval 86400, so the absence of a
    # StartInterval key in that plist is intentional and not drift. The
    # wrapper declares cadence_seconds=86400 to walker_health, and the
    # plist's own Comment has always said "Daily freshness check".
    #
    # So the 26h ceiling here is this job's OWN daily cadence plus ~1h of
    # grace — not an hourly job measured against a daily artifact.
    #
    # Why the wording mattered: on 2026-11-28 this description string was
    # read as authoritative and the canary was reported to Charlie as "last
    # succeeded 7.4h ago despite being hourly" — a false alarm. It had run
    # exactly on time at 01:00 ET (rc=0, dump age=3h) and was sitting at
    # 0.31x of its real cadence. A second reader then repeated "hourly" in
    # the mpt_supply_history_canary entry below, so the wrong word had
    # already reproduced once before anyone checked the plist. A description
    # that contradicts the schedule is not a cosmetic defect: it is read as
    # the schedule.
    ("pg_backup_canary", 26 * 3600,
     "daily 01:00-ET freshness check for pg_backup B2 dump (age <= 25h threshold; stamps last_ok on success)"),
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
    # 2026-11-27: mpt_supply_history_canary joins the meta-watch, the day
    # after it was written. It exists because the MPT holders walk sat dead
    # for 88 days and NOTHING paged: mpt_holders_refresh exited 0 on ~2000
    # consecutive runs while writing history_rows=0, so walker_health's ok
    # axis never moved and its own counters filed an RPC error under a
    # pagination label. Every signal we had asked the WALKER how its run
    # went, and the run went fine. The canary asks the one question a walker
    # doing nothing cannot answer falsely — did the TABLE grow.
    #
    # So this entry is the watcher-of-the-watcher for exactly the class of
    # fault that motivated it. A silent canary is indistinguishable from a
    # healthy series, and that is the whole 88-day failure repeated one level
    # up.
    #
    # Ceiling 3h against an hourly cadence = two missed ticks of grace.
    # Tighter than pg_backup_canary's 26h, which is sized to ITS OWN daily
    # cadence (01:00 ET once a day) plus about an hour of grace. An earlier
    # version of this comment said that job was "hourly but sized to the
    # DAILY artifact it watches"; that was inherited from a stale
    # description string and is corrected above. Here the canary is the ONLY
    # watcher of mpt_supply_history, so its own silence should surface in
    # hours, not a day. Looser than a 2x ratio because launchd StartInterval ticks are
    # skipped across sleep rather than queued, and this is a liveness probe,
    # not the staleness alert — the staleness finding has its own axis.
    #
    # NOTE the deliberate asymmetry with its siblings: this wrapper stamps
    # last_ok on rc 0 (fresh) OR rc 1 (stale finding), because both mean the
    # check completed. The two signals answer different questions —
    # findings_count asks "is the series stale" (the page), last_ok asks "is
    # this canary alive" (this entry). Stamping only on rc 0 would withhold
    # the heartbeat for exactly as long as a real staleness incident lasted,
    # reporting one fault as two and making this ceiling mean something it
    # does not say. Any other rc (crash, 120s wrapper timeout, missing env)
    # leaves last_ok stale on purpose and surfaces here.
    #
    # Added only AFTER the canary's first successful post-merge run stamped
    # launchd_state/mpt_supply_history_canary_last_ok (2026-11-27 20:55 ET,
    # "clean: newest row 4282s old, 527 rows inside the 46800s window").
    # Adding it while the code was still unmerged would have meant a
    # missing_last_ok_file finding — a guaranteed false page in the gap
    # between registering the watch and the thing being watchable.
    ("mpt_supply_history_canary", 3 * 3600,
     "hourly freshness check that mpt_supply_history still gains rows (13h staleness ceiling); stamps last_ok on rc 0 OR rc 1 — both mean the check completed"),
]


# ══ Generic staleness rule over walker_health (Charlie ruling 2026-11-28) ══
#
# Everything above is a HAND-WRITTEN list of labels checked by their
# launchd_state stamp. That list is the problem it cannot see: a job absent
# from it is unwatched forever, and nothing notices the absence.
#
# The 2026-11-28 silence audit measured the hole. Of 81 walkers in
# walker_health (supply_escrow_walker excluded as paused), SEVEN declare no
# cadence at all, and six of those had not succeeded in 43-65 days:
#
#   escrow_supply_walker                 last success 2026-09-24
#   rwa_nav_transparency_walker          last success 2026-09-25
#   openclaw_cli_backend_drift           last success 2026-09-26
#   supply_block_walker                  last success 2026-09-26
#   nft_activity_reconcile-type-rollup   last success 2026-09-26
#   escrow_seed_walk                     last success 2026-10-16
#   nft_activity_backfill                healthy, but still undeclared
#
# Not one is in JOBS, and because they declare no cadence, no staleness rule
# could fire on them however long they stayed silent. They were invisible to
# the exact check meant to catch this class -- the same shape as the MPT
# holders walk sitting dead for 88 days while the signal everyone trusted
# measured something else.
#
# So this rule reads walker_health DIRECTLY and judges every row by its own
# declaration, instead of by membership of a list someone must remember to
# update. A new walker is covered the first time it stamps.
#
# TWO FINDINGS, both on the existing findings_count axis:
#
#   <name>=UNDECLARED-CADENCE   the row declares no cadence_seconds. A
#       finding in its OWN right, not merely a missing input: a job that
#       declares no cadence is permanently unwatchable, so the omission IS
#       the defect. Reported by key name only -- there is no declared
#       cadence to compare against, so there is nothing else to say.
#
#   <name>=STALE <age>h vs <limit>h   last success older than
#       STALENESS_MULTIPLE x its own declared cadence.
#
# "Noticed within two cycles" is the brief, so the multiple is 2 and the
# limit derives from each job's own declaration rather than a per-job
# constant someone has to tune.
STALENESS_MULTIPLE = 2

# ── Exemptions ─────────────────────────────────────────────────
#
# Every exemption carries a REASON, so an exemption is an assertion rather
# than an absence -- same discipline as the RETIRED map in
# xrpl_env_drift_guard. The maps are themselves guarded below: a name in two
# maps is a finding, an exemption naming a walker absent from walker_health
# is a finding, and a blank reason is a finding. Without those, an exemption
# map just becomes the new place jobs go to die quietly.

# Charlie ruling 2026-11-28: demand-driven counters are exempt from the
# staleness test but MUST still declare themselves as demand-driven. Being
# in this map IS that declaration. These are not schedules -- they are
# stamped by a request handler when a person or an agent does something, so
# "overdue" is not a defined idea for them and 2x-cadence asks the wrong
# question. mcp_connect_redirect currently sits at 2.05x its nominal 30-day
# cadence and would otherwise be a standing false finding.
DEMAND_DRIVEN: dict[str, str] = {
    "mcp_connect_redirect": (
        "request-time stamp in app.py:_stamp_connect_redirect, fired by a "
        "click on /connect/<slug>; counts arrivals at the door. It is the "
        "belt-and-suspenders path: directories hand out ?ref= URLs directly "
        "and mcp-remote preserves query strings, so the primary path works "
        "and this one can be quiet for months. Its own scope declaration "
        "says a long quiet gap is normal, not a fault. Verified 2026-11-28: "
        "/connect/<slug> returns 302 to the ref-tagged endpoint and "
        "mcp_session_start was stamping the same day."
    ),
    "mcp_session_start": (
        "one stamp per MCP session, bound to the ?ref= that opened it. "
        "Driven by agent traffic, not by a schedule."
    ),
}

# Paused on an owner decision, expected to resume -- so NOT retired.
# EMPTY as of 2026-11-28: supply_escrow_walker is RESUMED on a daily
# schedule, so it is no longer exempt -- it is now judged by its declared
# cadence like any other walker. See CADENCE_OVERRIDE below for the bridge
# that makes that safe, and paused_walkers.RESUMED for what "stage 4" turned
# out to mean.
PAUSED: dict[str, str] = {}

# ── Cadence the row declares WRONGLY, corrected until it re-stamps ──────
#
# Distinct from CADENCE_FALLBACK, which fills in a MISSING cadence. This one
# OVERRIDES a present-but-wrong one, and it exists because of a sequencing
# hazard that would otherwise have paged tonight.
#
# supply_escrow_walker's row currently declares cadence_seconds=3600, written
# by the one-off catch-up run on 2026-11-28 while the code still said hourly.
# The code now says 86400, but a walker_health row only gains the new value
# when the job NEXT RUNS -- 06:30 ET tomorrow. Removing the PAUSED exemption
# without this override would have judged it at 2 x 3600 = 2h against a last
# success of 18:34 UTC, so the generic rule would have reported STALE from
# about 20:34 UTC tonight for a walker that is working exactly as designed.
#
# Fixing the data instead of the code was the alternative, i.e. UPDATE the
# row's cadence by hand. That was rejected: hand-editing walker_health to
# satisfy a checker teaches the checker to trust a value nobody wrote, and
# the next person cannot tell a real declaration from a patched one.
#
# A test asserts this equals supply_escrow_walker.CADENCE_SECONDS, so the two
# copies cannot drift. Remove the entry once the row has re-stamped 86400 --
# it is a bridge, and leaving it would mean the canary permanently preferred
# its own opinion to the walker's.
CADENCE_OVERRIDE: dict[str, int] = {
    "supply_escrow_walker": 86400,
}

# Decommissioned for good. Kept here so a dead row stops paging WITHOUT
# deleting the walker_health row, because deleting it would destroy the
# history that proves when the job last worked.
#
# Each reason below is the job's OWN recorded words where it left any --
# quoted from its last walker_health message or from paused_walkers.py --
# rather than my summary of them. Decided by Charlie 2026-11-28 after the
# read-only investigation; every one of these had last_run_ok=true and
# last_failure_at=NULL, i.e. none of them ever failed. They stopped RUNNING
# because they were switched off on purpose.
RETIRED: dict[str, str] = {
    "escrow_supply_walker": (
        "its own last message: 'RETIRED 2026-09-25: plist archived "
        "(private_infra 5101578); supply block confirmed, drift canary 0 "
        "findings - walker redundant. cadence NULL so l1_pager walker_stale "
        "excludes it; last_success_at kept as the honest last run.' Plist and "
        "wrapper on disk as .retired-2026-09-25, LaunchAgents copy as "
        ".retired-2026-09-24, not loaded. NOTE it is a DIFFERENT job from "
        "supply_escrow_walker (paused) and from supply_block_walker "
        "(retired) - three similar names, three separate jobs."
    ),
    "supply_block_walker": (
        "its own last message: 'RETIRED 2026-09-25: superseded 2026-09-24 by "
        "supply_fast_walker + supply_escrow_walker writing the "
        "xrp_supply_block singleton; no launchd job exists for this name'. "
        "Only run_supply_block_walker.sh.retired-2026-09-25 remains; no plist "
        "anywhere, not loaded."
    ),
    "escrow_seed_walk": (
        "retired 2026-11-02, recorded in paused_walkers.py: it 'was the "
        "stage-2 one-shot, not a standing census'. Last run 2026-10-16 "
        "succeeded (seeded 14614 escrow objects at pinned ledger 107687537). "
        "No plist. The full-ledger escrow census it fed is deliberately "
        "UNCOVERED pending escrow stage 4 - see the `coverage` string in "
        "paused_walkers.py, which is a free-text absence precisely so it "
        "cannot be mistaken for a working substitute."
    ),
    "openclaw_cli_backend_drift": (
        "retired 2026-09-25: plist and wrapper on disk renamed "
        ".retired-2026-09-25, not loaded, and its last success (2026-09-26) "
        "matches that date. Superseded when the CLI backend drift check "
        "stopped being a separate concern."
    ),
}

# Real work, deliberately NOT on a schedule: a human or a curator runs it
# when there is something to run it for. Distinct from DEMAND_DRIVEN, which
# is a request handler stamping a counter -- these are walkers someone
# invokes by hand.
RESEARCH_ONLY: dict[str, str] = {
    "rwa_nav_transparency_walker": (
        "its own last message: 'research-only, no launchd job (Charlie "
        "2026-09-22 ruling); cadence NULL 2026-09-25 so walker_stale never "
        "pages an unscheduled walker'. It has never had a plist. BLOCKED on "
        "curator input, not on code: the last real pass (2026-09-22 23:00Z) "
        "recorded ondo_finance=not_a_pdf openeden=not_a_pdf because both "
        "source URLs are docs HTML pages with no stable PDF URL. Its output "
        "is curator-gated and feeds no auto-updating live field, so there is "
        "nothing on the site going stale behind it."
    ),
}

# Not a scheduled job at all: an on-demand MODE of another walker, which
# writes its own walker_health row under a composite name. Asking whether it
# is overdue is a category error -- it runs when someone audits.
ON_DEMAND: dict[str, str] = {
    "nft_activity_reconcile-type-rollup": (
        "not a job: the `--mode reconcile-type-rollup` audit mode of "
        "nft_activity_walker (registered at nft_activity_walker.py MODE_DISPATCH), "
        "which writes walker_health under f'{WALKER_NAME}_{mode}'. No plist "
        "exists or should. Its last run PASSED - 'through_id=7866979 "
        "rollup_total=7776300 full_total=7776300 types=5 MATCH' - and the "
        "table it audits, nft_activity_type_rollup, is maintained by the "
        "scheduled activity/summary modes and was fresh to the minute when "
        "checked on 2026-11-28."
    ),
}

# ── Cadence the WRITER declares at runtime, mirrored here ───────────────
#
# nft_activity_backfill is genuinely scheduled -- its own plist runs it every
# 900s and launchctl shows it loaded -- but nft_activity_walker passed
# cadence_seconds=None for it, because WALKER_CADENCE_SECONDS (300) is the
# ACTIVITY-mode interval and would have been wrong for backfill. So the row
# said "undeclared" while the job was running fine every 15 minutes.
#
# The real fix is in the writer (nft_activity_walker now declares a cadence
# per mode). This map is the BRIDGE: a walker_health row only gains the new
# value when the job next stamps, so without it the rule would report a
# finding for a job that is already fixed. Used ONLY when the row itself
# declares nothing.
#
# The duplication is deliberate but guarded: a test asserts this value
# equals the writer's declaration for that mode AND the plist's
# StartInterval, so the two copies cannot drift silently. Verified from the
# plist on 2026-11-28, not from a comment -- two separate cadence claims in
# this file had already been wrong by believing prose.
CADENCE_FALLBACK: dict[str, int] = {
    "nft_activity_backfill": 900,
}


# Every exemption table, in one place so the guards below cannot miss one.
EXEMPTION_TABLES: tuple[tuple[str, dict[str, str]], ...] = (
    ("demand-driven", DEMAND_DRIVEN),
    ("paused", PAUSED),
    ("retired", RETIRED),
    ("research-only", RESEARCH_ONLY),
    ("on-demand", ON_DEMAND),
)


def _exempt_reason(name: str) -> tuple[str, str] | None:
    """(kind, reason) if `name` is exempt, else None."""
    for kind, table in EXEMPTION_TABLES:
        if name in table:
            return kind, table[name]
    return None


def _declaration_result(label: str, ok: bool, reason: str,
                        age_seconds=None, ceiling_seconds=None) -> dict:
    """Shape a result exactly like check_one does, so main() prints it and
    _write_walker_health counts it with no special-casing."""
    return {
        "label": label,
        "ok": ok,
        "reason": reason,
        "path": "walker_health",
        "age_seconds": int(age_seconds) if age_seconds is not None else None,
        "ceiling_seconds": int(ceiling_seconds) if ceiling_seconds is not None else 0,
    }


def evaluate_walker_declarations(rows: list[dict]) -> list[dict]:
    """Judge every walker_health row by its own declaration. Pure.

    Takes `rows` as an argument so the whole rule is testable with no
    database -- the findings logic must not be provable only in production.
    Returns one result dict per row, plus one per map-integrity problem.
    """
    results: list[dict] = []
    present = {r.get("walker_name") for r in rows if isinstance(r, dict)}
    all_exempt = sorted({n for _kind, t in EXEMPTION_TABLES for n in t})

    # Guard-the-guard 1: contradictory declarations. A name claiming to be
    # both paused and demand-driven means nobody knows what it is.
    # Iterates EXEMPTION_TABLES so adding a sixth category cannot silently
    # escape this guard -- the earlier version listed three tables by hand
    # and would have ignored RESEARCH_ONLY and ON_DEMAND entirely.
    for name in all_exempt:
        hits = [k for k, t in EXEMPTION_TABLES if name in t]
        if len(hits) > 1:
            results.append(_declaration_result(
                f"declare:{name}", False,
                f"exempt_in_multiple_maps:{'+'.join(hits)}"))

    # Guard-the-guard 2: an exemption that matches no walker. Without this a
    # RENAMED job keeps its old name exempt forever while the new name is
    # never noticed -- which is how escrow_supply_walker and
    # supply_escrow_walker could both hide behind one entry.
    #
    # Only meaningful when we actually HAVE a population to compare against.
    # On an empty or unavailable walker_health every exemption would look
    # unknown, which is a different fault (already reported as
    # pg_unavailable / read_failed) dressed up as several bogus ones.
    if present:
        for name in all_exempt:
            if name not in present:
                results.append(_declaration_result(
                    f"declare:{name}", False, "exemption_for_unknown_walker"))

    # Guard-the-guard 3: an exemption with no stated reason is not a
    # decision, it is a silence with extra steps.
    for kind, table in EXEMPTION_TABLES:
        for name, reason in sorted(table.items()):
            if not (reason or "").strip():
                results.append(_declaration_result(
                    f"declare:{name}", False,
                    f"exemption_without_reason:{kind}"))

    for row in rows:
        if not isinstance(row, dict):
            continue
        name = row.get("walker_name")
        if not name:
            continue

        exempt = _exempt_reason(name)
        if exempt is not None:
            kind, _reason = exempt
            results.append(_declaration_result(
                f"declare:{name}", True, f"exempt:{kind}",
                row.get("last_success_age_seconds")))
            continue

        # Precedence: a known-wrong declared value is corrected first, then
        # the row's own declaration, then a fallback for a missing one.
        cadence = (CADENCE_OVERRIDE.get(name)
                   or row.get("cadence_seconds")
                   or CADENCE_FALLBACK.get(name))
        age = row.get("last_success_age_seconds")

        if not cadence:
            results.append(_declaration_result(
                f"declare:{name}", False, "UNDECLARED-CADENCE", age))
            continue

        limit = STALENESS_MULTIPLE * cadence
        if age is None:
            # Declared a cadence and has never recorded a success. Not
            # silence: a job that has never worked is the loudest case.
            results.append(_declaration_result(
                f"declare:{name}", False, "STALE never_succeeded", None, limit))
        elif age > limit:
            results.append(_declaration_result(
                f"declare:{name}", False,
                f"STALE {age / 3600:.1f}h vs {limit / 3600:.1f}h", age, limit))
        else:
            results.append(_declaration_result(
                f"declare:{name}", True, "fresh", age, limit))

    return results


def check_walker_declarations() -> list[dict]:
    """Read walker_health and evaluate it. Never raises.

    An unreadable walker_health is itself a FINDING, never silence: the
    entire point of this rule is that a check which cannot see must not
    report health. Same discipline as the mpt_supply_history canary, where
    an unreadable DB and an empty table are findings rather than a pass.
    """
    try:
        sys.path.insert(0, "/Users/charliebruce/xrpl_test")
        import db
        if not db.pg_available():
            return [_declaration_result(
                "declare:_walker_health", False, "pg_unavailable")]
        rows = db.read_walker_health_all() or []
    except Exception as e:  # noqa: BLE001
        return [_declaration_result(
            "declare:_walker_health", False, f"read_failed:{type(e).__name__}")]
    return evaluate_walker_declarations(rows)


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

    # Generic declaration + staleness rule over EVERY walker_health row.
    # The three loops above check a hand-written list; this one checks
    # everything that exists, so a job nobody remembered to register still
    # gets noticed within two of its own cycles.
    for r in check_walker_declarations():
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
