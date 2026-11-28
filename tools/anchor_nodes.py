"""One full-history witness list for every anchor reader.

Two scripts read the anchor account's history from the ledger: the
divergence canary (tools/anchor_canary.py, public repo) and the Friday
stamp reminder (tools/anchor_friday_reminder.py, Lenovo-only). Until
2026-10-24 each carried its own node list. The canary defaulted to a
genesis-to-tip witness and saw all 12 anchors; the reminder tried our own
node first and STOPPED at the first node that answered -- our node has a
~54k-ledger window, so it answered with zero anchors and the Friday nudge
said "hours since last anchor: unknown" while the canary, minutes later,
reported #12 fine. Same account, same chain, two answers.

This module is the single source of both the list and its env override, so
the two cannot drift again. Public endpoints only -- our own node's LAN
address is never written here; callers prepend it themselves if they want
a sovereign first read (the reminder does, for the fast `today_done` check).

Env: ANCHOR_FULL_HISTORY_NODES — comma-separated, overrides the default.
Legacy ANCHOR_CANARY_FULL_HISTORY_NODES is still honoured (canary only)
so an existing deployment keeps working; the new name wins if both are set.
"""
from __future__ import annotations

import os

DEFAULT_FULL_HISTORY_NODES: tuple[str, ...] = (
    "https://s2-clio.ripple.com:51234",   # Clio, genesis-to-tip
    "https://xrplcluster.com",            # independent full-history cluster
)

ENV_NAME = "ANCHOR_FULL_HISTORY_NODES"


def full_history_nodes(*legacy_env_names: str) -> list[str]:
    """Return the witness list. Order: ANCHOR_FULL_HISTORY_NODES, then any
    legacy env names given (first non-empty wins), then the default."""
    for name in (ENV_NAME, *legacy_env_names):
        raw = os.environ.get(name, "").strip()
        if raw:
            return [u.strip() for u in raw.split(",") if u.strip()]
    return list(DEFAULT_FULL_HISTORY_NODES)


# ─── MPT holders: Clio-only, so it needs its own list ────────────────────────
#
# `mpt_holders` is NOT implemented by rippled 3.4.1. Measured 2026-11-27:
# our own node and xrplcluster.com (both rippled 3.4.1) each return
# {"error": "unknownCmd", "error_code": 32, "error_message": "Unknown
# method."}, while Clio 2.8.0 answers it. Two independent rippled servers of
# the same version rejecting it is what makes this a per-BUILD finding rather
# than a host problem -- re-check after a rippled upgrade so the note cannot
# harden into a permanent refusal.
#
# This is therefore a DISCLOSED THIRD-PARTY read, not a sovereign one. Our own
# node cannot answer it at all, so holder rows carry source + sovereign=False
# and every surface that renders a holder count says so. Callers must not
# prepend our own node here: it would burn a request per issuance to receive
# unknownCmd, and a cascade that silently lands on public infra is exactly the
# undisclosed read the sovereignty covenant forbids.
#
# Endpoints tried on 2026-11-27 (server_info identifies Clio vs rippled):
#   https://s2-clio.ripple.com:51234   Clio 2.8.0     OK
#   https://s1.ripple.com:51234        Clio 2.8.0     OK   <- fallback
#   https://s2.ripple.com:51234        Clio 2.8.0     OK
#   https://xrplcluster.com            rippled 3.4.1  unknownCmd
#   https://s1-clio.ripple.com:51234   unreachable    URLError
#   https://clio.xrplcluster.com       unreachable    URLError
#   https://clio.ripple.com:51234      unreachable    URLError
#   https://xrpl.ws                    unreachable    HTTPError
DEFAULT_MPT_HOLDERS_NODES: tuple[str, ...] = (
    "https://s2-clio.ripple.com:51234",   # Clio 2.8.0, genesis-to-tip
    "https://s1.ripple.com:51234",        # Clio 2.8.0, independent fallback
)

MPT_HOLDERS_ENV_NAME = "MPT_HOLDERS_NODES"


def mpt_holders_nodes() -> list[str]:
    """Clio endpoints for the `mpt_holders` walk, in try order.

    Env MPT_HOLDERS_NODES (comma-separated) overrides the default, so a
    deployment can repoint the walk without a code change.
    """
    raw = os.environ.get(MPT_HOLDERS_ENV_NAME, "").strip()
    if raw:
        return [u.strip() for u in raw.split(",") if u.strip()]
    return list(DEFAULT_MPT_HOLDERS_NODES)
