"""Forbidden-verdict-phrase tests (Charlie ruling 2026-09-20).

The site states facts, never verdicts — words like "impostor", "scam",
"fake", "fraud" MUST NOT appear as labels or headings applied to a
specific issuer/token/wallet. They MAY appear:
- as third-party token *names* rendered factually (e.g., a token named
  "Scams Detective" — that IS the on-ledger name; we report it, we do
  not endorse it),
- as descriptive language on educational help pages
  (/help/already-sent-money describes recovery-scam patterns; /check
  teaches "warning signs commonly used in fraud attempts"),
- as defensive language on legal pages
  (/terms says '"no identity claim on file" is not a scam warning'),
- as antifraud policy citations (/regulation),
- as SIM/debug-mode descriptions (rlusd.html "fake animation events"),
- and as internal CSS classes / variable names / Jinja comments.

But NEVER as an assertion by us about a specific issuer/token/wallet.

If this test fails, review whether the new copy is a verdict or a fact.
Positive-knowledge phrasings:
- verdict `impostor pool` → fact `this pool's RLUSD is not Ripple's RLUSD`
- verdict `scam token`   → fact `issuer is not the canonical issuer for this ticker`
- verdict `fake issuer`  → fact `issuer domain does not match`
"""
from __future__ import annotations

import pathlib
import re

import pytest

import app as app_module


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
TEMPLATES_DIR = REPO_ROOT / "templates"


# Two-tier coverage.
#
# TIER A — verdict-critical routes. On these pages every visible string
# is written by us about a specific issuer/token/wallet, so a bare
# verdict word ANYWHERE reads as our verdict. The RLUSD ticker-collision
# token page is the canonical failure mode we're guarding against.
VERDICT_CRITICAL_ROUTES = [
    "/token/524C555344000000000000000000000000000000/rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De",  # canonical RLUSD
    "/token/524C555344000000000000000000000000000000/rLUSDtykL2NVz3HJe1Jqoc7dsxWFVcsmuK",  # ticker-collision RLUSD
]


FORBIDDEN_VERDICT_WORDS = [
    "impostor",
    "imposter",
    "scam",
    "fake",
    "fraud",
]


# TIER B — verdict PHRASES that must not appear in ANY template file.
# These are the shapes that read as verdicts even in prose we control
# (methodology, taxonomy, warning banners). Third-party token names,
# educational copy, and defensive prose don't match these patterns.
FORBIDDEN_VERDICT_PHRASES = [
    r"\bimpostor\s+(?:pool|token|issuer|USD|USDT|USDC|DAI|RLUSD)\b",
    r"\bimposter\s+(?:pool|token|issuer|USD|USDT|USDC|DAI|RLUSD)\b",
    r"\bscam\s+(?:pool|token|issuer|coin|address|wallet)\b",
    r"\bfake\s+(?:pool|token|issuer|coin|address|wallet)\b",
    r"\bfraudulent\s+(?:pool|token|issuer|coin|address|wallet)\b",
]


@pytest.fixture(scope="module")
def client():
    return app_module.app.test_client()


@pytest.mark.parametrize("route", VERDICT_CRITICAL_ROUTES)
def test_no_verdict_word_on_verdict_critical_route(client, route):
    r = client.get(route)
    if r.status_code == 302:
        loc = r.headers.get("Location") or ""
        if loc.startswith("http"):
            pytest.skip(f"{route} redirected off-site to {loc}")
        r = client.get(loc)
    if r.status_code != 200:
        pytest.skip(f"{route} returned {r.status_code}")
    body = r.data.decode(errors="replace")
    # Strip <script> and <style> blocks — those carry the JSON payload
    # of third-party token names ("Scams Detective") that we render
    # factually, and CSS rules for .scam-warn* classes on other pages.
    stripped = re.sub(r"<script[^>]*>.*?</script>", "", body, flags=re.DOTALL | re.IGNORECASE)
    stripped = re.sub(r"<style[^>]*>.*?</style>", "", stripped, flags=re.DOTALL | re.IGNORECASE)
    lower = stripped.lower()
    hits = [w for w in FORBIDDEN_VERDICT_WORDS if w in lower]
    assert not hits, (
        f"Verdict word(s) {hits} appeared as visible text on {route} — "
        f"the site states facts, never verdicts. Replace with "
        f"positive-knowledge wording. See "
        f"tests/test_no_verdict_words_on_public_surfaces.py header."
    )


def _iter_template_lines():
    for path in TEMPLATES_DIR.rglob("*.html"):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # Strip Jinja comments {# ... #} — those never render.
        text = re.sub(r"\{#.*?#\}", "", text, flags=re.DOTALL)
        # Strip HTML comments <!-- ... -->.
        text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
        # Strip <script> / <style> blocks; verdict-phrase policing does
        # not apply to CSS class names, JS variable names, or JS
        # comments about "not faking a pulse".
        text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
        for i, line in enumerate(text.splitlines(), 1):
            yield path.relative_to(REPO_ROOT), i, line


@pytest.mark.parametrize("pattern", FORBIDDEN_VERDICT_PHRASES)
def test_no_verdict_phrase_in_templates(pattern):
    rx = re.compile(pattern, re.IGNORECASE)
    hits = []
    for path, lineno, line in _iter_template_lines():
        if rx.search(line):
            hits.append(f"{path}:{lineno}: {line.strip()[:120]}")
    assert not hits, (
        f"Verdict phrase /{pattern}/ appeared in template copy:\n"
        + "\n".join(hits)
        + "\n\nReplace with positive-knowledge wording — see test header."
    )
