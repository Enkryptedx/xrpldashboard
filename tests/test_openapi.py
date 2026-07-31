"""Day 5 OpenAPI decoration tests.

The spec at /openapi.json is the machine-readable index of xrpldashboard's
LIVE free surface. Every claim it makes must hold true for the running
server — if these tests pass, the spec is honest about what actually ships.

Fences (mirrored from the OpenAPI init block in app.py):
  • The Api instance is used ONLY for /openapi.json + /docs + spec metadata.
    No smorest Blueprint is registered. Existing @app.errorhandler(404)/(500)
    HTML handlers must stay dominant — test_error_handlers_still_html guards
    that.
  • agents.json.openapi_ready must be True ONLY when /openapi.json serves 200
    — test_agents_json_openapi_ready_matches_reality guards that.
  • MCP inventory count in the spec must match the actual registered tools
    in mcp_server — test_mcp_inventory_count_matches_server guards that.
"""

import json

import pytest


def test_openapi_json_returns_200(client):
    """The one contract that lets an agent trust everything else."""
    r = client.get("/openapi.json")
    assert r.status_code == 200
    assert r.content_type == "application/json"


def test_openapi_json_is_valid_openapi_3(client):
    spec = client.get("/openapi.json").get_json()
    assert spec["openapi"].startswith("3."), spec["openapi"]
    assert spec["info"]["title"] == "xrpldashboard — Agent Tier (read-only)"
    assert spec["info"]["version"] == "v1"


def test_docs_returns_swagger_ui(client):
    r = client.get("/docs")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    # Swagger UI page contains the swagger-ui div/script boilerplate.
    assert "swagger-ui" in body.lower()


def test_agents_json_openapi_ready_matches_reality(client):
    """agents.json declares openapi_ready=True. That must be honest —
    /openapi.json must actually serve 200. If this test fails, we're
    telling agents the spec is live when it isn't."""
    aj = client.get("/.well-known/agents.json").get_json()
    if aj["status"]["openapi_ready"]:
        r = client.get("/openapi.json")
        assert r.status_code == 200, (
            "agents.json says openapi_ready=True but /openapi.json failed"
        )
        assert aj["openapi"] and aj["openapi"].endswith("/openapi.json"), (
            f"agents.json.openapi should point to /openapi.json, got {aj['openapi']!r}"
        )
    else:
        # Belt-and-braces: if openapi_ready is False, the URL field
        # should not be a live pointer either.
        assert aj["openapi"] in (None, ""), (
            f"openapi_ready=False but openapi URL is set: {aj['openapi']!r}"
        )


def test_agents_json_mcp_ready_still_false(client):
    """MCP daemon is deferred to post-Lenovo-migration Phase 3. Until
    then, mcp_ready must stay False regardless of what else ships."""
    aj = client.get("/.well-known/agents.json").get_json()
    assert aj["status"]["mcp_ready"] is False


def test_llms_txt_has_openapi_reference(client):
    """Day 5 flip: llms.txt must point at the live OpenAPI spec.
    Discovery-layer invariant per docs/AGENT_TIER_DESIGN.md."""
    body = client.get("/llms.txt").get_data(as_text=True)
    assert "openapi.json" in body, "llms.txt missing /openapi.json reference"
    assert "/docs" in body, "llms.txt missing /docs reference"


def test_proof_annotation_envelope_schema_present(client):
    """The envelope schema is the standard response wrapper for every
    MCP tool (today) and every future JSON endpoint. Missing = the
    spec has lost its headline promise."""
    spec = client.get("/openapi.json").get_json()
    schemas = spec.get("components", {}).get("schemas", {})
    assert "ProofAnnotationEnvelope" in schemas
    env = schemas["ProofAnnotationEnvelope"]
    # Required fields on the top-level envelope.
    assert set(env["required"]) == {"data", "proof", "server"}
    # Required fields on the proof block — these are the receipts.
    proof = env["properties"]["proof"]
    assert "source" in proof["required"]
    assert "as_of" in proof["required"]
    assert "freshness_contract" in proof["required"]
    assert "methodology_url" in proof["required"]
    # Freshness enum must match mcp_server.VALID_FRESHNESS_CONTRACTS.
    from mcp_server import VALID_FRESHNESS_CONTRACTS
    spec_enum = set(proof["properties"]["freshness_contract"]["enum"])
    assert spec_enum == VALID_FRESHNESS_CONTRACTS, (
        f"envelope schema enum {spec_enum} != mcp_server "
        f"VALID_FRESHNESS_CONTRACTS {VALID_FRESHNESS_CONTRACTS}"
    )
    # Cross-check enum must also match.
    from mcp_server import VALID_CROSS_CHECK_STATUSES
    spec_cc = set(proof["properties"]["cross_check_status"]["enum"])
    assert spec_cc == VALID_CROSS_CHECK_STATUSES


def test_mcp_inventory_count_matches_server(client):
    """The static AGENT_TIER_MCP_INVENTORY in app.py must stay in sync
    with the actual number of tools mcp_server registers. Divergence
    means the spec is lying about what agents can call."""
    spec = client.get("/openapi.json").get_json()
    inventory = spec["info"]["x-mcp-tools"]
    declared = inventory["tool_count"]
    listed = len(inventory["tools"])
    assert declared == listed, (
        f"x-mcp-tools.tool_count={declared} but tools list has {listed}"
    )

    # Compare against the live mcp_server tool registration.
    from unittest.mock import MagicMock
    import mcp_server
    fake_mcp = MagicMock()
    n = mcp_server._register_tools(fake_mcp)
    assert declared == n, (
        f"OpenAPI declares {declared} MCP tools but "
        f"mcp_server._register_tools registered {n}"
    )


def test_mcp_inventory_tool_names_match_actual_functions():
    """Every tool named in AGENT_TIER_MCP_INVENTORY must exist as a
    tool_<name> function in one of the mcp_tools_* modules. Renamed or
    deleted tools must be reflected in the inventory."""
    from app import AGENT_TIER_MCP_INVENTORY
    import mcp_tools_ledger
    import mcp_tools_value_flows
    import mcp_tools_amm_tokens

    modules = [mcp_tools_ledger, mcp_tools_value_flows, mcp_tools_amm_tokens]
    for tool in AGENT_TIER_MCP_INVENTORY:
        fn_name = f"tool_{tool['name']}"
        found = any(hasattr(m, fn_name) for m in modules)
        assert found, (
            f"OpenAPI inventory references tool {tool['name']!r} but no "
            f"{fn_name}() exists in mcp_tools_*.py"
        )


def test_documented_paths_all_serve_200(client):
    """Every path documented in the spec must actually respond. This
    catches the "documented but doesn't exist" failure mode where an
    endpoint gets removed but its spec entry survives."""
    spec = client.get("/openapi.json").get_json()
    # Only test paths that are static (no {param}) — dynamic paths
    # need per-endpoint fixtures.
    for path in spec["paths"]:
        if "{" in path:
            continue
        r = client.get(path)
        assert r.status_code == 200, (
            f"spec documents {path} but GET returned {r.status_code}"
        )


def test_error_handlers_still_html(client):
    """flask-smorest's Api can install JSON error handlers that clobber
    the existing branded 404/500 templates. This test guards against
    that regression — 404s must stay HTML per the /methodology page's
    'humans never friction' rule."""
    r = client.get("/definitely-not-a-real-page-xyz-day5-test")
    assert r.status_code == 404
    assert r.content_type.startswith("text/html"), (
        f"404 returned {r.content_type} — smorest may have clobbered "
        "the HTML error handler"
    )
    # And the actual template renders (not a bare "404 Not Found").
    body = r.get_data(as_text=True)
    assert len(body) > 200, "404 body suspiciously short — template may not have rendered"


def test_freshness_stamp_matches_agents_json(client):
    """LAST_VERIFIED_AGENT_TIER_METHODOLOGY drives three surfaces:
    llms.txt, agents.json, and now openapi.json's x-agent-tier-freshness.
    They must show the same date — bumping the constant refreshes all
    three from one edit (the single-source-of-truth rule)."""
    aj = client.get("/.well-known/agents.json").get_json()
    spec = client.get("/openapi.json").get_json()
    aj_last = aj["last_verified"]
    spec_last = spec["info"]["x-agent-tier-freshness"]["last_verified"]
    assert aj_last == spec_last, (
        f"agents.json last_verified={aj_last!r} != "
        f"openapi.json x-agent-tier-freshness.last_verified={spec_last!r}"
    )
