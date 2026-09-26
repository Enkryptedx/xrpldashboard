"""homepage_summary_walker freshness guard — the walker must persist only a
LIVE render (cache bypass) and refuse cache echoes / stale bodies.

Founding incident 2026-09-26: fetching `/` without `?nocache=1` returned the
walker's own fresh homepage_summary row, which it re-saved every 5 min; all
10 locales froze at the 2026-09-25 12:35Z render for ~25.5 h while every
health signal stayed green.
"""
from __future__ import annotations
import datetime as dt
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import homepage_summary_walker as H  # noqa: E402

UTC = dt.timezone.utc


def _body(iso):
    return f'<html><span id="cached-ts" data-iso="{iso}">x</span></html>'.encode()


def test_fresh_body_passes():
    now = dt.datetime(2026, 9, 26, 14, 10, tzinfo=UTC)
    age = H.assert_fresh_render(_body("2026-09-26T14:09:30Z"), None, now=now, locale="en")
    assert 0 <= age <= 60


def test_stale_body_refused():
    now = dt.datetime(2026, 9, 26, 14, 10, tzinfo=UTC)
    with pytest.raises(RuntimeError, match="stale render"):
        H.assert_fresh_render(_body("2026-09-25T12:35:39Z"), None, now=now, locale="en")


def test_cache_echo_refused_even_if_fresh():
    now = dt.datetime(2026, 9, 26, 14, 10, tzinfo=UTC)
    with pytest.raises(RuntimeError, match="cache echo"):
        H.assert_fresh_render(_body("2026-09-26T14:09:30Z"),
                              "homepage_summary locale=en age=12s gen_ms=400", now=now)


def test_missing_timestamp_refused():
    with pytest.raises(RuntimeError, match="no cached-ts"):
        H.assert_fresh_render(b"<html></html>", None)


def test_render_uses_cache_bypass(monkeypatch):
    calls = []

    class _R:
        status_code = 200
        headers = {}
        data = _body(dt.datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))

    class _C:
        def set_cookie(self, **kw): pass
        def get(self, path):
            calls.append(path); return _R()

    class _App:
        class app:
            @staticmethod
            def test_client(): return _C()

    body, gen_ms = H._render_homepage_locale(_App, "en")
    assert calls == ["/?nocache=1"]
    assert b"cached-ts" in body and gen_ms >= 0
