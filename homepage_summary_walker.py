"""homepage_summary walker — 5-min cadence, meta-watched, stamps _last_ok.

Charlie ruling 2026-09-22 Tue AM: the homepage `/` cold-renders in
~4.8s because it aggregates several PG + XRPL summaries at request
time. Render's health-check probes `/healthz` so this isn't a
liveness risk, but a real reader hitting a cold Render worker eats
the full latency. Same summary-row pattern we shipped for /whales,
/wallet, and /nfts: pre-render the body every 5 min into a
singleton PG row, serve sub-ms on cache miss, fall through to the
live render for everyone else.

## Route consumer

`app.py` `/` route: check `db.read_homepage_summary()` first; if the
row is <30 min old, serve directly with `X-Homepage-Cache: age=Ns`
header. Else fall through to the inline render (which will succeed
but slowly — the "warming up" state).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import db  # noqa: E402


WALKER_NAME = "homepage_summary_walker"
WALKER_CADENCE_SECONDS = 300  # 5 min
LAST_OK_STAMP = os.path.join(HERE, "launchd_state", "homepage_summary_walker_last_ok")


# Freshness guard (2026-09-26 incident): the body we persist must be a LIVE
# render. The route bakes the render time into `id="cached-ts" data-iso=…`;
# anything older than this is either a cache echo or a broken clock, and
# persisting it would freeze the homepage. Fail loud instead.
MAX_BAKED_AGE_S = int(os.environ.get("HOMEPAGE_PRERENDER_MAX_BAKED_AGE_S", "900"))
_BAKED_TS_RE = re.compile(r'id="cached-ts"\s+data-iso="([^"]+)"')


def baked_render_time(body) -> "dt.datetime | None":
    """UTC datetime baked into the homepage body, or None if absent."""
    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else body
    m = _BAKED_TS_RE.search(text)
    if not m:
        return None
    try:
        return dt.datetime.fromisoformat(m.group(1).replace("Z", "+00:00")).astimezone(dt.timezone.utc)
    except ValueError:
        return None


def assert_fresh_render(body, served_from_cache_header, now=None, locale: str = "?") -> float:
    """Raise unless `body` is a live render younger than MAX_BAKED_AGE_S.
    Returns the baked age in seconds. Founding incident 2026-09-26: the
    walker fetched `/` WITHOUT the cache bypass, the route handed back the
    walker's own fresh homepage_summary row, and the walker re-saved it
    every 5 min — every locale froze at the 2026-09-25 12:35Z render
    (baked ledger 107,226,154) for ~25.5 h while walker_health, the stamp
    and the meta-watcher all stayed green."""
    if served_from_cache_header:
        raise RuntimeError(
            f"/ [locale={locale}] was served from the homepage_summary cache "
            f"({served_from_cache_header}) — walker refusing to persist a cache echo"
        )
    baked = baked_render_time(body)
    if baked is None:
        raise RuntimeError(f"/ [locale={locale}] body carries no cached-ts — refusing to persist")
    now = now or dt.datetime.now(dt.timezone.utc)
    age = (now - baked).total_seconds()
    if age > MAX_BAKED_AGE_S:
        raise RuntimeError(
            f"/ [locale={locale}] baked render time {baked.isoformat()} is {int(age)}s old "
            f"(> {MAX_BAKED_AGE_S}s) — stale render, refusing to persist"
        )
    return age


def _render_homepage_locale(app_module, locale: str) -> tuple[bytes, int]:
    """Render `/` LIVE via test_client (cache bypass `?nocache=1`, the same
    switch the route exposes for fresh-vs-cached comparisons) with the
    `xrpl_lang` cookie set so Flask-Babel picks up the given locale.
    Returns (body, gen_ms). Refuses cache echoes and stale renders."""
    c = app_module.app.test_client()
    # Set the language cookie so i18n.select_locale() returns this locale
    # instead of the Accept-Language fallback (which is empty in test).
    c.set_cookie(key="xrpl_lang", value=locale, domain="localhost")
    t0 = time.perf_counter()
    r = c.get("/?nocache=1")
    gen_ms = int((time.perf_counter() - t0) * 1000)
    if r.status_code != 200:
        raise RuntimeError(
            f"/ [locale={locale}] returned {r.status_code} — walker refusing to persist"
        )
    assert_fresh_render(r.data, r.headers.get("X-Homepage-Cache"), locale=locale)
    return r.data, gen_ms


def _persist(locale: str, body_html: str, gen_ms: int) -> None:
    if not db.pg_available():
        return

    def _do(conn):
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO homepage_summary (locale, body_html, gen_ms, computed_at) "
                "VALUES (%s, %s, %s, now()) "
                "ON CONFLICT (locale) DO UPDATE SET "
                "  body_html = EXCLUDED.body_html, "
                "  gen_ms = EXCLUDED.gen_ms, "
                "  computed_at = now()",
                (locale, body_html, gen_ms),
            )
    db._writer_execute_with_retry(f"homepage_summary_persist[{locale}]", _do)


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
        env_fail = db.check_page_render_env_or_fail()
        if env_fail:
            message = f"env_guard_fail: {env_fail}"
            print(f"[homepage_summary_walker] REFUSE {message}", file=sys.stderr, flush=True)
            return 1
        import app as app_module
        from i18n import LANGUAGE_CODES

        t0 = time.time()
        per_locale_ms: dict[str, int] = {}
        rendered = 0
        for locale in LANGUAGE_CODES:
            try:
                body, gen_ms = _render_homepage_locale(app_module, locale)
                body_str = body.decode("utf-8", errors="replace")
                _persist(locale, body_str, gen_ms)
                per_locale_ms[locale] = gen_ms
                rendered += 1
                print(f"[homepage_summary_walker] locale={locale}  gen={gen_ms}ms  bytes={len(body_str)}")
            except Exception as e:
                # One-locale failure shouldn't sink the whole cycle.
                print(
                    f"[homepage_summary_walker] locale={locale} FAIL: {type(e).__name__}: {e}",
                    file=sys.stderr, flush=True,
                )
        _stamp_last_ok()
        elapsed = time.time() - t0
        slowest = sorted(per_locale_ms.items(), key=lambda kv: -kv[1])[:3]
        message = (
            f"rendered={rendered}/{len(LANGUAGE_CODES)} elapsed={elapsed:.1f}s slowest="
            + ", ".join(f"{k}:{v}ms" for k, v in slowest)
        )
        print(f"[homepage_summary_walker] {message}")
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
