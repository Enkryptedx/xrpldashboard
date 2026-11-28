"""ezr_verdict.py — map an existing /check engine result to one traffic light.

EZR is a consumer scam-checker. It reuses the EXISTING engine in
check_data.py verbatim; this module adds NOTHING to the lookups and only
decides which of three lights to show.

    GREEN  server-side verification passed
    AMBER  suspicious, or we could not verify
    RED    known-bad / scam-pattern hit

# EZR-NOTE: the brief asked for "// EZR-NOTE:" comments. This is Python,
# where `//` is integer division and a `//` comment is a SyntaxError, so
# every note in a .py file uses `# EZR-NOTE:`. Templates use
# `<!-- EZR-NOTE: -->` and JS uses `// EZR-NOTE:`. The token EZR-NOTE: is
# identical everywhere, so `grep -rn "EZR-NOTE:"` finds all of them.

────────────────────────────────────────────────────────────────────────
THE CENTRAL TENSION, AND WHY THIS MODULE IS CAUTIOUS BY CONSTRUCTION
────────────────────────────────────────────────────────────────────────

check_data.py is deliberately FACTS-NOT-VERDICTS. Its own module docstring
says the status pill "summarizes WHAT IDENTITY CLAIM EXISTS ON THE LEDGER,
not whether the account is safe to interact with", and it ships an
anti-reassurance tooltip whose stated purpose is to kill the inference
"they have a website and a name, so they're probably legit".

A traffic light IS a verdict. So this module is doing exactly the thing the
engine refuses to do, and the engine is right to refuse. Two consequences
are baked in here rather than left to the template:

  1. GREEN is deliberately hard to reach, and it NEVER means "safe to send
     money". It means one specific thing: a first-party identity claim was
     verified server-side, from our own node, with nothing unchecked.
     `caveat` carries that sentence and the template must print it.
  2. Every light is returned WITH the engine's own signals and sources, so
     the light summarises evidence rather than replacing it.

# EZR-NOTE: the engine's signal dicts carry only label/value/source_label/
# source_url/checked_at_utc -- there is NO severity, level or code field
# (verified by inspection: 29 _signal() call sites, no severity key). So a
# light must NEVER be derived by pattern-matching signal prose. Earlier the
# same day, a suppression-reason substring check in token_label.is_withheld()
# turned human explanation text into control flow and withheld a name purely
# because its evidence note mentioned another reason class by name. Prose is
# written for humans and gets reworded; it is not an API. Everything below
# keys off STRUCTURED fields only:
#     result["tier"] / ["tier_canonical"]   documented vocabulary
#     result["couldnt_check"]               list, non-empty = unverified
#     result["sourcing"]                    sovereign_tunnel_client constant
#   plus RED checks run against structured data directly (the OFAC snapshot
#   dict and the engine's own lookalike helpers), never against prose.
"""

from __future__ import annotations

from typing import Any

RED = "red"
AMBER = "amber"
GREEN = "green"

# Order of severity, worst first. Used to combine independent judgements:
# the final light is the WORST of them, never an average.
_SEVERITY = (RED, AMBER, GREEN)


def worst(*lights: str) -> str:
    """The most cautious of several lights.

    # EZR-NOTE: combining is deliberately min-by-severity, not any kind of
    # score or majority. One credible known-bad hit must not be diluted by
    # several reassuring facts -- that averaging is precisely the reasoning
    # a scam victim does ("it had a website AND a name AND a token").
    """
    present = [l for l in lights if l in _SEVERITY]
    if not present:
        return AMBER
    for level in _SEVERITY:
        if level in present:
            return level
    return AMBER


# ── tier vocabulary ──────────────────────────────────────────────────
#
# Observed in check_data.py: "verified", "labeled", "self",
# "self-described", "bare", "unknown".

# Only this tier represents a first-party claim the server actually verified
# (named_accounts entry with verified_via, attested via the issuer's own
# xrp-ledger.toml).
_GREEN_TIERS = frozenset({"verified"})

# EZR-NOTE: "labeled" is a CURATOR-set label from our own database, not a
# server-side verification of a first-party claim, so it does NOT earn GREEN.
# It is better than nothing, so it is not RED either -> AMBER. This is the
# safer reading of an ambiguous tier, as instructed.
#
# EZR-NOTE: "self" / "self-described" is the single most dangerous tier to
# show as GREEN, and the engine says so in its own words: "A scammer can
# fill in a website and a name just as easily as a legitimate project can."
# Self-reported identity is explicitly NOT a safety signal -> AMBER.
_AMBER_TIERS = frozenset({"labeled", "self", "self-described", "bare", "unknown"})


# ── sourcing ─────────────────────────────────────────────────────────
#
# sovereign_tunnel_client: SOURCING_SOVEREIGN = "sovereign",
# SOURCING_FALLBACK = "fallback-public-rpc",
# SOURCING_NO_TUNNEL = "public-no-tunnel-configured",
# SOURCING_STALE_CACHE = "stale-cache".
_SOVEREIGN_SOURCING = "sovereign"


def _sourcing_light(result: dict) -> tuple[str, str | None]:
    """GREEN only when the ledger read came from our OWN node.

    # EZR-NOTE: if the on-chain read fell back to a public RPC, or came from
    # a stale cache, we cannot claim "server-side verification passed" on our
    # own authority -- we are repeating someone else's answer. That caps the
    # light at AMBER. A missing `sourcing` key is NOT treated as a failure,
    # because check_url() results legitimately have no sourcing field (a
    # domain check does no ledger read); those are capped by tier instead.
    """
    sourcing = result.get("sourcing")
    if sourcing is None:
        return GREEN, None  # not applicable; tier still governs
    if sourcing == _SOVEREIGN_SOURCING:
        return GREEN, None
    return AMBER, (
        f"The on-chain read did not come from our own node "
        f"(sourcing: {sourcing}), so this could not be verified "
        f"on our own authority."
    )


def _couldnt_check_light(result: dict) -> tuple[str, str | None]:
    """Anything the engine could not check blocks GREEN.

    # EZR-NOTE: this is the "0/94 vendors flagged this" trap. An unreachable
    # source is an ABSENCE of evidence, and absence must never read as a
    # clean bill of health. The engine already refuses to drop these
    # silently, which is exactly why they are load-bearing here.
    """
    couldnt = result.get("couldnt_check") or []
    if not couldnt:
        return GREEN, None
    labels = [c.get("label") for c in couldnt if isinstance(c, dict) and c.get("label")]
    shown = ", ".join(labels[:3]) if labels else f"{len(couldnt)} check(s)"
    return AMBER, (
        f"Some checks could not be completed ({shown}). "
        f"That is missing information, not a clean result."
    )


def _tier_light(result: dict) -> tuple[str, str | None]:
    """The identity-claim tier, read structurally."""
    tier = (result.get("tier_canonical") or result.get("tier") or "").strip().lower()
    if tier in _GREEN_TIERS:
        return GREEN, None
    if tier in _AMBER_TIERS:
        return AMBER, _tier_reason(tier)
    # EZR-NOTE: an UNRECOGNISED tier string is the clearest case for the
    # safer choice. A future engine change could add a tier this module has
    # never seen; defaulting unknown vocabulary to GREEN would silently
    # reassure on data we do not understand. Default is AMBER.
    return AMBER, (
        f"This check returned an identity tier EZR does not recognise "
        f"({tier or 'none'}), so it is being treated cautiously."
    )


def _tier_reason(tier: str) -> str:
    if tier in ("self", "self-described"):
        return (
            "The details here are self-reported. Anyone can publish a name "
            "and a website, including a scammer, so this is not evidence "
            "that it is genuine."
        )
    if tier == "labeled":
        return (
            "This has a label from our own records, but no verified "
            "first-party proof of identity."
        )
    if tier in ("bare", "unknown"):
        return (
            "There is no identity claim attached to this at all. That is "
            "common and not proof of anything bad, but nothing has been "
            "verified either."
        )
    return "Identity could not be verified."


def verdict(result: dict | None,
            red_flags: list[dict] | None = None,
            input_error: str | None = None) -> dict:
    """Turn one engine result into one light.

    `result`      a dict from check_data.check_address / check_token /
                  check_url, or None.
    `red_flags`   structured known-bad hits from ezr_redflags.collect(),
                  each {"code", "headline", "detail"}.
    `input_error` a user-input problem (unparseable paste, failed safety
                  gate) or an engine exception message.

    Returns {"light", "headline", "reasons", "red_flags", "caveat",
             "result", "input_error"}.
    """
    red_flags = list(red_flags or [])

    # --- RED: a structured known-bad hit outranks everything -----------
    if red_flags:
        return {
            "light": RED,
            "headline": red_flags[0].get("headline") or "Known warning sign found",
            "reasons": [f.get("detail") for f in red_flags if f.get("detail")],
            "red_flags": red_flags,
            "caveat": None,
            "result": result,
            "input_error": None,
        }

    # --- no usable result -> AMBER, never GREEN ------------------------
    # EZR-NOTE: a bad paste or a crashed lookup is UNVERIFIABLE, which is
    # amber by definition. It is emphatically not green ("nothing bad found")
    # and not red either -- we have no evidence about the subject at all, so
    # claiming a known-bad hit would be a lie. Showing green on an engine
    # exception would be the single worst failure mode this module could
    # have, because the user pasted something precisely to be warned.
    # EZR-NOTE: an EMPTY dict counts as unusable too. It is a dict and has no
    # "error" key, so it would otherwise fall through to the tier gates and be
    # described as "no identity claim" -- which reads as a real finding about
    # a real subject when in fact nothing was looked up. Degenerate results
    # must say "couldn't check", not report an absence as evidence.
    #
    # EZR-NOTE: `result` is read defensively because it is NOT always a dict.
    # A first pass did `(result or {}).get("error")` here, which raised
    # AttributeError on a plain string and would have turned a malformed
    # engine return into a 500 on the one page a worried user came to for an
    # answer. Caught by the parametrised test below; the fix is to extract the
    # message only when we actually have a mapping.
    if (result is None or not isinstance(result, dict) or not result
            or result.get("error")):
        engine_error = result.get("error") if isinstance(result, dict) else None
        msg = (input_error
               or engine_error
               or "That could not be checked.")
        return {
            "light": AMBER,
            "headline": "Couldn’t check this",
            "reasons": [msg],
            "red_flags": [],
            "caveat": None,
            "result": None,
            "input_error": msg,
        }

    # --- GREEN requires every independent gate to pass -----------------
    lights, reasons = [], []
    for gate in (_tier_light, _sourcing_light, _couldnt_check_light):
        light, reason = gate(result)
        lights.append(light)
        if reason:
            reasons.append(reason)

    light = worst(*lights)

    if light == GREEN:
        headline = "Verified"
        caveat = (
            "Verified means one thing only: this published a first-party "
            "identity claim and our own server confirmed it. It does NOT "
            "mean it is safe to send money to."
        )
        reasons = [
            result.get("status_line")
            or "A first-party identity claim was verified server-side."
        ]
    else:
        headline = "Be careful"
        caveat = (
            "This is not an accusation. It means nothing here has been "
            "verified, so treat it as unconfirmed."
        )
        if not reasons:
            reasons = [result.get("status_line") or "Nothing could be verified."]

    return {
        "light": light,
        "headline": headline,
        "reasons": reasons,
        "red_flags": [],
        "caveat": caveat,
        "result": result,
        "input_error": None,
    }


def light_label(light: str) -> str:
    """Short human word for a light. Kept here so the template never
    hardcodes verdict wording."""
    return {
        RED: "Stop",
        AMBER: "Be careful",
        GREEN: "Verified",
    }.get(light, "Be careful")
