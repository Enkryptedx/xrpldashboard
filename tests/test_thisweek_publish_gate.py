"""Regression tests for _thisweek_is_published (Charlie 2026-09-20).

Filed after live-publish night when 2026-09-20 refused to publish even
with `manual_go: true` in front-matter: the line-based front-matter
parser stringifies every value, so `manual_go` became the string
'true', and the gate's `is not True` identity check failed every time.
No weekly edition had actually published since the gate was added.
Prior tests only covered the fail-closed side of the gate — this file
covers the OPEN side too.

Invariants the gate must uphold:
1. `manual_go: true` (YAML boolean, arrives as Python bool True) → OPEN
2. `manual_go: 'true'` (line-parser string, ANY case) → OPEN
3. `manual_go: false` / `manual_go: 'false'` → CLOSED
4. `manual_go` missing entirely → CLOSED
5. Any other value (garbage, integer, list, empty string) → CLOSED
6. `manual_go` true AND published_at_utc in the FUTURE → CLOSED
7. `manual_go` true AND published_at_utc in the PAST → OPEN
8. `manual_go` true AND published_at_utc MISSING → OPEN (explicit ruling)

The `a timer NEVER publishes` invariant is #4/#5/#6 — a bare timestamp
never fires without the explicit go.
"""
from __future__ import annotations

import datetime as dt

import pytest


import app as app_module


PAST = "2020-01-01T00:00:00Z"      # always in the past
FUTURE = "2099-12-31T23:59:59Z"    # always in the future


@pytest.mark.parametrize("value", [True, "true", "True", "TRUE", " true "])
def test_gate_opens_for_truthy_manual_go(value):
    """Charlie ruling 2026-09-20: bool True or case-insensitive string
    'true' opens the gate (with a past published_at_utc)."""
    front = {"manual_go": value, "published_at_utc": PAST}
    assert app_module._thisweek_is_published(front) is True


@pytest.mark.parametrize("value", [False, "false", "False", "FALSE"])
def test_gate_closes_for_falsy_manual_go(value):
    """`manual_go: false` never publishes, regardless of timestamp."""
    front = {"manual_go": value, "published_at_utc": PAST}
    assert app_module._thisweek_is_published(front) is False


def test_gate_closes_when_manual_go_missing():
    """Missing manual_go fails-closed (`a timer NEVER publishes`)."""
    front = {"published_at_utc": PAST}
    assert app_module._thisweek_is_published(front) is False


@pytest.mark.parametrize("value", ["", "yes", "1", "on", "garbage", 0, 1, [], {}, None])
def test_gate_closes_for_garbage_manual_go(value):
    """Anything other than bool True or string 'true' fails closed."""
    front = {"manual_go": value, "published_at_utc": PAST}
    assert app_module._thisweek_is_published(front) is False


def test_gate_closes_when_published_at_utc_in_future():
    """Even with manual_go: true, a future-dated edition is a draft
    (window not yet open). Admin preview still 200; public path 404."""
    front = {"manual_go": True, "published_at_utc": FUTURE}
    assert app_module._thisweek_is_published(front) is False


def test_gate_opens_when_published_at_utc_missing():
    """`manual_go: true` with no timestamp publishes immediately —
    explicit go is the load-bearing bit."""
    front = {"manual_go": True}
    assert app_module._thisweek_is_published(front) is True


def test_gate_closes_on_none_front():
    """`_load_thisweek_edition` returns None when the file is missing.
    The gate must fail-closed on that path too."""
    assert app_module._thisweek_is_published(None) is False


def test_gate_opens_on_string_true_with_unparseable_timestamp():
    """Front-matter parser is line-based, so an intentionally
    malformed timestamp still parses as a string. The gate should
    treat unparseable timestamps as `no timestamp given` and let
    `manual_go: true` publish anyway (matches the docstring in
    _thisweek_is_published)."""
    front = {"manual_go": "true", "published_at_utc": "not-a-date"}
    assert app_module._thisweek_is_published(front) is True
