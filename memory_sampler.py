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


def _current_rss_mb() -> float | None:
    """Peak RSS in MB. Linux ru_maxrss is KB; macOS is bytes. Render is
    Linux, so we treat as KB. On macOS the number is off by 1024×; the
    daily report still summarizes correctly per-dyno since the sampler
    is per-process."""
    try:
        import resource
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
    except Exception:
        return None


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
                   SUM(CASE WHEN rss_mb > %s THEN 1 ELSE 0 END)
              FROM memory_samples
             WHERE ts >= %s AND ts < %s
               AND process_role = 'web'
            """,
            (MEMORY_SOFT_CEILING_MB * 0.9, ts_start, ts_end),
        )
        row = cur.fetchone()
    except Exception as e:  # noqa: BLE001
        return f"7. Memory: (query failed — {type(e).__name__})"
    if not row or row[2] == 0:
        return "7. Memory: no samples (self-hosted sampler not running or PG unreachable)."
    peak, avg, n, hi = row
    peak = float(peak or 0)
    avg = float(avg or 0)
    hi = int(hi or 0)
    n = int(n)
    return (f"7. Memory (self-hosted getrusage sampler): peak {peak:.0f} MB, "
            f"avg {avg:.0f} MB across {n} samples; "
            f"{hi} sample{'s' if hi != 1 else ''} above "
            f"{int(MEMORY_SOFT_CEILING_MB * 0.9)} MB "
            f"(soft ceiling {MEMORY_SOFT_CEILING_MB} MB).")
