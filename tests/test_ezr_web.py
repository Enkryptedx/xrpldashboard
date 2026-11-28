"""EZR input handling: one box, three kinds of input, and a safety gate.

Two properties matter most here and are pinned hardest:

1. EZR and /check must AGREE about what a paste MEANS. The precedence in
   ezr_web.classify() is copied from app.py:check_page() rather than
   invented. If they disagreed, a string could be a token on one page and
   a domain on the other, and the _domain_is_safe() gate that protects
   /check would not be protecting EZR.

2. The _domain_is_safe() gate is kept on BOTH domain paths. It rejects
   private-network addresses and malformed hosts; dropping it for
   convenience would turn a public paste box into an SSRF probe.

No network: check_data lookups are monkeypatched everywhere.
"""

import pytest

import check_data
import ezr_web as W


GOOD_ADDR = "rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De"
OTHER_ADDR = "rvYAfWj5gh67oV6fW32ZzP3Aw4Eubs59B"


# ── address validation mirrors app.py ────────────────────────────────

def test_a_real_address_validates():
    assert W.is_xrpl_address(GOOD_ADDR) is True


@pytest.mark.parametrize("bad", [
    "", None, 123,
    "xMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De",   # wrong prefix
    "r" + "A" * 2,                          # too short
    "r" + "a" * 40,                         # too long
    "rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5D0",   # '0' not in base58 alphabet
])
def test_bad_addresses_are_rejected(bad):
    assert W.is_xrpl_address(bad) is False


def test_alphabet_is_identical_to_app_py():
    """Kept byte-identical so EZR and the site cannot disagree about what
    a valid address looks like."""
    import app as A
    assert W._XRPL_ADDR_CHARS == A._XRPL_ADDR_CHARS


# ── classification precedence (copied from /check) ───────────────────

def test_explicit_scheme_is_a_url():
    assert W.classify("https://bitstamp.net/login") == "url"


def test_token_form_is_symbol_dot_issuer():
    assert W.classify(f"USD.{GOOD_ADDR}") == "token"


def test_a_bare_domain_is_a_url():
    assert W.classify("bitstamp.com") == "url"


def test_a_bare_address_is_an_address():
    assert W.classify(GOOD_ADDR) == "address"


def test_a_dotted_thing_with_a_bad_issuer_is_treated_as_a_domain():
    """Mirrors /check: SYMBOL.<not-an-address> falls through to the domain
    path, where the safety gate decides."""
    assert W.classify("USD.notanaddress") == "url"


@pytest.mark.parametrize("junk", ["", "   ", "hello world", "rrr"])
def test_unparseable_input_is_unknown(junk):
    assert W.classify(junk) == "unknown"


def test_scheme_outranks_the_token_form():
    """An explicit scheme wins even when the rest looks tokenish."""
    assert W.classify(f"https://USD.{GOOD_ADDR}") == "url"


# ── the safety gate ──────────────────────────────────────────────────

def test_an_unsafe_domain_is_refused_not_fetched(monkeypatch):
    """A private-network host must never reach check_url()."""
    called = []
    monkeypatch.setattr(check_data, "_extract_hostname",
                        lambda q: "192.168.1.1", raising=False)
    monkeypatch.setattr(check_data, "_domain_is_safe",
                        lambda h: False, raising=False)
    monkeypatch.setattr(check_data, "check_url",
                        lambda q: called.append(q), raising=False)
    result, err = W.lookup("http://192.168.1.1/admin")
    assert result is None
    assert "safety check" in err
    assert called == [], "check_url was called on an unsafe host"


def test_a_safe_domain_reaches_check_url(monkeypatch):
    monkeypatch.setattr(check_data, "_extract_hostname",
                        lambda q: "bitstamp.com", raising=False)
    monkeypatch.setattr(check_data, "_domain_is_safe",
                        lambda h: True, raising=False)
    monkeypatch.setattr(check_data, "check_url",
                        lambda q: {"kind": "url", "subject": "bitstamp.com"},
                        raising=False)
    result, err = W.lookup("https://bitstamp.com")
    assert err is None
    assert result["kind"] == "url"


def test_the_bare_domain_path_is_gated_too(monkeypatch):
    """Not just the explicit-scheme path -- both, as /check does it."""
    monkeypatch.setattr(check_data, "_domain_is_safe",
                        lambda h: False, raising=False)
    result, err = W.lookup("something.internal")
    assert result is None and "safety check" in err


# ── dispatch to the existing engine ──────────────────────────────────

def test_an_address_dispatches_to_check_address(monkeypatch):
    seen = []
    monkeypatch.setattr(check_data, "check_address",
                        lambda a: seen.append(a) or {"kind": "wallet"},
                        raising=False)
    W.lookup(GOOD_ADDR)
    assert seen == [GOOD_ADDR]


def test_a_token_dispatches_with_symbol_and_issuer_split(monkeypatch):
    seen = []
    monkeypatch.setattr(
        check_data, "check_token",
        lambda c, i: seen.append((c, i)) or {"kind": "token"}, raising=False)
    W.lookup(f"USD.{GOOD_ADDR}")
    assert seen == [("USD", GOOD_ADDR)]


def test_empty_input_asks_for_input():
    result, err = W.lookup("   ")
    assert result is None and "Paste something" in err


def test_an_overlong_paste_is_clamped_not_processed(monkeypatch):
    """A multi-kilobyte paste is a message (a later phase) or abuse."""
    called = []
    monkeypatch.setattr(check_data, "check_address",
                        lambda a: called.append(a), raising=False)
    result, err = W.lookup("r" + "A" * (W.MAX_INPUT_LEN + 10))
    assert result is None and "too long" in err
    assert called == []


def test_an_engine_crash_becomes_a_generic_message(monkeypatch):
    """Exception text can carry internal hostnames; the visitor sees a
    generic line and the caller logs the real error."""
    def boom(a):
        raise RuntimeError("tunnel at 192.168.40.95:5006 refused")
    monkeypatch.setattr(check_data, "check_address", boom, raising=False)
    result, err = W.lookup(GOOD_ADDR)
    assert result is None
    assert "192.168" not in err
    assert "went wrong" in err


# ── check_input end to end ───────────────────────────────────────────

def test_check_input_never_greens_unparseable_junk():
    out = W.check_input("hello world")
    assert out["light"] == "amber"
    assert out["kind"] == "unknown"
    assert out["query"] == "hello world"


def test_check_input_greens_a_verified_sovereign_wallet(monkeypatch):
    monkeypatch.setattr(check_data, "check_address", lambda a: {
        "kind": "wallet", "address": a, "tier": "verified",
        "sourcing": "sovereign", "couldnt_check": [], "signals": [],
        "status_line": "Verified.",
    }, raising=False)
    monkeypatch.setattr(check_data, "_load_ofac_snapshot",
                        lambda: {"addresses": {}}, raising=False)
    monkeypatch.setattr(check_data, "_lookalike_named_address",
                        lambda a: None, raising=False)
    out = W.check_input(GOOD_ADDR)
    assert out["light"] == "green"
    assert out["light_label"] == "Verified"
    assert "does NOT mean it is safe to send money" in out["caveat"]


def test_a_sanctioned_wallet_is_red_even_when_verified(monkeypatch):
    monkeypatch.setattr(check_data, "check_address", lambda a: {
        "kind": "wallet", "address": a, "tier": "verified",
        "sourcing": "sovereign", "couldnt_check": [], "signals": [],
    }, raising=False)
    monkeypatch.setattr(check_data, "_load_ofac_snapshot", lambda: {
        "addresses": {GOOD_ADDR: {"entity_name": "SANCTIONED CO",
                                  "programs": ["P"]}}}, raising=False)
    monkeypatch.setattr(check_data, "_lookalike_named_address",
                        lambda a: None, raising=False)
    out = W.check_input(GOOD_ADDR)
    assert out["light"] == "red"
    assert out["light_label"] == "Stop"


def test_check_input_always_returns_render_keys():
    out = W.check_input("junk")
    for key in ("light", "light_label", "headline", "reasons", "caveat",
                "result", "query", "kind"):
        assert key in out


# ── the route ────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def client():
    import app as A
    A.app.config["TESTING"] = True
    return A.app.test_client()


def test_get_ezr_renders_the_empty_form(client):
    r = client.get("/ezr")
    assert r.status_code == 200
    assert b'name="q"' in r.data


def test_post_ezr_renders_a_verdict(client, monkeypatch):
    import ezr_web
    monkeypatch.setattr(ezr_web, "check_input", lambda q: {
        "light": "amber", "light_label": "Be careful",
        "headline": "Be careful", "reasons": ["Nothing verified."],
        "red_flags": [], "caveat": "Unconfirmed.", "result": None,
        "input_error": None, "query": q, "kind": "address",
    })
    r = client.post("/ezr", data={"q": GOOD_ADDR})
    assert r.status_code == 200
    body = r.data.decode("utf-8", "replace")
    assert "Be careful" in body
    assert "Nothing verified." in body


def test_the_route_degrades_to_amber_when_check_input_crashes(client, monkeypatch):
    """A crash must NOT render green. The visitor pasted this to be
    warned, so false reassurance is the worst available outcome."""
    import ezr_web

    def boom(q):
        raise RuntimeError("engine exploded")
    monkeypatch.setattr(ezr_web, "check_input", boom)
    r = client.post("/ezr", data={"q": GOOD_ADDR})
    assert r.status_code == 200
    body = r.data.decode("utf-8", "replace")
    assert "verdict amber" in body
    assert "verdict green" not in body


def test_ezr_is_noindex_while_it_is_a_skeleton(client):
    r = client.get("/ezr")
    assert b"noindex" in r.data


def test_the_existing_check_page_still_works(client):
    """EZR must leave /check untouched and working."""
    assert client.get("/check").status_code == 200
