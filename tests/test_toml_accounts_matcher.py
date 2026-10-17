"""The fail-closed TOML matcher must read the tables the spec actually uses.

THE BUG. `_find_issuer_entry` scanned ONLY [[ISSUERS]] and [[TOKENS]], and
its docstring claimed "the xrp-ledger.toml spec uses [[ISSUERS]] arrays".
The spec has no [[ISSUERS]] table at all: it uses [[ACCOUNTS]] for addresses
and [[CURRENCIES]] to pair a code with an issuer. Measured against five real
TOMLs captured 2026-10-17 (committed under tests/fixtures/toml/), four
publish NEITHER scanned table:

  evecoin.org    ACCOUNTS=2  CURRENCIES=1
  hvi3.com       ACCOUNTS=5
  pay.aiic.nz    ACCOUNTS=2  CURRENCIES=2
  xrpturbo.com   ACCOUNTS=1  CURRENCIES=1
  weildx.com     ACCOUNTS=40 CURRENCIES=40 ISSUERS=40 TOKENS=40

So this fail-closed verifier rejected 4 of 5 legitimate issuers with
`toml_missing_issuer_or_currency_entry`.

AND ITS ONE PASS WAS A FALSE PASS. weildx.com was the only fixture the old
matcher accepted, via the `entry_curr == ""` wildcard -- which treats a
block declaring NO code as a currency match. It matched a generic org block:

  {"address": "rsscn6Gd...", "name": "WeildX", "domain": "weildx.com"}

That block says nothing about wAAPL. verify_submission then read
`entry.get("category")` from it rather than from the token's own
[[CURRENCIES]] block. So the wildcard did not merely over-accept, it
returned the wrong block.

WHY NOBODY WAS TURNED AWAY. registry_form_submissions does not exist in the
database -- it is created lazily by the registry form handler on first
submission (app.py). Zero submissions have ever been made, so the walker
has never had a row to process. The bug was latent, and the first genuine
submitter would have been its first victim.

THE FIX. Scan [[ACCOUNTS]], [[CURRENCIES]], [[ISSUERS]], [[TOKENS]] and
return a match LEVEL rather than a bare block:

  MATCH_CURRENCY -- a block names this issuer AND this code
  MATCH_ISSUER   -- a block names the address; no code declared anywhere

A codeless block can now only ever produce MATCH_ISSUER.

Hermetic: committed fixtures only. No network, no DB.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import tomllib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures" / "toml"
sys.path.insert(0, str(ROOT))


def _load_walker():
    spec = importlib.util.spec_from_file_location(
        "toml_matcher_under_test", ROOT / "two_way_toml_verifier_walker.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def w():
    return _load_walker()


def fixture_toml(name):
    return tomllib.loads((FIXTURES / f"{name}.toml").read_text(encoding="utf-8"))


def old_matcher(toml, currency_hex, issuer):
    """The replaced implementation, kept verbatim so the regression is
    demonstrated rather than asserted."""
    for arr_name in ("ISSUERS", "TOKENS"):
        arr = toml.get(arr_name) or []
        if not isinstance(arr, list):
            continue
        for entry in arr:
            if not isinstance(entry, dict):
                continue
            addr = (entry.get("address") or entry.get("issuer") or "").strip()
            curr = (entry.get("currency") or "").strip().upper()
            if addr == issuer and (curr == currency_hex or curr == ""):
                return entry
    return None


# (fixture, currency, issuer, expected level)
REAL = [
    ("xrpturbo_com", "XRT", "rPvuCxw1m8u2SWTqtRN6ZEcb1xPbU4cPFR", "currency"),
    ("evecoin_org", "EVE", "r3UBRqUVhUdhHSDQ8PysAJo78SyGhR4oVd", "currency"),
    ("pay_aiic_nz", "WST", "rBoZPmku6RKZPvZs1wBAWQgUGFALzw9WST", "currency"),
    ("weildx_com", "774141504C000000000000000000000000000000",
     "rsscn6GdUxqkCbtmcA7uTbYyXWfxxY6Dqo", "currency"),
    ("hvi3_com", "06083C9758DFB58EBE866100FEDB2013D123744243658B7D",
     "r9avT7NURuqC7jbVxUjcrMkHAr5aqmHkSN", "issuer"),
]


# ── fixtures are the real thing, not hand-written ───────────────────
class TestFixturesAreReal:
    @pytest.mark.parametrize("name", [c[0] for c in REAL])
    def test_fixture_parses(self, name):
        assert isinstance(fixture_toml(name), dict)

    def test_four_of_five_lack_both_old_tables(self):
        """The bug, stated as data."""
        without = [n for n, *_ in REAL
                   if not (fixture_toml(n).get("ISSUERS")
                           or fixture_toml(n).get("TOKENS"))]
        assert len(without) == 4
        assert "weildx_com" not in without


# ── the regression ──────────────────────────────────────────────────
class TestOldMatcherRejectedRealIssuers:
    @pytest.mark.parametrize("name,cur,iss,_lvl", REAL)
    def test_old_rejected_everything_but_weildx(self, name, cur, iss, _lvl):
        got = old_matcher(fixture_toml(name), cur, iss)
        if name == "weildx_com":
            assert got is not None
        else:
            assert got is None, "old matcher should have rejected this"

    @pytest.mark.parametrize("name,cur,iss,level", REAL)
    def test_new_matcher_classifies_all_five(self, w, name, cur, iss, level):
        block, got = w._find_toml_match(fixture_toml(name), cur, iss)
        assert block is not None
        assert got == level


# ── the false pass ──────────────────────────────────────────────────
class TestWeildxFalsePass:
    """The old matcher's only success returned a block that names no code."""

    ISS = "rsscn6GdUxqkCbtmcA7uTbYyXWfxxY6Dqo"
    HEX = "774141504C000000000000000000000000000000"

    def test_old_match_block_declared_no_code(self, w):
        got = old_matcher(fixture_toml("weildx_com"), self.HEX, self.ISS)
        assert got is not None
        assert w._block_codes(got) == set(), "matched despite naming no code"

    def test_new_match_block_names_the_code(self, w):
        block, level = w._find_toml_match(
            fixture_toml("weildx_com"), self.HEX, self.ISS)
        assert level == w.MATCH_CURRENCY
        assert self.HEX in w._block_codes(block)

    def test_currency_block_outranks_an_earlier_codeless_hit(self, w):
        """ACCOUNTS is scanned FIRST and its hit declares no code, so the
        matcher must keep looking rather than settle for issuer-level."""
        toml = fixture_toml("weildx_com")
        accounts_hit = [b for b in toml["ACCOUNTS"]
                        if b.get("address") == self.ISS][0]
        assert w._block_codes(accounts_hit) == set()
        _block, level = w._find_toml_match(toml, self.HEX, self.ISS)
        assert level == w.MATCH_CURRENCY


# ── the wildcard is gone ────────────────────────────────────────────
class TestNoEmptyCurrencyWildcard:
    def test_codeless_block_is_issuer_level_only(self, w):
        toml = {"ACCOUNTS": [{"address": "rX", "desc": "no code here"}]}
        block, level = w._find_toml_match(toml, "ABC", "rX")
        assert block is not None
        assert level == w.MATCH_ISSUER

    def test_matching_code_is_currency_level(self, w):
        toml = {"ACCOUNTS": [{"address": "rX", "code": "ABC"}]}
        assert w._find_toml_match(toml, "ABC", "rX")[1] == w.MATCH_CURRENCY

    def test_a_different_code_does_not_become_a_currency_match(self, w):
        """Old behaviour would fall through to the wildcard on the next
        block; a declared-but-different code must stay issuer-level."""
        toml = {"CURRENCIES": [{"issuer": "rX", "code": "ZZZ"}]}
        assert w._find_toml_match(toml, "ABC", "rX")[1] == w.MATCH_ISSUER

    def test_unknown_address_matches_nothing(self, w):
        toml = {"ACCOUNTS": [{"address": "rOTHER", "code": "ABC"}]}
        assert w._find_toml_match(toml, "ABC", "rX") == (None, None)


# ── code spelling ───────────────────────────────────────────────────
class TestCodeCandidates:
    XAGA_HEX = "5841476100000000000000000000000000000000"

    def test_hex_and_ascii_are_equivalent(self, w):
        """XAGa's real block declares code=<hex> plus ticker=XAGa, so either
        spelling of the query must match."""
        toml = {"CURRENCIES": [{"code": self.XAGA_HEX,
                                "ticker": "XAGa", "issuer": "rQ"}]}
        assert w._find_toml_match(toml, self.XAGA_HEX, "rQ")[1] == w.MATCH_CURRENCY
        assert w._find_toml_match(toml, "XAGa", "rQ")[1] == w.MATCH_CURRENCY

    def test_hex_decodes_to_ascii_candidate(self, w):
        assert "XAGA" in w._code_candidates(self.XAGA_HEX)

    def test_non_ascii_hex_does_not_explode(self, w):
        assert w._code_candidates("FF" * 20)

    def test_short_code_has_no_decoded_form(self, w):
        assert w._code_candidates("XRT") == {"XRT"}

    def test_ticker_counts_as_a_code(self, w):
        assert w._block_codes({"ticker": "wAAPL"}) == {"WAAPL"}


# ── defensive inputs ────────────────────────────────────────────────
class TestMalformedInput:
    def test_empty_issuer_never_matches(self, w):
        toml = {"ACCOUNTS": [{"address": "rX", "code": "ABC"}]}
        assert w._find_toml_match(toml, "ABC", "") == (None, None)

    def test_non_dict_toml(self, w):
        assert w._find_toml_match(None, "ABC", "rX") == (None, None)
        assert w._find_toml_match([], "ABC", "rX") == (None, None)

    def test_non_list_array_is_skipped(self, w):
        assert w._find_toml_match({"ACCOUNTS": "nope"}, "ABC", "rX") == (None, None)

    def test_non_dict_blocks_are_skipped(self, w):
        toml = {"ACCOUNTS": ["string", 42, {"address": "rX"}]}
        assert w._find_toml_match(toml, "ABC", "rX")[1] == w.MATCH_ISSUER

    def test_blank_address_never_matches_blank_issuer(self, w):
        toml = {"ACCOUNTS": [{"address": "   "}]}
        assert w._find_toml_match(toml, "ABC", "") == (None, None)


# ── back-compat ─────────────────────────────────────────────────────
class TestBackCompatShim:
    """scripts/verify_rwa_families.py imports _find_issuer_entry and only
    tests truthiness, so it keeps working and gains ACCOUNTS support."""

    def test_shim_returns_the_block(self, w):
        toml = fixture_toml("xrpturbo_com")
        got = w._find_issuer_entry(toml, "XRT",
                                   "rPvuCxw1m8u2SWTqtRN6ZEcb1xPbU4cPFR")
        assert isinstance(got, dict)

    def test_shim_returns_none_when_unmatched(self, w):
        assert w._find_issuer_entry(fixture_toml("xrpturbo_com"),
                                    "XRT", "rNOBODY") is None

    def test_shim_now_accepts_what_it_used_to_reject(self, w):
        """The whole point: an ACCOUNTS-only TOML."""
        toml = fixture_toml("hvi3_com")
        iss = "r9avT7NURuqC7jbVxUjcrMkHAr5aqmHkSN"
        assert old_matcher(toml, "ANY", iss) is None
        assert w._find_issuer_entry(toml, "ANY", iss) is not None


# ── the rejection reason ────────────────────────────────────────────
def test_rejection_reason_names_the_issuer_not_the_currency(w):
    """`toml_missing_issuer_or_currency_entry` conflated two outcomes. The
    gate is now specifically "does this domain vouch for the issuer", since
    issuer-level is acceptable and lands as self-described either way."""
    src = (ROOT / "two_way_toml_verifier_walker.py").read_text(encoding="utf-8")
    assert '"toml_does_not_name_issuer"' in src
    # The old reason survives in prose (the docstring explains the bug it
    # named), so assert it is gone as a STRING LITERAL -- i.e. no longer
    # returned -- rather than absent from the file.
    assert '"toml_missing_issuer_or_currency_entry"' not in src


def test_spec_tables_are_scanned_first(w):
    """ACCOUNTS and CURRENCIES are the real spec tables; ISSUERS/TOKENS are
    registry extensions and must not take priority."""
    assert w._SCANNED_ARRAYS[:2] == ("ACCOUNTS", "CURRENCIES")
    for table in ("ACCOUNTS", "CURRENCIES", "ISSUERS", "TOKENS"):
        assert table in w._SCANNED_ARRAYS
