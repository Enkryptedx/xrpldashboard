"""
Background writer for the MPT registry.

Walks every MPTokenIssuance on mainnet, decodes XLS-89 metadata,
enriches the top-N by outstanding supply with holder counts via
mpt_holders, and writes mpt_snapshot.json atomically.

The /mpts route reads this snapshot when fresh (<2hr). Falls back to
the in-process fetcher on Render where the snapshot file doesn't exist
(slow first call, then 30min cache).

Cadence: hourly via launchd. A full ledger walk for MPTs runs longer
than the 5-min lending walk because the type filter walks every page
of the ledger and MPTs are sparse — so hourly snapshot keeps the
production page snappy.

Run manually: venv/bin/python mpt_snapshot.py
"""

import json
import os
import sys
import time

import httpx

import db
from mpt_data import (
    SNAPSHOT_PATH,
    XRPL_NODE,
    fetch_mpt_data,
)

HOLDER_ENRICH_TOP_N = int(os.environ.get("MPT_ENRICH_TOP_N", "50"))


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


def _count_holders(issuance_id):
    """Walk mpt_holders pages for one issuance, count rows. Returns None on
    error so the template can render '—' rather than misleading 0."""
    if not issuance_id:
        return None
    total = 0
    marker = None
    for _ in range(100):
        params = {"mpt_issuance_id": issuance_id, "limit": 400}
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


def _enrich_holders(data, top_n):
    rows = data.get("issuances") or []
    for r in rows[:top_n]:
        r["holders"] = _count_holders(r.get("issuance_id"))
    for r in rows[top_n:]:
        r.setdefault("holders", None)


def _atomic_write(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, separators=(",", ":"))
    os.replace(tmp, path)


def run_once(path=None, enrich_top_n=None):
    path = path or SNAPSHOT_PATH
    top_n = enrich_top_n if enrich_top_n is not None else HOLDER_ENRICH_TOP_N

    t0 = time.time()
    data = fetch_mpt_data()
    fetch_secs = time.time() - t0

    enrich_secs = 0.0
    if data.get("ok") and data.get("total"):
        t1 = time.time()
        _enrich_holders(data, top_n)
        enrich_secs = time.time() - t1

    data["written_at"] = int(time.time())
    data["fetch_seconds"] = round(fetch_secs, 1)
    data["enrich_seconds"] = round(enrich_secs, 1)

    _atomic_write(path, data)
    # Mirror into Postgres so Render (no local file, no worker) can serve
    # /mpts without falling into the multi-minute ledger walk fallback.
    db.write_mpt_snapshot(data)
    return data


if __name__ == "__main__":
    d = run_once()
    print(f"wrote {SNAPSHOT_PATH}")
    print(f"  node={d.get('node')}")
    print(f"  ok={d.get('ok')}  total={d.get('total')}  issuers={d.get('unique_issuers')}")
    print(f"  by_class={d.get('by_class')}")
    print(f"  fetch={d.get('fetch_seconds')}s  enrich={d.get('enrich_seconds')}s")
    sys.exit(0 if d.get("ok") else 1)
