"""Per-stage telemetry for nft_activity_walker --mode summary.

WHY THIS EXISTS (2026-10-13, Charlie)
-------------------------------------
`--mode summary` hit `statement_timeout` three times (17:49, 17:59:25,
18:09:57 ET) and then recovered unaided at 18:20:37. Every candidate was then
timed individually and NONE could reproduce a 25s path:

    24h tx_type        0.07s   (after nft_activity_close_time_idx)
    7d  tx_type        1.19s
    7d  top_issuers    1.19s
    rollup delta       0.0003s  (Index Scan on pkey, flat out to a
                                 2,000,000-row simulated backlog)
    ------------------------------------------------------------------
    TOTAL              2.45s   against a 25s ceiling

So the failure is NOT steady-state query cost. The strongest signal found was
I/O variance on full-heap scans: the two 7d queries read the ENTIRE 2.3 GB heap
every run (`shared hit=16 read=294,431`) because shared_buffers is 287 MB, and
one of them measured 1.19s then 9.96s on consecutive runs -- an 8x swing with
no plan change. Under concurrent load (supply_escrow_walker ran 44 min and
token_issuer_flags_walker 31 min, both overlapping the window) that swing is a
plausible route past 25s, but it was never caught in the act.

Four hypotheses were tested and REJECTED. Recorded so nobody re-runs them:
  * compute suspend/resume -- refuted: pg_postmaster_start_time() is
    2026-10-09 21:50:54 ET, continuously up across the whole window.
  * stale planner statistics -- refuted: pg_class.reltuples was 8,110,879
    against max_id 8,869,125, ~8% off. (The "31x stale" figure came from
    misreading pg_stat_user_tables.n_live_tup, a stats-collector counter,
    as the planner's estimate.)
  * work_mem spill -- refuted: sorts report `quicksort Memory: 25kB`, no
    external merge. The 293 GB in pg_stat_database.temp_bytes is cumulative
    and not attributable to these queries (+6 MB across both).
  * lock contention -- no evidence: 0 ungranted locks, 0 open transactions,
    0 deadlocks; waits were idle Client:ClientRead only.

Rather than guess a fifth time, instrument it so the NEXT slow cycle names its
own cause. Nothing here may break the walker: every entry point is wrapped and
any telemetry failure is swallowed and reported, never raised.

NOTE ON LOCK / IO ACCOUNTING
----------------------------
`track_io_timing = off` and `log_lock_waits = off` on this server, and we are
not superuser, so neither can be turned on from here. Server-side I/O wait is
therefore unavailable. The substitute is a sampler thread polling
pg_stat_activity for THIS backend's wait_event over a second connection, which
attributes wall time to Lock / IO / IPC / other wait classes. neondb_owner is
a member of pg_read_all_stats (verified), so the failure snapshot can also see
OTHER sessions' queries, durations and wait events.
"""

from __future__ import annotations

import os
import threading
import time

# Poll interval for the wait sampler. 250 ms keeps the extra connection's load
# trivial (4 cheap queries/sec) while still resolving a multi-second wait.
_SAMPLE_INTERVAL_S = 0.25

_LOCK_CLASSES = ("Lock", "LWLock")
_IO_CLASSES = ("IO", "BufferPin")


def _direct_url() -> str | None:
    """Direct (non-pooler) URL for the sampler's own connection.

    The pooler is avoided deliberately: PgBouncer in transaction mode hands
    out a different backend per transaction, so the MAIN connection's
    pg_backend_pid() would not be observable from the sampler.
    """
    url = os.environ.get("DATABASE_URL_DIRECT") or os.environ.get("DATABASE_URL")
    return url.replace("-pooler.", ".") if url else None


class StageTimer:
    """Per-stage wall time, tracking which stage is still OPEN.

    `stage(name)` closes the previous stage and opens `name`. If an exception
    escapes, the stage left open is by definition the one that died -- that is
    what `failed_stage` reports. Deliberately plain calls rather than context
    managers so telemetry drops in between existing statements without
    re-indenting the walker body and so it cannot alter control flow.
    """

    def __init__(self) -> None:
        self.stages: list[tuple[str, float]] = []
        self.open_stage: str | None = None
        self._t0 = time.monotonic()
        self._stage_t0 = self._t0

    def stage(self, name: str | None) -> None:
        now = time.monotonic()
        if self.open_stage is not None:
            self.stages.append((self.open_stage, now - self._stage_t0))
        self.open_stage = name
        self._stage_t0 = now

    def done(self) -> None:
        """Close the final stage. After this, failed_stage is None."""
        self.stage(None)

    @property
    def failed_stage(self) -> str | None:
        return self.open_stage

    def total(self) -> float:
        return time.monotonic() - self._t0

    def summary(self) -> str:
        parts = " ".join(f"{n}={d:.2f}s" for n, d in self.stages)
        return f"{parts} total={self.total():.2f}s" if parts else \
               f"stages=none total={self.total():.2f}s"


class WaitSampler:
    """Attributes a backend's wall time to wait classes by sampling.

    Fail-open: if it cannot start it records the reason and every accessor
    still returns something printable, so the walker is unaffected.
    """

    def __init__(self, backend_pid: int):
        self.backend_pid = backend_pid
        self.samples = 0
        self.lock_samples = 0
        self.io_samples = 0
        self.other_wait_samples = 0
        self.events: dict[str, int] = {}
        self.error: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> "WaitSampler":
        try:
            self._thread = threading.Thread(
                target=self._run, name="nft-wait-sampler", daemon=True
            )
            self._thread.start()
        except Exception as exc:
            self.error = f"start_failed: {type(exc).__name__}: {exc}"
        return self

    def _run(self) -> None:
        try:
            import psycopg
        except Exception as exc:
            self.error = f"psycopg_missing: {exc}"
            return
        url = _direct_url()
        if not url:
            self.error = "no_database_url"
            return
        try:
            with psycopg.connect(url, autocommit=True, connect_timeout=10) as conn:
                with conn.cursor() as cur:
                    # The sampler must never itself block or time out.
                    cur.execute("SET statement_timeout = '2s'")
                    while not self._stop.is_set():
                        cur.execute(
                            "SELECT wait_event_type, wait_event "
                            "FROM pg_stat_activity WHERE pid = %s",
                            (self.backend_pid,),
                        )
                        row = cur.fetchone()
                        self.samples += 1
                        if row and row[0]:
                            etype, event = row[0], row[1] or "?"
                            key = f"{etype}:{event}"
                            self.events[key] = self.events.get(key, 0) + 1
                            if etype in _LOCK_CLASSES:
                                self.lock_samples += 1
                            elif etype in _IO_CLASSES:
                                self.io_samples += 1
                            else:
                                self.other_wait_samples += 1
                        self._stop.wait(_SAMPLE_INTERVAL_S)
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"

    def stop(self) -> "WaitSampler":
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
        return self

    def summary(self) -> str:
        if self.error:
            return f"wait_sampler=unavailable({self.error})"
        if not self.samples:
            return "wait_sampler=no_samples"
        s = _SAMPLE_INTERVAL_S
        top = sorted(self.events.items(), key=lambda kv: -kv[1])[:3]
        top_txt = " ".join(f"{k}={v * s:.1f}s" for k, v in top) or "none"
        return (
            f"lock_wait={self.lock_samples * s:.1f}s "
            f"io_wait={self.io_samples * s:.1f}s "
            f"other_wait={self.other_wait_samples * s:.1f}s "
            f"top_waits[{top_txt}]"
        )


class RunTelemetry:
    """Timer + sampler for one --mode summary run."""

    def __init__(self) -> None:
        self.timer = StageTimer()
        self.sampler: WaitSampler | None = None

    def attach_sampler(self, cur) -> None:
        """Start the sampler against this cursor's own backend. Fail-open."""
        try:
            cur.execute("SELECT pg_backend_pid()")
            self.sampler = WaitSampler(int(cur.fetchone()[0])).start()
        except Exception:
            self.sampler = None

    def stop(self) -> None:
        if self.sampler is not None:
            try:
                self.sampler.stop()
            except Exception:
                pass

    def summary(self) -> str:
        parts = [self.timer.summary()]
        parts.append(self.sampler.summary() if self.sampler is not None
                     else "wait_sampler=not_started")
        return " ".join(parts)


# Module-level handle on the in-flight run so the walker's top-level error
# handler can report stage timings and a concurrency snapshot WITHOUT the
# telemetry being threaded through every call site. The walker is
# single-threaded and runs one mode at a time, so a module global is safe.
ACTIVE: RunTelemetry | None = None


def begin_run() -> RunTelemetry:
    global ACTIVE
    ACTIVE = RunTelemetry()
    return ACTIVE


def end_run() -> str:
    """Close out a SUCCESSFUL run and return its one-line telemetry."""
    global ACTIVE
    if ACTIVE is None:
        return "telemetry=not_started"
    ACTIVE.timer.done()
    ACTIVE.stop()
    return ACTIVE.summary()


def failure_report() -> str:
    """One line for the walker's top-level except block: which stage died,
    per-stage timings, wait-class attribution, and what else was running.
    """
    parts: list[str] = []
    if ACTIVE is not None:
        parts.append(f"failed_stage={ACTIVE.timer.failed_stage or 'unknown'}")
        parts.append(ACTIVE.summary())
        ACTIVE.stop()
    else:
        parts.append("telemetry=not_started")
    parts.append("concurrency[" + snapshot_activity() + "]")
    return " ".join(parts)


def snapshot_activity(limit: int = 8) -> str:
    """One-shot pg_stat_activity + lock snapshot, for failure paths only.

    Answers "what else was running when this timed out": other sessions'
    queries, how long they had been running, and their wait events. Opens its
    OWN connection because the walker's connection is, by definition, the one
    that just failed.
    """
    try:
        import psycopg
    except Exception as exc:
        return f"snapshot_unavailable(psycopg: {exc})"
    url = _direct_url()
    if not url:
        return "snapshot_unavailable(no DATABASE_URL)"
    try:
        lines: list[str] = []
        with psycopg.connect(url, autocommit=True, connect_timeout=10) as conn:
            with conn.cursor() as cur:
                cur.execute("SET statement_timeout = '5s'")
                cur.execute("SELECT count(*) FROM pg_locks WHERE NOT granted")
                lines.append(f"ungranted_locks={cur.fetchone()[0]}")
                cur.execute(
                    "SELECT pid, COALESCE(application_name,'-'), state, "
                    "       COALESCE(wait_event_type||':'||wait_event,'none'), "
                    "       round(EXTRACT(epoch FROM (now()-query_start))::numeric,1), "
                    "       left(regexp_replace(COALESCE(query,''),'\\s+',' ','g'), 90) "
                    "FROM pg_stat_activity "
                    "WHERE datname = current_database() AND pid <> pg_backend_pid() "
                    "  AND state <> 'idle' "
                    "ORDER BY query_start NULLS LAST LIMIT %s",
                    (limit,),
                )
                rows = cur.fetchall()
                if not rows:
                    lines.append("other_active_sessions=none")
                for pid, app, state, wait, dur, q in rows:
                    lines.append(
                        f"pid={pid} app={app} state={state} wait={wait} "
                        f"dur={dur}s q={q}"
                    )
        return " | ".join(lines)
    except Exception as exc:
        return f"snapshot_failed({type(exc).__name__}: {exc})"
