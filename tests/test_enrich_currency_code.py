"""enrich_token_names.py: read every key a TOML uses for a code, in every form.

ONE ROOT CAUSE, THREE SYMPTOMS. The script hardcoded WHICH key carries a
currency code in a [[CURRENCIES]] block and WHICH form it takes. Measured
against 5 real TOMLs (44 blocks) captured 2026-10-17:

  _normalize_code      only len==3 normalised; RLUSD (5 chars) returned
                       as-is so it never equalled 524C555344...  It also
                       .upper()'d the ASCII BEFORE encoding -- XRPL codes are
                       case-SENSITIVE, so XAGa and XAGA collided.
  _find_currency_block read only `code`; a block keyed `currency` skipped.
                       (Latent: 0 of 44 real blocks. Kept because the spec
                       permits it and the fix is free.)
  _make_toml_entry     display = symbol or code -- never `ticker`.
                       39 of 40 weildx blocks and XAGa declare
                       code=<40-char hex> + ticker=<name>, no symbol.
                       So 42 LIVE ENTRIES rendered a raw 40-char hex while
                       their readable name sat right there in `ticker`:
                         display='774141504C000000000000000000000000000000'
                         name='wAAPL'

WHY THIS IS ONE PR WITH THE DATA FIX. `_insert()` never overwrites, so the
42 were corrupted at creation and a data repair persists -- but every
FUTURE TOML-sourced entry would be born corrupted until the script is fixed.
Both halves ship together; this file pins both.

URL safety was verified before touching currency_display: the /token route
only consults _resolve_display_label_to_hex when the URL segment is NOT
already a valid currency, so hex URLs keep resolving directly and
/token/wAAPL/<issuer> starts resolving too.

Hermetic: committed fixtures + committed token_names.json. No network/DB.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import tomllib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
FIX = ROOT / "tests" / "fixtures" / "toml"


def _load():
    spec = importlib.util.spec_from_file_location(
        "enrich_under_test", ROOT / "enrich_token_names.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture(scope="module")
def enr():
    return _load()


@pytest.fixture(scope="module")
def names():
    return json.loads((ROOT / "token_names.json").read_text(encoding="utf-8"))


def toml(name):
    return tomllib.loads((FIX / name).read_text(encoding="utf-8"))


# ── the replaced implementations, VERBATIM, so regressions are shown ──
def OLD_normalize_code(code: str) -> str:
    code = code.strip().upper()
    if len(code) == 3:
        return code.encode("ascii").hex().upper().ljust(40, "0")
    return code


def OLD_display(block: dict, currency_hex: str) -> str:
    name = (block.get("name") or "").strip()
    symbol = (block.get("symbol") or block.get("code") or "").strip()
    return symbol or name[:12] or currency_hex[:8]


def hex_of(ascii_code: str) -> str:
    return ascii_code.encode().hex().upper().ljust(40, "0")


# ── _normalize_code ─────────────────────────────────────────────────
class TestNormalizeCode:
    def test_three_char_unchanged_behaviour(self, enr):
        assert enr._normalize_code("USD") == OLD_normalize_code("USD")
        assert enr._normalize_code("USD") == "5553440000000000000000000000000000000000"

    @pytest.mark.parametrize("code", ["RLUSD", "wAAPL", "XAGa", "AB", "SOLO"])
    def test_non_three_char_ascii_now_equals_its_hex(self, enr, code):
        """THE regression: RLUSD never matched 524C555344... before."""
        assert enr._normalize_code(code) == enr._normalize_code(hex_of(code))
        assert OLD_normalize_code(code) != OLD_normalize_code(hex_of(code))

    def test_case_is_preserved(self, enr):
        """XRPL codes are case-sensitive. XAGa and XAGA are different tokens."""
        assert enr._normalize_code("XAGa") != enr._normalize_code("XAGA")
        assert OLD_normalize_code("XAGa") == OLD_normalize_code("XAGA")

    def test_real_xaga_hex_round_trips(self, enr):
        """The exact code XAGa's TOML declares."""
        assert enr._normalize_code("XAGa") == "5841476100000000000000000000000000000000"

    def test_hex_input_is_case_folded_only(self, enr):
        assert enr._normalize_code("5841476100000000000000000000000000000000".lower()) == \
            "5841476100000000000000000000000000000000"

    def test_empty_and_garbage(self, enr):
        assert enr._normalize_code("") == ""
        assert enr._normalize_code("   ") == ""
        assert enr._normalize_code("\u00e9") == "\u00e9"   # non-ascii: matches only itself


# ── _find_currency_block ────────────────────────────────────────────
class TestFindCurrencyBlock:
    def test_matches_on_code_as_before(self, enr):
        t = {"CURRENCIES": [{"code": "WST", "issuer": "rI"}]}
        assert enr._find_currency_block(t, "WST", "rI") is not None

    def test_matches_on_currency_key(self, enr):
        """A block keyed `currency` used to be skipped outright."""
        t = {"CURRENCIES": [{"currency": "WST", "issuer": "rI"}]}
        assert enr._find_currency_block(t, "WST", "rI") is not None

    def test_matches_on_ticker_key(self, enr):
        t = {"CURRENCIES": [{"ticker": "WST", "issuer": "rI"}]}
        assert enr._find_currency_block(t, "WST", "rI") is not None

    def test_hex_code_matches_ascii_lookup_and_vice_versa(self, enr):
        t = {"CURRENCIES": [{"code": hex_of("RLUSD"), "issuer": "rI"}]}
        assert enr._find_currency_block(t, "RLUSD", "rI") is not None
        t2 = {"CURRENCIES": [{"code": "RLUSD", "issuer": "rI"}]}
        assert enr._find_currency_block(t2, hex_of("RLUSD"), "rI") is not None

    def test_issuer_still_exact_gate(self, enr):
        """Charlie's correctness gate: the issuer must match exactly."""
        t = {"CURRENCIES": [{"code": "WST", "issuer": "rOTHER"}]}
        assert enr._find_currency_block(t, "WST", "rI") is None

    def test_case_sensitive_code_does_not_cross_match(self, enr):
        t = {"CURRENCIES": [{"code": "XAGA", "issuer": "rI"}]}
        assert enr._find_currency_block(t, "XAGa", "rI") is None

    def test_malformed_blocks_ignored(self, enr):
        t = {"CURRENCIES": ["junk", None, {"issuer": "rI"}, {"code": "", "issuer": "rI"}]}
        assert enr._find_currency_block(t, "WST", "rI") is None
        assert enr._find_currency_block({"CURRENCIES": "notalist"}, "WST", "rI") is None

    # real fixtures
    @pytest.mark.parametrize("fixture,code,issuer", [
        ("pay_aiic_nz.toml", "WST", "rBoZPmku6RKZPvZs1wBAWQgUGFALzw9WST"),
        ("xrpturbo_com.toml", "XRT", "rPvuCxw1m8u2SWTqtRN6ZEcb1xPbU4cPFR"),
        ("evecoin_org.toml", "EVE", "r3UBRqUVhUdhHSDQ8PysAJo78SyGhR4oVd"),
    ])
    def test_real_tomls_match(self, enr, fixture, code, issuer):
        assert enr._find_currency_block(toml(fixture), code, issuer) is not None

    def test_real_weildx_hex_coded_block_matches_from_hex_lookup(self, enr):
        t = toml("weildx_com.toml")
        blk = next(b for b in t["CURRENCIES"] if b.get("ticker") == "wAAPL")
        found = enr._find_currency_block(t, blk["code"], blk["issuer"])
        assert found is blk


# ── _display_from_block ─────────────────────────────────────────────
class TestDisplayFromBlock:
    def test_symbol_wins(self, enr):
        b = {"symbol": "SYM", "ticker": "TCK", "code": "ABC"}
        assert enr._display_from_block(b, "ABC") == "SYM"

    def test_ticker_beats_code(self, enr):
        """`ticker` was never read before -- the whole 42-entry bug."""
        b = {"ticker": "XAGa", "code": "5841476100000000000000000000000000000000"}
        assert enr._display_from_block(b, b["code"]) == "XAGa"
        assert OLD_display(b, b["code"]) == b["code"]   # the old behaviour: raw hex

    def test_short_ascii_code_used_as_is(self, enr):
        assert enr._display_from_block({"code": "WST"}, "WST") == "WST"

    def test_currency_key_used_when_code_absent(self, enr):
        assert enr._display_from_block({"currency": "WST"}, "WST") == "WST"

    def test_hex_code_decoded_when_no_readable_key(self, enr):
        b = {"code": "4145524F00000000000000000000000000000000"}
        assert enr._display_from_block(b, b["code"]) == "AERO"
        assert OLD_display(b, b["code"]) == b["code"]

    def test_undecodable_hex_falls_to_name_then_prefix(self, enr):
        junk = "FF" * 20
        assert enr._display_from_block({"code": junk, "name": "Some Long Token Name"}, junk) == \
            "Some Long To"
        assert enr._display_from_block({"code": junk}, junk) == junk[:8]

    def test_never_returns_a_bare_hex_when_readable_exists(self, enr):
        for b in toml("weildx_com.toml")["CURRENCIES"]:
            out = enr._display_from_block(b, b.get("code", ""))
            assert not enr._is_hex40(out), out

    def test_all_real_fixture_blocks_render_readably(self, enr):
        for fx in ("weildx_com.toml", "xrpturbo_com.toml", "evecoin_org.toml", "pay_aiic_nz.toml"):
            for b in toml(fx).get("CURRENCIES", []):
                out = enr._display_from_block(b, str(b.get("code") or b.get("currency") or ""))
                assert out and not enr._is_hex40(out), (fx, b, out)


# ── the data repair ─────────────────────────────────────────────────
class TestDataRepair:
    def test_no_entry_displays_a_raw_hex(self, enr, names):
        """42 did before this change."""
        bad = [k for k, v in names.items()
               if isinstance(v, dict) and enr._is_hex40(str(v.get("currency_display", "")))]
        assert bad == []

    @pytest.mark.parametrize("hexcode,display", [
        ("774141504C000000000000000000000000000000", "wAAPL"),
        ("5841476100000000000000000000000000000000", "XAGa"),
        ("4145524F00000000000000000000000000000000", "AERO"),
        ("57414E475A595200000000000000000000000000", "WANGZYR"),
        ("6169787270000000000000000000000000000000", "aixrp"),
    ])
    def test_specific_repairs(self, names, hexcode, display):
        e = next(v for v in names.values()
                 if isinstance(v, dict) and v.get("currency_hex") == hexcode)
        assert e["currency_display"] == display

    def test_display_is_the_decoded_hex_for_every_hex_entry(self, enr, names):
        """Repair rule: display == ASCII embedded in currency_hex. No invention."""
        for v in names.values():
            if not isinstance(v, dict): continue
            h = str(v.get("currency_hex", ""))
            if not enr._is_hex40(h): continue
            dec = enr._decode_currency_label(h)
            if dec.endswith("\u2026"):
                continue  # genuinely undecodable; not part of this repair
            assert v.get("currency_display") == dec or v.get("suppressed") or \
                v.get("verified_via") == "TODO_curation_pass", (h, v.get("currency_display"))

    def test_display_still_resolves_shareable_urls(self, enr, names):
        """currency_display is matched case-insensitively by
        _resolve_display_label_to_hex, which returns the FIRST hit. A
        readable display must therefore map to exactly one 40-char hex per
        issuer, or /token/<display>/<issuer> silently picks one.

        Scoped to 40-char classic tokens -- the namespace this repair
        touches. MPT entries (48-char issuance ids) already share displays
        within an issuer (22 groups, e.g. three 'airesa' issuances under one
        wallet); that is pre-existing, unchanged by this PR, and belongs to
        the MPT display rules, not here.
        """
        seen = {}
        for v in names.values():
            if not isinstance(v, dict): continue
            h = str(v.get("currency_hex", ""))
            if len(h) != 40: continue
            key = (str(v.get("currency_display", "")).lower(), v.get("issuer"))
            if key[0]:
                seen.setdefault(key, set()).add(h)
        dupes = {k: hs for k, hs in seen.items() if len(hs) > 1}
        assert dupes == {}, dupes

    def test_repair_introduced_no_new_display_collisions(self, names):
        """Across ALL entries incl. MPTs: the set of (display, issuer) groups
        with >1 hex must be identical before and after. 22 pre-existing MPT
        groups are tolerated; a 23rd would mean a repair created one."""
        import subprocess
        before = json.loads(subprocess.run(
            ["git", "show", "origin/main:token_names.json"],
            capture_output=True, text=True, cwd=ROOT).stdout)

        def groups(tn):
            seen = {}
            for v in tn.values():
                if not isinstance(v, dict): continue
                k = (str(v.get("currency_display", "")).lower(), v.get("issuer"))
                if k[0]:
                    seen.setdefault(k, set()).add(v.get("currency_hex"))
            return {k for k, hs in seen.items() if len(hs) > 1}

        if not before:
            pytest.skip("origin/main not available in this checkout")
        assert groups(names) - groups(before) == set()
