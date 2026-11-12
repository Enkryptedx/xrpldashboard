"""The trust caveat on /mpt/issuer/<addr> (2026-11-12).

THE GAP THIS CLOSES. On 2026-11-12 BOTC (060B0AE9) and DATEC (060B0AEA)
were published at `domain_fallback`, citing the operator's own
www.xrptokenizer.app TOML. The caveat shipped and rendered nowhere: both
issuers have no `token_category_current` row, so `/tokens` -- which gates
on that view -- never lists them, and the only surface that does show them,
`/mpt/issuer/<addr>`, built its rows from the MPT ledger snapshot, which
carries no tier at all. A fully-examined issuance and an unexamined one
were pixel-identical.

This is the degenerate case the token-trust skill warns about from the
other direction: there, a label was computed with no print site; here, a
tier existed with no surface that read it. Both are answered by asking
what the chokepoint emits, so this suite checks three things and not one:

  1. the pure composition (the exact reader-visible sentence),
  2. that the chokepoint attaches it (app.py read as text), and
  3. that the template actually PRINTS it (a non-zero grep).

Drop any one and the feature can look finished while rendering nothing.

WHY A CAVEAT AND NOT A BADGE. Neither tier here means the ticker was
checked -- a [[CURRENCIES]] block cannot describe an MPT, so issuer level
is the ceiling for one. The wording is shared with /tokens via
token_label.label_for() so there is exactly one string to maintain, and
the span is deliberately muted with no tick, no colour and no border. An
MPT surface showing a green check would vouch for a self-declared name on
the surface where impersonation risk is highest.

TWO TIERS, TWO SENTENCES (split 2026-11-12, owner-approved). The first cut
of this PR rendered "issuer verified via <host>" for BOTC and DATEC, and
the owner stopped it before merge: that wording overstates a ONE-WAY
claim. Reading both AccountRoots from our own node settled it, and split
his premise in half:

  tier             evidence                                   renders
  ---------------  ----------------------------------------  --------------
  domain_fallback  TOML names the address AND the issuer's    "issuer
                   on-ledger Domain points back at that host  verified via
                   (HVI3ESC: Domain = "hvi3.com")             <host>"
  domain_listed    TOML names the address and the AccountRoot "listed by
                   has NO Domain field at all, so the ledger  <host> ... not
                   never confirms it (BOTC, DATEC)            confirmed
                                                              on-ledger"

So the remedy was to SPLIT the tier, not re-word it -- a blanket relabel
would have understated HVI3ESC, whose reverse leg is genuinely present.
Both cases are asserted below on this surface, because "the label renders"
is not the same claim as "the two tiers are distinguishable".

WHY THE ROW'S OWN NAME. The sentence uses the row's on-chain XLS-89 name
("DateC"), not token_names' `currency_display` ("DATEC"). Rewriting it
would make the registry disagree with what the issuer actually minted.
Only the qualifier is shared.

WHY THE TEMPLATE COMPOSES IT. A first cut also attached a pre-composed
`name_labelled` field built by a `token_label.issuance_sentence()` helper.
The wiring test below caught that `name_labelled` had ZERO print sites --
the template keeps the <a> on the name alone so the caveat is not part of
the link target -- which made the field, and the helper's only caller,
dead code of exactly the kind this suite exists to catch. Both were
deleted rather than wired up. The template is therefore the one
composition site, and the sentence here is reconstructed FROM the
template's own separator so it still cannot drift unnoticed.

SUPPRESSION IS BY CONSTRUCTION. `label_for_issuance` reads the dict
`app._load_token_names_dict()` builds, and that builder already drops
suppressed entries. So the superseded duplicate 060B0AE4 "BOTC" cannot
inherit its published sibling's caveat -- there is no second check to
forget. That is asserted here, because it is the whole reason the
duplicate was suppressed rather than deleted.

Hermetic: pure helpers plus text reads of app.py and the template. No DB,
no network, no node, no snapshot file.
"""

import os
import re

import pytest

import token_label as tl

def _template_separator(html: str) -> str:
    """What a READER sees between the name and the caveat.

    Read from the template rather than hardcoded, so a separator change
    cannot pass silently. Two pieces make it up, and the first is easy to
    miss: the template puts the caveat span on its own source line, and
    HTML collapses that newline-plus-indent to a single space. The span
    itself then opens with the middot. So the rendered separator is
    " <middot> ", not the "<middot> " the span literally contains --
    normalise it the way a browser would rather than asserting the raw
    capture, which is what two red tests here first reported.
    """
    m = re.search(r'<span class="trust-label">(.*?)\{\{ r\.trust_label \}\}',
                  html)
    assert m, "trust-label span shape changed; re-read what separates them"
    inner = m.group(1).strip()
    assert inner == "\u00b7", f"separator is no longer a bare middot: {inner!r}"
    return f" {inner} "


def _rendered_sentence(row_name, label, html):
    """What a reader sees in the name cell, composed the way the template
    composes it: name, separator, caveat -- or just the name."""
    name = (row_name or "").strip() or "(unnamed)"
    return f"{name}{_template_separator(html)}{label}" if label else name


HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_PY = os.path.join(HERE, "app.py")
TEMPLATE = os.path.join(HERE, "templates", "mpt_issuer.html")

ISSUER = "rE2WJYnYCbBf75ZA2U8QtnGug6tWrfKLcV"
BOTC_PUBLISHED = "060B0AE99FE7F2B50E46A1E9127A580EB575FA6850C683F7"
DATEC_PUBLISHED = "060B0AEA9FE7F2B50E46A1E9127A580EB575FA6850C683F7"
BOTC_SUPPRESSED = "060B0AE49FE7F2B50E46A1E9127A580EB575FA6850C683F7"
X40AM_UNTAGGED = "060B0AE59FE7F2B50E46A1E9127A580EB575FA6850C683F7"

# ONE-WAY: the TOML names the address, the AccountRoot has no Domain field.
CAVEAT = "listed by www.xrptokenizer.app \u00b7 not confirmed on-ledger"
CITE = "https://www.xrptokenizer.app/.well-known/xrp-ledger.toml"

# TWO-WAY contrast case: hvi3.com's issuer really does carry
# Domain = "hvi3.com" on-ledger (read from our own node 2026-11-12), so it
# keeps the stronger word. Present here so this surface is proven to
# render the two tiers DIFFERENTLY, not just to render one of them.
HVI3_ISSUER = "r9avT7NURuqC7jbVxUjcrMkHAr5aqmHkSN"
HVI3_HEX = "06083C9758" + "0" * 38
HVI3_CITE = "https://hvi3.com/.well-known/xrp-ledger.toml"
HVI3_CAVEAT = "issuer verified via hvi3.com"


def _published(hexid, display):
    """A one-way entry: cited by its publisher, unconfirmed on-ledger."""
    return {
        "currency_hex": hexid,
        "currency_display": display,
        "issuer": ISSUER,
        "tier": tl.DOMAIN_LISTED,
        "verified_via": CITE,
    }


def _two_way(hexid, display):
    """A two-way entry: publisher names the address AND the AccountRoot's
    Domain field points back at that host."""
    return {
        "currency_hex": hexid,
        "currency_display": display,
        "issuer": HVI3_ISSUER,
        "tier": tl.DOMAIN_FALLBACK,
        "verified_via": HVI3_CITE,
    }


@pytest.fixture
def tokens_by_key():
    """Shaped exactly like app._load_token_names_dict(): keyed on
    (currency_hex, issuer), suppressed entries ALREADY absent, and each
    surviving entry carrying the `_trust_label` the chokepoint computed."""
    out = {}
    for hexid, display in ((BOTC_PUBLISHED, "BOTC"), (DATEC_PUBLISHED, "DATEC")):
        e = _published(hexid, display)
        e["_trust_label"] = tl.label_for(e)
        out[(hexid, ISSUER)] = e
    hv = _two_way(HVI3_HEX, "HVI3ESC")
    hv["_trust_label"] = tl.label_for(hv)
    out[(HVI3_HEX, HVI3_ISSUER)] = hv
    # X40AM: present on the ledger and in no curated entry at all.
    # 060B0AE4: suppressed, so the builder never emitted it. Neither key
    # appears here, and that absence IS the test fixture.
    return out


class TestTheRenderedSentence:
    """The exact string a reader sees in the name cell."""

    def test_one_way_name_carries_the_unconfirmed_caveat(self, tokens_by_key):
        """ONE-WAY case. The publisher's TOML names the address; the
        AccountRoot carries no Domain field, so nothing on the ledger
        confirms it. The sentence must say which half is missing and must
        not contain the word "verified"."""
        html = open(TEMPLATE, encoding="utf-8").read()
        label = tl.label_for_issuance(BOTC_PUBLISHED, ISSUER, tokens_by_key)
        assert _rendered_sentence("BOTC", label, html) == (
            "BOTC \u00b7 listed by www.xrptokenizer.app "
            "\u00b7 not confirmed on-ledger"
        )
        assert "verified" not in label

    def test_two_way_name_keeps_the_verified_wording(self, tokens_by_key):
        """TWO-WAY case. hvi3.com names the address and that issuer's
        on-ledger Domain reads "hvi3.com", so both legs are present and
        only the currency level is missing. This one keeps "verified"."""
        html = open(TEMPLATE, encoding="utf-8").read()
        label = tl.label_for_issuance(HVI3_HEX, HVI3_ISSUER, tokens_by_key)
        assert _rendered_sentence("HVI3ESC", label, html) == (
            "HVI3ESC \u00b7 issuer verified via hvi3.com"
        )
        assert label == HVI3_CAVEAT

    def test_the_two_tiers_never_render_the_same_sentence(self, tokens_by_key):
        """The point of the split, asserted on THIS surface: a one-way and
        a two-way entry must be distinguishable by a reader."""
        one = tl.label_for_issuance(BOTC_PUBLISHED, ISSUER, tokens_by_key)
        two = tl.label_for_issuance(HVI3_HEX, HVI3_ISSUER, tokens_by_key)
        assert one != two
        assert "not confirmed on-ledger" in one
        assert "not confirmed on-ledger" not in two

    def test_datec_keeps_its_own_ledger_name(self, tokens_by_key):
        """"DateC" on-chain, "DATEC" in the registry. The surface showing
        the issuance must say what was minted."""
        html = open(TEMPLATE, encoding="utf-8").read()
        label = tl.label_for_issuance(DATEC_PUBLISHED, ISSUER, tokens_by_key)
        assert _rendered_sentence("DateC", label, html) == (
            "DateC \u00b7 listed by www.xrptokenizer.app "
            "\u00b7 not confirmed on-ledger"
        )

    def test_wording_is_identical_to_the_tokens_surface(self, tokens_by_key):
        """One string, one place. If /tokens' wording changes, this moves
        with it rather than drifting into a second dialect."""
        entry = tokens_by_key[(BOTC_PUBLISHED, ISSUER)]
        assert tl.label_for_issuance(BOTC_PUBLISHED, ISSUER, tokens_by_key) \
            == tl.label_for(entry)
        assert tl.display_labelled(entry).endswith(CAVEAT)

    def test_caveat_names_the_reciprocating_host(self, tokens_by_key):
        """The apex redirects to www., so www. is what reciprocates. A
        caveat citing the tidier-looking apex would be an unreciprocated
        claim."""
        label = tl.label_for_issuance(BOTC_PUBLISHED, ISSUER, tokens_by_key)
        assert "www.xrptokenizer.app" in label
        assert "None" not in label


class TestSilenceWhereItBelongs:
    """Rows that must render exactly as they did before this change."""

    def test_suppressed_duplicate_gets_no_label(self, tokens_by_key):
        """060B0AE4 is the superseded second "BOTC". It must not inherit
        its sibling's caveat -- and it cannot, because the builder never
        emitted it."""
        assert (BOTC_SUPPRESSED, ISSUER) not in tokens_by_key
        assert tl.label_for_issuance(BOTC_SUPPRESSED, ISSUER, tokens_by_key) is None
        html = open(TEMPLATE, encoding="utf-8").read()
        assert _rendered_sentence("BOTC", None, html) == "BOTC"

    def test_untagged_issuance_gets_no_label(self, tokens_by_key):
        """X40AM is on the ledger and in no curated entry. Untagged is not
        a verdict, so it renders bare."""
        assert tl.label_for_issuance(X40AM_UNTAGGED, ISSUER, tokens_by_key) is None
        html = open(TEMPLATE, encoding="utf-8").read()
        assert _rendered_sentence("X40AM Utility Token", None, html) \
            == "X40AM Utility Token"

    def test_unnamed_row_still_renders_something(self):
        """The template's own fallback for a nameless issuance."""
        html = open(TEMPLATE, encoding="utf-8").read()
        assert "r.name or _('(unnamed)')" in html
        assert _rendered_sentence(None, None, html) == "(unnamed)"

    @pytest.mark.parametrize("bad", [
        (None, ISSUER), (BOTC_PUBLISHED, None), ("", ""),
    ])
    def test_missing_identifiers_never_raise(self, tokens_by_key, bad):
        assert tl.label_for_issuance(bad[0], bad[1], tokens_by_key) is None

    def test_empty_registry_never_raises(self):
        for empty in (None, {}):
            assert tl.label_for_issuance(BOTC_PUBLISHED, ISSUER, empty) is None

    def test_wrong_issuer_does_not_match(self, tokens_by_key):
        """The join is on BOTH halves of the key. An issuance id alone must
        not pull a caveat from a different issuer's entry."""
        assert tl.label_for_issuance(
            BOTC_PUBLISHED, "rJgBHWnRLWqaJy17jmqv5duZpxN8cgF59Q", tokens_by_key
        ) is None


class TestItIsActuallyWired:
    """A computed label with no print site is the failure mode this
    guards. Counts are pinned so a later edit that drops the wiring, or
    adds an MPT surface without it, goes red."""

    def test_chokepoint_attaches_the_label(self):
        """_enrich_mpt_rows is the ONE function /mpts, /mpt/<id> and
        /mpt/issuer/<addr> all pass through."""
        src = open(APP_PY, encoding="utf-8").read()
        assert src.count("token_label.label_for_issuance(") == 1
        # the ASSIGNMENT, not every mention -- one attachment site only
        assert src.count('copy["trust_label"] = ') == 1
        # a pre-composed name field had no print site; it must not come back
        assert "name_labelled" not in src, "dead field reintroduced"
        body = src[src.index("def _enrich_mpt_rows("):]
        body = body[:body.index("\n@app.route")]
        assert "label_for_issuance" in body, "label attached outside the chokepoint"
        assert "_load_token_names_dict()" in body

    def test_template_prints_it(self):
        """The dead-flag check: a non-zero grep on the template. Without
        this the whole feature can pass its unit tests and render nothing."""
        html = open(TEMPLATE, encoding="utf-8").read()
        assert html.count("r.trust_label") >= 1, "no print site: dead flag"
        assert "name_labelled" not in html
        assert re.search(r'\{%\s*if r\.trust_label\s*%\}', html), \
            "caveat must be conditional, never printed for an untagged row"

    def test_caveat_is_not_styled_as_a_badge(self):
        """More evidence means fewer words, not a credential. No tick, no
        colour, no border on this span."""
        html = open(TEMPLATE, encoding="utf-8").read()
        block = html[html.index(".trust-label {"):]
        block = block[:block.index("}")]
        assert "var(--muted)" in block
        for forbidden in ("border", "background", "var(--green)", "var(--pink)"):
            assert forbidden not in block, f"badge-like styling: {forbidden}"
        assert "\u2713" not in html.split(".trust-label")[-1][:400]

    def test_link_target_excludes_the_caveat(self):
        """The anchor stays on the name alone; the caveat is not clickable
        and is not part of the /mpt/<id> target."""
        html = open(TEMPLATE, encoding="utf-8").read()
        anchor = re.search(
            r'<a class="name-link" href="/mpt/\{\{ r\.issuance_id \}\}">'
            r'\{\{ r\.name or _\(.\(unnamed\).\) \}\}</a>', html)
        assert anchor, "name-link shape changed; re-check what the link wraps"
        assert "trust_label" not in html[anchor.start():anchor.end()]
