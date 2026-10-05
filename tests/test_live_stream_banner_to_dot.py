"""Banner → dot (Charlie 2026-10-05, branch banner-dot-2026-10-05).

`static/js/live_stream.js` used to paint a floating amber banner: a
`position:fixed` div pinned bottom-left on EVERY page that loads the script,
sitting on top of whatever was underneath it (it landed over an /amendments
countdown card during screenshot capture on 2026-10-04).

That banner is removed site-wide. The same sentence now rides on a small amber
dot in the existing top bar (`templates/_liveness_chip.html`), shown ONLY while
the public-feed fallback is actually active, revealing the sentence on hover or
tap.

What these tests prove:

  (a) the floating banner is gone from the JS — no DOM creation, no fixed
      positioning, no `data-live-stream-banner` attribute;
  (b) the banner is gone from rendered pages site-wide;
  (c) the dot markup ships, starts hidden, and carries no wording of its own;
  (d) the dot is revealed ONLY in the 'fallback' state;
  (e) the sentence exists in exactly one place (live_stream.js), so the
      template cannot drift from it.

Hermetic: reads the static asset off disk and renders through the real Flask
Jinja env. No DB, no network, no browser.
"""
from __future__ import annotations

import os
import re
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

JS_PATH = os.path.join(REPO, "static", "js", "live_stream.js")
CHIP_PATH = os.path.join(REPO, "templates", "_liveness_chip.html")

FALLBACK_SENTENCE = (
    "Our node is still connecting \u2014 showing the public feed until it does"
)


def _js():
    with open(JS_PATH, encoding="utf-8") as fh:
        return fh.read()


def _chip():
    with open(CHIP_PATH, encoding="utf-8") as fh:
        return fh.read()


def _strip_comments(src):
    """Drop // and /* */ comments so prose about the old banner can't match."""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", src)


# --------------------------------------------------------------------------
# (a) the floating banner is gone from the JS
# --------------------------------------------------------------------------

def test_no_floating_banner_in_live_stream_js():
    code = _strip_comments(_js())

    assert "BANNER_STYLE" not in code, "the banner style block is still present"
    assert "bannerEl" not in code, "the banner element variable is still present"
    assert "data-live-stream-banner" not in code, (
        "the banner's data attribute is still emitted"
    )
    assert "position:fixed" not in code, (
        "live_stream.js must not pin any fixed-position element any more"
    )
    # the script must not build or attach a banner node
    assert "createElement" not in code, (
        "live_stream.js must create no DOM at all now; the dot lives in the "
        "liveness chip template"
    )
    assert "appendChild" not in code, (
        "live_stream.js must not attach anything to the document"
    )


def test_source_state_machine_is_pure_state():
    """setSourceState notifies listeners and touches no DOM."""
    code = _js()
    m = re.search(r"function setSourceState\(state\)\s*\{(.*?)\n  \}", code, re.S)
    assert m, "setSourceState not found"
    body = m.group(1)
    for banned in ("document", "createElement", "style", "appendChild"):
        assert banned not in body, (
            f"setSourceState must not touch the DOM; found {banned!r}"
        )
    assert "sourceListeners" in body, "setSourceState must notify subscribers"


def test_js_exposes_source_state_api():
    code = _js()
    for fn in ("onSourceState", "getSourceState", "getSourceMessage"):
        assert re.search(rf"\b{fn}\s*:", code), f"public API is missing {fn}"


def test_three_states_still_supported():
    """The state machine keeps its three states; only the painting changed."""
    code = _js()
    for state in ("'connecting'", "'fallback'", "'hidden'"):
        assert state in code, f"state {state} disappeared from the machine"


# --------------------------------------------------------------------------
# (e) the sentence exists in exactly ONE place
# --------------------------------------------------------------------------

def test_fallback_sentence_defined_once_in_js():
    code = _js()
    assert f"'{FALLBACK_SENTENCE}'" in code, (
        "the fallback sentence must still exist as a JS constant"
    )
    # exactly one string literal (a doc comment mentioning it is fine)
    literals = re.findall(re.escape(f"'{FALLBACK_SENTENCE}'"), code)
    assert len(literals) == 1, (
        f"the sentence must be defined once, found {len(literals)} literals"
    )


def test_sentence_not_duplicated_into_the_template():
    """The chip pulls the text from live_stream.js; it must not hardcode it."""
    chip = _chip()
    assert FALLBACK_SENTENCE not in chip, (
        "the liveness chip must not carry its own copy of the sentence \u2014 it "
        "reads it from live_stream.js so the two cannot drift"
    )
    assert "onSourceState" in chip, (
        "the chip must subscribe to the live_stream source state"
    )


def test_no_new_wording_in_the_chip_template():
    """The dot ships no visible words of its own."""
    chip = _chip()
    block = chip[chip.index('<span class="live-source"'):]
    markup = block[:block.index("</span>")]
    # the dot's own markup carries no text node
    text = re.sub(r"<[^>]+>", "", markup).strip()
    assert text == "", f"the dot markup must contain no wording; found {text!r}"


# --------------------------------------------------------------------------
# (c)/(d) the dot: present, hidden by default, revealed only on fallback
# --------------------------------------------------------------------------

def test_dot_markup_present_and_hidden_by_default():
    chip = _chip()
    m = re.search(r'<span class="live-source"[^>]*>', chip)
    assert m, "the dot wrapper is missing from the chip template"
    assert "hidden" in m.group(0), (
        "the dot must start hidden so it can never flash on a healthy page"
    )
    assert "data-live-source-dot" in chip, "the dot button is missing"
    assert "data-live-source-msg" in chip, "the message element is missing"


def test_dot_revealed_only_in_fallback_state():
    """The chip reveals the dot on 'fallback' and hides it otherwise."""
    chip = _chip()
    m = re.search(r"function apply\(state, text\)\s*\{(.*?)\n  \}", chip, re.S)
    assert m, "the chip's apply(state, text) handler was not found"
    body = m.group(1)

    assert "state === 'fallback'" in body, (
        "the dot must be gated on the 'fallback' state specifically"
    )
    # the reveal happens in the fallback branch, the hide in the else branch
    branches = body.split("} else {")
    assert len(branches) == 2, "expected a single fallback/else split"
    reveal, hide = branches
    assert "wrap.hidden = false" in reveal, (
        "the fallback branch must reveal the dot"
    )
    assert "wrap.hidden = true" in hide, (
        "every non-fallback state must hide the dot"
    )
    # 'connecting' must NOT reveal it
    assert "state === 'connecting'" not in body, (
        "'connecting' must not reveal the dot \u2014 fallback only"
    )


def test_dot_survives_the_mobile_chip_hide():
    """The dot must not be nested inside .liveness-chip.

    .liveness-chip is display:none below 720px. The banner this replaces WAS
    visible on phones, so nesting the dot would silently drop the signal for
    every mobile reader.
    """
    chip = _chip()
    dot_at = chip.index('<span class="live-source"')
    # the dot markup sits after the chip's {% endif %}, not inside the <a>
    anchor = re.search(r'<a class="liveness-chip', chip)
    assert anchor, "liveness chip anchor not found"
    anchor_close = chip.index("</a>", anchor.start())
    assert dot_at > anchor_close, (
        "the dot must live outside the .liveness-chip element so the "
        "max-width:720px hide cannot take it away on phones"
    )
    assert re.search(r"\.live-source\s*\{", chip), (
        "the dot needs its own style rule, independent of .liveness-chip"
    )


# --------------------------------------------------------------------------
# (b) the banner is gone from rendered pages, site-wide
# --------------------------------------------------------------------------

@pytest.mark.parametrize("path", ["/amendments", "/", "/network"])
def test_no_banner_markup_in_rendered_pages(path):
    import app

    app.app.config["TESTING"] = True
    resp = app.app.test_client().get(path)
    assert resp.status_code == 200, f"{path} returned {resp.status_code}"
    page = resp.data.decode()

    assert "data-live-stream-banner" not in page, (
        f"{path}: the old banner attribute is still rendered"
    )
    assert FALLBACK_SENTENCE not in page, (
        f"{path}: the sentence must not be baked into the HTML; it is "
        "supplied by live_stream.js only when the fallback is active"
    )


def test_dot_ships_on_a_page_that_uses_the_live_stream():
    import app

    app.app.config["TESTING"] = True
    page = app.app.test_client().get("/amendments").data.decode()
    assert "data-live-source" in page, "the dot did not ship on /amendments"
    m = re.search(r'<span class="live-source"[^>]*>', page)
    assert m and "hidden" in m.group(0), (
        "the dot must render hidden until live_stream.js reveals it"
    )
