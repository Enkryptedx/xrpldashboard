"""Every URL referenced in the copy-pasteable snippet block must resolve.

Sibling to `test_claim_envelope_urls_resolve.py` — same idea (a citation
contract is only as good as the URL still working) applied to a different
surface: the "Use this data" block on /claims and the pointer to it from
/methodology and /llms.txt.

Founding case: the client snippets (curl/Python/JS + Ed25519 verifier)
shipped 2026-08-04 as the client-side follow-up to Grok red-team #1.
A broken example on the site that preaches verification would be Red
Team finding #2 waiting to happen — so the URLs the snippets fetch,
and the anchors that point at them, are guarded here.

Guarded:
  - `/claims#use-this-data`               (primary home of the snippets)
  - `/methodology#use-this-data-methodology` (pointer into /claims)
  - `/claims/xrpl.rlusd.xrpl_supply.json` (Tier-1 target)
  - `/.well-known/snapshots/chain.json`   (Tier-2 chain index)
  - `/.well-known/snapshots/pubkey.pem`   (Tier-2 pubkey)
  - `/llms.txt`                            (pointer surface)
"""

from __future__ import annotations

import pytest


SNIPPET_ANCHORS = [
    ("/claims", "use-this-data"),
    ("/methodology", "use-this-data-methodology"),
]

SNIPPET_ROUTES = [
    "/claims/xrpl.rlusd.xrpl_supply.json",
    "/.well-known/snapshots/chain.json",
    "/.well-known/snapshots/pubkey.pem",
    "/llms.txt",
]


@pytest.mark.parametrize("path,anchor", SNIPPET_ANCHORS)
def test_snippet_anchor_exists(client, path, anchor):
    body = client.get(path).data.decode(errors="replace")
    assert (
        f'id="{anchor}"' in body or f"id='{anchor}'" in body
    ), (
        f"snippet block anchor #{anchor} missing on {path} — rename "
        f"cascades to /methodology + /llms.txt + this test"
    )


@pytest.mark.parametrize("path", SNIPPET_ROUTES)
def test_snippet_route_resolves(client, path):
    r = client.get(path)
    assert r.status_code != 404, (
        f"snippet fetches {path} but route returns 404 — "
        f"a broken example on the site that preaches verification "
        f"would be the next Red Team finding"
    )


def test_llms_txt_points_at_use_this_data(client):
    body = client.get("/llms.txt").data.decode(errors="replace")
    assert "/claims#use-this-data" in body, (
        "llms.txt lost its pointer to the client snippets — "
        "agents crawl llms.txt first, so this is the primary onramp"
    )
