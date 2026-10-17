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

# An MPT's currency_hex is the 48-char MPTokenIssuanceID (4-byte sequence +
# 20-byte issuer AccountID). Key length partitions token_names.json exactly:
#   3-char  -> 38 classic tickers
#   40-char -> 328 entries, including ALL 240 lp_derived LP tokens
#   48-char -> 293 entries, all source=mpt_metadata
# So scoping the automatic rule to 48 excludes every LP token STRUCTURALLY
# rather than by trust tier. `LP: XRP/SOLO` and `LP: RLUSD/mXRP` cannot reach
# the rule at all, which is a stronger guarantee than the tier gate it
# replaces.
MPT_HEX_LEN = 48

# Entries held out of the automatic rule pending an owner decision. Written
# in the DATA, not hardcoded here, so the exemption is auditable and expires
# by deleting three lines. A test pins the count at 3 so a fourth cannot
# quietly appear.
REVIEW_MARKER = "lookalike_review"


def is_mpt(entry: dict) -> bool:
    """True when this entry is an MPT (48-char MPTokenIssuanceID)."""
    if not isinstance(entry, dict):
        return False
    return len(entry.get("currency_hex") or "") == MPT_HEX_LEN


def merge_qualifiers(trust_label: str | None,
                     collision_note: str | None) -> str | None:
    """Combine the two qualifier mechanisms into ONE "not X" phrase.

    The ticker-collision guard (token_naming.resolve_display) and the trust
    tier are deliberately separate: the guard keys on the DECODED currency
    code, while MPT names live only in token_names.json. For an MPT the
    decoded code is hex (`06683FF67A0F…`), never the human ticker, so the
    guard structurally cannot fire for one — verified 2026-10-16, all five
    sampled MPTs returned `collision: None`.

    That means today the two can never both fire, and this function is
    dormant. It exists for the case that WILL eventually arrive: a classic
    (non-MPT) token with ticker `RLUSD` from a non-canonical issuer, tagged
    self_described, would trip both paths and render two "not X" phrases.

    Precedence: the collision guard owns the "not X" slot. It is the older,
    curated mechanism with audited canonical-issuer data and the three-state
    Circle-USDC wording; the trust tier contributes only its
    "self-described" suffix.
    """
    if not collision_note:
        return trust_label
    if not trust_label:
        return collision_note
    if trust_label.startswith("\u26a0 not "):
        # Both assert "not X" — keep the guard's phrase, append the tier once.
        return f"{collision_note} \u00b7 self-described"
    return f"{collision_note} \u00b7 {trust_label}"


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
    if entry.get(REVIEW_MARKER):
        # Explicitly held for an owner decision (XRP NFT / XRP+ / XRP3D:
        # "XRP" there may describe the project rather than imitate the
        # asset, which a substring rule cannot tell apart).
        return None
    # AUTOMATIC for every MPT, regardless of tier — new lookalikes are caught
    # the moment a walker discovers them, with no hand-tagging. Non-MPT
    # entries still require an explicit self_described tier, which preserves
    # the 2026-10-16 PR-1 behaviour.
    if not (is_mpt(entry) or entry.get("tier") == SELF_DESCRIBED):
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

    # AUTOMATIC lookalike warning, checked BEFORE the tier gate so a
    # newly-walked MPT is caught with no hand-tagging. lookalike_asset()
    # already scopes itself: automatic for 48-char MPT keys, tier-gated for
    # classic tokens, and skipped entirely for review-marked entries.
    #
    # This ordering is the fix for a half-wired rule caught by
    # test_label_is_appended: lookalike_asset() was made automatic while
    # label_for() still gated on tier, so an UNTAGGED MPT lookalike produced
    # no label at all. The nine hand-tagged entries masked it.
    asset = lookalike_asset(entry)
    if asset:
        return (f"\u26a0 not {CURATED_ASSETS[asset]['label']} "
                f"\u00b7 self-described")

    if tier == SELF_DESCRIBED:
        return LABEL_SELF_DESCRIBED
    return None                  # untagged, not a lookalike: renders bare


def display_labelled(entry: dict, collision_note: str | None = None) -> str:
    """The ONE string every surface renders. Never a bare self-described name.

    This is the chokepoint. `currency_display` stays pristine because
    _resolve_display_label_to_hex matches on it to resolve shareable
    /token/<ticker>/<issuer> URLs — mutating it would break those.
    """
    name = entry.get("currency_display") or ""
    qualifier = merge_qualifiers(label_for(entry), collision_note)
    return f"{name} \u00b7 {qualifier}" if qualifier else name


def render_decision(entry: dict) -> tuple[bool, str | None]:
    """(should_render, label). Suppressed entries never render."""
    if is_suppressed(entry):
        return False, None
    return True, label_for(entry)
