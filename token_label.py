"""Render decisions for token_names.json entries.

WHY THIS EXISTS (2026-10-16). `_load_token_names_dict()` suppressed exactly
one thing: `verified_via == "TODO_curation_pass"`. An entry with NO
`verified_via` field fell straight through and rendered. 14 freshly-walked
MPT entries had no `verified_via` at all, so committing them as data would
have published 5 Ripple-RLUSD lookalikes, 2 XRP lookalikes and 5
self-declared test tokens under curated-looking names.

The root confusion was the `source` field doubling as a trust tier.
`source: mpt_metadata` means "the name came from XLS-89 on-ledger metadata",
which is protocol-deterministic — we read the ledger correctly. It does NOT
mean the issuer's claim is true: anyone can mint an MPT and self-declare any
ticker. So trust now lives in its own field:

    source : where the name came from        (unchanged, provenance)
    tier   : how far we trust it             (self_described | domain_fallback | …)

Untagged entries behave exactly as before — this module only changes
behaviour for entries that carry an explicit `tier`, which keeps the
2026-10-16 change scoped to the 14 reviewed entries and leaves the ~286
untagged ones for a separate review.
"""
from __future__ import annotations

from urllib.parse import urlparse

# Assets whose ticker we will defend. `canonical_issuers` empty means the
# asset has NO legitimate issuer: XRP is the native asset, so any *issued*
# token calling itself XRP is by definition not the native one.
CURATED_ASSETS: dict[str, dict] = {
    "RLUSD": {
        "label": "Ripple's RLUSD",
        "canonical_issuers": frozenset({"rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De"}),
    },
    "XRP": {
        "label": "XRP",
        "canonical_issuers": frozenset(),
    },
}

SELF_DESCRIBED = "self_described"
DOMAIN_FALLBACK = "domain_fallback"

LABEL_SELF_DESCRIBED = "self-described (unverified)"


def is_suppressed(entry: dict) -> bool:
    """True when an entry must never reach a rendered surface."""
    if not isinstance(entry, dict):
        return True
    if entry.get("suppressed"):
        return True
    # Pre-existing sentinel, kept working: "pending curation" also means
    # "do not publish yet".
    return entry.get("verified_via") == "TODO_curation_pass"


def lookalike_asset(entry: dict, assets: dict | None = None) -> str | None:
    """Return the curated asset key this entry imitates, else None.

    Mechanical: the display ticker CONTAINS a curated ticker but is not that
    asset from its canonical issuer.

    Deliberately gated on `tier == self_described`. Without that gate this
    would fire on 92 legitimate `lp_derived` LP tokens (`LP: XRP/SOLO`,
    `LP: RLUSD/mXRP`, …) and on 3 pre-existing self-described entries that
    have not been reviewed. Trust tier, not string matching alone, decides
    who is eligible for the check.
    """
    assets = CURATED_ASSETS if assets is None else assets
    if not isinstance(entry, dict):
        return None
    if entry.get("tier") != SELF_DESCRIBED:
        return None
    disp = (entry.get("currency_display") or "").upper()
    issuer = entry.get("issuer")
    for key, meta in assets.items():
        if key not in disp:
            continue
        if disp == key and issuer in meta["canonical_issuers"]:
            return None          # the real thing
        if issuer in meta["canonical_issuers"]:
            return None          # canonical issuer, variant ticker
        return key
    return None


def cited_domain(entry: dict) -> str | None:
    """Host of a `verified_via` URL, or None."""
    vv = entry.get("verified_via") if isinstance(entry, dict) else None
    if not vv or not isinstance(vv, str) or vv == "TODO_curation_pass":
        return None
    host = urlparse(vv).netloc or None
    return host.lower() if host else None


def domain_is_reciprocated(address: str, toml_text: str | None) -> bool:
    """True when the cited domain's own TOML names the issuer address.

    Owner rule 2026-10-16: a `Domain` field is a one-way claim. DDG's
    AccountRoot claimed `bithomp.com`, but bithomp's TOML lists two
    unrelated accounts — a memecoin asserting a well-known explorer as its
    domain, unreciprocated. A claim nobody confirms is not evidence, so it
    is ignored rather than displayed.
    """
    if not address or not toml_text:
        return False
    return address in toml_text


def label_for(entry: dict) -> str | None:
    """The exact string appended to a display name, or None for no label."""
    if not isinstance(entry, dict):
        return None
    tier = entry.get("tier")
    if tier == DOMAIN_FALLBACK:
        host = cited_domain(entry)
        # Issuer-level only: a TOML [[CURRENCIES]] block cannot describe an
        # MPT (the spec predates MPTs), so this proves WHO issued it, never
        # that the ticker is right.
        return f"issuer verified via {host}" if host else LABEL_SELF_DESCRIBED
    if tier == SELF_DESCRIBED:
        asset = lookalike_asset(entry)
        if asset:
            return (f"\u26a0 not {CURATED_ASSETS[asset]['label']} "
                    f"\u00b7 self-described")
        return LABEL_SELF_DESCRIBED
    return None                  # untagged: unchanged behaviour


def render_decision(entry: dict) -> tuple[bool, str | None]:
    """(should_render, label). Suppressed entries never render."""
    if is_suppressed(entry):
        return False, None
    return True, label_for(entry)
