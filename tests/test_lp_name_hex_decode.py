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


# ── the routes ──────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def client():
    import app as app_module
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


@pytest.mark.parametrize("path", ["/tokens", "/pools"])
def test_route_serves_no_raw_hex_lp_name(client, path):
    resp = client.get(path)
    assert resp.status_code == 200, f"{path} -> {resp.status_code}"
    body = resp.get_data(as_text=True)
    bad = [m.group(0) for m in HEX40.finditer(body)
           if "LP:" in body[max(0, m.start() - 60):m.start()]]
    assert bad == [], f"{path} renders raw hex in an LP name: {bad[:3]}"
