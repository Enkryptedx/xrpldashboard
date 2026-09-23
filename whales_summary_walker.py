"""whales_summary walker — 5-min cadence, meta-watched, stamps _last_ok.
Charlie ruling 2026-09-21 Mon PM: /whales cold-render costs 5-19s on
`?tier=1m`, `?tier=50k`, and `?type=trustset`. Extended in-process
SWR ceiling to 30 min helped inside a single Render instance but a
canary refire an hour later proves not enough. Proper fix: walker
computes every tier×filter combo into `whales_summary`; route reads
sub-ms from the row on cache miss.

## Design

- Renders every one of the 12 (tier, filter_type) combinations by
  driving the app's own `/whales` route via `test_client()`. Cache
  bypass flag `_CACHE_REBUILD_LOCAL.bypass=True` forces a fresh
  render each cell.
- Persists the 12 body strings into `whales_summary.cells` JSONB
  keyed by `<tier>:<filter>`, plus `computed_at` and a small
  `stats` JSONB (per-cell gen_ms so we can see which combos hurt).
- Meta-watched: stamps `~/xrpl_test/launchd_state/whales_summary_walker_last_ok`
  on success only, per the meta-watcher pattern
  (`~/xrpl_test/dockvault_mirror_freshness_canary.py` JOBS list).
- Silent-safe on PG-unavailable dev boxes: skips the write, still
  stamps _last_ok if render succeeded so the meta-watcher doesn't
  flap during local iteration.

## Route consumer

`app.py` /whales route: if the in-process `_WHALES_CACHE` misses,
call `db.read_whales_summary_cell(tier, filter_type)`; if it
returns a body and the row is <30 min old, seat it in
`_WHALES_CACHE` (5-min TTL) and serve directly. Else fall through
to the inline render (which will succeed but slowly — the first-
ever miss with an empty summary row is the "warming up" state).
"""
from __future__ import annotations

import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import db  # noqa: E402


WALKER_NAME = "whales_summary_walker"
WALKER_CADENCE_SECONDS = 300  # 5 min
LAST_OK_STAMP = os.path.join(HERE, "launchd_state", "whales_summary_walker_last_ok")


# The exact set /whales renders. Keep in sync with app.py tier_map +
# valid_types (`large_xfer`, `tagged`, `trustset`).
TIERS = ("100k", "1m", "50k")
FILTERS = ("", "large_xfer", "tagged", "trustset")


def _render_cell(app_module, tier: str, filter_type: str) -> tuple[bytes, int]:
    """Render one /whales?tier=…&type=… response. Bypasses the
    in-process cache so the walker always produces fresh output."""
    # Force cache bypass — see _CACHE_REBUILD_LOCAL in app.py
    if hasattr(app_module, "_CACHE_REBUILD_LOCAL"):
        app_module._CACHE_REBUILD_LOCAL.bypass = True
    try:
        params = []
        if tier != "100k":
            params.append(f"tier={tier}")
        if filter_type:
            params.append(f"type={filter_type}")
        query = ("?" + "&".join(params)) if params else ""
        c = app_module.app.test_client()
        t0 = time.perf_counter()
        r = c.get(f"/whales{query}")
        gen_ms = int((time.perf_counter() - t0) * 1000)
        if r.status_code != 200:
            raise RuntimeError(
                f"/whales{query} returned {r.status_code} — walker refusing to persist"
            )
        return r.data, gen_ms
    finally:
        if hasattr(app_module, "_CACHE_REBUILD_LOCAL"):
            app_module._CACHE_REBUILD_LOCAL.bypass = False


def _persist(cells: dict, stats: dict) -> None:
    if not db.pg_available():
        return

    def _do(conn):
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO whales_summary (id, cells, stats, computed_at) "
                "VALUES (1, %s::jsonb, %s::jsonb, now()) "
                "ON CONFLICT (id) DO UPDATE SET "
                "  cells = EXCLUDED.cells, "
                "  stats = EXCLUDED.stats, "
                "  computed_at = now()",
                (json.dumps(cells), json.dumps(stats)),
            )
    db._writer_execute_with_retry("whales_summary_persist", _do)


def _stamp_last_ok() -> None:
    try:
        os.makedirs(os.path.dirname(LAST_OK_STAMP), exist_ok=True)
        with open(LAST_OK_STAMP, "w") as f:
            f.write(str(int(time.time())))
    except Exception:
        pass


def main() -> int:
    db.write_walker_health_start(WALKER_NAME, cadence_seconds=WALKER_CADENCE_SECONDS)
    ok = False
    message = "not_yet_stamped"
    try:
        # Fail loudly if the WSS-primary env is missing — this walker
        # renders /whales via test_client which reads env from THIS
        # process. Silent default = cached body without the relay URL
        # = sovereignty regression on every visitor. Charlie ruling
        # 2026-09-23 19:44 ET, shared helper in db.py.
        env_fail = db.check_page_render_env_or_fail()
        if env_fail:
            message = f"env_guard_fail: {env_fail}"
            print(f"[whales_summary_walker] REFUSE {message}", file=sys.stderr, flush=True)
            return 1
        # Import lazily so a Flask-boot problem is captured under
        # ok=False rather than crashing the wrapper.
        import app as app_module

        cells: dict[str, str] = {}
        stats: dict[str, int] = {}
        t0 = time.time()
        for tier in TIERS:
            for filter_type in FILTERS:
                key = f"{tier}:{filter_type}"
                body, gen_ms = _render_cell(app_module, tier, filter_type)
                # Store as UTF-8 str for JSONB text — the route's serve
                # path emits Response(body_str). Decoding here also
                # confirms the body is valid utf-8.
                cells[key] = body.decode("utf-8", errors="replace")
                stats[key] = gen_ms
        elapsed = time.time() - t0
        _persist(cells, stats)
        _stamp_last_ok()
        slow = sorted(stats.items(), key=lambda kv: -kv[1])[:3]
        message = (
            f"cells={len(cells)} elapsed={elapsed:.1f}s slowest="
            + ", ".join(f"{k}:{v}ms" for k, v in slow)
        )
        print(f"[whales_summary_walker] {message}")
        ok = True
        return 0
    except Exception as e:
        message = f"exception: {type(e).__name__}: {e}"
        raise
    finally:
        db.write_walker_health_end(
            WALKER_NAME, ok=ok,
            message=message or ("clean_no_message" if ok else "unlabeled_failure"),
        )


if __name__ == "__main__":
    sys.exit(main())
