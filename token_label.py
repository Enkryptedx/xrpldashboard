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

# One-way citation (2026-11-12). Split out of DOMAIN_FALLBACK because that
# tier was conflating two genuinely different evidence states, and the
# weaker one was borrowing the stronger one's word:
#
#   DOMAIN_FALLBACK  TOML names the issuer address AND the issuer's
#                    on-ledger Domain field points back at that host.
#                    BOTH legs present; only the CURRENCY level is
#                    missing (an MPT has no [[CURRENCIES]] block to
#                    match). "verified" is honest here.
#   DOMAIN_LISTED    TOML names the issuer address and nothing else.
#                    The AccountRoot carries no Domain field at all, so
#                    the ledger never confirms the claim. One party --
#                    the website -- asserts the link unilaterally.
#
# Verified 2026-11-12 from our own node: hvi3.com's issuer
# r9avT7NURuqC7jbVxUjcrMkHAr5aqmHkSN has Domain = "hvi3.com" (two-way),
# while rE2WJYnYCbBf75ZA2U8QtnGug6tWrfKLcV (BOTC, DATEC) has no Domain
# field (one-way). Same tier, opposite evidence.
#
# The reverse leg is an RPC read, so it CANNOT be computed at render time
# (web requests never stall on a node call). The tier therefore records
# the verdict reached at curation time, and label_for() stays pure.
DOMAIN_LISTED = "domain_listed"

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
        # that the ticker is right. "verified" is earned because BOTH legs
        # are present -- the TOML names the address and the AccountRoot's
        # Domain field points back at this host.
        return f"issuer verified via {host}" if host else LABEL_SELF_DESCRIBED

    if tier == DOMAIN_LISTED:
        host = cited_domain(entry)
        # ONE-WAY. The site claims the address; the ledger says nothing.
        # Deliberately avoids the word "verified": only the publisher
        # asserts this, and anyone can publish an xrp-ledger.toml naming
        # any address. "not confirmed on-ledger" states exactly which
        # half is missing rather than implying the claim is false.
        return (f"listed by {host} \u00b7 not confirmed on-ledger"
                if host else LABEL_SELF_DESCRIBED)

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


# ── source labels for UNTIERED entries (2026-11-22) ──────────────────
# An untiered name renders BARE -- which is EXACTLY how the top `toml` tier
# renders, since evidence means fewer words. So a bare name is ambiguous:
# it may be reviewed or merely unexamined. These labels say where the name
# CAME FROM without claiming a verdict on it.
#
# Two variants because the surfaces differ: token.html, wallet and
# /mpt/issuer have room for a sentence; a /tokens row is ~38 chars on a
# phone, so it takes the tag.
#
# Measured 2026-11-22 over 467 labelled names: mpt_metadata 187,
# lp_derived 240, other-url 27, curator_inferred 10, toml-cited 3. The
# `xrpscan` and `none` buckets are deliberately absent -- today's tiering
# emptied both, so wording for them would ship as dead copy.
SOURCE_LABELS = {
    "mpt_metadata":     ("name set by issuer \u00b7 not verified", "unverified"),
    "lp_derived":       ("name from pool data",                    "pool data"),
    "other-url":        ("informal source \u00b7 not verified",    "informal source"),
    "curator_inferred": ("name inferred from activity",            "inferred"),
    "toml-cited":       ("source cited \u00b7 could not confirm",  "unconfirmed"),
}


IMPERSONATION_REASON = "impersonation_risk_pending_review"
WITHHELD_LABEL = "name withheld \u00b7 under review"
LABEL_UNVERIFIED = "unverified"


def is_withheld(entry: dict) -> bool:
    """True when an entry is suppressed for IMPERSONATION RISK.

    These keep their row and its link but must never publish the name:
    the name itself is the harm. Distinct from every other suppression
    reason, which only means "not reviewed" and takes `unverified`.
    """
    if not isinstance(entry, dict):
        return False
    return IMPERSONATION_REASON in str(entry.get("suppressed") or "")


def suppressed_display(entry: dict, name: str | None = None) -> str:
    """The name slot for a SUPPRESSED entry, on wallet and /mpt/issuer.

    Suppression removes an entry from _load_token_names_dict(), which is
    what /tokens and labels.json read -- but wallet builds its own lookup
    from the RAW file and /mpt/issuer builds rows from the ledger
    snapshot, so 182 of 226 suppressed names were still rendering BARE
    across 384 rows on 2026-11-22: name published, every caveat stripped.
    """
    if is_withheld(entry):
        return WITHHELD_LABEL
    base = name or (entry.get("currency_display") if isinstance(entry, dict) else None) or ""
    return f"{base} \u00b7 {LABEL_UNVERIFIED}" if base else LABEL_UNVERIFIED


def source_bucket(entry: dict) -> str | None:
    """Which source label an entry earns, or None.

    Returns None for a TIERED entry: its tier already speaks for it (or
    deliberately says nothing, as `toml` does), and stacking a source label
    on top would double-label the row. That single rule is the guard --
    precedence lives in display_labelled, not in each template.

    Also None for a suppressed entry (never rendered) and for a sentinel
    with an empty issuer (cannot form a row at all).
    """
    if not isinstance(entry, dict):
        return None
    if entry.get("tier"):
        return None
    if is_suppressed(entry):
        return None
    # REVIEW-MARKED entries render BARE, exactly as they do for the
    # lookalike check. Caught by TestReviewMarker: a source label is still
    # a published claim about provenance, and an entry parked for review
    # has not earned one. Same exclusion is_suppressed() gets.
    if entry.get(REVIEW_MARKER):
        return None
    if not (entry.get("issuer") or ""):
        return None
    src = entry.get("source")
    via = entry.get("verified_via") or ""
    if src == "lp_derived":
        return "lp_derived"
    if "xrp-ledger.toml" in via:
        return "toml-cited"
    if via.startswith("http"):
        return "other-url"
    if src == "mpt_metadata":
        return "mpt_metadata"
    if via:
        return "curator_inferred"
    return None


def source_label(entry: dict, short: bool = False) -> str | None:
    """The full sentence, or the short row tag when short=True."""
    bucket = source_bucket(entry)
    if not bucket:
        return None
    full, tag = SOURCE_LABELS[bucket]
    return tag if short else full


def display_labelled(entry: dict, collision_note: str | None = None,
                     short: bool = False) -> str:
    """The ONE string every surface renders. Never a bare self-described name.

    This is the chokepoint. `currency_display` stays pristine because
    _resolve_display_label_to_hex matches on it to resolve shareable
    /token/<ticker>/<issuer> URLs — mutating it would break those.
    """
    name = entry.get("currency_display") or ""
    # Tier wording WINS. A source label only ever fills the gap where a
    # tier says nothing, so the two can never stack on one row.
    trust = label_for(entry) or source_label(entry, short=short)
    qualifier = merge_qualifiers(trust, collision_note)
    return f"{name} \u00b7 {qualifier}" if qualifier else name


def render_decision(entry: dict) -> tuple[bool, str | None]:
    """(should_render, label). Suppressed entries never render."""
    if is_suppressed(entry):
        return False, None
    return True, label_for(entry)

def label_for_issuance(issuance_id: str, issuer: str,
                       tokens_by_key: dict | None) -> str | None:
    """The caveat for ONE MPT issuance row on /mpt/issuer/<addr>, or None.

    `tokens_by_key` is {(currency_hex, issuer): entry} exactly as
    app._load_token_names_dict() builds it. That builder already drops
    suppressed entries via render_decision(), so a suppressed issuance
    gets no label here BY CONSTRUCTION rather than by a second check --
    which is what keeps a superseded duplicate (060B0AE4 "BOTC") from
    picking up its published sibling's caveat.

    An MPT issuance_id IS the 48-hex currency_hex of its token_names
    entry, so the join needs no normalisation.

    Returns the same string /tokens appends, so one wording is maintained
    in one place: `label_for()`. Untagged issuances return None and the
    row renders exactly as it does today.
    """
    if not issuance_id or not issuer or not tokens_by_key:
        return None
    entry = tokens_by_key.get((issuance_id, issuer))
    if not isinstance(entry, dict):
        return None
    # Prefer the label the chokepoint already computed; fall back to
    # recomputing so this helper is usable on a plain token_names entry.
    label = (entry["_trust_label"] if "_trust_label" in entry
             else label_for(entry))
    # An UNTIERED issuance used to return None here, so the 187
    # mpt_metadata names rendered bare on /mpt/* -- identical to a
    # reviewed one. Fall back to the source label, SHORT because these are
    # list rows. `or` keeps the precedence: a tier's own wording always
    # wins, so the two can never stack on one row.
    return label or source_label(entry, short=True)
