"""tools/anchor_nodes.py — one full-history witness list for both readers.

Two scripts read the anchor account's history: the divergence canary
(tools/anchor_canary.py, this repo) and the Friday stamp reminder
(tools/anchor_friday_reminder.py, Lenovo-only). Until 2026-10-24 each
carried its own node list, and they disagreed: the reminder stopped at the
first node that answered, our own node has a ~54k-ledger window, so it
answered with zero anchors and the Friday nudge said "hours since last
anchor: unknown" while the canary minutes later reported #12 fine.

These tests lock the properties that keep them from drifting again:
  1. the default is TWO independent full-history witnesses, not one;
  2. the canary reads the shared list rather than an inline constant;
  3. the shared env name wins, legacy canary-only env still works;
  4. the import contract the Lenovo reminder depends on keeps working;
  5. no private/LAN address is ever baked into the shared list.

Hermetic: pure imports and env manipulation. No DB, no network.
"""
from __future__ import annotations

import importlib
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(REPO_ROOT, "tools")
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

import anchor_nodes  # noqa: E402
import anchor_canary  # noqa: E402

SHARED_ENV = "ANCHOR_FULL_HISTORY_NODES"
LEGACY_ENV = "ANCHOR_CANARY_FULL_HISTORY_NODES"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(SHARED_ENV, raising=False)
    monkeypatch.delenv(LEGACY_ENV, raising=False)


# ------------------------------------------------- 1. two witnesses

def test_default_has_two_independent_full_history_witnesses():
    """One witness means a single Clio outage is a LOUD SKIP."""
    nodes = anchor_nodes.DEFAULT_FULL_HISTORY_NODES
    assert len(nodes) >= 2, f"need >=2 independent witnesses, got {nodes}"
    joined = " ".join(nodes)
    assert "s2-clio.ripple.com" in joined
    assert "xrplcluster.com" in joined


def test_witnesses_are_distinct_hosts():
    hosts = [n.split("//", 1)[-1].split("/")[0].split(":")[0]
             for n in anchor_nodes.DEFAULT_FULL_HISTORY_NODES]
    assert len(hosts) == len(set(hosts)), f"duplicate witness host in {hosts}"


# ------------------------------------------------- 2. canary reads shared

def test_canary_default_is_the_shared_list():
    """Regression: the canary used to hold its own single-node tuple."""
    assert anchor_canary.full_history_nodes() == list(
        anchor_nodes.DEFAULT_FULL_HISTORY_NODES
    )


def test_canary_exposes_the_shared_default_name():
    """Kept as a name for any caller that imported it from the canary."""
    assert anchor_canary.DEFAULT_FULL_HISTORY_NODES == \
        anchor_nodes.DEFAULT_FULL_HISTORY_NODES


# ------------------------------------------------- 3. env precedence

def test_shared_env_overrides_the_default(monkeypatch):
    monkeypatch.setenv(SHARED_ENV, "https://a.invalid, https://b.invalid")
    assert anchor_canary.full_history_nodes() == [
        "https://a.invalid", "https://b.invalid"]


def test_legacy_canary_env_still_honoured(monkeypatch):
    """An existing deployment must keep working after the rename."""
    monkeypatch.setenv(LEGACY_ENV, "https://legacy.invalid")
    assert anchor_canary.full_history_nodes() == ["https://legacy.invalid"]


def test_shared_env_wins_when_both_are_set(monkeypatch):
    monkeypatch.setenv(SHARED_ENV, "https://new.invalid")
    monkeypatch.setenv(LEGACY_ENV, "https://legacy.invalid")
    assert anchor_canary.full_history_nodes() == ["https://new.invalid"]


def test_blank_env_falls_through_to_default(monkeypatch):
    monkeypatch.setenv(SHARED_ENV, "   ")
    assert anchor_canary.full_history_nodes() == list(
        anchor_nodes.DEFAULT_FULL_HISTORY_NODES
    )


# ------------------------------------------------- 4. reminder contract

def test_reminder_import_contract_holds():
    """The Lenovo reminder does `from anchor_nodes import full_history_nodes`
    and calls it with its own legacy env name. Keep that signature."""
    fn = anchor_nodes.full_history_nodes
    assert callable(fn)
    assert fn() == list(anchor_nodes.DEFAULT_FULL_HISTORY_NODES)
    # Tolerates zero or many legacy names without raising.
    assert fn("SOME_LEGACY_NAME") == list(anchor_nodes.DEFAULT_FULL_HISTORY_NODES)


def test_reminder_legacy_name_is_consulted(monkeypatch):
    monkeypatch.setenv("ANCHOR_REMINDER_NODES", "https://r.invalid")
    assert anchor_nodes.full_history_nodes("ANCHOR_REMINDER_NODES") == [
        "https://r.invalid"]


# ------------------------------------------------- 5. no private addresses

def test_no_private_or_lan_address_in_the_shared_list():
    """The module promises public endpoints only -- callers prepend our own
    node themselves. A LAN address here would leak infra and would also
    reintroduce the ~54k-window node that caused the disagreement."""
    joined = " ".join(anchor_nodes.DEFAULT_FULL_HISTORY_NODES)
    for bad in ("192.168.", "10.", "127.0.0.1", "localhost", ".local"):
        assert bad not in joined, f"private address {bad!r} in shared witness list"


def test_all_witnesses_are_https():
    for n in anchor_nodes.DEFAULT_FULL_HISTORY_NODES:
        assert n.startswith("https://"), n
