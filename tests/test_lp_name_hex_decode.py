"""LP names must never ship a raw 40-char hex currency code.

THE BUG. `_shippable_side` built each LP side from the component entry's own
`currency_display`, verbatim:

    disp = (existing.get("currency_display") or "").strip()
    return (bool(disp), disp or _decode_currency_label(cur))

The `disp or _decode_currency_label(cur)` tail was DEAD CODE: when `disp` was
empty the tuple's first element was `False`, so `enrich_lp_tokens` hit
`if not (ok1 and ok2): skipped_unverified += 1; continue` and threw the
decoded value away. `_decode_currency_label` could never reach an LP name.

So a component whose own display was still a raw 40-char hex put that hex
straight into the pool label:

    LP: 4242524C00000000000000000000000000000000/RLUSD     (50 chars)

against a ~38-char phone row. Decoded, that is `LP: BBRL/RLUSD` (14).

SCOPE. The decode happens for the LP LABEL ONLY. No component's own
`currency_display` is written to, so `_resolve_display_label_to_hex` --
which resolves /token/<ticker>/<issuer> by matching that field -- is
unaffected. The both-sides shippability gate is unchanged: it still keys on
`bool(disp)`, so a component with no display of its own stays un-shippable
and this cannot widen which pools get labelled.
"""

import json
import os
import re

import pytest

import enrich_token_names as E

HEX40 = re.compile(r'(?<![0-9A-Fa-f])[0-9A-Fa-f]{40}(?![0-9A-Fa-f])')
NAMES_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                          "token_names.json")


@pytest.fixture(scope="module")
def names():
    with open(NAMES_PATH, encoding="utf-8") as fh:
        return json.load(fh)


# ── the predicate ───────────────────────────────────────────────────
def test_is_raw_hex40_is_narrow():
    assert E._is_raw_hex40("4242524C00000000000000000000000000000000")
    assert not E._is_raw_hex40("BBRL")
    assert not E._is_raw_hex40("XRP")
    assert not E._is_raw_hex40("USD.Bitstamp")
    assert not E._is_raw_hex40("")
    # 39 and 41 chars, and a non-hex character at full length
    assert not E._is_raw_hex40("4" * 39)
    assert not E._is_raw_hex40("4" * 41)
    assert not E._is_raw_hex40("Z" + "4" * 39)


# ── the producer ────────────────────────────────────────────────────
def test_shippable_side_decodes_a_raw_hex_component_display():
    cur = "4242524C00000000000000000000000000000000"
    iss = "rBBRLissuer"
    entry = {"currency_display": cur, "verified_via": "https://example.test/x"}
    pool = {cur + ":" + iss: entry}
    ok, label = E._shippable_side({"currency": cur, "issuer": iss}, pool)
    assert ok is True
    assert label == "BBRL"
    # the component's own display is NOT rewritten
    assert entry["currency_display"] == cur


def test_shippable_side_leaves_a_good_display_alone():
    pool = {"USD:rG": {"currency_display": "USD.Bitstamp",
                       "verified_via": "https://example.test/x"}}
    ok, label = E._shippable_side({"currency": "USD", "issuer": "rG"}, pool)
    assert (ok, label) == (True, "USD.Bitstamp")


def test_gate_is_not_loosened():
    """A component with no display of its own stays un-shippable, and one
    pending curation stays un-shippable. If either flipped, this change
    would be labelling pools it has no right to."""
    pool = {"AAA:rX": {"currency_display": "",
                       "verified_via": "https://example.test/x"},
            "BBB:rY": {"currency_display": "BBB",
                       "verified_via": "TODO_curation_pass"}}
    assert E._shippable_side({"currency": "AAA", "issuer": "rX"}, pool)[0] is False
    assert E._shippable_side({"currency": "BBB", "issuer": "rY"}, pool)[0] is False
    # an entry absent from token_names.json is never shippable
    assert E._shippable_side({"currency": "ZZZ", "issuer": "rZ"}, {})[0] is False
    # XRP always is
    assert E._shippable_side({"currency": "XRP", "issuer": ""}, {}) == (True, "XRP")


# ── the stored data ─────────────────────────────────────────────────
def test_no_lp_name_contains_raw_hex(names):
    offenders = [(k, v.get("currency_display")) for k, v in names.items()
                 if isinstance(v, dict) and v.get("source") == "lp_derived"
                 and HEX40.search(v.get("currency_display") or "")]
    assert offenders == [], offenders


def test_lp_names_fit_a_phone_row(names):
    """360px is ~38 characters. The worst LP name was 50 before the decode."""
    lp = [v.get("currency_display") or "" for v in names.values()
          if isinstance(v, dict) and v.get("source") == "lp_derived"]
    assert lp, "no lp_derived entries found"
    longest = max(lp, key=len)
    assert len(longest) <= 38, f"{len(longest)}c: {longest}"


def test_non_lp_displays_carry_no_raw_hex(names):
    """The follow-up guard. Components are the upstream source of LP sides,
    so a raw hex reappearing here is how the LP names would regress."""
    offenders = [(k, v.get("currency_display")) for k, v in names.items()
                 if isinstance(v, dict) and v.get("source") != "lp_derived"
                 and E._is_raw_hex40((v.get("currency_display") or "").strip())]
    assert offenders == [], offenders


# ── the surfaces ────────────────────────────────────────────────────
# THE TESTS THIS REPLACES WERE VACUOUS, in two independent ways.
#
#   @pytest.mark.parametrize("path", ["/tokens", "/pools"])
#   bad = [m for m in HEX40.finditer(body)
#          if "LP:" in body[m.start()-60:m.start()]]
#   assert bad == []
#
# 1. WRONG SURFACES. Measured live at x-commit a68021978dc3, neither
#    /tokens nor /pools renders an lp_derived display at all: /tokens
#    contains the substring "LP:" exactly ONCE in the whole document and
#    /pools, /pools.json and /tokens/labels.json contain it zero times.
#    The two surfaces that DO render it are token detail and wallet:
#
#      /token/033CD374C77160C0239D3945A465915B84793008/rpSVcd53bQUWy7vUneK4dZrEAjxBkxjiBZ
#        <title>LP: BBRL/RLUSD · token · xrpldashboard</title>
#      /wallet/rB3bK9PnHueTTabMVHedKuBzG5RSFYAUEH
#        <span class="issuance-symbol">LP: USD.Bitstamp/RLUSD</span>
#
# 2. ABSENCE PREDICATE. Even on the right surface the assertion could
#    never fail: it only looked at hex runs that had "LP:" within the
#    preceding 60 chars. With the name absent there is nothing to find, so
#    an empty result reads as a pass. The 40-char hex runs that DO appear
#    on /tokens are 64-char SHA-256 emblem filenames under /static/ and
#    the `token_icon_lookup` JSON keys -- icon-cache keys, not displays.
#
# So the assertions below are POSITIVE: the decoded name must be PRESENT.
# A presence assertion fails when rendering is absent; an absence one
# passes. Proven to fail on pre-fix fbf041a, where the stored display is
# still the raw hex.

LP_DECODED_SAMPLES = (
    "LP: BBRL/RLUSD",
    "LP: BBRL/BITx",
    "LP: XRP/aixrp",
    "LP: BITx/aixrp",
    "LP: XRP/USDB",
    "LP: USD.Bitstamp/RLUSD",
)


def test_decoded_lp_names_are_present_in_the_data(names):
    """Deterministic presence check on the string every surface renders.

    This is the one that fails on the pre-fix commit: before the decode,
    these entries held e.g.
    'LP: 4242524C00000000000000000000000000000000/RLUSD'.
    """
    displays = {v.get("currency_display") for v in names.values()
                if isinstance(v, dict) and v.get("source") == "lp_derived"}
    missing = [s for s in LP_DECODED_SAMPLES if s not in displays]
    assert missing == [], f"decoded LP names absent from the data: {missing}"


def test_rendered_string_for_each_sample_is_the_decoded_name(names):
    """display_labelled() is the chokepoint both surfaces render through,
    so assert on its output rather than on a page body."""
    import token_label as TL
    by_display = {v.get("currency_display"): v for v in names.values()
                  if isinstance(v, dict) and v.get("source") == "lp_derived"}
    for sample in LP_DECODED_SAMPLES:
        entry = by_display.get(sample)
        assert entry is not None, sample
        rendered = TL.display_labelled(entry)
        assert sample in rendered, (sample, rendered)
        assert not HEX40.search(rendered), (sample, rendered)


@pytest.mark.skipif(os.environ.get("LP_LIVE_ROUTE_TEST") != "1",
                    reason="opt-in: token detail renders live amm_info, so "
                           "it reaches a public cluster when XRPL_NODE is "
                           "unset. Run with LP_LIVE_ROUTE_TEST=1.")
def test_token_detail_route_renders_the_decoded_name():
    """Route-level presence on the surface that actually renders it.

    Deliberately opt-in rather than silently skipped-by-default elsewhere:
    a test that no-ops in CI is how the previous version stayed green while
    asserting nothing. The wallet surface is NOT covered here -- it renders
    0 occurrences under the local test client because its issuance list
    needs data the local process does not have, so a local presence
    assertion there would fail for environmental reasons. Wallet coverage
    belongs in the live canary, not this file.
    """
    import app as app_module
    app_module.app.config["TESTING"] = True
    client = app_module.app.test_client()
    cur, iss = ("033CD374C77160C0239D3945A465915B84793008",
                "rpSVcd53bQUWy7vUneK4dZrEAjxBkxjiBZ")
    resp = client.get(f"/token/{cur}/{iss}")
    assert resp.status_code == 200, resp.status_code
    body = resp.get_data(as_text=True)
    assert "LP: BBRL/RLUSD" in body, "token detail did not render the decoded name"
