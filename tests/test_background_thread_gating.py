"""Module-scope background daemons must start ONLY in the web tier.

Incident (2026-10-29): app.py started two daemons at module scope, so they
started on any `import app`. signed_snapshot.py does `import app as _app`,
and 16 launchd entrypoints reach app transitively through it, so every Mac
walker tick spawned the web tier's daemons. In a short-lived walker process
the burst-cohort scanner loop's trailing time.sleep(86400) never executes,
so nothing throttled it: the 90-day scan_burst_cohorts ran about every
343s instead of once a day. Its own INFO line appears 28,520 times across
four walker .err.log files.

This was NOT gunicorn worker recycle -- Render logs showed 3 "Booting
worker" lines and 0 "WORKER TIMEOUT" in 24h.

The AST test at the bottom is the one that stops a recurrence: it fails if
a bare module-level threading.Thread(...).start() is reintroduced.

No DB, no network assertions here.
"""

import ast
import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

APP_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py"
)

import app  # noqa: E402


# ── 1. the property that was broken ─────────────────────────────────────

def test_no_background_threads_start_outside_web_tier():
    """pytest is not gunicorn, so importing app must start nothing.

    This is the walker's situation exactly: a plain `import app` in a
    process that does not serve HTTP.
    """
    assert app._BACKGROUND_THREADS_STARTED == [], (
        "module-scope daemons started on a plain import: "
        f"{app._BACKGROUND_THREADS_STARTED}"
    )


def test_the_gated_daemons_are_not_running():
    import threading
    alive = {t.name for t in threading.enumerate()}
    for name in ("analytics-warmer", "burst-cohort-scanner"):
        assert name not in alive, f"{name} thread is running under pytest"


def test_is_web_tier_false_under_plain_import():
    assert app._is_web_tier() is False


# ── 2. the gate's own logic ─────────────────────────────────────────────

def test_is_web_tier_true_when_gunicorn_imported(monkeypatch):
    """Positive detection: gunicorn is in sys.modules exactly when
    gunicorn imported this module."""
    monkeypatch.delenv("XRPLD_NO_BG_THREADS", raising=False)
    monkeypatch.delenv("XRPLD_WEB_TIER", raising=False)
    monkeypatch.setitem(sys.modules, "gunicorn", object())
    assert app._is_web_tier() is True


def test_is_web_tier_true_on_gunicorn_submodule(monkeypatch):
    monkeypatch.delenv("XRPLD_NO_BG_THREADS", raising=False)
    monkeypatch.delenv("XRPLD_WEB_TIER", raising=False)
    monkeypatch.setitem(sys.modules, "gunicorn.app.base", object())
    assert app._is_web_tier() is True


def test_explicit_web_tier_env_forces_on(monkeypatch):
    monkeypatch.delenv("XRPLD_NO_BG_THREADS", raising=False)
    monkeypatch.setenv("XRPLD_WEB_TIER", "1")
    assert app._is_web_tier() is True


def test_kill_switch_beats_every_positive_signal(monkeypatch):
    """XRPLD_NO_BG_THREADS=1 must win over both other signals."""
    monkeypatch.setenv("XRPLD_NO_BG_THREADS", "1")
    monkeypatch.setenv("XRPLD_WEB_TIER", "1")
    monkeypatch.setitem(sys.modules, "gunicorn", object())
    assert app._is_web_tier() is False


def test_starter_returns_false_and_records_nothing_when_gated(monkeypatch):
    monkeypatch.setenv("XRPLD_NO_BG_THREADS", "1")
    before = list(app._BACKGROUND_THREADS_STARTED)
    calls = []
    started = app._start_background_thread(
        lambda: calls.append(1), "probe-thread"
    )
    assert started is False
    assert app._BACKGROUND_THREADS_STARTED == before
    assert calls == []


# ── the greppable boot lines ──────────────────────────────────────
# Both branches must emit a distinct INFO token so "did this build start
# the warmer / the scanner?" is answerable by grepping Render, instead of
# being inferred from analytics_rollup_cache rows at the hour rollover.

class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


@pytest.fixture
def app_logs():
    """Capture app.logger records directly.

    A handler attached to app.logger is used rather than caplog, so the
    assertion does not depend on Flask's logger propagation settings.
    """
    handler = _Capture()
    previous = app.app.logger.level
    app.app.logger.addHandler(handler)
    app.app.logger.setLevel(logging.INFO)
    try:
        yield handler.messages
    finally:
        app.app.logger.removeHandler(handler)
        app.app.logger.setLevel(previous)


def test_skip_emits_greppable_line(monkeypatch, app_logs):
    monkeypatch.setenv("XRPLD_NO_BG_THREADS", "1")
    app._start_background_thread(lambda: None, "probe-skip")
    assert any(
        m.startswith("background_thread_skipped: probe-skip")
        for m in app_logs
    ), f"no skip line emitted; got {app_logs}"


def test_start_emits_greppable_line(monkeypatch, app_logs):
    import threading
    monkeypatch.delenv("XRPLD_NO_BG_THREADS", raising=False)
    monkeypatch.setenv("XRPLD_WEB_TIER", "1")
    done = threading.Event()
    before = list(app._BACKGROUND_THREADS_STARTED)
    try:
        app._start_background_thread(done.set, "probe-start")
        assert done.wait(timeout=5)
    finally:
        del app._BACKGROUND_THREADS_STARTED[len(before):]
    assert "background_thread_started: probe-start" in app_logs, (
        f"no start line emitted; got {app_logs}"
    )


def test_the_two_tokens_are_distinct_and_prefix_stable():
    """Grepping one token must never match the other."""
    src = open(APP_PATH, encoding="utf-8").read()
    assert '"background_thread_started: %s"' in src
    assert "background_thread_skipped: %s (not web tier)" in src
    assert not "background_thread_started".startswith(
        "background_thread_skipped"
    )


def test_starter_runs_and_records_when_web_tier(monkeypatch):
    import threading
    monkeypatch.delenv("XRPLD_NO_BG_THREADS", raising=False)
    monkeypatch.setenv("XRPLD_WEB_TIER", "1")
    done = threading.Event()
    before = list(app._BACKGROUND_THREADS_STARTED)
    try:
        started = app._start_background_thread(done.set, "probe-thread-live")
        assert started is True
        assert done.wait(timeout=5), "gated-on thread never ran"
        assert "probe-thread-live" in app._BACKGROUND_THREADS_STARTED
    finally:
        del app._BACKGROUND_THREADS_STARTED[len(before):]


# ── 3. the regression guard that matters ────────────────────────────────

def _module_level_thread_starts(path):
    """Every module-scope `threading.Thread(...).start()` in `path`.

    Walks only top-level statements, so a Thread started inside a def is
    correctly ignored -- those are on-demand rebuild helpers, not import
    side effects.
    """
    tree = ast.parse(open(path, encoding="utf-8").read())
    # ast.walk() would descend INTO top-level defs and flag the on-demand
    # rebuild helpers (_trigger_analytics_rebuild etc), which are correct.
    # Prune at every function/class boundary so only import-time code is
    # considered.
    skip = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
    found = []
    for top in tree.body:
        if isinstance(top, skip):
            continue
        stack = [top]
        while stack:
            cur = stack.pop()
            for child in ast.iter_child_nodes(cur):
                if not isinstance(child, skip):
                    stack.append(child)
            if not isinstance(cur, ast.Call):
                continue
            fn = cur.func
            if not (isinstance(fn, ast.Attribute) and fn.attr == "start"):
                continue
            inner = fn.value
            if isinstance(inner, ast.Call):
                tgt = inner.func
                name = (
                    tgt.attr if isinstance(tgt, ast.Attribute)
                    else getattr(tgt, "id", "")
                )
                if name == "Thread":
                    found.append(getattr(top, "lineno", -1))
    return sorted(found)


def test_no_bare_module_level_thread_start_in_app():
    """A new module-scope daemon must go through _start_background_thread.

    If this fails, someone added `threading.Thread(...).start()` at module
    scope in app.py -- which every walker import would then execute.
    """
    offenders = _module_level_thread_starts(APP_PATH)
    assert offenders == [], (
        "bare module-level threading.Thread(...).start() at app.py lines "
        f"{offenders}; route it through _start_background_thread() instead"
    )


def test_ast_guard_actually_catches_the_old_pattern(tmp_path):
    """Guard the guard: prove the detector fires on the pre-fix shape, so
    a broken detector can't silently pass app.py."""
    bad = tmp_path / "bad_app.py"
    bad.write_text(
        "import threading\n"
        "def loop():\n    pass\n"
        "threading.Thread(target=loop, daemon=True, name='x').start()\n"
    )
    assert _module_level_thread_starts(str(bad)) == [4]

    good = tmp_path / "good_app.py"
    good.write_text(
        "import threading\n"
        "def loop():\n    pass\n"
        "def _start(t, n):\n"
        "    threading.Thread(target=t, name=n).start()\n"
        "_start(loop, 'x')\n"
    )
    assert _module_level_thread_starts(str(good)) == [], (
        "detector descended into a function body"
    )

    # A module-scope `if` is still import-time code and must be caught.
    guarded = tmp_path / "guarded_app.py"
    guarded.write_text(
        "import threading\n"
        "def loop():\n    pass\n"
        "if True:\n"
        "    threading.Thread(target=loop, name='x').start()\n"
    )
    assert _module_level_thread_starts(str(guarded)) == [4]


def test_both_known_daemons_are_registered_through_the_helper():
    """The two incident daemons must still be wired, via the helper."""
    src = open(APP_PATH, encoding="utf-8").read()
    assert "_start_background_thread(_analytics_warmer_loop," in src
    assert "_burst_cohort_scanner_loop, \"burst-cohort-scanner\"" in src
