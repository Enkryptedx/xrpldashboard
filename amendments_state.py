"""
Live XRPL amendments tracker for the /amendments page.

Pulls two public RPCs:
  - `feature`         — every amendment the responding node knows about,
                         with enabled/supported flags
  - `ledger_entry`    — the Amendments ledger object, which lists the
                         currently-enabled amendments AND any amendments
                         in the 14-day Majorities activation window

Combines them into a single state document the page can render straight.
The interesting cases are: in-flight (supported but not yet enabled), and
the rare "in Majorities but the responding node does not recognize the
hash" case — meaning validators on a newer build are voting on an
amendment definition that current released rippled binaries don't carry.

TTL=300s; amendment voting state changes slowly enough that 5-min cache
freshness is invisible and reduces s1.ripple.com load 5x vs 60s.
"""

import os
import threading
import time

import httpx
from flask_babel import lazy_gettext as _l

import amendments_network_votes
import xrpl_client

XRPL_NODE = os.environ.get("XRPL_NODE", "https://s1.ripple.com:51234")
CACHE_TTL = int(os.environ.get("AMENDMENTS_CACHE_TTL", "300"))

# Canonical ledger index for the Amendments singleton ledger object.
# This is a fixed, documented index — same on every XRPL network.
AMENDMENTS_LEDGER_INDEX = (
    "7DB0788C020F02780A673DC74757F23823FA3014C1866E72CC4CD8B226CD6EF4"
)

# XRPL epoch = 2000-01-01 00:00:00 UTC. unix = xrpl + this.
XRPL_EPOCH_OFFSET = 946684800

# Validators that have a majority for 14 consecutive days activate the
# amendment at the first close after the window elapses.
ACTIVATION_WINDOW_SECONDS = 14 * 24 * 3600

# Amendments the responding node still reports as enabled=False but
# which have been superseded by a later amendment that bundled their
# effects (and IS enabled). The XRPL node `feature` API doesn't expose
# an `obsolete` flag, so we maintain this list manually against the
# canonical registry.
#
# Source of truth: https://xrpl.org/resources/known-amendments
# When adding entries, verify the "Obsolete" status there before shipping.
OBSOLETE_AMENDMENTS = {
    "NonFungibleTokensV1",   # superseded by NonFungibleTokensV1_1
    "fixNFTokenDirV1",        # effects bundled into NonFungibleTokensV1_1
    "fixNFTokenNegOffer",     # effects bundled into NonFungibleTokensV1_1
}

# Amendments XRPLF has published as XLS specs but which haven't shipped
# in a rippled release yet. They don't appear in the `feature` RPC and
# have no on-chain hash, so the in-flight iteration above can't surface
# them. We curate them here so /amendments can show the in-development
# roadmap, not just what the responding binary already knows about.
#
# Source of truth: https://xrpl.org/resources/known-amendments
# Each entry must cite the XLS spec; verify status before shipping.
#
# Entry shape:
#   {
#       "xls_number":   "XLS-NN",                  # display, e.g. "XLS-68"
#       "name":         "Amendment Name",          # display
#       "kind":         "feature" | "fix",         # for kind badge
#       "summary":      _l("..."),                 # lazy_gettext — must be
#                                                  #   request-context-safe
#       "source_label": "XLS-NN Foo (XRPL-Standards)",
#       "source_url":   "https://github.com/XRPLF/XRPL-Standards/...",
#       "dependencies": ["XLS-NN Other (status)", ...],  # may be empty
#   }
#
# Empty list = section does not render on /amendments. Content commits
# add entries one at a time so each ships and verifies in isolation.
IN_DEVELOPMENT_AMENDMENTS = [
    {
        "xls_number":   "XLS-68",
        "name":         "Sponsor",
        "kind":         "feature",
        "summary":      _l(
            "Lets one account pay the transaction fees and reserves for "
            "another account, so end users can transact on XRPL without "
            "holding XRP themselves. Supports two modes: co-signed (the "
            "sponsor signs each transaction) and pre-funded (the sponsor "
            "opens a Sponsorship ledger object the other account can draw "
            "from). Defines two granular permissions — SponsorFee and "
            "SponsorReserve — both drawn from the account-permission "
            "namespace established by XLS-74."
        ),
        "source_label": "XLS-68 Sponsored Fees and Reserves (XRPL-Standards)",
        "source_url":   "https://github.com/XRPLF/XRPL-Standards/tree/master/XLS-0068-sponsored-fees-and-reserves",
        "dependencies": ["XLS-74 Account Permissions (Final)"],
    },
    {
        "xls_number":   "XLS-100",
        "name":         "Smart Escrows",
        "kind":         "feature",
        "summary":      _l(
            "Adds programmable conditions to XRPL Escrows: a small piece "
            "of WASM code lives on the Escrow ledger object and decides "
            "whether the escrow can be released or canceled — going "
            "beyond today's time-based and crypto-conditional gates. "
            "Stacks on top of TokenEscrow (already enabled on mainnet, "
            "which extended escrow to IOU and MPT balances), opening "
            "the door to tokenized RWA workflows like conditional "
            "release on attestation or oracle-driven triggers. The "
            "WASM engine and API are defined in a separate companion "
            "XLS that hasn't been assigned a number yet — Smart "
            "Escrows can't ship until that companion lands."
        ),
        "source_label": "XLS-100 Smart Escrows (XRPL-Standards)",
        "source_url":   "https://github.com/XRPLF/XRPL-Standards/tree/master/XLS-0100-smart-escrows",
        "dependencies": ["WASM engine and API spec (companion XLS, no number assigned yet)"],
    },
    {
        "xls_number":   "XLS-96",
        "name":         "Confidential Transfers for Multi-Purpose Tokens",
        "kind":         "feature",
        "summary":      _l(
            "Adds confidential balances and transfers to Multi-Purpose "
            "Tokens: individual balances and transfer amounts are "
            "encrypted under EC-ElGamal and validated by zero-knowledge "
            "proofs, so validators and external observers can't see "
            "them while supply invariants are still enforced. Introduces "
            "five new transaction types covering the confidential-MPT "
            "round-trip and clawback. Builds on XLS-33 MPTokensV1 "
            "(already enabled on mainnet); the sfMutableFlags portion "
            "also requires DynamicMPT once it activates."
        ),
        "source_label": "XLS-96 Confidential Transfers for MPTs (XRPL-Standards)",
        "source_url":   "https://github.com/XRPLF/XRPL-Standards/tree/master/XLS-0096-confidential-mpt",
        "dependencies": [
            "XLS-33 MPTokensV1 (enabled on mainnet)",
            "XLS-94 DynamicMPT (in-flight; required for sfMutableFlags)",
        ],
    },
]


# Interim vote-count notes have been REPLACED by the live-fetch module
# `amendments_network_votes.fetch_network_vote_tallies_cached()`, which
# pulls UNL-scoped trusted-validator tallies from VHS
# (data.xrpl.org/v1/network/amendments/vote/main) on the same 300s cache
# TTL as this module. The symbol is kept empty for one release so that
# whoever reads this file next sees the pointer to the new module
# inline; it will be deleted in a follow-up cleanup.
INTERIM_VOTE_NOTES: dict = {}


# Hashes we can name from off-ledger sources even when the responding
# node doesn't carry the definition (e.g. amendments merged to rippled
# `develop` but not yet in a released binary). Each entry must cite a
# verifiable source the reader can re-check.
KNOWN_UNRECOGNIZED_HASHES = {
    "303ACB16CF8DBD3B5C34F131A9D19A7DE01AE05F480A8A682B869D1B4AAC8CFC": {
        "name": "fixCleanup3_1_3",
        "source_label": "rippled PR #7128 (merged 2026-05-13)",
        "source_url": "https://github.com/XRPLF/rippled/pull/7128",
    },
}

_cache_lock = threading.Lock()
_cache = {"fetched_at": 0.0, "data": None}


def _post(method, params, fetcher=None):
    """Tunnel-first RPC POST. 2026-09-06: switched from direct httpx.post
    to SovereignFetcher.call so /amendments now cascades to public XRPL
    only when the sovereign tunnel is unavailable. If a caller shares one
    fetcher across multiple _post calls (fetch_amendments_state does),
    a cascade after the first sticks and the whole page's sourcing
    reflects worst-case.
    """
    if fetcher is None:
        # Standalone call — one-shot fetcher (rare; almost every caller
        # threads the shared fetcher through)
        from sovereign_tunnel_client import SovereignFetcher
        import xrpl_client as _xc
        fetcher = SovereignFetcher(
            public_url=_xc.PUBLIC_NODES[0],
            walker_name="amendments_state",
        )
    return fetcher.call(method, params)


def _xrpl_close_to_iso(close_time):
    if close_time is None:
        return None
    try:
        return time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime(int(close_time) + XRPL_EPOCH_OFFSET),
        )
    except (TypeError, ValueError):
        return None


def fetch_amendments_state():
    """Return a fresh combined state dict. No caching here; see the
    `_cached` wrapper for memoization.

    2026-09-06: uses one SovereignFetcher for both RPCs so a cascade
    after the first sticks (correct per-page semantic — worst case
    wins for sourcing disclosure). Return dict gains `sourcing` for
    downstream banner rendering.
    """
    from sovereign_tunnel_client import SovereignFetcher
    fetcher = SovereignFetcher(
        public_url=xrpl_client.PUBLIC_NODES[0],
        walker_name="amendments_state",
    )
    feat_result = _post("feature", {}, fetcher=fetcher)
    ledger_result = _post(
        "ledger_entry",
        {"index": AMENDMENTS_LEDGER_INDEX, "ledger_index": "validated"},
        fetcher=fetcher,
    )
    if not feat_result or not ledger_result:
        return {"ok": False, "sourcing": fetcher.sourcing}

    features = feat_result.get("features") or {}
    node = (ledger_result.get("node") or {})
    majorities_raw = node.get("Majorities") or []
    enabled_hashes = set(node.get("Amendments") or [])

    network_votes_env = amendments_network_votes.fetch_network_vote_tallies_cached()
    network_votes = network_votes_env.get("data") or {}

    enabled = []
    in_flight = []
    superseded = []
    for h, info in features.items():
        if not isinstance(info, dict):
            continue
        name = info.get("name")
        if info.get("enabled"):
            enabled.append({"hash": h, "name": name})
        elif name in OBSOLETE_AMENDMENTS:
            superseded.append({"hash": h, "name": name})
        elif info.get("supported"):
            entry = {"hash": h, "name": name}
            net_vote = network_votes.get((h or "").upper())
            if net_vote is not None:
                entry["network_vote"] = net_vote
            in_flight.append(entry)

    enabled.sort(key=lambda x: (x["name"] or "").lower())
    in_flight.sort(key=lambda x: (x["name"] or "").lower())
    superseded.sort(key=lambda x: (x["name"] or "").lower())

    # Canonical enabled count comes from the Amendments ledger object, not
    # from feature-RPC matches. Any amendment enabled on-chain that the
    # responding node doesn't recognize would otherwise be silently dropped
    # from the hero count.
    recognized_enabled_hashes = {e["hash"] for e in enabled}
    unrecognized_enabled = []
    for h in enabled_hashes - recognized_enabled_hashes:
        meta = KNOWN_UNRECOGNIZED_HASHES.get(h)
        unrecognized_enabled.append({
            "hash": h,
            "name": (meta or {}).get("name"),
            "source_label": (meta or {}).get("source_label"),
            "source_url": (meta or {}).get("source_url"),
        })
    unrecognized_enabled.sort(key=lambda x: (x["name"] or x["hash"]).lower())

    majorities = []
    for entry in majorities_raw:
        m = (entry or {}).get("Majority") or {}
        h = m.get("Amendment")
        close = m.get("CloseTime")
        if not h:
            continue
        feat = features.get(h) or {}
        name = feat.get("name")
        recognized = bool(feat)
        known_meta = KNOWN_UNRECOGNIZED_HASHES.get(h) if not recognized else None
        majority_iso = _xrpl_close_to_iso(close)
        activation_iso = _xrpl_close_to_iso(
            close + ACTIVATION_WINDOW_SECONDS if close is not None else None
        )
        majorities.append({
            "hash": h,
            "name": name,
            "recognized": recognized,
            "known_meta": known_meta,
            "majority_close_time_xrpl": close,
            "majority_reached_iso": majority_iso,
            "activation_eta_iso": activation_iso,
        })

    in_flight_with_votes = sum(1 for e in in_flight if "network_vote" in e)

    return {
        "ok": True,
        "sourcing": fetcher.sourcing,
        "ledger_index": ledger_result.get("ledger_index")
            or feat_result.get("ledger_index"),
        "enabled_count": len(enabled_hashes),
        "unrecognized_enabled": unrecognized_enabled,
        "unrecognized_enabled_count": len(unrecognized_enabled),
        "in_flight": in_flight,
        "in_flight_count": len(in_flight),
        "in_flight_with_votes_count": in_flight_with_votes,
        "in_flight_without_votes_count": len(in_flight) - in_flight_with_votes,
        "network_votes_source": {
            "url": network_votes_env.get("source_url"),
            "as_of_iso": network_votes_env.get("as_of_iso"),
            "status": network_votes_env.get("status"),
            "stale_age_seconds": network_votes_env.get("stale_age_seconds"),
        },
        "superseded": superseded,
        "superseded_count": len(superseded),
        "in_development": IN_DEVELOPMENT_AMENDMENTS,
        "in_development_count": len(IN_DEVELOPMENT_AMENDMENTS),
        "majorities": majorities,
        "fetched_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def fetch_amendments_state_cached(ttl=None):
    ttl = ttl if ttl is not None else CACHE_TTL
    now = time.time()
    with _cache_lock:
        if _cache["data"] is not None and (now - _cache["fetched_at"]) < ttl:
            data = dict(_cache["data"])
            data["cached_age_seconds"] = round(now - _cache["fetched_at"], 1)
            return data
        fresh = fetch_amendments_state()
        if fresh.get("ok"):
            _cache["fetched_at"] = now
            _cache["data"] = fresh
        result = dict(fresh)
        result["cached_age_seconds"] = 0.0
        return result
