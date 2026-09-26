"""memory_sampler — self-hosted process-RSS sampler.

Charlie ruling 2026-09-24 15:37 ET (OOM post-mortem follow-up):
"Memory line = self-hosted getrusage sampler; no Render API key."

Every SAMPLE_INTERVAL_SECONDS, `sample_and_record()` reads
`resource.getrusage(resource.RUSAGE_SELF).ru_maxrss` and inserts one row
into `memory_samples`. The standing-orders daily report queries this
table for the previous UTC day's peak / avg / hi-water-count.

Started from app.py at import time (Render web dyno). Also safe to call
from any Mac-side walker that wants to trace its own footprint, though
the primary target is the Render Flask app.

Fail-open: if PG is unavailable or `resource` misbehaves, the sampler
loop swallows the error and retries next tick. This is instrumentation;
a broken sampler must never crash the web process.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import socket
import sys
import threading
import time


log = logging.getLogger("memory_sampler")

SAMPLE_INTERVAL_SECONDS = int(os.environ.get("MEMORY_SAMPLER_INTERVAL_S", "60"))

# Rough soft-ceiling used to flag "high memory" samples in the daily
# report. Render Starter dynos have ~512 MB; a healthy web process peaks
# well below that. Configurable via env in case dyno size changes.
MEMORY_SOFT_CEILING_MB = int(os.environ.get("MEMORY_SOFT_CEILING_MB", "460"))


_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS memory_samples (
  ts BIGINT NOT NULL,
  process_role TEXT NOT NULL,
  process_pid INTEGER NOT NULL,
  hostname TEXT NOT NULL,
  rss_mb NUMERIC(10,2) NOT NULL,
  PRIMARY KEY (ts, process_role, process_pid)
);
CREATE INDEX IF NOT EXISTS memory_samples_ts_idx ON memory_samples (ts);
"""


# Render dyno hostnames are the pod name, "srv-<service-id>-<rs>-<pod>".
# The daily report only aggregates rows from these hosts; anything else
# (a Mac walker that imported app.py, a Lenovo process) is counted and
# named as "off-dyno" so the line can never blend two machines again.
RENDER_HOSTNAME_PREFIX = os.environ.get("MEMORY_SAMPLER_HOST_PREFIX", "srv-")


def _maxrss_divisor(platform: str | None = None) -> float:
    """ru_maxrss unit -> MB divisor. Linux reports KB; macOS (darwin)
    reports BYTES. 2026-09-26 founding bug: a single Mac-mini sample
    (377664 "MB" = 369 MB real) made line 7 read peak 377664 MB / avg
    531 MB for a Render dyno that never left ~270 MB."""
    plat = platform if platform is not None else sys.platform
    return 1024.0 * 1024.0 if plat.startswith("darwin") else 1024.0


def _current_rss_mb() -> float | None:
    """Peak RSS in MB, unit-corrected per platform (see _maxrss_divisor)."""
    try:
        import resource
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / _maxrss_divisor()
    except Exception:
        return None


def should_run_sampler(env: dict | None = None) -> bool:
    """The sampler exists to watch the Render web dyno. Run only when the
    process is on Render (Render sets RENDER=true) or explicitly forced
    with MEMORY_SAMPLER_FORCE=1 — never from a Mac shell that happened to
    import app.py (tests, walkers, one-off scripts)."""
    e = os.environ if env is None else env
    if str(e.get("MEMORY_SAMPLER_FORCE", "")).strip().lower() in ("1", "true", "yes"):
        return True
    return str(e.get("RENDER", "")).strip().lower() in ("1", "true", "yes")


def _ensure_table() -> None:
    import db
    if not db.pg_available():
        return
    with db.pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(_TABLE_DDL)
        conn.commit()


def sample_and_record(process_role: str = "web") -> None:
    """Take one RSS reading and insert one row. Idempotent per (ts,
    process_role, process_pid) — a same-second double-fire from two
    threads would insert one row and drop the other; acceptable."""
    rss_mb = _current_rss_mb()
    if rss_mb is None:
        return
    now = int(time.time())
    pid = os.getpid()
    hostname = socket.gethostname()[:80]
    try:
        import db
        if not db.pg_available():
            return
        with db.pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO memory_samples
                        (ts, process_role, process_pid, hostname, rss_mb)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (ts, process_role, process_pid) DO NOTHING
                    """,
                    (now, process_role, pid, hostname, rss_mb),
                )
            conn.commit()
    except Exception as e:  # noqa: BLE001
        log.debug("memory_sampler write failed: %s: %s", type(e).__name__, e)


_sampler_thread: threading.Thread | None = None
_sampler_stop = threading.Event()


def _sampler_loop(process_role: str) -> None:
    # First sample happens after one interval; the very first tick can
    # inflate ru_maxrss with import-time allocations we don't want to
    # flag as a "peak."
    while not _sampler_stop.wait(SAMPLE_INTERVAL_SECONDS):
        try:
            sample_and_record(process_role)
        except Exception as e:  # noqa: BLE001
            log.debug("memory_sampler tick error: %s: %s", type(e).__name__, e)


def start_background_sampler(process_role: str = "web") -> None:
    """Start (idempotent) the background sampler. Safe to call multiple
    times — only the first call spins the thread."""
    global _sampler_thread
    if _sampler_thread is not None and _sampler_thread.is_alive():
        return
    if not should_run_sampler():
        log.info("memory_sampler: not on Render (RENDER unset) and "
                 "MEMORY_SAMPLER_FORCE unset — sampler not started")
        return
    try:
        _ensure_table()
    except Exception as e:  # noqa: BLE001
        log.debug("memory_sampler table-ensure failed: %s: %s",
                  type(e).__name__, e)
    _sampler_thread = threading.Thread(
        target=_sampler_loop,
        args=(process_role,),
        name="memory_sampler",
        daemon=True,
    )
    _sampler_thread.start()


# ------------------------------------------------------------------
# Report helper — the daily standing-orders report calls this.
# ------------------------------------------------------------------

def daily_memory_line(cur, ts_start: int, ts_end: int) -> str:
    """Line 7 of the standing-orders daily report.

    Reports for the window [ts_start, ts_end):
      - peak RSS (max) across all samples
      - avg RSS (mean)
      - count of samples above MEMORY_SOFT_CEILING_MB × 0.9
      - sample count (proxy for sampler uptime)

    Fail-open: if the sampler never wrote a row (Render web dyno was
    down, or the module wasn't imported), returns an honest "no
    samples" line rather than pretending.
    """
    try:
        cur.execute(
            """
            SELECT MAX(rss_mb), AVG(rss_mb), COUNT(*),
                   SUM(CASE WHEN rss_mb > %s THEN 1 ELSE 0 END),
                   COUNT(DISTINCT hostname)
              FROM memory_samples
             WHERE ts >= %s AND ts < %s
               AND process_role = 'web'
               AND hostname LIKE %s
            """,
            (MEMORY_SOFT_CEILING_MB * 0.9, ts_start, ts_end,
             RENDER_HOSTNAME_PREFIX + "%"),
        )
        row = cur.fetchone()
        cur.execute(
            """
            SELECT COUNT(*)
              FROM memory_samples
             WHERE ts >= %s AND ts < %s
               AND process_role = 'web'
               AND hostname NOT LIKE %s
            """,
            (ts_start, ts_end, RENDER_HOSTNAME_PREFIX + "%"),
        )
        off = cur.fetchone()
        off_dyno = int((off or [0])[0] or 0)
    except Exception as e:  # noqa: BLE001
        return f"7. Memory: (query failed — {type(e).__name__})"
    if not row or row[2] == 0:
        return "7. Memory: no samples (self-hosted sampler not running or PG unreachable)."
    peak, avg, n, hi, hosts = row
    peak = float(peak or 0)
    avg = float(avg or 0)
    hi = int(hi or 0)
    n = int(n)
    hosts = int(hosts or 0)
    line = (f"7. Memory (Render web dyno, getrusage sampler): peak {peak:.0f} MB, "
            f"avg {avg:.0f} MB across {n} samples on {hosts} dyno{'s' if hosts != 1 else ''}; "
            f"{hi} sample{'s' if hi != 1 else ''} above "
            f"{int(MEMORY_SOFT_CEILING_MB * 0.9)} MB "
            f"(soft ceiling {MEMORY_SOFT_CEILING_MB} MB).")
    if off_dyno:
        line += f" {off_dyno} off-dyno sample{'s' if off_dyno != 1 else ''} excluded."
    return line
