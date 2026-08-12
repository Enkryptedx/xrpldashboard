"""x402 rails-dark — testnet round-trip harness.

Exercises the full presigned-payment flow against t54's XRPL testnet
facilitator so we can capture ship-gate evidence for the rails-dark
build without touching mainnet money.

Not part of the CI test suite. This is a developer-run script: it
requires a funded testnet wallet, real network access, and produces
receipts we file under `docs/x402_dark_ship_evidence/`. Ship-gate
discipline per § 4 of `docs/X402_RAILS_DARK_SCOPING.md`:

    1. Set X402_ENFORCEMENT=dry_run, X402_FACILITATOR_URL=testnet,
       X402_NETWORK=xrpl:1, X402_PAY_TO=<our-testnet-address>.
    2. Fund an agent-side testnet wallet with testnet XRP + RLUSD from
       the XRPL testnet faucet.
    3. Curl a candidate endpoint → capture the 402 body.
    4. Sign a testnet Payment tx offline via xrpl-py using the agent
       secret (NEVER committed).
    5. Re-hit endpoint with X-PAYMENT: <presigned-blob> → 200 + body +
       receipt header.
    6. Verify testnet ledger shows the tx.
    7. Delete testnet secrets from local env; commit only redacted
       evidence (tx hashes + response bodies).

Usage:
    export AGENT_TESTNET_SECRET=sEd7...       # NEVER commit this
    export X402_TESTNET_PAY_TO=r...           # our testnet destination
    export X402_TEST_ENDPOINT_URL=http://localhost:5001/api/x402_test_route
    python tools/x402_testnet_dryrun.py

Exit codes:
    0 = full round-trip PASS (402 → sign → 200 + receipt captured).
    1 = 402 shape wrong (missing field, wrong network, wrong asset).
    2 = signing failed (agent wallet unfunded, xrpl-py error).
    3 = second-request verification failed (facilitator rejected, or
        200 lacked a receipt header).
    4 = misconfiguration (missing env var).

No secrets are ever printed to stdout. The receipts written to disk
strip signature material — only tx_hash + response body shape.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any

# Deliberately lazy imports — this file is a script, not a module.
# Importing `requests` at top makes `python -c "import tools.x402_testnet_dryrun"`
# fail on a bare CI runner without the dep.


def _die(exit_code: int, msg: str) -> None:
    sys.stderr.write(f"[x402_testnet_dryrun] {msg}\n")
    sys.exit(exit_code)


def _require_env(var: str) -> str:
    val = os.environ.get(var, "").strip()
    if not val:
        _die(4, f"missing required env var: {var}")
    return val


def _fetch_402(endpoint_url: str) -> dict:
    import requests

    r = requests.get(endpoint_url, timeout=10)
    if r.status_code != 402:
        _die(1, f"expected 402, got {r.status_code}: {r.text[:200]}")
    body = r.json()
    for field in ("x402Version", "accepts", "error"):
        if field not in body:
            _die(1, f"402 body missing field: {field}")
    accept = body["accepts"][0]
    for field in ("network", "asset", "payTo", "maxAmountRequired", "resource"):
        if field not in accept:
            _die(1, f"402 accepts[0] missing field: {field}")
    if not accept["network"].startswith("xrpl:1"):
        _die(1, f"expected testnet (xrpl:1), got {accept['network']!r}")
    return body


def _sign_presigned_tx(requirements: dict, agent_secret: str) -> str:
    """Sign a testnet XRPL Payment tx per the requirements.

    Uses xrpl-py directly rather than x402-xrpl's higher-level helper
    so this harness stays useful even if the x402-xrpl API drifts.
    Returns the presigned tx blob suitable for the X-PAYMENT header.

    xrpl-py is NOT imported at file top so tests that grep this file
    or lint it don't need xrpl-py installed."""
    try:
        from xrpl.wallet import Wallet
        from xrpl.models.transactions import Payment
        from xrpl.transaction import autofill_and_sign
        from xrpl.clients import JsonRpcClient
    except ImportError:
        _die(2, "xrpl-py not installed; pip install xrpl-py")
        return ""  # unreachable

    try:
        wallet = Wallet.from_seed(agent_secret)
    except Exception as e:
        _die(2, f"failed to derive wallet from AGENT_TESTNET_SECRET: {e}")
        return ""  # unreachable

    accept = requirements["accepts"][0]
    testnet_client = JsonRpcClient("https://s.altnet.rippletest.net:51234")

    try:
        payment = Payment(
            account=wallet.address,
            destination=accept["payTo"],
            amount=accept["maxAmountRequired"],  # in drops for XRP
        )
        signed = autofill_and_sign(payment, testnet_client, wallet)
        # x402 presigned blob shape is base64-of-signed-tx per the spec.
        # For dry_run we just serialize the tx_blob field xrpl-py exposes.
        return signed.tx_blob if hasattr(signed, "tx_blob") else json.dumps(signed.to_dict())
    except Exception as e:
        _die(2, f"tx signing failed: {e}")
        return ""  # unreachable


def _submit_with_payment(endpoint_url: str, payment_header: str) -> dict:
    import requests

    r = requests.get(
        endpoint_url,
        headers={"X-PAYMENT": payment_header},
        timeout=15,
    )
    if r.status_code != 200:
        _die(3, f"expected 200 after payment, got {r.status_code}: {r.text[:200]}")
    receipt = r.headers.get("X-PAYMENT-RECEIPT", "")
    if not receipt:
        _die(3, "200 lacked X-PAYMENT-RECEIPT header")
    return {"status": 200, "receipt": receipt, "body": r.json()}


def _write_evidence(evidence: dict[str, Any], out_path: str) -> None:
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    # Strip anything that even smells like a secret before write.
    redacted = {k: v for k, v in evidence.items() if k not in ("presigned_blob",)}
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(redacted, f, indent=2, sort_keys=True)


def main() -> int:
    endpoint_url = _require_env("X402_TEST_ENDPOINT_URL")
    agent_secret = _require_env("AGENT_TESTNET_SECRET")
    _require_env("X402_TESTNET_PAY_TO")  # sanity-check the operator set it

    print("[1/3] fetching 402...", flush=True)
    requirements = _fetch_402(endpoint_url)
    print(f"      network={requirements['accepts'][0]['network']}"
          f" asset={requirements['accepts'][0]['asset']}"
          f" amount={requirements['accepts'][0]['maxAmountRequired']}",
          flush=True)

    print("[2/3] signing testnet presigned tx...", flush=True)
    presigned = _sign_presigned_tx(requirements, agent_secret)

    print("[3/3] submitting with X-PAYMENT header...", flush=True)
    result = _submit_with_payment(endpoint_url, presigned)
    print(f"      receipt tx_hash={result['receipt']}", flush=True)

    from datetime import datetime, timezone
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "docs",
        "x402_dark_ship_evidence",
        f"testnet_roundtrip_{stamp}.json",
    )
    _write_evidence(
        {
            "when_utc": stamp,
            "endpoint_url": endpoint_url,
            "requirements": requirements,
            "receipt_tx_hash": result["receipt"],
            "response_body": result["body"],
        },
        out_path,
    )
    print(f"PASS. evidence written to {out_path}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
