"""Batch-2 /check content-negotiation tests.

The /check endpoint historically returned HTML only. Grok's 2026-08-26
QUADFECTA audit flagged the missing machine representation and ChatGPT
timed out on the URL form. Fix (2026-08-27):
  - Tightened external-lookup timeouts (D3 5→2s, RDAP 6→3s, crt.sh 8→4s)
    so browsers keep their sub-second render budget and machine fetchers
    stop timing out at ~10s.
  - Added Accept-header content negotiation: `Accept: application/json`
    returns the proof-annotation envelope wrapping the same result dict.

These tests lock the shape so a future template refactor can't quietly
drop JSON support and re-open the honesty defect.
"""

import json


def test_check_empty_default_is_html(client):
    """Empty query on / behaves as the paste-box landing page. No Accept
    header on a browser hit → HTML."""
    r = client.get("/check")
    assert r.status_code == 200
    assert r.content_type.startswith("text/html")


def test_check_json_negotiation_returns_envelope_for_wallet(client, known_wallet):
    """Accept: application/json on a wallet lookup returns a proof-
    annotation envelope with the wallet's data in `data`. Browser
    Accept-shape stays HTML."""
    r = client.get(
        f"/check?q={known_wallet}",
        headers={"Accept": "application/json"},
    )
    assert r.status_code == 200
    assert r.content_type.startswith("application/json"), (
        f"expected JSON body, got {r.content_type}"
    )
    body = r.get_json()

    # Envelope shape — same three top-level keys every MCP tool returns.
    assert set(body.keys()) == {"data", "proof", "server"}, (
        f"envelope top-level keys drifted: {sorted(body.keys())}"
    )
    proof = body["proof"]
    for key in (
        "source", "as_of", "freshness_contract", "methodology_url",
        "cross_check_status", "honest_partial",
    ):
        assert key in proof, f"proof missing key {key!r}"
    assert proof["source"] == "xrpldashboard/check-endpoint"
    assert proof["freshness_contract"] == "≤ 5min"
    assert proof["methodology_url"].startswith("https://xrpldashboard.com/")

    # data — a check_address() result carrying the honest signal shape.
    data = body["data"]
    assert data["kind"] == "wallet"
    assert data["address"] == known_wallet
    assert data["tier"] in {"verified", "self", "bare"}
    assert isinstance(data["signals"], list) and data["signals"], (
        "signals list should carry at least one entry (identity + on-chain)"
    )


def test_check_json_negotiation_html_still_default(client, known_wallet):
    """With no Accept header (or */* only), the wallet lookup still
    renders the HTML paste-box. Browsers must not be forced into JSON."""
    r = client.get(f"/check?q={known_wallet}")
    assert r.status_code == 200
    assert r.content_type.startswith("text/html"), (
        f"expected HTML fallthrough for browser Accept, got {r.content_type}"
    )


def test_check_json_negotiation_html_pref_wins(client, known_wallet):
    """A real browser Accept string like `text/html,application/xhtml+xml,
    application/xml;q=0.9,image/webp,*/*;q=0.8` picks HTML — content
    negotiation must NOT quietly flip to JSON just because JSON is in
    the list at lower quality."""
    r = client.get(
        f"/check?q={known_wallet}",
        headers={
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/webp,*/*;q=0.8"
            )
        },
    )
    assert r.status_code == 200
    assert r.content_type.startswith("text/html")


def test_check_json_negotiation_400_on_bad_input(client):
    """A machine fetcher sending Accept: application/json to /check with
    a garbage query gets a structured JSON error, not HTML. Charlie's
    machine-representation rule: agents get a machine signal on every
    branch, not just the happy path."""
    r = client.get(
        "/check?q=not-a-real-anything",
        headers={"Accept": "application/json"},
    )
    assert r.status_code == 400
    assert r.content_type.startswith("application/json")
    body = r.get_json()
    assert "error" in body
    assert body.get("query") == "not-a-real-anything"


def test_check_json_negotiation_envelope_valid_enums(client, known_wallet):
    """The envelope's freshness_contract and cross_check_status must be
    members of the canonical enums so mcp-directory-side validators
    accepting our schema don't reject the response."""
    from mcp_server import VALID_FRESHNESS_CONTRACTS, VALID_CROSS_CHECK_STATUSES
    r = client.get(
        f"/check?q={known_wallet}",
        headers={"Accept": "application/json"},
    )
    proof = r.get_json()["proof"]
    assert proof["freshness_contract"] in VALID_FRESHNESS_CONTRACTS
    assert proof["cross_check_status"] in VALID_CROSS_CHECK_STATUSES
