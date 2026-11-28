"""ezr_web.py — input handling for the EZR consumer scam-checker skeleton.

One box, one of three kinds of input, one traffic light.

This module does the PARSING and the DISPATCH, then hands off:
    ezr_redflags.collect()  structured known-bad hits
    ezr_verdict.verdict()   the light

Everything it dispatches to is the EXISTING engine in check_data.py. No
lookup logic is reimplemented here.

# EZR-NOTE: the input precedence below is copied deliberately from
# app.py:check_page() rather than invented, so EZR and /check agree about
# what a given paste MEANS. If they disagreed, a string could be a token on
# one page and a domain on the other, and the safety gate that protects
# /check would not protect EZR. The order is: explicit scheme -> token form
# (SYMBOL.rIssuer) -> bare domain -> bare r-address.
"""

from __future__ import annotations

import check_data
import ezr_redflags
import ezr_verdict

# Mirrors app.py:_XRPL_ADDR_CHARS (base58, XRPL alphabet).
# # EZR-NOTE: duplicated from app.py because importing app from here would
# be a circular import (app imports this module to serve the route). The
# proper fix is to promote _is_xrpl_address into a shared helper module;
# left as a follow-up because this skeleton is not allowed to refactor the
# existing site. Kept byte-identical to app.py's alphabet so the two cannot
# disagree about what a valid address looks like.
_XRPL_ADDR_CHARS = set("rpshnaf39wBUDNEGHJKLM4PQRST7VWXYZ2bcdeCg65jkm8oFqi1tuvAxyz")

MAX_INPUT_LEN = 2048

# Accepted-input help string, shown on the form and on a parse failure.
INPUT_HELP = (
    "Paste an XRPL wallet address (starts with “r”), a website domain, "
    "or a token as SYMBOL.rIssuer."
)


def is_xrpl_address(s: str) -> bool:
    """Loose XRPL address validation. Mirrors app.py:_is_xrpl_address."""
    if not s or not isinstance(s, str):
        return False
    if not s.startswith("r"):
        return False
    if not (25 <= len(s) <= 35):
        return False
    return all(c in _XRPL_ADDR_CHARS for c in s)


def classify(q: str) -> str:
    """What KIND of thing the user pasted, without doing any lookup.

    Returns one of: "url", "token", "address", "unknown". Pure and cheap,
    so the route can label the result and tests can pin precedence without
    touching the network.
    """
    q = (q or "").strip()
    if not q:
        return "unknown"
    if "://" in q:
        return "url"
    if "." in q:
        symbol, _, rest = q.partition(".")
        if symbol.strip() and is_xrpl_address(rest.strip()):
            return "token"
        return "url"
    if is_xrpl_address(q):
        return "address"
    return "unknown"


def lookup(q: str) -> tuple[dict | None, str | None]:
    """Run the EXISTING engine for one pasted string.

    Returns (result, input_error) with exactly one of them set.

    # EZR-NOTE: the _domain_is_safe() gate is kept for both the explicit-
    # scheme and bare-domain paths, exactly as /check does it. It rejects
    # private-network IPs and malformed hosts, which is what stops the
    # checker being pointed at internal addresses. Dropping it for
    # convenience would turn a public form into an SSRF probe.
    """
    q = (q or "").strip()
    if not q:
        return None, "Paste something to check first."
    if len(q) > MAX_INPUT_LEN:
        # EZR-NOTE: clamp rather than process. A multi-kilobyte paste is
        # either a message (a later phase, not this skeleton) or abuse.
        return None, "That input is too long to check."

    kind = classify(q)

    try:
        if kind == "url":
            host = check_data._extract_hostname(q) if "://" in q else q
            if host and check_data._domain_is_safe(host):
                return check_data.check_url(q), None
            return None, (
                "That web address fails a basic safety check (private "
                "network address, invalid shape, or unusual characters)."
            )

        if kind == "token":
            symbol, _, issuer = q.partition(".")
            return check_data.check_token(symbol.strip(), issuer.strip()), None

        if kind == "address":
            return check_data.check_address(q), None

        return None, INPUT_HELP

    except Exception as exc:
        # EZR-NOTE: the exception text is NOT shown to the user -- it can
        # carry internal hostnames and stack detail. The caller logs the
        # real exception; the visitor gets a generic line and an AMBER
        # light, because a crashed lookup is unverifiable, never clean.
        return None, "Something went wrong checking that. Try again in a moment."


def check_input(q: str) -> dict:
    """Full EZR pass for one pasted string: lookup -> red flags -> light.

    Returns the ezr_verdict.verdict() dict, plus `query` and `kind` for
    rendering. Never raises.
    """
    result, input_error = lookup(q)
    red_flags = ezr_redflags.collect(result) if result else []
    out = ezr_verdict.verdict(result, red_flags=red_flags,
                              input_error=input_error)
    out["query"] = (q or "").strip()
    out["kind"] = classify(q)
    out["light_label"] = ezr_verdict.light_label(out["light"])
    return out
