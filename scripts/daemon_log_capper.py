#!/usr/bin/env python3
"""daemon_log_capper — size-cap the launchd logs that must NOT be
rename-rotated, by truncating them IN PLACE so the inode survives.

WHY THIS EXISTS (2026-10-14)
----------------------------
`/etc/newsyslog.d/xrpldashboard.conf` rotates `launchd_logs/*.err.log` and
`*.out.log` at 50 MB by RENAMING the file and gzipping the old one. That is
correct for the ~163 periodic walkers: launchd re-opens the path on each
spawn, so the next run writes to the fresh file. Proven 2026-10-14 —
`answer_plausibility_walker.err.log` rotated and had grown 309 KB again
within four hours.

It is WRONG for a continuously-running job. launchd opens the job's
`StandardErrorPath` once and hands that descriptor to the process; after a
rename the descriptor still points at the OLD inode, so the daemon keeps
writing into the renamed/gzipped file and the live path stays empty
forever. Proven 2026-10-14 with a controlled test: a writer holding
`exec 3>> file` emitted 8 lines across a rename and ALL 8 landed in the
renamed inode — the new path was never even created.

So the two genuinely long-running jobs were given explicit newsyslog
entries ahead of the globs with an unreachable 2 GB threshold, which makes
newsyslog skip them (verified by root dry run: `<0> ... [2097152] -->
skipping`, while `rlusd_refresher.err.log` kept `<7Z> ... [51200]`). That
stops the data loss but removes their size ceiling — hence this guard.

WHY TRUNCATE IN PLACE RATHER THAN ROTATE
----------------------------------------
A rename cannot be made safe for a held descriptor, but an in-place
truncate can: opening `r+b`, rewriting the retained tail and calling
`truncate()` keeps the SAME inode, so the daemon's descriptor stays valid.
launchd opens stderr with O_APPEND, so the daemon's next write lands at the
new end of file instead of its old (now past-EOF) offset. Proven
2026-10-14: inode identical before/after, and all 8 lines written by a
concurrent fd-holding writer survived the cap.

KNOWN, ACCEPTED RACE
--------------------
Between reading the tail and calling `truncate()` the daemon may append a
few lines, and those bytes are discarded. The window is sub-millisecond and
the alternative (rename) loses EVERY subsequent line permanently, so this
is a deliberate trade. It is logged rather than hidden.

TARGETS must stay in sync with the newsyslog drop-in: anything newsyslog is
told to skip belongs here, and nothing else does. A periodic walker added
here would be capped twice for no reason; a daemon left out of BOTH grows
without limit.

Cadence: 3600s (1 hour). At the observed ~1 MB/day on mac_tunnel.err.log a
one-hour check is far tighter than needed, which is the point — the cap
should never be the thing that notices lateness.
"""
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

WALKER_NAME = "daemon_log_capper"
WALKER_CADENCE_SECONDS = 3600

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(REPO_ROOT, "launchd_logs")

# Trigger at 20 MB, retain the most recent 5 MB. Deliberately well under
# newsyslog's 50 MB so a mis-ordered conf change cannot let a daemon log
# reach the rename threshold before this guard acts on it.
CAP_BYTES = 20 * 1024 * 1024
KEEP_BYTES = 5 * 1024 * 1024

# Must mirror the explicit skip entries in /etc/newsyslog.d/xrpldashboard.conf.
TARGETS = (
    "mac_tunnel.err.log",
    "sig_service.err.log",
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger(WALKER_NAME)


def retained_tail(data: bytes, keep_bytes: int) -> bytes:
    """Return the last `keep_bytes` of `data`, starting at a line boundary.

    Slicing a log at an arbitrary byte offset leaves a severed partial line
    at the top of the file, which reads as a corrupt entry to anything
    parsing it. Drop that fragment. If the retained slice contains no
    newline at all (one absurdly long line) return it unchanged rather than
    discarding everything.
    """
    if len(data) <= keep_bytes:
        return data
    tail = data[-keep_bytes:]
    nl = tail.find(b"\n")
    if nl == -1:
        return tail
    return tail[nl + 1:]


def cap_file(path: str, cap_bytes: int = CAP_BYTES,
             keep_bytes: int = KEEP_BYTES) -> dict:
    """Truncate `path` in place if it exceeds `cap_bytes`. Inode preserved.

    Returns a result dict describing what happened. Never raises for the
    ordinary "file is absent" case — a daemon that has not logged yet, or
    one that has been retired, is not an error.
    """
    name = os.path.basename(path)
    if not os.path.exists(path):
        return {"file": name, "action": "absent"}

    size_before = os.path.getsize(path)
    inode_before = os.stat(path).st_ino
    if size_before <= cap_bytes:
        return {
            "file": name,
            "action": "under_cap",
            "size_before": size_before,
            "inode_before": inode_before,
            "inode_after": inode_before,
        }

    with open(path, "rb") as fh:
        fh.seek(max(0, size_before - keep_bytes))
        tail = retained_tail(fh.read(), keep_bytes)

    # The whole point: r+b keeps the inode. Do NOT reopen with "wb" via a
    # temp-file-and-rename dance, which is exactly the failure being fixed.
    with open(path, "r+b") as fh:
        fh.seek(0)
        fh.write(tail)
        fh.truncate()

    size_after = os.path.getsize(path)
    inode_after = os.stat(path).st_ino
    return {
        "file": name,
        "action": "capped",
        "size_before": size_before,
        "size_after": size_after,
        "reclaimed": size_before - size_after,
        "inode_before": inode_before,
        "inode_after": inode_after,
    }


def run(log_dir: str = LOG_DIR, targets=TARGETS,
        cap_bytes: int = CAP_BYTES, keep_bytes: int = KEEP_BYTES) -> list:
    results = []
    for name in targets:
        try:
            results.append(
                cap_file(os.path.join(log_dir, name), cap_bytes, keep_bytes)
            )
        except OSError as exc:
            log.error("cap failed for %s: %s", name, exc)
            results.append({"file": name, "action": "error", "error": str(exc)})
    return results


def main() -> int:
    try:
        import db
    except Exception:  # pragma: no cover - db is optional for the cap itself
        db = None

    if db is not None:
        try:
            db.write_walker_health_start(
                WALKER_NAME, cadence_seconds=WALKER_CADENCE_SECONDS
            )
        except Exception as exc:
            log.warning("walker_health start write failed: %s", exc)

    results = run()
    failed = [r for r in results if r["action"] == "error"]
    for r in results:
        if r["action"] == "capped":
            log.info(
                "capped %s: %d -> %d bytes (reclaimed %d, inode %s preserved=%s)",
                r["file"], r["size_before"], r["size_after"], r["reclaimed"],
                r["inode_before"], r["inode_before"] == r["inode_after"],
            )
        else:
            log.info("%s: %s", r["file"], r["action"])

    ok = not failed
    if db is not None:
        try:
            db.write_walker_health_finish(WALKER_NAME, ok=ok)
        except Exception as exc:
            log.warning("walker_health finish write failed: %s", exc)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
