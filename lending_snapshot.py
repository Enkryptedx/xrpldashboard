"""
Background writer for the XLS-66 lending stack.

Pulls the full broker/vault/loan picture via lending_data.fetch_lending_data(),
enriches the top brokers by TVL with depositor counts (mpt_holders RPC on
each vault's ShareMPTID), and writes lending_snapshot.json atomically.

The /lending route prefers this snapshot over the in-process fetcher,
so the page returns in <100ms even though a fresh broker scan can take
minutes when there are many ledger objects.

Designed to run from launchd on a 15-30 min cadence. Dormant pre-
activation: if amendments aren't live, writes an "activated: false"
snapshot and exits — no wasted RPC calls.

Run manually: venv/bin/python lending_snapshot.py
With devnet override:
  XRPL_NODE=https://s.devnet.rippletest.net:51234 \\
  XRPL_LENDING_NODE=https://s.devnet.rippletest.net:51234 \\
  venv/bin/python lending_snapshot.py
"""

import json
import os
import sys
import time

import httpx

from lending_data import (
    SNAPSHOT_PATH,
    XRPL_NODE,
    fetch_lending_data,
)

DEPOSITOR_ENRICH_TOP_N = int(os.environ.get("LENDING_ENRICH_TOP_N", "30"))


def _rpc(method, params):
    try:
        resp = httpx.post(
            XRPL_NODE,
            json={"method": method, "params": [params]},
            timeout=15.0,
        )
        return (resp.json() or {}).get("result") or {}
    except Exception:
        return None


def _count_mpt_holders(mpt_issuance_id):
    """Walk mpt_holders pages for one issuance, count rows. Returns None on
    error so the template can render '—' rather than a misleading 0."""
    if not mpt_issuance_id:
        return None
    total = 0
    marker = None
    for _ in range(50):
        params = {"mpt_issuance_id": mpt_issuance_id, "limit": 400}
        if marker is not None:
            params["marker"] = marker
        result = _rpc("mpt_holders", params)
        if not result:
            return None
        holders = result.get("holders") or []
        total += len(holders)
        marker = result.get("marker")
        if not marker:
            break
    return total


def _enrich_depositors(data, top_n):
    """Mutates data['brokers']: adds depositors count to the top N by TVL."""
    brokers = data.get("brokers") or []
    for b in brokers[:top_n]:
        b["depositors"] = _count_mpt_holders(b.get("share_mpt_id"))
    for b in brokers[top_n:]:
        b.setdefault("depositors", None)


def _atomic_write(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, separators=(",", ":"))
    os.replace(tmp, path)


def run_once(path=None, enrich_top_n=None):
    path = path or SNAPSHOT_PATH
    top_n = enrich_top_n if enrich_top_n is not None else DEPOSITOR_ENRICH_TOP_N

    t0 = time.time()
    data = fetch_lending_data()
    fetch_secs = time.time() - t0

    enrich_secs = 0.0
    if data.get("ok") and data.get("activated"):
        t1 = time.time()
        _enrich_depositors(data, top_n)
        enrich_secs = time.time() - t1

    data["written_at"] = int(time.time())
    data["fetch_seconds"] = round(fetch_secs, 1)
    data["enrich_seconds"] = round(enrich_secs, 1)

    _atomic_write(path, data)
    return data


if __name__ == "__main__":
    d = run_once()
    print(f"wrote {SNAPSHOT_PATH}")
    print(f"  node={d.get('node')}")
    print(f"  activated={d.get('activated')}  ok={d.get('ok')}")
    print(f"  brokers={d.get('broker_count')}  vaults={d.get('vault_count')}  loans={d.get('loan_count')}")
    print(f"  fetch={d.get('fetch_seconds')}s  enrich={d.get('enrich_seconds')}s")
    sys.exit(0 if d.get("ok") else 1)
