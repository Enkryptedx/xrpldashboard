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

import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import db  # noqa: E402


WALKER_NAME = "homepage_summary_walker"
WALKER_CADENCE_SECONDS = 300  # 5 min
LAST_OK_STAMP = os.path.join(HERE, "launchd_state", "homepage_summary_walker_last_ok")


def _render_homepage_locale(app_module, locale: str) -> tuple[bytes, int]:
    """Render `/` via test_client with the `xrpl_lang` cookie set so
    Flask-Babel picks up the given locale. Returns (body, gen_ms)."""
    c = app_module.app.test_client()
    # Set the language cookie so i18n.select_locale() returns this locale
    # instead of the Accept-Language fallback (which is empty in test).
    c.set_cookie(key="xrpl_lang", value=locale, domain="localhost")
    t0 = time.perf_counter()
    r = c.get("/")
    gen_ms = int((time.perf_counter() - t0) * 1000)
    if r.status_code != 200:
        raise RuntimeError(
            f"/ [locale={locale}] returned {r.status_code} — walker refusing to persist"
        )
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
