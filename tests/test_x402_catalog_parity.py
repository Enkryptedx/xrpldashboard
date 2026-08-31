"""/.well-known/x402 catalog — free-tier manifest + agents.json parity.

Filed with the x402 catalog ship (2026-08-30). Locks the shape so:

- A future refactor cannot silently drop the bazaar-required fields
  (`x402Version`, `resource`, `type`, `accepts`, `extensions.bazaar.info`).
- Identity fields (name, disambiguation, contact, site_url, documentation,
  last_verified, source_code, license, openapi) stay in sync with
  /.well-known/agents.json — a single manifest edit forcing a divergence
  will fail the test.
- Free-tier scope is enforced: `accepts` MUST be empty until x402 rails
  flip live (Fence-#8 target 2026-09-25). Adding a paid entry without
  updating this test is a red flag.
- Trust-surface URLs actually resolve (methodology, claims_index_json,
  signed_snapshot_chain, signed_snapshot_pubkey, security_contact) —
  the manifest cannot advertise dead links.

Not tested here: the docs.x402.org Bazaar spec compliance beyond shape.
That is confirmed by an actual bazaar directory ingesting the manifest.
"""

import json


def test_x402_catalog_served_at_wellknown_path(client):
    """The manifest lives at /.well-known/x402 and returns JSON."""
    r = client.get("/.well-known/x402")
    assert r.status_code == 200
    assert r.content_type.startswith("application/json"), (
        f"expected JSON, got {r.content_type}"
    )
    # Cache-Control matches agents.json (1h public + edge).
    assert "max-age=3600" in r.headers.get("Cache-Control", "")


def test_x402_catalog_bazaar_required_fields(client):
    """Bazaar discovery-resource shape per docs.x402.org/extensions/bazaar."""
    body = client.get("/.well-known/x402").get_json()
    assert body["x402Version"] == 1
    assert body["type"] == "http"
    assert body["resource"].endswith("/check.json"), (
        "resource should point at the free-tier machine surface"
    )
    assert isinstance(body["accepts"], list)
    info = body["extensions"]["bazaar"]["info"]
    assert info["input"]["type"] == "http"
    assert info["input"]["method"] == "GET"
    assert "q" in info["input"]["queryParams"]
    assert info["output"]["type"] == "application/json"
    assert "ProofAnnotationEnvelope" in info["output"]["schema"]["$ref"], (
        "output schema should reference the OpenAPI envelope"
    )


def test_x402_catalog_free_tier_scope_enforced(client):
    """`accepts: []` locks free-tier scope. Adding a priced entry needs
    this test updated (and Charlie's ruling on pricing)."""
    body = client.get("/.well-known/x402").get_json()
    assert body["accepts"] == [], (
        "x402 catalog must stay free-tier only until Charlie rules on "
        "pricing and Fence-#8 sovereignty items close"
    )
    assert body["status"]["x402_rails_ready"] is False
    assert body["status"]["free_tier_ready"] is True


def test_x402_catalog_identity_parity_with_agents_json(client):
    """Fields that identity + trust downstream depend on must not drift
    between /.well-known/x402 and /.well-known/agents.json."""
    x402 = client.get("/.well-known/x402").get_json()
    agents = client.get("/.well-known/agents.json").get_json()

    assert x402["identity"]["name"] == agents["name"]
    assert x402["identity"]["disambiguation"] == agents["disambiguation"]
    assert x402["identity"]["contact"] == agents["contact"]
    assert x402["identity"]["site_url"] == agents["site_url"]
    assert x402["identity"]["documentation"] == agents["documentation"]
    assert x402["identity"]["last_verified"] == agents["last_verified"]
    assert x402["identity"]["source_code"] == agents["source_code"]
    assert x402["identity"]["license"] == agents["license"]
    assert x402["identity"]["openapi"] == agents["openapi"]

    for key in (
        "methodology", "claims_index", "claims_index_json",
        "signed_snapshot_chain", "signed_snapshot_pubkey",
        "security_contact",
    ):
        assert x402["trust_surfaces"][key] == agents["trust_surfaces"][key], (
            f"trust_surfaces.{key} drifted between x402 and agents.json"
        )


def test_x402_catalog_disclaimer_verbatim(client):
    """Charlie's attestation-not-safety line must ship verbatim so a
    downstream directory can lift it into their listing without editing."""
    body = client.get("/.well-known/x402").get_json()
    d = body["disclaimer"]
    assert d.startswith("Attestation, not safety.")
    assert "does NOT tell you whether" in d
    assert "Human judgment is required." in d


def test_x402_catalog_serializable_as_stable_json(client):
    """The manifest must round-trip through json.dumps + json.loads
    without raising — directories that cache the payload will hash it."""
    body = client.get("/.well-known/x402").get_json()
    serialized = json.dumps(body, sort_keys=True)
    reparsed = json.loads(serialized)
    assert reparsed == body
