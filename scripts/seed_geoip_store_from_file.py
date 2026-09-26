#!/usr/bin/env python3
"""Seed Postgres geoip_db_blob from a MANUALLY downloaded GeoLite2-City
tarball (MaxMind portal → Download files → GeoLite2 City → GZIP + SHA256).

Charlie ruling 2026-09-26 15:55 ET: after the daily-download-limit incident
the app never calls MaxMind; the store is fed by this one-shot (today) and
the weekly walker (once the Mac has the key).

Usage (owner env, writes):
  venv/bin/python scripts/seed_geoip_store_from_file.py \
      ~/Downloads/GeoLite2-City_YYYYMMDD.tar.gz ~/Downloads/GeoLite2-City_YYYYMMDD.tar.gz.sha256

Steps: verify the tarball's sha256 against the .sha256 file (MaxMind format:
"<hex>  <filename>"), extract GeoLite2-City.mmdb, gzip it, UPSERT
geoip_db_blob with sha256(mmdb), source=mac_manual_download, fetched_at =
the build date in the tarball name. Never commits the .mmdb anywhere.
"""
from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import os
import re
import sys
import tarfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import db  # noqa: E402

EDITION = "GeoLite2-City"


def main(argv) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    tarball, shafile = argv[1], (argv[2] if len(argv) > 2 else None)
    with open(tarball, "rb") as f:
        tb = f.read()
    tar_sha = hashlib.sha256(tb).hexdigest()
    if shafile:
        expected = open(shafile).read().split()[0].strip().lower()
        if expected != tar_sha:
            print(f"[seed_geoip] SHA256 MISMATCH tarball={tar_sha} expected={expected}")
            return 1
        print(f"[seed_geoip] tarball sha256 verified: {tar_sha}")
    else:
        print(f"[seed_geoip] WARNING no .sha256 file given; tarball sha256={tar_sha} (unverified)")
    mode = "r:gz" if tarball.endswith(".gz") else "r:"
    with tarfile.open(tarball, mode) as tf:
        member = next((m for m in tf.getmembers() if m.name.endswith(f"{EDITION}.mmdb")), None)
        if member is None:
            print(f"[seed_geoip] {EDITION}.mmdb not found in tarball — wrong edition? members: "
                  f"{[m.name for m in tf.getmembers()][:5]}")
            return 1
        data = tf.extractfile(member).read()
    if len(data) < 1_000_000:
        print(f"[seed_geoip] mmdb too small ({len(data)} B)")
        return 1
    m = re.search(r"(\d{8})", os.path.basename(tarball))
    fetched_at = (dt.datetime.strptime(m.group(1), "%Y%m%d").replace(tzinfo=dt.timezone.utc)
                  if m else dt.datetime.now(dt.timezone.utc))
    sha = hashlib.sha256(data).hexdigest()
    gz = gzip.compress(data, compresslevel=6)
    ok = db.write_geoip_db_blob(EDITION, gz, sha, "mac_manual_download", fetched_at=fetched_at)
    if not ok:
        print("[seed_geoip] store write FAILED")
        return 1
    meta = db.read_geoip_db_blob_meta(EDITION)
    print(f"[seed_geoip] SEEDED edition={EDITION} mmdb_sha256={sha} mmdb_bytes={len(data):,} "
          f"gz_bytes={len(gz):,} fetched_at={meta[0].isoformat() if meta else '?'} source=mac_manual_download")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
