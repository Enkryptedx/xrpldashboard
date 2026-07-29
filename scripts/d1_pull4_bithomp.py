#!/usr/bin/env python3
"""D1 pull 4: Bithomp public label cross-ref.

LICENSING BLOCK: Bithomp's Terms and Conditions API Terms section
explicitly state:
  "you are not authorized to duplicate, reproduce, copy, store, derive
   from, or translate any Data, API Documentation, or information
   expressed by the Data"

  "selling, renting, leasing, sublicensing, redistributing, or
   syndicating access to the Bithomp API or any part thereof is not
   permitted unless pursuant to the terms of an Executed Agreement
   with Bithomp"

Source: https://bithomp.com/terms-and-conditions (fetched 2026-07-03).

Because we (xrpldashboard) would be re-exporting these labels to public
visitors, this source is legally UNAVAILABLE for the /tokens hero use case
without a signed agreement with Bithomp.

We therefore do NOT scrape and do NOT store Bithomp labels.

This script exists to (a) document the block, (b) confirm the API rejects
unauth requests (proving the terms are enforced), and (c) note that a
future Executed Agreement would unlock this source.
"""
import json
import ssl
import time
import urllib.request

import certifi

OUT_PATH = "/Users/charliebruce/xrpl_test/scratch/d1_bithomp.json"
SSL_CTX = ssl.create_default_context(cafile=certifi.where())
UA = "xrpldashboard-d1/1.0 (+https://xrpldashboard.com)"


def probe_unauth() -> dict:
    """One un-authenticated request to /api/v2/address/{addr} to
    verify the terms-enforcing 403. Uses a well-known well-labeled
    address (Ripple's rHb9CJAWyB4rj91VRWn96DkukG4bwdtyTh)."""
    url = "https://bithomp.com/api/v2/address/rHb9CJAWyB4rj91VRWn96DkukG4bwdtyTh"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=10, context=SSL_CTX) as resp:
            return {
                "status": resp.status,
                "body_sample": resp.read(500).decode("utf-8", errors="replace"),
                "note": "unexpectedly succeeded without auth",
            }
    except urllib.error.HTTPError as e:
        return {
            "status": e.code,
            "body_sample": e.read(500).decode("utf-8", errors="replace"),
            "note": "API enforces auth as terms describe" if e.code in (401, 403) else None,
        }
    except Exception as e:
        return {"status": 0, "note": f"{type(e).__name__}: {str(e)[:100]}"}


def main():
    probe = probe_unauth()
    out = {
        "generated_at": int(time.time()),
        "source": "bithomp.com",
        "status": "LEGALLY_BLOCKED",
        "reason": (
            "Bithomp Terms and Conditions API Terms explicitly prohibit "
            "duplicating, copying, storing, or deriving from their data "
            "without an Executed Agreement. Re-exporting labels to "
            "xrpldashboard visitors is a covered use. Source: "
            "https://bithomp.com/terms-and-conditions (fetched 2026-07-03)."
        ),
        "api_key_required": True,
        "unauth_probe": probe,
        "coverage_analysis": None,
        "recommendation": (
            "Exclude from enrichment pipeline until a signed Executed "
            "Agreement is in place. Do NOT scrape the frontend HTML as "
            "a workaround — the data restriction applies regardless of "
            "extraction channel."
        ),
        "issuers_probed": 0,
        "labels_stored": 0,
    }
    with open(OUT_PATH, "w") as f:
        json.dump(out, f, indent=2)
    print(f"Written: {OUT_PATH}")
    print("Status: LEGALLY_BLOCKED (terms prohibit re-export)")
    print(f"Unauth probe: HTTP {probe['status']}")


if __name__ == "__main__":
    main()
