"""The 2026-11-12 curation pass: BOTC + DATEC published, SCPO "12222" held.

Three entries were suppressed `pending_review` on 2026-11-06 -- a reversible
hold meaning "nobody has assessed this yet", resting on no evidence against
them. This pass assessed all three from our own node plus the cited TOML and
the owner decided each one.

WHAT THE LEDGER SAID (read 2026-11-12 from our own rippled via ledger_entry;
`account_tx` was unavailable because the node's history began at 108218363
after the 2026-11-11 reboot, so funding and offer counts are NOT asserted
here -- absent evidence is recorded as absent, never inferred):

  entry                      Max          Outstanding  flags
  -------------------------  -----------  -----------  ------------------------
  BOTC    060B0AE9  rE2WJ..  40bn          2.00bn      CanEscrow+CanTransfer
  DATEC   060B0AEA  rE2WJ..  100bn         2.00bn      +CanLock+CanTrade
  SCPO    066E0A9F  rJgBH..  1             0           CanEscrow+CanClawback

BOTC + DATEC -> PUBLISHED at `domain_listed` (one-way).
`https://www.xrptokenizer.app/.well-known/xrp-ledger.toml` returns 200 and
names the issuer address in [[ISSUERS]] *and* both exact `mpt_issuance_id`s
in [[TOKENS]]. That is first-party evidence the address is theirs -- but it
is the only leg. Neither AccountRoot carries a Domain field, so the ledger
never points back and ONLY the publisher asserts the link.

They shipped as `domain_fallback` and were re-tiered the same day, before
the render PR merged, when the owner read the proposed label and said it
overstated a one-way claim. He was right, and only half-right: HVI3ESC,
the other entry on that tier, turned out to have an on-ledger Domain of
"hvi3.com" matching its cited host -- genuinely two-way. So the fix was to
SPLIT the tier rather than re-word it, and `domain_fallback` now means
"both legs, currency level missing" while `domain_listed` means "one leg".

ISSUER LEVEL IS THE CEILING, NOT A HOLD. An MPT can never reach the `toml`
tier: a [[CURRENCIES]] block cannot describe one (the spec predates MPTs),
so a naming TOML proves *who issued it*, never that the ticker is right.
`domain_fallback` therefore renders the CAVEAT "issuer verified via
www.xrptokenizer.app" -- not a badge, and no green check anywhere. The
owner's decision was explicit on that point: "issuer-level, no checkmark".

Two things deliberately NOT claimed:
  * Neither AccountRoot carries a `Domain` field, so the reverse leg of the
    two-way check is absent. The forward leg alone is what earns issuer
    level; this is why these are not `toml` and never will be.
  * The cited host is the one that reciprocates. The apex redirects to
    `www.`, so `www.xrptokenizer.app` is what goes in `verified_via`.

SCPO "12222" -> STAYS SUPPRESSED, reason restated `issuer_cluster_test`.
`pending_review` was wrong to keep: this entry has now been assessed and the
evidence is against it. But `self_declared_test` would also be wrong -- the
name predicate does NOT fire, because the entry's own fields say "12222" and
"SC.PO" and contain the word "test" nowhere. Borrowing a predicate the entry
fails would hide a judgement call behind a substring match that was never
satisfied. The evidence class that actually applies is the issuer cluster:
MaximumAmount 1 with OutstandingAmount 0 (never reached a holder), a
placeholder `https://example.com/scpo.png` icon, an `ext` buyer field equal
to the issuer itself, a "0 Days" payment term, and a 7th SC.PO issuer
address whose siblings are already suppressed on the same grounds.

THE SIBLING `060B0AE4` -> SUPPRESSED `display_collision_superseded`.
Publishing 060B0AE9 turned the repo's own renderable-collision guard red
(test_enrich_currency_code.py::test_repair_introduced_no_new_display_
collisions flagged ('botc', rE2WJ..)), because a second entry from the SAME
issuer also displays as "BOTC". That red was the correct answer, not an
assertion to widen: before this pass only the sibling rendered, so there
was no collision to see. 060B0AE4 carries byte-identical CiteFleet
metadata, the same self-declared 2026-09-30 date, the same 40bn cap,
OutstandingAmount 0, CanTransfer only, and is absent from the operator's
own TOML -- which names 060B0AE9 instead. A superseded draft. The owner
decided it in this PR after seeing the evidence; the guard is left
untouched and now passes on the data.

Hermetic: reads the committed token_names.json. No network, no DB, no node.
"""

import json
import os

import pytest

import token_label as tl

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NAMES_JSON = os.path.join(HERE, "token_names.json")

CITE = "https://www.xrptokenizer.app/.well-known/xrp-ledger.toml"
CITED_HOST = "www.xrptokenizer.app"

BOTC_PUBLISHED = (
    "060B0AE99FE7F2B50E46A1E9127A580EB575FA6850C683F7"
    ":rE2WJYnYCbBf75ZA2U8QtnGug6tWrfKLcV"
)
DATEC_PUBLISHED = (
    "060B0AEA9FE7F2B50E46A1E9127A580EB575FA6850C683F7"
    ":rE2WJYnYCbBf75ZA2U8QtnGug6tWrfKLcV"
)
BOTC_SIBLING = (
    "060B0AE49FE7F2B50E46A1E9127A580EB575FA6850C683F7"
    ":rE2WJYnYCbBf75ZA2U8QtnGug6tWrfKLcV"
)
SCPO_HELD = (
    "066E0A9FC1E2F226F24CF1CBBB4FE7F562581EDBC9968E27"
    ":rJgBHWnRLWqaJy17jmqv5duZpxN8cgF59Q"
)

SCPO_REASON_PREFIX = "issuer_cluster_test:"
PUBLISHED = (BOTC_PUBLISHED, DATEC_PUBLISHED)


@pytest.fixture(scope="module")
def raw():
    with open(NAMES_JSON, encoding="utf-8") as f:
        return json.load(f)


class TestPublishedPair:
    """BOTC + DATEC: issuer-level publication, caveat wording, no badge."""

    @pytest.mark.parametrize("key", PUBLISHED)
    def test_entry_exists(self, raw, key):
        assert key in raw, f"curated entry vanished: {key}"

    @pytest.mark.parametrize("key", PUBLISHED)
    def test_no_longer_suppressed(self, raw, key):
        """The owner published these; the pending_review hold is gone."""
        assert not raw[key].get("suppressed")
        assert not tl.is_suppressed(raw[key])

    @pytest.mark.parametrize("key", PUBLISHED)
    def test_tier_is_domain_listed_one_way(self, raw, key):
        """ONE-WAY, and the tier has to say so. Published 2026-11-12 as
        `domain_fallback`; the owner caught before merge that this borrowed
        the word "verified" from a two-way claim. Neither AccountRoot here
        carries a Domain field (read from our own node), so the ledger
        never confirms the TOML -- only the publisher asserts the link.
        `domain_fallback` is now reserved for entries whose on-ledger
        Domain points back, like HVI3ESC."""
        assert raw[key].get("tier") == tl.DOMAIN_LISTED
        assert raw[key].get("tier") != tl.DOMAIN_FALLBACK

    @pytest.mark.parametrize("key", PUBLISHED)
    def test_citation_is_the_reciprocating_host(self, raw, key):
        """The apex redirects to www., so www. is what reciprocates."""
        assert raw[key].get("verified_via") == CITE
        assert tl.cited_domain(raw[key]) == CITED_HOST

    @pytest.mark.parametrize("key", PUBLISHED)
    def test_label_is_the_one_way_caveat_verbatim(self, raw, key):
        """Owner-approved wording. Names which half is missing rather than
        implying the claim is false -- and never says "verified"."""
        label = tl.label_for(raw[key])
        assert label == (
            "listed by www.xrptokenizer.app \u00b7 not confirmed on-ledger"
        )
        assert "verified" not in label

    @pytest.mark.parametrize("key", PUBLISHED)
    def test_never_reaches_the_toml_tier(self, raw, key):
        """A [[CURRENCIES]] block cannot describe an MPT, so currency-level
        evidence is unobtainable for these two by construction. If a later
        pass promotes one to `toml` it would render BARE -- dropping the
        caveat and implying we checked the ticker. That must fail here."""
        assert tl.is_mpt(raw[key]), "precondition: these are MPTs"
        assert raw[key].get("tier") != "toml"

    @pytest.mark.parametrize("key", PUBLISHED)
    def test_label_still_discloses_something_unproven(self, raw, key):
        """The whole point of publishing at issuer level: a reader must not
        see a clean name. `domain_fallback` must keep rendering a qualifier."""
        label = tl.label_for(raw[key])
        assert label is not None
        assert "None" not in label


class TestScpoHeld:
    """SCPO "12222": assessed, evidence against it, reason names the class."""

    def test_still_suppressed(self, raw):
        assert tl.is_suppressed(raw[SCPO_HELD])

    def test_reason_is_issuer_cluster_not_pending_review(self, raw):
        reason = raw[SCPO_HELD].get("suppressed", "")
        assert reason.startswith(SCPO_REASON_PREFIX), reason
        assert "pending_review" not in reason

    def test_reason_string_is_used_exactly_once(self, raw):
        """A distinct reason must stay distinct, or it silently becomes a
        bucket and the judgement behind it is no longer reviewable."""
        reason = raw[SCPO_HELD]["suppressed"]
        n = sum(1 for v in raw.values()
                if isinstance(v, dict) and v.get("suppressed") == reason)
        assert n == 1, f"reason reused {n}x: {reason}"

    def test_entry_itself_never_says_test(self, raw):
        """Why the reason is issuer_cluster_test and NOT self_declared_test.
        If a later walk renames this entry to include "test", this test
        fails and the reason gets re-chosen rather than quietly becoming
        wrong -- the name predicate would then genuinely apply."""
        e = raw[SCPO_HELD]
        for field in ("name", "desc", "issuer_name", "currency_display"):
            assert "test" not in str(e.get(field, "")).lower(), field

    def test_not_published_by_accident(self, raw):
        """Suppressed entries carry no tier-driven caveat into the render."""
        assert raw[SCPO_HELD].get("tier") not in (
            tl.DOMAIN_FALLBACK, tl.DOMAIN_LISTED)
        assert not raw[SCPO_HELD].get("verified_via")


class TestScope:
    """What this pass did and did not touch."""

    def test_the_three_pending_reviews_are_resolved(self, raw):
        """All three entries held on 2026-11-06 have now been decided."""
        for key in (*PUBLISHED, SCPO_HELD):
            assert "pending_review" not in str(raw[key].get("suppressed", ""))

    def test_botc_sibling_is_suppressed_as_superseded(self, raw):
        """060B0AE4 is the SECOND "BOTC" from the same issuer. It is held
        out of the render as superseded -- not published, not cited."""
        e = raw[BOTC_SIBLING]
        assert e.get("currency_display") == "BOTC"
        assert e.get("issuer") == raw[BOTC_PUBLISHED]["issuer"]
        assert tl.is_suppressed(e)
        assert e["suppressed"].startswith("display_collision_superseded:")
        assert e.get("tier") is None, "a superseded draft earns no tier"
        assert not e.get("verified_via"), "the TOML does not name this one"

    def test_exactly_one_botc_renders(self, raw):
        """The point of the suppression: a reader sees ONE "BOTC", and it is
        the issuance the operator's own TOML names. If a later pass
        unsuppresses the sibling, this fails before the render does."""
        rendering = {k for k, v in raw.items()
                     if isinstance(v, dict)
                     and v.get("currency_display") == "BOTC"
                     and not tl.is_suppressed(v)
                     and not v.get(tl.REVIEW_MARKER)}
        assert rendering == {BOTC_PUBLISHED}, sorted(rendering)

    def test_superseded_reason_is_used_exactly_once(self, raw):
        reason = raw[BOTC_SIBLING]["suppressed"]
        n = sum(1 for v in raw.values()
                if isinstance(v, dict) and v.get("suppressed") == reason)
        assert n == 1, f"reason reused {n}x"

    def test_only_two_entries_cite_this_toml(self, raw):
        """Blast radius: the operator's TOML also lists a third issuance
        (060B0AE5 "X40AM Utility Token") that we do not carry under this
        citation. Exactly the two approved entries may cite it."""
        citing = {k for k, v in raw.items()
                  if isinstance(v, dict) and v.get("verified_via") == CITE}
        assert citing == set(PUBLISHED), f"citation spread: {sorted(citing)}"
