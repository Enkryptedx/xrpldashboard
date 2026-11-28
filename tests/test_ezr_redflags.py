"""EZR RED detection reads STRUCTURED data, never signal prose.

The engine's signal dicts carry only label/value/source_label/source_url/
checked_at_utc -- there is no severity, level or code field. Worse, its OFAC
signal returns a populated dict BOTH when an address is sanctioned and when
it is clean ("not present" is itself a real signal), so the only honest way
to tell a hit from a miss is to look at the snapshot.

That matters because earlier the same day a substring check in
token_label.is_withheld() turned human explanation text into control flow
and withheld a name purely because its evidence note mentioned another
reason class by name. Prose is written for humans and gets reworded; it is
not an API. These tests pin that EZR keys off dict/None returns only.

A crash in any detector must yield NO red flag -- never a fabricated alarm,
and never a fabricated all-clear either (the all-clear path depends on the
engine having recorded the failure in couldnt_check, which caps the light
at AMBER independently).

No network, no DB: check_data helpers are monkeypatched.
"""

import pytest

import check_data
import ezr_redflags as R


ADDR = "rAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
NEAR = "rBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"


@pytest.fixture(autouse=True)
def _no_real_lookups(monkeypatch):
    """Default every helper to 'nothing found' so each test opts in."""
    monkeypatch.setattr(check_data, "_load_ofac_snapshot",
                        lambda: {"addresses": {}}, raising=False)
    monkeypatch.setattr(check_data, "_lookalike_named_address",
                        lambda a: None, raising=False)
    monkeypatch.setattr(check_data, "_load_brand_domains",
                        lambda: {}, raising=False)
    monkeypatch.setattr(check_data, "_find_lookalike",
                        lambda h, b: None, raising=False)


def wallet(**kw):
    d = {"kind": "wallet", "address": ADDR}
    d.update(kw)
    return d


# ── clean cases produce no flags ─────────────────────────────────────

def test_a_clean_wallet_has_no_red_flags():
    assert R.collect(wallet()) == []


@pytest.mark.parametrize("bad", [None, {}, "str", {"error": "nope"}])
def test_unusable_results_produce_no_flags(bad):
    """No evidence about a subject must not become a known-bad claim."""
    assert R.collect(bad) == []


def test_an_unrecognised_kind_produces_no_flags():
    """Degrades to AMBER via the verdict tier gate, not to a false alarm."""
    assert R.collect({"kind": "spaceship", "address": ADDR}) == []


# ── OFAC sanctions ──────────────────────────────────────────────────

def test_an_ofac_hit_is_a_red_flag(monkeypatch):
    monkeypatch.setattr(check_data, "_load_ofac_snapshot", lambda: {
        "addresses": {ADDR: {"chain": "XRP", "entity_name": "LAZARUS GROUP",
                             "programs": ["DPRK3", "CYBER2"]}}}, raising=False)
    flags = R.collect(wallet())
    assert len(flags) == 1
    assert flags[0]["code"] == "ofac_sdn_match"
    assert "LAZARUS GROUP" in flags[0]["detail"]
    assert "DPRK3" in flags[0]["detail"]
    assert "Do not send funds" in flags[0]["detail"]


def test_a_different_address_is_not_a_hit(monkeypatch):
    monkeypatch.setattr(check_data, "_load_ofac_snapshot", lambda: {
        "addresses": {"rSOMEONEELSE": {"entity_name": "X"}}}, raising=False)
    assert R.collect(wallet()) == []


def test_ofac_matching_is_case_sensitive_for_xrpl(monkeypatch):
    """XRPL r-addresses are case-sensitive base58, so a lowercased variant
    is a DIFFERENT address, not the same one."""
    monkeypatch.setattr(check_data, "_load_ofac_snapshot", lambda: {
        "addresses": {ADDR.lower(): {"entity_name": "X"}}}, raising=False)
    assert R.collect(wallet()) == []


def test_a_broken_snapshot_yields_no_flag(monkeypatch):
    """_load_error means we could not check. That must not read as a hit.

    It also must not read as clean: check_address records the unavailable
    snapshot in couldnt_check, which caps the light at AMBER separately.
    """
    monkeypatch.setattr(check_data, "_load_ofac_snapshot",
                        lambda: {"_load_error": "missing", "addresses": {}},
                        raising=False)
    assert R.collect(wallet()) == []


def test_a_crashing_snapshot_loader_does_not_raise(monkeypatch):
    def boom():
        raise RuntimeError("disk gone")
    monkeypatch.setattr(check_data, "_load_ofac_snapshot", boom, raising=False)
    assert R.collect(wallet()) == []


def test_a_non_dict_snapshot_entry_is_ignored(monkeypatch):
    monkeypatch.setattr(check_data, "_load_ofac_snapshot",
                        lambda: {"addresses": {ADDR: "corrupt"}}, raising=False)
    assert R.collect(wallet()) == []


# ── lookalike / impersonation addresses ─────────────────────────────

def test_a_lookalike_address_is_a_red_flag(monkeypatch):
    monkeypatch.setattr(check_data, "_lookalike_named_address", lambda a: {
        "name": "Bitstamp", "verified_via": "bitstamp.net",
        "distance": 2, "near_address": NEAR}, raising=False)
    flags = R.collect(wallet())
    assert len(flags) == 1
    assert flags[0]["code"] == "lookalike_address"
    assert "Bitstamp" in flags[0]["detail"]
    assert "2 character(s)" in flags[0]["detail"]
    assert "character by character" in flags[0]["detail"]


def test_a_crashing_lookalike_helper_does_not_raise(monkeypatch):
    def boom(a):
        raise RuntimeError("named_accounts unreadable")
    monkeypatch.setattr(check_data, "_lookalike_named_address", boom,
                        raising=False)
    assert R.collect(wallet()) == []


def test_both_flags_can_fire_together(monkeypatch):
    monkeypatch.setattr(check_data, "_load_ofac_snapshot", lambda: {
        "addresses": {ADDR: {"entity_name": "E", "programs": []}}},
        raising=False)
    monkeypatch.setattr(check_data, "_lookalike_named_address", lambda a: {
        "name": "N", "distance": 1, "near_address": NEAR}, raising=False)
    codes = {f["code"] for f in R.collect(wallet())}
    assert codes == {"ofac_sdn_match", "lookalike_address"}


# ── tokens: the ISSUER is the subject ───────────────────────────────

def test_a_token_is_judged_on_its_issuer(monkeypatch):
    """The currency code is just text and cannot be sanctioned; the issuer
    created the token and controls supply."""
    seen = []
    monkeypatch.setattr(check_data, "_lookalike_named_address",
                        lambda a: seen.append(a), raising=False)
    R.collect({"kind": "token", "currency": "USD", "issuer": ADDR})
    assert seen == [ADDR]


def test_a_sanctioned_token_issuer_is_red(monkeypatch):
    monkeypatch.setattr(check_data, "_load_ofac_snapshot", lambda: {
        "addresses": {ADDR: {"entity_name": "E", "programs": ["P"]}}},
        raising=False)
    flags = R.collect({"kind": "token", "currency": "USD", "issuer": ADDR})
    assert len(flags) == 1
    assert "token's issuer" in flags[0]["detail"]


def test_a_token_with_no_issuer_is_not_a_flag():
    assert R.collect({"kind": "token", "currency": "USD", "issuer": ""}) == []


# ── domains ─────────────────────────────────────────────────────────

def test_a_lookalike_domain_is_a_red_flag(monkeypatch):
    monkeypatch.setattr(check_data, "_load_brand_domains",
                        lambda: {"xaman": {"xaman.app"}}, raising=False)
    monkeypatch.setattr(check_data, "_find_lookalike",
                        lambda h, b: ("xaman", {"xaman.app"}), raising=False)
    flags = R.collect({"kind": "url", "subject": "xarnan.app"})
    assert len(flags) == 1
    assert flags[0]["code"] == "lookalike_domain"
    assert "xaman.app" in flags[0]["detail"]
    assert "seed phrase" in flags[0]["detail"]


def test_a_clean_domain_has_no_flags(monkeypatch):
    assert R.collect({"kind": "url", "subject": "bitstamp.com"}) == []


def test_a_crashing_domain_helper_does_not_raise(monkeypatch):
    def boom():
        raise RuntimeError("brand list unreadable")
    monkeypatch.setattr(check_data, "_load_brand_domains", boom, raising=False)
    assert R.collect({"kind": "url", "subject": "x.com"}) == []


def test_a_url_with_no_host_is_not_a_flag():
    assert R.collect({"kind": "url", "subject": ""}) == []


# ── flag contract ───────────────────────────────────────────────────

def test_every_flag_has_code_headline_and_detail(monkeypatch):
    monkeypatch.setattr(check_data, "_load_ofac_snapshot", lambda: {
        "addresses": {ADDR: {"entity_name": "E", "programs": ["P"]}}},
        raising=False)
    monkeypatch.setattr(check_data, "_lookalike_named_address", lambda a: {
        "name": "N", "distance": 1, "near_address": NEAR}, raising=False)
    for f in R.collect(wallet()):
        assert f["code"] and f["headline"] and f["detail"]
        # Consumer-facing copy: no engine internals leaking into the page.
        assert "_" not in f["headline"]
