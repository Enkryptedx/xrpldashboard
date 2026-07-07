"""
Transaction-type mix — what the XRP Ledger is actually being used for.

The stream handler (xrpl_stream.tx_type_bucket_handler) counts every
validated, tesSUCCESS transaction into tx_type_hourly (per raw XRPL
TransactionType, per hour bucket, running upsert). This module reads
those counts and folds them into visitor-facing buckets.

Design decisions codified elsewhere (do not re-litigate here):
  * Count, not dollar value. This module counts transactions. The number
    of trust-line changes says nothing about the size of a payment. The
    UI puts a prominent label on this at the top of the module — do not
    quietly change the aggregation to something value-weighted.
  * "Since [date]" honesty. When the window a visitor picked (24h / 7d /
    30d) has not fully accrued yet (because we started counting on the
    day the feature shipped), fetch_tx_type_mix() returns an is_partial
    flag + since_iso, so the template can say "since <date>" instead of
    implying a full window of coverage.
  * No historical backfill. XRPL's tx history goes back years, but
    re-scanning it to synthesise "true" 30d numbers before we had a
    stream running would be inventing data. The unfillable gap is the
    truth signal (feedback: unfillable_gaps_are_truth_signal).

Category taxonomy is the 9 approved buckets. Every TransactionType we
recognise is mapped here. Any TransactionType the classifier has not
seen is folded into "Newer & experimental" (also the home of the
XLS-70 amendment family) — visible as a bucket, not silently dropped.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

import db


# ─── Bucket labels (visitor-facing) ─────────────────────────────────
# Order = display order in the UI. Charlie approved the 9-bucket
# taxonomy at Gate 1.

BUCKET_ORDER = [
    "Payments",
    "DEX order book",
    "AMM activity",
    "Trust-line changes",
    "NFT operations",
    "Escrow / channels / checks",
    "Account admin",
    "Newer & experimental",
    "Amendments & consensus",
]

# One-line plain-English explanation shown under each bucket name so a
# first-time visitor knows what it means without opening a glossary.
BUCKET_BLURB = {
    "Payments":
        "Direct account-to-account transfers, including routed swaps "
        "that settle through the DEX or an AMM but arrive as a Payment.",
    "DEX order book":
        "Adding and cancelling limit orders on XRPL's native order book.",
    "AMM activity":
        "Automated market maker events — creating pools, adding or "
        "removing liquidity, voting, or bidding on auction slots.",
    "Trust-line changes":
        "A holder adjusting the relationship with an issued-currency "
        "issuer (opening, closing, or updating a trust line). "
        "Trust-line count is a housekeeping metric, not a demand "
        "signal.",
    "NFT operations":
        "Minting, burning, offering, or accepting XRPL native NFTs.",
    "Escrow / channels / checks":
        "Time-locked or conditional payments — escrows, payment "
        "channels, and Checks.",
    "Account admin":
        "Housekeeping on an existing account: flag changes, regular-key "
        "rotations, signer-list updates, ticket allocation, account "
        "deletion.",
    "Newer & experimental":
        "Recently-enabled features — MPTs, DIDs, credentials, "
        "permissioned domains, and cross-chain bridges.",
    "Amendments & consensus":
        "Protocol-level events emitted by validators themselves — "
        "amendment votes, fee-vote settlement, UNL modifications.",
}

# Raw TransactionType → bucket. Order-agnostic; the display order is
# BUCKET_ORDER. Types not in this map fall into "Newer & experimental"
# so a new amendment doesn't silently drop counts.
TX_TYPE_TO_BUCKET: dict[str, str] = {
    # Payments
    "Payment":                          "Payments",

    # DEX order book
    "OfferCreate":                      "DEX order book",
    "OfferCancel":                      "DEX order book",

    # AMM
    "AMMCreate":                        "AMM activity",
    "AMMDeposit":                       "AMM activity",
    "AMMWithdraw":                      "AMM activity",
    "AMMBid":                           "AMM activity",
    "AMMVote":                          "AMM activity",
    "AMMDelete":                        "AMM activity",
    "AMMClawback":                      "AMM activity",

    # Trust lines (Clawback recovers an issued balance from a trust
    # line — belongs with trust-line changes, not account admin).
    "TrustSet":                         "Trust-line changes",
    "Clawback":                         "Trust-line changes",

    # NFTs
    "NFTokenMint":                      "NFT operations",
    "NFTokenBurn":                      "NFT operations",
    "NFTokenCreateOffer":               "NFT operations",
    "NFTokenAcceptOffer":               "NFT operations",
    "NFTokenCancelOffer":               "NFT operations",
    "NFTokenModify":                    "NFT operations",

    # Escrow / channels / checks
    "EscrowCreate":                     "Escrow / channels / checks",
    "EscrowFinish":                     "Escrow / channels / checks",
    "EscrowCancel":                     "Escrow / channels / checks",
    "PaymentChannelCreate":             "Escrow / channels / checks",
    "PaymentChannelFund":               "Escrow / channels / checks",
    "PaymentChannelClaim":              "Escrow / channels / checks",
    "CheckCreate":                      "Escrow / channels / checks",
    "CheckCash":                        "Escrow / channels / checks",
    "CheckCancel":                      "Escrow / channels / checks",

    # Account admin
    "AccountSet":                       "Account admin",
    "AccountDelete":                    "Account admin",
    "SetRegularKey":                    "Account admin",
    "SignerListSet":                    "Account admin",
    "DepositPreauth":                   "Account admin",
    "TicketCreate":                     "Account admin",

    # Newer / experimental (XLS-70 family + XChain bridges + any
    # unrecognised type via the fallback in _bucket_for).
    "CredentialCreate":                 "Newer & experimental",
    "CredentialAccept":                 "Newer & experimental",
    "CredentialDelete":                 "Newer & experimental",
    "PermissionedDomainSet":            "Newer & experimental",
    "PermissionedDomainDelete":         "Newer & experimental",
    "MPTokenIssuanceCreate":            "Newer & experimental",
    "MPTokenIssuanceSet":               "Newer & experimental",
    "MPTokenIssuanceDestroy":           "Newer & experimental",
    "MPTokenAuthorize":                 "Newer & experimental",
    "DIDSet":                           "Newer & experimental",
    "DIDDelete":                        "Newer & experimental",
    "XChainAccountCreateCommit":        "Newer & experimental",
    "XChainAddAccountCreateAttestation":"Newer & experimental",
    "XChainAddClaimAttestation":        "Newer & experimental",
    "XChainClaim":                      "Newer & experimental",
    "XChainCommit":                     "Newer & experimental",
    "XChainCreateBridge":               "Newer & experimental",
    "XChainCreateClaimID":              "Newer & experimental",
    "XChainModifyBridge":               "Newer & experimental",
    "OracleSet":                        "Newer & experimental",
    "OracleDelete":                     "Newer & experimental",

    # Amendments & consensus (emitted by validators, not user accounts)
    "EnableAmendment":                  "Amendments & consensus",
    "SetFee":                           "Amendments & consensus",
    "UNLModify":                        "Amendments & consensus",
}


# Windows the visitor can pick. Keep the list short; each option maps
# to hours-back for db.read_tx_type_counts. The default (7d) balances
# recency against noise from a single high-activity hour.
WINDOWS = [
    ("24h", 24),
    ("7d",  24 * 7),
    ("30d", 24 * 30),
]
DEFAULT_WINDOW = "7d"

# How many minutes of history are needed before we drop the "since
# [date]" partial-window label for a given window. If a visitor picks
# 30d but we only have 4d of data, they should see "since 2026-07-04",
# not a bare "30d" that implies coverage we don't have.
FULLY_ACCRUED_TOLERANCE_HOURS = 1


def _bucket_for(tx_type: str) -> str:
    return TX_TYPE_TO_BUCKET.get(tx_type, "Newer & experimental")


def fetch_tx_type_mix(window: str = DEFAULT_WINDOW) -> dict:
    """Read the running counts for the selected window and return a
    dict shaped for direct rendering by templates/network.html.

    Returns
    -------
    {
        "window": "7d",
        "windows": [{"key": "24h", "label": "24h", "hours": 24}, ...],
        "total": int,                # sum across all buckets
        "buckets": [
            {"key": ..., "label": ..., "blurb": ..., "count": int,
             "pct": float, "raw_types": [(type, count), ...]},
            ...
        ],
        "is_partial": bool,          # True if our data doesn't yet
                                     # cover the full requested window
        "since_iso": str | None,     # earliest hour recorded in DB
        "hours_covered": int,        # distinct hour buckets in window
        "hours_requested": int,      # from the window key
        "fetched_at_iso": str,
    }
    """
    hours_by_key = {k: h for k, h in WINDOWS}
    if window not in hours_by_key:
        window = DEFAULT_WINDOW
    hours_requested = hours_by_key[window]

    counts, min_bucket, max_bucket, distinct_buckets = db.read_tx_type_counts(
        hours_requested
    )
    first_bucket_ever = db.read_tx_type_first_bucket()

    # Fold raw types into buckets.
    bucket_counts: dict[str, int] = {b: 0 for b in BUCKET_ORDER}
    bucket_raw: dict[str, list[tuple[str, int]]] = {b: [] for b in BUCKET_ORDER}
    for tx_type, n in counts.items():
        if n <= 0:
            continue
        b = _bucket_for(tx_type)
        bucket_counts[b] += int(n)
        bucket_raw[b].append((tx_type, int(n)))

    for b in bucket_raw:
        bucket_raw[b].sort(key=lambda x: x[1], reverse=True)

    total = sum(bucket_counts.values())

    buckets = []
    for name in BUCKET_ORDER:
        c = bucket_counts[name]
        pct = (100.0 * c / total) if total > 0 else 0.0
        buckets.append({
            "key": _slug(name),
            "label": name,
            "blurb": BUCKET_BLURB[name],
            "count": c,
            "pct": round(pct, 2),
            "raw_types": bucket_raw[name],
        })

    # Honest window: are we short of the requested hours?
    is_partial = (
        distinct_buckets < (hours_requested - FULLY_ACCRUED_TOLERANCE_HOURS)
    )
    since_iso = _hour_bucket_to_iso(first_bucket_ever)

    return {
        "window": window,
        "windows": [
            {"key": k, "label": k, "hours": h} for k, h in WINDOWS
        ],
        "total": total,
        "buckets": buckets,
        "is_partial": is_partial,
        "since_iso": since_iso,
        "hours_covered": distinct_buckets,
        "hours_requested": hours_requested,
        "fetched_at_iso": datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
    }


def _slug(label: str) -> str:
    out = []
    prev_dash = False
    for ch in label.lower():
        if ch.isalnum():
            out.append(ch)
            prev_dash = False
        elif not prev_dash:
            out.append("-")
            prev_dash = True
    return "".join(out).strip("-")


def _hour_bucket_to_iso(hour_bucket: int | None) -> str | None:
    if hour_bucket is None:
        return None
    ts = int(hour_bucket) * 3600
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
