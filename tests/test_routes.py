"""Route-level smoke tests.

Each public-facing route must return a successful response and render
without leaking Jinja syntax or losing the legal disclaimer. These
tests are the canary for "did a refactor break a page" — they don't
assert specific copy, only structural invariants.
"""

import re

import pytest


PUBLIC_PAGES = [
    "/",
    "/health",
    "/whales",
    "/tokens",
    "/about",
    "/institutional",
    "/pools",
    "/cold-storage",
]


@pytest.mark.parametrize("path", PUBLIC_PAGES)
def test_public_page_returns_200(client, path):
    r = client.get(path)
    assert r.status_code == 200, f"{path} returned {r.status_code}"
    assert r.data, f"{path} returned empty body"


@pytest.mark.parametrize("path", PUBLIC_PAGES)
def test_public_page_has_disclaimer(client, path):
    """Every public page must carry the not-financial-advice line.
    Loss of this is a compliance regression, not a copy choice."""
    body = client.get(path).data.decode()
    assert "Not financial advice" in body, f"{path} missing disclaimer"


@pytest.mark.parametrize("path", PUBLIC_PAGES)
def test_public_page_no_jinja_leakage(client, path):
    """Unrendered {{ ... }} or {% ... %} in output means a context var
    was missing or a template tag was malformed."""
    body = client.get(path).data.decode()
    assert not re.search(r"\{\{[^}]*\}\}", body), f"{path} leaks {{{{...}}}}"
    assert not re.search(r"\{%[^%]*%\}", body), f"{path} leaks {{%...%}}"
    assert "jinja2.exceptions" not in body, f"{path} contains jinja exception"


def test_v2_redirects(client):
    r = client.get("/v2")
    assert r.status_code in (301, 302)


def test_lookup_redirects_to_pools(client):
    """/lookup is a legacy route — redirects unconditionally to /pools.
    Both with and without a query string, since the param is ignored."""
    for path in ("/lookup", "/lookup?q=anything"):
        r = client.get(path)
        assert r.status_code in (301, 302), f"{path} did not redirect"
        assert r.headers.get("Location", "").endswith("/pools")


def test_healthz_ok(client):
    r = client.get("/healthz")
    assert r.status_code == 200


def test_api_ledger_tip_returns_json(client):
    r = client.get("/api/ledger-tip")
    assert r.status_code == 200
    assert r.is_json or r.headers.get("Content-Type", "").startswith("application/json")


def test_wallet_valid_address_renders(client, known_wallet):
    r = client.get(f"/wallet/{known_wallet}")
    assert r.status_code == 200
    body = r.data.decode()
    assert "Not financial advice" in body
    # Address-short pattern (rXxxxx…XXXX) should appear in the title or page
    assert "…" in body, "expected truncated address rendering somewhere on page"


def test_wallet_invalid_address_404(client):
    """Malformed addresses render the branded 404, not a zero-filled wallet
    view — zeros would silently misrepresent a bad address as an empty
    wallet, which is misleading on a page that doubles as an integrity check."""
    r = client.get("/wallet/notARealAddress")
    assert r.status_code == 404
    body = r.data.decode()
    assert "404" in body
    assert "isn't here" in body or "not found" in body.lower()


def test_wallet_title_no_longer_says_hybrid_mock(client, known_wallet):
    """Regression: production wallet detail page must not show the dev
    title 'wallet preview (hybrid mock)' that was leftover from mock work."""
    body = client.get(f"/wallet/{known_wallet}").data.decode()
    title_match = re.search(r"<title>([^<]+)</title>", body)
    assert title_match, "wallet page missing <title>"
    assert "hybrid mock" not in title_match.group(1).lower()
    assert "wallet" in title_match.group(1).lower()


def test_token_valid_renders(client, known_token):
    currency, issuer = known_token
    r = client.get(f"/token/{currency}/{issuer}")
    assert r.status_code == 200
    assert "Not financial advice" in r.data.decode()


def test_token_invalid_currency_404(client, known_wallet):
    """Malformed currency renders the branded 404, not a zero-filled token
    view. The old stub rendering echoed raw `currency`/`issuer` into the
    page title, hero, and external explorer URLs — visible reflection of
    attacker-supplied input. Mirrors the wallet route's invalid-input handling."""
    r = client.get(f"/token/!!!!/{known_wallet}")
    assert r.status_code == 404
    assert "isn't here" in r.data.decode() or "not found" in r.data.decode().lower()


def test_token_invalid_issuer_404(client):
    r = client.get("/token/USD/notAnAddress")
    assert r.status_code == 404
    assert "isn't here" in r.data.decode() or "not found" in r.data.decode().lower()


def test_about_links_to_institutional(client):
    """The institutional tier reference in /about must be a live link
    to /institutional — that's the only nav path to the page by design."""
    body = client.get("/about").data.decode()
    assert 'href="/institutional"' in body


def test_institutional_has_mailto_with_prefilled_subject(client):
    """The mailto is the page's only conversion path. Subject and body
    must remain pre-filled or we lose the partner-prompting structure."""
    body = client.get("/institutional").data.decode()
    assert "mailto:contact@xrpldashboard.com" in body
    assert "subject=" in body.lower()
    assert "body=" in body.lower()


def test_institutional_not_in_shared_nav(client):
    """Per launch positioning: institutional must be discoverable from
    /about only, never from the global nav. Regression test: if anyone
    adds it to the shared _nav.html or any page's top-of-page nav block,
    this fails fast."""
    # Sample a page that uses the shared _nav.html include + a page with
    # its own nav block, plus the homepage's nav-strip.
    for path in ("/health", "/about", "/"):
        body = client.get(path).data.decode()
        # Match either pattern: <div class="nav"> or <nav class="nav-strip">
        nav_blocks = re.findall(
            r'<(?:div\s+class="nav"|nav\s+class="nav-strip")[^>]*>.*?</(?:div|nav)>',
            body, flags=re.S,
        )
        assert nav_blocks, f"{path} missing recognizable nav block"
        for block in nav_blocks:
            assert "/institutional" not in block, (
                f"{path} shared nav now links to /institutional"
            )


def test_unknown_route_404(client):
    r = client.get("/this-route-does-not-exist")
    assert r.status_code == 404


# ─────────────────────────────────────────────────────────────────────
# /connect — 60-second onboarding page (Distribution Day 2026-08-05)
# ─────────────────────────────────────────────────────────────────────
#
# Guard test for the page /agents.json.mcp_servers[0].connect_docs and the
# directory-submission entries (Anthropic, Smithery, glama.ai) all point
# at. If any of these anchors moves, the URL guard in
# tests/test_claim_envelope_urls_resolve.py catches the resolver-side
# break; this test catches the content-side break — the page still
# carries the config snippet, the three sample prompts, the beta window,
# the enforced-limit copy, and the stable #connect-in-60-seconds anchor.

def test_connect_page_returns_200(client):
    r = client.get("/connect")
    assert r.status_code == 200


def test_connect_carries_60_second_anchor(client):
    body = client.get("/connect").data.decode()
    assert 'id="connect-in-60-seconds"' in body, (
        "/connect must keep the stable #connect-in-60-seconds anchor — "
        "agents.json, llms.txt, and the directory-submission specs all "
        "point at it."
    )


def test_connect_names_public_url(client):
    body = client.get("/connect").data.decode()
    assert "https://mcp.xrpldashboard.com/mcp" in body, (
        "/connect must name the live public MCP URL verbatim"
    )


def test_connect_ships_config_snippet(client):
    """The mcp-remote bridge config is the one dogfooded on 2026-08-05.
    The snippet must be present in a copy-paste-ready form. Per the
    snippets law: publish nothing you haven't verified against the URL."""
    body = client.get("/connect").data.decode()
    assert 'mcp-remote@latest' in body
    assert '"mcpServers"' in body
    assert '"xrpldashboard"' in body


def test_connect_three_sample_prompts(client):
    """Primitive / aggregation / verify — the demo shape.
    The verify prompt is the moat demo (signed snapshot + pubkey pin);
    if it's not on the page a fresh user has no path to the differentiator.
    """
    body = client.get("/connect").data.decode()
    assert "get_ledger_stats" in body, "primitive-tool sample prompt missing"
    assert "get_rlusd_supply" in body, "aggregation-tool sample prompt missing"
    assert "verify_snapshot_signature" in body, "verify (moat) sample prompt missing"


def test_connect_states_beta_window_and_limit(client):
    """The public-beta-through-2026-09 label and the enforced 600/hr line
    are both promises made on /agents.json and /llms.txt. /connect must
    carry the same copy in running-behavior tense."""
    body = client.get("/connect").data.decode()
    assert "2026-09" in body, "/connect missing beta-window date"
    assert "600" in body, "/connect missing rate-limit number"
    assert "429" in body or "Retry-After" in body, (
        "/connect missing the honest 429 + Retry-After copy — enforcement "
        "shape has to match /agents.json.rate_limits.mcp_session"
    )


def test_connect_carries_disclaimer(client):
    """Same class-of-page invariant every public page enforces."""
    body = client.get("/connect").data.decode()
    assert "Not financial advice" in body


def test_connect_no_jinja_leakage(client):
    """Same class-of-page invariant every public page enforces."""
    body = client.get("/connect").data.decode()
    assert not re.search(r"\{\{[^}]*\}\}", body), "/connect leaks {{...}}"
    assert not re.search(r"\{%[^%]*%\}", body), "/connect leaks {%...%}"


@pytest.mark.parametrize("path", PUBLIC_PAGES)
def test_public_page_has_og_tags(client, path):
    """Every public page must carry Open Graph + Twitter card tags so
    launch-day shares to X / Discord / Telegram / Reddit unfurl with a
    proper title and description, not a bare URL."""
    body = client.get(path).data.decode()
    assert 'property="og:title"' in body, f"{path} missing og:title"
    assert 'property="og:description"' in body, f"{path} missing og:description"
    assert 'property="og:url"' in body, f"{path} missing og:url"
    assert 'property="og:type"' in body, f"{path} missing og:type"
    assert 'property="og:image"' in body, f"{path} missing og:image"
    assert 'name="twitter:card"' in body, f"{path} missing twitter:card"
    # We use the large-image card variant — confirm it's not the bare summary.
    assert 'content="summary_large_image"' in body, (
        f"{path} not using summary_large_image card"
    )
    assert 'rel="canonical"' in body, f"{path} missing canonical link"
    assert 'name="description"' in body, f"{path} missing meta description"


def test_og_image_asset_exists():
    """The committed OG image must be present at static/og-image.png so
    every social unfurl resolves. If this fails, regenerate via
    scripts/generate_og_image.py and commit the PNG."""
    import os
    here = os.path.dirname(os.path.abspath(__file__))
    img = os.path.join(os.path.dirname(here), "static", "og-image.png")
    assert os.path.exists(img), f"missing OG image at {img}"
    # Sanity: the image isn't a 0-byte placeholder.
    assert os.path.getsize(img) > 5_000, "OG image is suspiciously small"


def test_og_image_served_via_static(client):
    """Flask's default /static/ handler must serve the OG image so social
    crawlers can fetch it. Regression guard for accidental static dir
    relocation."""
    r = client.get("/static/og-image.png")
    assert r.status_code == 200
    assert r.headers.get("Content-Type", "").startswith("image/")


@pytest.mark.parametrize(
    "asset",
    [
        "/static/favicon.svg",
        "/static/favicon-32.png",
        "/static/favicon-16.png",
        "/static/apple-touch-icon.png",
    ],
)
def test_favicon_assets_served(client, asset):
    """Each favicon variant must be reachable. A blank tab on launch day
    is a small but visible 'this isn't finished' signal."""
    r = client.get(asset)
    assert r.status_code == 200, f"{asset} returned {r.status_code}"
    ct = r.headers.get("Content-Type", "")
    assert ct.startswith("image/"), f"{asset} wrong content-type: {ct}"


@pytest.mark.parametrize("path", PUBLIC_PAGES)
def test_public_page_links_favicon(client, path):
    """Every public page's <head> must reference the favicon, otherwise
    browser tabs render the blank-page icon."""
    body = client.get(path).data.decode()
    assert 'rel="icon"' in body, f"{path} missing <link rel='icon'>"
    assert "/static/favicon.svg" in body, f"{path} missing svg favicon link"
    assert "apple-touch-icon" in body, f"{path} missing apple-touch-icon link"


@pytest.mark.parametrize("path", ["/", "/about", "/health", "/api/ledger-tip", "/healthz"])
def test_security_headers_on_every_response(client, path):
    """Security headers apply to all responses, not just HTML pages —
    JSON endpoints get them too. If any header is absent, the after_request
    hook regressed."""
    r = client.get(path)
    assert r.headers.get("X-Content-Type-Options") == "nosniff", (
        f"{path} missing/wrong X-Content-Type-Options"
    )
    assert r.headers.get("X-Frame-Options") == "DENY", (
        f"{path} missing/wrong X-Frame-Options"
    )
    assert r.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin", (
        f"{path} missing/wrong Referrer-Policy"
    )


def test_robots_txt_serves_plaintext(client):
    r = client.get("/robots.txt")
    assert r.status_code == 200
    assert r.headers.get("Content-Type", "").startswith("text/plain")
    body = r.data.decode()
    assert "User-agent: *" in body
    assert "Sitemap:" in body
    # Detail-page space must stay disallowed — unbounded URL space.
    assert "Disallow: /wallet/" in body
    assert "Disallow: /token/" in body
    # Operational endpoints excluded.
    assert "Disallow: /healthz" in body
    assert "Disallow: /api/" in body


def test_sitemap_xml_lists_public_pages(client):
    r = client.get("/sitemap.xml")
    assert r.status_code == 200
    assert "xml" in r.headers.get("Content-Type", "").lower()
    body = r.data.decode()
    assert body.startswith('<?xml')
    # Every page in PUBLIC_PAGES must be in the sitemap.
    for path in PUBLIC_PAGES:
        # Match either the bare path with trailing tag or with the host prefix.
        assert f"{path}</loc>" in body or f"{path}\n" in body, (
            f"sitemap missing {path}"
        )
    # Unbounded detail pages must NOT be in the sitemap.
    assert "/wallet/" not in body
    assert "/token/" not in body


def test_404_page_is_branded(client):
    """The 404 must keep the visitor on-site (brand bar, navigation back
    into public pages) rather than dump them at Flask's default page."""
    r = client.get("/this-page-truly-does-not-exist")
    assert r.status_code == 404
    body = r.data.decode()
    assert "xrpl" in body.lower() and "dashboard" in body.lower()
    # Branded copy from templates/404.html
    assert "That page isn" in body
    # Navigation back into the site
    assert 'href="/"' in body
    # 404s shouldn't be indexed
    assert 'name="robots"' in body and "noindex" in body
