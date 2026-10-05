"""/thisweek edition page must wrap long hashes and links at phone width.

Edition bodies carry 64-char ledger-object and transaction hashes inside
<code>, plus full web.archive.org URLs as links. None of those contain a
space or hyphen, so without an explicit rule the browser has no break
opportunity and the token sets the page's min-content width. Measured
2026-10-05 on edition 4: document.scrollWidth 565 against a 390px viewport,
which clipped the right-hand end of both hashes and the Wayback URL.

No DB, no network: the template is read from disk.
"""

import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(os.path.dirname(HERE), "templates", "thisweek.html")


def _css():
    with open(TEMPLATE, encoding="utf-8") as f:
        return f.read()


def _style_block():
    css = _css()
    m = re.search(r"<style>(.*?)</style>", css, re.S)
    assert m, "thisweek.html has no <style> block"
    return m.group(1)


def test_code_spans_wrap():
    """Long hashes live in <code>; they must be allowed to break."""
    style = _style_block()
    rule = re.search(r"\.content code[^{]*\{([^}]*)\}", style)
    assert rule, ".content code rule not found"
    # the wrap may be declared on the base rule or a later grouped selector
    assert "overflow-wrap" in style, (
        "no overflow-wrap anywhere in the edition stylesheet"
    )
    grouped = re.search(
        r"\.content code\s*,\s*\.content a\s*\{([^}]*)\}", style)
    assert grouped, (
        "expected a grouped `.content code, .content a` wrap rule"
    )
    assert "anywhere" in grouped.group(1), (
        "overflow-wrap must be `anywhere` so a token breaks ONLY when it "
        f"cannot otherwise fit; got: {' '.join(grouped.group(1).split())}"
    )


def test_links_wrap():
    """Full archive URLs render as links and must break too."""
    style = _style_block()
    assert re.search(r"\.content a[^{]*\{[^}]*overflow-wrap\s*:\s*anywhere",
                     style), ".content a must set overflow-wrap: anywhere"


def test_wrap_is_scoped_to_the_edition_body():
    """Must not leak to other pages: no bare `code {` / `a {` wrap rule."""
    style = _style_block()
    for sel in (r"^\s*code\s*\{", r"^\s*a\s*\{"):
        for m in re.finditer(sel, style, re.M):
            block = style[m.end():style.index("}", m.end())]
            assert "overflow-wrap" not in block, (
                f"wrap rule leaked onto a bare selector ({sel!r}); it must be "
                "scoped under .content so other pages are unaffected"
            )
    # every overflow-wrap declaration must sit on a .content selector
    for m in re.finditer(r"([^{}]+)\{([^}]*overflow-wrap[^}]*)\}", style):
        selector = " ".join(m.group(1).split())
        assert ".content" in selector, (
            f"overflow-wrap declared on non-scoped selector: {selector!r}"
        )


def test_prose_containers_can_shrink():
    """min-width:0 lets list items/paragraphs actually shrink to the box."""
    style = _style_block()
    assert re.search(r"\.content p\s*,\s*\.content li\s*\{[^}]*min-width\s*:\s*0",
                     style), ".content p/li should set min-width: 0"
