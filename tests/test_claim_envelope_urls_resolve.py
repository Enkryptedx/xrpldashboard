"""Every URL every claim envelope emits must resolve.

Founding case: Grok red-team #1 (2026-08-04) — the `walker_health` field
in every claim's `verification` block hardcoded `SITE_URL/walker_health`,
which is a localhost-only admin route that aborts 404 for external
requests. All 110 claim envelopes cited a dead verification URL.

Same class as the /methodology anchor bug caught by the anchor sweep
(and same class as the /whales whale-tier constant collision). The
proof-layer isn't allowed to point at things that don't exist.

Guard shape:

  1. Walk every claim envelope produced by `claim_json` + `index_json` +
     `all_claims_json` — collecting every value that looks like a URL.
  2. For each internal URL (starts with SITE_URL): route-resolve via
     Flask test client and assert not 404.
  3. For URLs carrying a fragment (`#anchor`): additionally fetch the
     page and assert `id="<anchor>"` or `name="<anchor>"` appears in the
     rendered HTML — silent rename of an anchor breaks a citation
     contract the same way a route rename does.

Extending PAGE_URL_OVERRIDES in claims_endpoint.py is safe as long as
this test still passes; that's the contract."""

from __future__ import annotations

import re
from urllib.parse import urlparse

import pytest

import claims_endpoint


SITE_URL = "https://xrpldashboard.com"
METHODOLOGY_URL = f"{SITE_URL}/methodology#for-ai-agents"

# Anything else is an external URL (github etc.) — pattern-check only.
EXTERNAL_URL_RE = re.compile(r"^https?://[^\s/$.?#].[^\s]*$")


def _collect_urls(node, out: list[str]) -> None:
    """Recursively walk any JSON-shaped structure and collect every
    string that looks like a URL."""
    if isinstance(node, dict):
        for v in node.values():
            _collect_urls(v, out)
    elif isinstance(node, list):
        for v in node:
            _collect_urls(v, out)
    elif isinstance(node, str) and node.startswith(("http://", "https://")):
        out.append(node)


def _every_emitted_url() -> set[str]:
    """Union of every URL any claims-endpoint response builder emits."""
    urls: list[str] = []
    for entry in claims_endpoint.all_claims():
        _collect_urls(claims_endpoint.claim_json(entry, SITE_URL, METHODOLOGY_URL), urls)
    _collect_urls(claims_endpoint.index_json(SITE_URL, METHODOLOGY_URL), urls)
    return set(urls)


ALL_URLS = sorted(_every_emitted_url())
INTERNAL_URLS = sorted(u for u in ALL_URLS if u.startswith(SITE_URL))
EXTERNAL_URLS = sorted(u for u in ALL_URLS if not u.startswith(SITE_URL))


@pytest.mark.parametrize("url", INTERNAL_URLS)
def test_internal_url_resolves(client, url):
    """Every internal URL in a claim envelope must route-resolve
    (not 404). Fragment is stripped for the route check — anchor
    presence is asserted separately below."""
    parsed = urlparse(url)
    path = parsed.path or "/"
    r = client.get(path)
    assert r.status_code != 404, (
        f"claim envelope cites {url} but path {path} returns 404 — "
        f"add to PAGE_URL_OVERRIDES in claims_endpoint.py or fix the route"
    )


@pytest.mark.parametrize(
    "url",
    sorted(u for u in INTERNAL_URLS if "#" in u),
)
def test_internal_url_fragment_anchor_exists(client, url):
    """If a claim envelope URL carries `#anchor`, the target page must
    render an element with that id (or name). Silent anchor rename is
    the same failure class as silent route rename."""
    parsed = urlparse(url)
    path = parsed.path or "/"
    anchor = parsed.fragment
    body = client.get(path).data.decode(errors="replace")
    assert (
        f'id="{anchor}"' in body or f"id='{anchor}'" in body
        or f'name="{anchor}"' in body or f"name='{anchor}'" in body
    ), (
        f"claim envelope cites {url} but page {path} has no id/name "
        f"='{anchor}' — anchor was renamed or removed"
    )


@pytest.mark.parametrize("url", EXTERNAL_URLS)
def test_external_url_is_well_formed(url):
    """External URLs (github blob, etc.) can't be existence-checked in
    CI without network flake, but they must at least be well-formed."""
    assert EXTERNAL_URL_RE.match(url), f"malformed external URL in envelope: {url}"


def test_at_least_one_url_emitted():
    """Sanity: the collector must actually find URLs. Guards against
    a refactor that silently stops emitting the verification block."""
    assert INTERNAL_URLS, "no internal URLs collected from claim envelopes"
    assert EXTERNAL_URLS, "no external URLs collected from claim envelopes"
