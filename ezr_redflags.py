"""ezr_redflags.py — structured known-bad detection for the EZR traffic light.

RED means "known-bad / scam-pattern hit". This module finds those hits by
calling the EXISTING check_data.py helpers against STRUCTURED data. It adds
no new intelligence and no new data sources; it re-asks questions the engine
already knows how to answer, and reads the answers as data rather than prose.

# EZR-NOTE: every check here reads a dict/None/tuple return value. None of it
# parses signal text. The engine's signal dicts carry no severity field, and
# its OFAC signal returns a populated dict BOTH when an address is sanctioned
# and when it is clean ("not present" is itself a real signal) -- so the only
# honest way to tell a hit from a miss is to look at the snapshot, which is
# what _ofac_hit does. Reading the sentence would have been a coin flip on
# wording.

# EZR-NOTE: this module calls underscore-prefixed helpers in check_data
# (_load_ofac_snapshot, _lookalike_named_address, _load_brand_domains,
# _find_lookalike). That is real coupling to private names and I am flagging
# it rather than hiding it. The alternative was re-implementing sanctions
# matching and Levenshtein vanity-address detection inside EZR, which would
# fork the engine -- explicitly forbidden, and worse besides (two
# implementations drift and the copy is the one that goes stale). The clean
# fix is a small public accessor layer in check_data; left as a follow-up so
# this skeleton does not edit the engine at all.
"""

from __future__ import annotations

from typing import Any

import check_data


def _ofac_hit(address: str) -> dict | None:
    """The OFAC SDN snapshot entry for `address`, or None.

    # EZR-NOTE: XRPL r-addresses are case-sensitive base58, so this is an
    # exact lookup only. check_data lowercases for ETH-family chains; EZR
    # currently accepts XRPL addresses only, so replicating that would add
    # a branch that can never fire here.
    #
    # EZR-NOTE: if the snapshot is MISSING, this returns None -- which looks
    # identical to "not sanctioned". That absence is NOT treated as clean,
    # because check_address() independently records the unavailable snapshot
    # in `couldnt_check`, and ezr_verdict caps any result with a non-empty
    # couldnt_check at AMBER. The reassuring path therefore depends on the
    # engine having actually performed the check, not on this returning None.
    """
    if not address:
        return None
    try:
        snap = check_data._load_ofac_snapshot() or {}
        if snap.get("_load_error"):
            return None
        addresses = snap.get("addresses") or {}
        hit = addresses.get(address)
        return hit if isinstance(hit, dict) else None
    except Exception:
        # EZR-NOTE: a crash here must not fabricate a RED. It also must not
        # fabricate a clean result; see the couldnt_check reasoning above.
        return None


def _address_flags(address: str, what: str) -> list[dict]:
    flags: list[dict] = []

    hit = _ofac_hit(address)
    if hit:
        entity = hit.get("entity_name") or "an unnamed entry"
        programs = ", ".join(hit.get("programs") or []) or "unspecified"
        flags.append({
            "code": "ofac_sdn_match",
            "headline": "On the US sanctions list",
            "detail": (
                f"This {what} appears on the US Treasury OFAC SDN list as a "
                f"digital-currency address for “{entity}” "
                f"(programs: {programs}). Do not send funds."
            ),
        })

    try:
        near = check_data._lookalike_named_address(address)
    except Exception:
        near = None
    if near:
        flags.append({
            "code": "lookalike_address",
            "headline": "Looks like a copy of a known address",
            "detail": (
                f"This {what} is only {near.get('distance')} character(s) "
                f"different from “{near.get('name')}”, a known account "
                f"({near.get('near_address')}). Generating a near-identical "
                f"address is a standard way to intercept a payment meant for "
                f"someone else. Check the address character by character."
            ),
        })

    return flags


def _domain_flags(host: str) -> list[dict]:
    """Typosquat detection for a domain.

    # EZR-NOTE: a deliberate DIVERGENCE from /check, flagged for Charlie.
    # /check renders its lookalike section as neutral facts only (see
    # check_data.py: "renders as neutral-facts-only per
    # feedback_check_lookalike_neutral_pill_only"). EZR is a consumer
    # scam-checker, and a domain one or two characters off a known brand is
    # the single most common phishing shape there is -- so here it is RED,
    # not a neutral fact. Instructed to pick the safer light when a mapping
    # is unclear, and for a consumer audience that is RED.
    #
    # The cost of being wrong is a false alarm on a legitimate lookalike
    # domain, which is why the wording says what was matched and tells the
    # reader to verify, rather than asserting fraud. If Charlie would rather
    # this were AMBER, changing the return below is the only edit needed.
    """
    if not host:
        return []
    try:
        brands = check_data._load_brand_domains() or {}
        found = check_data._find_lookalike(host, brands)
    except Exception:
        return []
    if not found:
        return []
    probe, roots = found[0], found[1]
    known = ", ".join(sorted(roots)[:3]) if roots else "a known brand"
    return [{
        "code": "lookalike_domain",
        "headline": "Looks like a copy of a known site",
        "detail": (
            f"“{host}” closely resembles {known}, but is not it. "
            f"Near-identical domains are the most common way phishing sites "
            f"collect wallet keys. Do not enter a seed phrase or connect a "
            f"wallet here."
        ),
    }]


def collect(result: dict | None) -> list[dict]:
    """Every structured known-bad hit for one engine result.

    Returns a list of {"code", "headline", "detail"}; empty means no RED.
    """
    if not isinstance(result, dict) or result.get("error"):
        return []

    kind = (result.get("kind") or "").strip().lower()

    if kind == "wallet":
        return _address_flags(result.get("address") or "", "wallet address")

    if kind == "token":
        # EZR-NOTE: for a token the sanctions/impersonation question applies
        # to the ISSUER -- the account that created it and controls supply.
        # The currency code itself is just text and cannot be sanctioned.
        return _address_flags(result.get("issuer") or "", "token's issuer")

    if kind == "url":
        # check_url sets `subject` to the parsed hostname.
        return _domain_flags(result.get("subject") or "")

    # EZR-NOTE: an unrecognised `kind` yields no RED flags rather than a
    # guess. It still cannot reach GREEN: ezr_verdict's tier gate defaults
    # unknown vocabulary to AMBER, so an unhandled kind degrades to "be
    # careful" instead of either a false alarm or a false reassurance.
    return []
