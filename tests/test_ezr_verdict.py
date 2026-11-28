"""EZR traffic light: GREEN must be hard to reach, and never a default.

check_data.py is deliberately FACTS-NOT-VERDICTS -- its own docstring says
the status pill describes "WHAT IDENTITY CLAIM EXISTS ON THE LEDGER, not
whether the account is safe to interact with", and it ships an
anti-reassurance tooltip to kill the inference "they have a website and a
name, so they're probably legit".

A traffic light IS a verdict, so these tests exist to pin that EZR errs
cautiously in every direction the engine leaves ambiguous:

  * one structured known-bad hit outranks any amount of reassuring data
  * GREEN needs ALL of: verified tier, our own node, nothing unchecked
  * every unrecognised or absent value degrades to AMBER, never GREEN
  * GREEN always carries the "does not mean safe to send money" caveat

Pure: no network, no DB, no engine calls.
"""

import pytest

import ezr_verdict as V


def res(**kw):
    """A minimal engine-shaped result. Defaults are the GREEN-eligible
    shape so each test can spoil exactly one field."""
    base = {
        "kind": "wallet",
        "subject": "rAAA",
        "address": "rAAA",
        "tier": "verified",
        "sourcing": "sovereign",
        "couldnt_check": [],
        "signals": [],
        "status_line": "Verified first-party claim.",
    }
    base.update(kw)
    return base


FLAG = {"code": "ofac_sdn_match", "headline": "On the US sanctions list",
        "detail": "Sanctioned address."}


# ── severity combination ─────────────────────────────────────────────

def test_worst_picks_the_most_cautious():
    assert V.worst("green", "amber", "red") == "red"
    assert V.worst("green", "amber") == "amber"
    assert V.worst("green", "green") == "green"


def test_worst_of_nothing_is_amber_not_green():
    """An empty judgement set must not read as a pass."""
    assert V.worst() == "amber"
    assert V.worst("nonsense") == "amber"


# ── RED outranks everything ──────────────────────────────────────────

def test_a_red_flag_beats_a_fully_verified_result():
    """THE anti-averaging test. A sanctioned-but-verified account is RED.

    The reasoning a scam victim does is additive ("it had a website AND a
    name AND a verified badge"); one credible known-bad hit must not be
    diluted by any number of reassuring facts.
    """
    out = V.verdict(res(), red_flags=[FLAG])
    assert out["light"] == V.RED
    assert out["headline"] == "On the US sanctions list"
    assert "Sanctioned address." in out["reasons"]


def test_red_carries_its_flags_through():
    out = V.verdict(res(), red_flags=[FLAG])
    assert out["red_flags"] == [FLAG]


# ── GREEN requires every gate ────────────────────────────────────────

def test_green_when_verified_sovereign_and_nothing_unchecked():
    out = V.verdict(res())
    assert out["light"] == V.GREEN
    assert out["headline"] == "Verified"


def test_green_always_carries_the_not_safe_to_send_caveat():
    """The sentence that stops "verified" being read as "safe to send
    money to" -- the exact inference the engine refuses to support."""
    out = V.verdict(res())
    assert out["caveat"]
    assert "does NOT mean it is safe to send money" in out["caveat"]


@pytest.mark.parametrize("sourcing", [
    "fallback-public-rpc", "public-no-tunnel-configured", "stale-cache",
])
def test_non_sovereign_sourcing_downgrades_green(sourcing):
    """If the ledger read came from someone else's node or a stale cache,
    we cannot claim verification on our own authority."""
    out = V.verdict(res(sourcing=sourcing))
    assert out["light"] == V.AMBER
    assert any("our own node" in r for r in out["reasons"])


def test_anything_unchecked_blocks_green():
    """The "0/94 vendors flagged this" trap: an unreachable source is an
    ABSENCE of evidence and must never read as a clean bill of health."""
    out = V.verdict(res(couldnt_check=[
        {"label": "OFAC SDN cross-check", "reason": "snapshot unavailable"}]))
    assert out["light"] == V.AMBER
    assert any("could not be completed" in r for r in out["reasons"])


def test_missing_sourcing_key_is_not_a_failure():
    """check_url() results legitimately have no `sourcing` field (a domain
    check does no ledger read). Tier still governs, so a verified domain
    can be green, but its absence must not be read as non-sovereign."""
    r = res(kind="url", tier="verified")
    r.pop("sourcing")
    assert V.verdict(r)["light"] == V.GREEN


# ── every weaker tier is AMBER ───────────────────────────────────────

@pytest.mark.parametrize("tier", ["self", "self-described"])
def test_self_described_is_amber_with_the_engines_own_warning(tier):
    """The most dangerous tier to show as green. The engine says it
    outright: a scammer can fill in a website and a name just as easily
    as a legitimate project can."""
    out = V.verdict(res(tier=tier))
    assert out["light"] == V.AMBER
    assert any("self-reported" in r for r in out["reasons"])


def test_curator_label_is_amber_not_green():
    """A label from our own records is not server-side verification of a
    first-party claim."""
    out = V.verdict(res(tier="labeled"))
    assert out["light"] == V.AMBER


@pytest.mark.parametrize("tier", ["bare", "unknown"])
def test_no_identity_claim_is_amber(tier):
    out = V.verdict(res(tier=tier))
    assert out["light"] == V.AMBER
    assert any("no identity claim" in r for r in out["reasons"])


def test_an_unrecognised_tier_degrades_to_amber():
    """A future engine change could add a tier this module has never seen.
    Unknown vocabulary must not silently reassure."""
    out = V.verdict(res(tier="brand_new_tier_from_the_future"))
    assert out["light"] == V.AMBER


def test_empty_tier_is_amber():
    assert V.verdict(res(tier=""))["light"] == V.AMBER


def test_tier_canonical_wins_over_legacy_tier():
    """check_address returns both; tier_canonical is the Part C field."""
    out = V.verdict(res(tier="verified", tier_canonical="self-described"))
    assert out["light"] == V.AMBER


def test_tier_matching_is_case_insensitive():
    assert V.verdict(res(tier="VERIFIED"))["light"] == V.GREEN


# ── no usable result ─────────────────────────────────────────────────

@pytest.mark.parametrize("bad", [None, {}, {"error": "nope"}, "not-a-dict"])
def test_no_usable_result_is_amber_never_green_or_red(bad):
    """A bad paste or a crashed lookup is UNVERIFIABLE: amber by
    definition. Not green (we found nothing reassuring) and not red (we
    have no evidence about the subject, so a known-bad claim would be a
    lie). Showing green on an engine exception would be the single worst
    failure mode available -- the user pasted this to be warned.
    """
    out = V.verdict(bad)
    assert out["light"] == V.AMBER
    assert out["headline"] == "Couldn’t check this"


def test_input_error_text_is_surfaced():
    out = V.verdict(None, input_error="That URL fails a safety gate.")
    assert out["reasons"] == ["That URL fails a safety gate."]


def test_result_is_cleared_when_unusable():
    """The template must not try to render signals off a failed lookup."""
    assert V.verdict({"error": "x"})["result"] is None


# ── labels ───────────────────────────────────────────────────────────

def test_light_labels_are_plain_consumer_words():
    assert V.light_label(V.RED) == "Stop"
    assert V.light_label(V.AMBER) == "Be careful"
    assert V.light_label(V.GREEN) == "Verified"


def test_unknown_light_label_defaults_cautious():
    assert V.light_label("chartreuse") == "Be careful"


# ── the verdict contract ─────────────────────────────────────────────

@pytest.mark.parametrize("args,kwargs", [
    ((res(),), {}),
    ((res(),), {"red_flags": [FLAG]}),
    ((None,), {}),
    (({"error": "x"},), {}),
])
def test_verdict_always_returns_the_full_shape(args, kwargs):
    out = V.verdict(*args, **kwargs)
    for key in ("light", "headline", "reasons", "red_flags", "caveat",
                "result", "input_error"):
        assert key in out
    assert out["light"] in (V.RED, V.AMBER, V.GREEN)
    assert isinstance(out["reasons"], list)


def test_reasons_are_never_empty():
    """An amber light with no explanation is a dead end for the reader."""
    for r in (res(tier="bare"), res(tier="labeled"), None, {"error": "x"}):
        assert V.verdict(r)["reasons"]
