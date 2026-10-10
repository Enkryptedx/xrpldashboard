"""The render-killer guards on /amendments must leave a trace.

Why this exists (2026-10-10): the roll-call card's guard was
`except Exception: roll_call = None` with NO logging. A stale monkeypatch
signature raised TypeError inside it and the card silently vanished from
the page — the failing test read "the card did not render" rather than
"your fake is stale", and finding it cost a full bisect. One warning turns
that into one log line.

The guards still swallow, deliberately: /amendments must never 500 because
a side card failed. What changes is that the failure is now visible.

Rate limited once per 10 minutes per (site, exception type) so that:
  * a persistent failure cannot flood the log on every request, and
  * a NEW failure mode still surfaces on its FIRST occurrence instead of
    waiting out an unrelated error's cooldown.

Hermetic: calls the helper directly with fake exceptions. No DB, no node,
no render.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_mod  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_state():
    """Each test starts with an empty cooldown table."""
    app_mod._silent_guard_last_logged.clear()
    yield
    app_mod._silent_guard_last_logged.clear()


def test_first_failure_is_logged():
    assert app_mod._log_silent_guard("roll_call_card", TypeError("boom")) is True


def test_repeat_of_same_site_and_type_is_suppressed():
    assert app_mod._log_silent_guard("roll_call_card", TypeError("boom")) is True
    for _ in range(5):
        assert app_mod._log_silent_guard("roll_call_card", TypeError("boom")) is False


def test_a_different_exception_type_is_not_muted_by_the_cooldown():
    """The whole point of keying on type: a NEW failure mode must not wait
    out an unrelated error's 10 minutes."""
    assert app_mod._log_silent_guard("roll_call_card", TypeError("boom")) is True
    assert app_mod._log_silent_guard("roll_call_card", KeyError("other")) is True


def test_a_different_site_is_not_muted_by_the_cooldown():
    assert app_mod._log_silent_guard("roll_call_card", TypeError("x")) is True
    assert app_mod._log_silent_guard("network_pulse (flag counter)",
                                     TypeError("x")) is True


def test_it_logs_again_after_the_interval(monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(app_mod.time, "monotonic", lambda: clock["t"])

    assert app_mod._log_silent_guard("roll_call_card", TypeError("boom")) is True
    clock["t"] += app_mod._SILENT_GUARD_LOG_INTERVAL_S - 1
    assert app_mod._log_silent_guard("roll_call_card", TypeError("boom")) is False
    clock["t"] += 2  # now past the interval
    assert app_mod._log_silent_guard("roll_call_card", TypeError("boom")) is True


def test_interval_is_ten_minutes():
    assert app_mod._SILENT_GUARD_LOG_INTERVAL_S == 600


def test_logging_failure_never_breaks_the_guard(monkeypatch):
    """A logging problem must not break the guard it reports on - the guard
    exists to keep the page up."""
    def boom(*a, **k):
        raise RuntimeError("logger exploded")
    monkeypatch.setattr(app_mod.app.logger, "warning", boom)
    assert app_mod._log_silent_guard("roll_call_card", TypeError("x")) is False


def test_both_guard_sites_are_wired_up():
    """Grep-level proof the helper is actually CALLED, not just defined.

    A helper nobody calls is the same failure class as a test nobody runs.
    """
    with open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "app.py"), encoding="utf-8") as fh:
        src = fh.read()
    assert src.count("_log_silent_guard(") >= 3  # 1 def + 2 call sites
    assert '_log_silent_guard("roll_call_card", e)' in src
    assert '_log_silent_guard("network_pulse (flag counter)", e)' in src
