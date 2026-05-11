#!/usr/bin/env python3
"""Verify an XRPL transaction against the canonical JSON-RPC node.

Usage:
    python3 verify.py <txhash>
    python3 verify.py --node https://s1.ripple.com:51234 <txhash>

Prints the fields a normal user wants to confirm against the dashboard:
hash, ledger index, validated flag, type, account, destination, amount,
fee, and close-time (UTC). No third-party dependencies — uses urllib so
this works in a fresh checkout without `pip install`.

Why this exists: XRPSCAN and livenet.xrpl.org are JavaScript apps; tools
without a browser can't read what they render. The XRPL itself, however,
exposes a public JSON-RPC API that anyone can hit — same source those
explorers consult on their backend. This script asks that source
directly and prints the answer in plain English.
"""

import argparse
import json
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone

DEFAULT_NODE = "https://s1.ripple.com:51234"
# XRPL "Ripple epoch" — seconds since 2000-01-01 UTC. To get a normal
# Unix timestamp: ripple_epoch + 946684800.
RIPPLE_EPOCH_OFFSET = 946_684_800


def fetch_tx(node_url, tx_hash):
    payload = json.dumps({
        "method": "tx",
        "params": [{"transaction": tx_hash, "binary": False}],
    }).encode("utf-8")
    req = urllib.request.Request(
        node_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def format_amount(amt):
    if amt is None:
        return "—"
    if isinstance(amt, str):
        try:
            return f"{int(amt) / 1_000_000:,.6f} XRP"
        except ValueError:
            return amt
    if isinstance(amt, dict):
        cur = amt.get("currency", "?")
        val = amt.get("value", "?")
        issuer = amt.get("issuer", "?")
        return f"{val} {cur} (issued by {issuer})"
    return str(amt)


def format_close_time(ripple_epoch_seconds):
    if ripple_epoch_seconds is None:
        return "—"
    unix = ripple_epoch_seconds + RIPPLE_EPOCH_OFFSET
    return datetime.fromtimestamp(unix, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )


def render(result):
    if "error" in result:
        msg = result.get("error_message") or result.get("error")
        return f"XRPL node returned an error: {msg}", 2

    tx = result.get("tx_json") or result
    meta = result.get("meta") or {}
    validated = result.get("validated", False)
    delivered = meta.get("delivered_amount")

    lines = []
    lines.append("─" * 60)
    lines.append(f"  hash         : {tx.get('hash')}")
    lines.append(f"  validated    : {validated}")
    lines.append(f"  ledger index : {result.get('ledger_index') or tx.get('ledger_index')}")
    lines.append(f"  close time   : {format_close_time(tx.get('date'))}")
    lines.append(f"  type         : {tx.get('TransactionType')}")
    lines.append(f"  from         : {tx.get('Account')}")
    if tx.get("Destination"):
        lines.append(f"  to           : {tx.get('Destination')}")
    amount_field = tx.get("Amount") or tx.get("DeliverMax")
    if amount_field is not None:
        lines.append(f"  amount       : {format_amount(amount_field)}")
    if delivered is not None and delivered != amount_field:
        lines.append(f"  delivered    : {format_amount(delivered)}")
    if tx.get("Fee"):
        lines.append(f"  fee          : {format_amount(tx.get('Fee'))}")
    if meta.get("TransactionResult"):
        lines.append(f"  result       : {meta.get('TransactionResult')}")
    lines.append("─" * 60)
    return "\n".join(lines), 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("txhash", help="64-character XRPL transaction hash")
    parser.add_argument("--node", default=DEFAULT_NODE,
                        help=f"XRPL JSON-RPC endpoint (default: {DEFAULT_NODE})")
    args = parser.parse_args(argv)

    tx_hash = args.txhash.strip().upper()
    if len(tx_hash) != 64 or any(c not in "0123456789ABCDEF" for c in tx_hash):
        print(f"error: '{args.txhash}' is not a 64-char hex transaction hash",
              file=sys.stderr)
        return 2

    try:
        envelope = fetch_tx(args.node, tx_hash)
    except urllib.error.URLError as e:
        print(f"error: could not reach XRPL node {args.node}: {e}",
              file=sys.stderr)
        return 1

    result = envelope.get("result", envelope)
    output, code = render(result)
    print(output)
    return code


if __name__ == "__main__":
    sys.exit(main())
