#!/usr/bin/env python3
"""refresh_ofac_sdn.py — download the OFAC SDN list and extract the
digital-currency-address subset into a local JSON snapshot consumed by
check_data.py's _signal_ofac_sdn_match.

Manual for now. A cron/walker cadence lands in Phase 2 of the /check
expansion; the design doc puts snapshot refresh discipline in the same
family as the FTC/FCC mirrors. Until then, run this by hand when the
snapshot ages past the freshness we're comfortable citing on-page.

Output: ofac_sdn_addresses.json at repo root. Schema:
{
  "source_url": "...",
  "fetched_at_utc": "YYYY-MM-DD HH:MM UTC",
  "publication_date": "YYYY-MM-DD" | null,
  "count": N,
  "addresses": {
    "<address_string>": {
      "chain": "XRP" | "ETH" | "XBT" | ...,
      "entity_name": "...",
      "sdn_uid": "12345",
      "sdn_type": "Entity" | "Individual",
      "programs": ["CYBER2", ...]
    },
    ...
  }
}

Freshness stamp is exposed on /check results so users see when the
underlying data was last synced.
"""

from __future__ import annotations

import json
import os
import re
import ssl
import sys
import urllib.request
from datetime import datetime, timezone

import certifi

SDN_URL = (
    "https://sanctionslistservice.ofac.treas.gov/"
    "api/PublicationPreview/exports/SDN.XML"
)
USER_AGENT = "xrpldashboard-ofac-refresh/1.0 (+https://xrpldashboard.com/check)"
TIMEOUT_SECS = 180

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
_OUT_PATH = os.path.join(_REPO_ROOT, "ofac_sdn_addresses.json")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _fetch_sdn_xml() -> bytes:
    ctx = ssl.create_default_context(cafile=certifi.where())
    req = urllib.request.Request(SDN_URL, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT_SECS, context=ctx) as resp:
        return resp.read()


def _extract_publication_date(xml_text: str) -> str | None:
    m = re.search(
        r"<Publish_Date>\s*(\d{2})/(\d{2})/(\d{4})\s*</Publish_Date>",
        xml_text,
    )
    if not m:
        return None
    mm, dd, yyyy = m.group(1), m.group(2), m.group(3)
    return f"{yyyy}-{mm}-{dd}"


def _parse_entries(xml_text: str) -> dict[str, dict]:
    """Walk <sdnEntry> blocks; extract any <id> where idType matches
    'Digital Currency Address - X'. Case-preserved storage: normalization
    for lookup happens at query time in check_data.py."""
    out: dict[str, dict] = {}

    for entry_match in re.finditer(
        r"<sdnEntry>(.*?)</sdnEntry>", xml_text, re.DOTALL
    ):
        block = entry_match.group(1)

        def _field(tag: str) -> str | None:
            m = re.search(rf"<{tag}>([^<]+)</{tag}>", block)
            return m.group(1).strip() if m else None

        uid = _field("uid") or ""
        sdn_type = _field("sdnType") or ""
        first = _field("firstName") or ""
        last = _field("lastName") or ""
        name = (f"{first} {last}").strip() or last or first or "(unnamed)"

        programs = re.findall(r"<program>([^<]+)</program>", block)

        for id_block in re.finditer(
            r"<id>(.*?)</id>", block, re.DOTALL
        ):
            id_inner = id_block.group(1)
            id_type_m = re.search(r"<idType>([^<]+)</idType>", id_inner)
            id_num_m = re.search(r"<idNumber>([^<]+)</idNumber>", id_inner)
            if not id_type_m or not id_num_m:
                continue
            id_type = id_type_m.group(1).strip()
            if not id_type.startswith("Digital Currency Address - "):
                continue
            chain = id_type[len("Digital Currency Address - "):]
            addr = id_num_m.group(1).strip()
            if not addr:
                continue
            out[addr] = {
                "chain": chain,
                "entity_name": name,
                "sdn_uid": uid,
                "sdn_type": sdn_type,
                "programs": programs,
            }

    return out


def main() -> int:
    print(f"[refresh_ofac_sdn] downloading {SDN_URL}", file=sys.stderr)
    raw = _fetch_sdn_xml()
    xml_text = raw.decode("utf-8", errors="replace")
    print(
        f"[refresh_ofac_sdn] {len(raw):,} bytes downloaded",
        file=sys.stderr,
    )
    pub_date = _extract_publication_date(xml_text)
    addresses = _parse_entries(xml_text)
    snapshot = {
        "source_url": SDN_URL,
        "fetched_at_utc": _now_iso(),
        "publication_date": pub_date,
        "count": len(addresses),
        "addresses": addresses,
    }
    # Atomic write: temp file + fsync + rename. A SIGKILL / power-loss mid-
    # write on the target path would leave a truncated JSON that check_data.py
    # loads as an empty snapshot on next boot — silent OFAC coverage loss.
    tmp_path = _OUT_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=2, sort_keys=True)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, _OUT_PATH)
    print(
        f"[refresh_ofac_sdn] wrote {len(addresses)} addresses to "
        f"{_OUT_PATH} (OFAC publication {pub_date})",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
