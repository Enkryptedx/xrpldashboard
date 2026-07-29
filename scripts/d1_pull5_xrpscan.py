#!/usr/bin/env python3
"""D1 pull 5: XRPScan public label cross-ref.

LICENSING NOTE: XRPScan Terms of Service (fetched 2026-07-03) require
attribution ("Source: XRPScan (https://xrpscan.com)") and prohibit
"bulk reproduction, resale, or redistribution in commercial or
enterprise contexts" without written consent. xrpldashboard is a free
public site (arguably non-commercial), so attribution is the primary
requirement, but "bulk redistribution" is a gray zone for us.

For the honest-floor calc we only NEED the coverage count. We store
counts + 10 sample rows so the synthesizer (Fable) can validate.
If we ship enrichment from this source we must attribute per ToS.
"""
import json
import os
import ssl
import time
import urllib.request

import certifi

IN_PATH = "/Users/charliebruce/xrpl_test/scratch/d1_unlabeled_top100.json"
OUT_PATH = "/Users/charliebruce/xrpl_test/scratch/d1_xrpscan.json"
SSL_CTX = ssl.create_default_context(cafile=certifi.where())
UA = "xrpldashboard-d1/1.0 (+https://xrpldashboard.com)"
API_BASE = "https://api.xrpscan.com/api/v1/account"


def fetch(address: str) -> dict | None:
    url = f"{API_BASE}/{address}"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=10, context=SSL_CTX) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception:
        return None


def main():
    src = json.load(open(IN_PATH))
    issuers_seen: set[str] = set()
    ordered_issuers: list[str] = []
    for r in src["rows"]:
        iss = r["issuer"]
        if iss not in issuers_seen:
            issuers_seen.add(iss)
            ordered_issuers.append(iss)

    labeled = 0
    unlabeled = 0
    verified = 0
    samples = []

    for i, issuer in enumerate(ordered_issuers, 1):
        data = fetch(issuer)
        if data is None:
            unlabeled += 1
            continue
        name_obj = data.get("accountName") or {}
        name = (name_obj.get("name") or "").strip()
        desc = (name_obj.get("desc") or "").strip()
        domain = (name_obj.get("domain") or "").strip()
        is_verified = bool(name_obj.get("verified"))
        if name:
            labeled += 1
            if is_verified:
                verified += 1
            if len(samples) < 10:
                samples.append({
                    "issuer": issuer,
                    "name": name,
                    "desc": desc,
                    "domain": domain,
                    "verified": is_verified,
                })
        else:
            unlabeled += 1
        if i % 10 == 0:
            print(f"  [{i:3d}/{len(ordered_issuers)}] labeled={labeled} verified={verified}")
        time.sleep(0.15)

    total = len(ordered_issuers)
    out = {
        "generated_at": int(time.time()),
        "source": "xrpscan.com public API",
        "attribution_required": True,
        "commercial_bulk_redistribution": "restricted per ToS",
        "issuers_probed": total,
        "labeled": labeled,
        "unlabeled": unlabeled,
        "verified_labels": verified,
        "labeled_share": labeled / total if total else 0,
        "verified_share": verified / total if total else 0,
        "samples": samples,
        "note": (
            "Counts only + 10 samples stored. Full label list intentionally "
            "not persisted. If we ship enrichment from this source we must "
            "attribute per XRPScan ToS."
        ),
    }
    with open(OUT_PATH, "w") as f:
        json.dump(out, f, indent=2)
    print()
    print(f"Written: {OUT_PATH}")
    print(f"Issuers probed:              {total}")
    print(f"  With XRPScan label:        {labeled:3d} ({labeled/total*100:.0f}%)")
    print(f"  With 'verified' label:     {verified:3d} ({verified/total*100:.0f}%)")


if __name__ == "__main__":
    main()
