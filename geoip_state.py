"""Self-hosted MaxMind GeoLite2 City lookup for state-level analytics.

Replaces the parked Cloudflare Worker / Managed Transform paths for
region_code. On import, downloads the GeoLite2-City.mmdb tarball from
MaxMind (keyed by MAXMIND_LICENSE_KEY), extracts the .mmdb, and holds a
process-wide geoip2.database.Reader for the life of the container.

Lookups are local-file only — no network I/O on the request hot path.
Sub-microsecond per call at our request volume.

Fail-open by design: if MAXMIND_LICENSE_KEY is unset, the download fails,
the tarball is malformed, or geoip2 isn't installed, the module boots in
"disabled" state and lookup_region_code() returns None. Callers keep
their existing fallback chain (CF-Region-Code / X-CF-Region-Code) intact.

Format: ISO 3166-2 short form "US-CA" (country-dash-subdivision) when both
values are present; falls back to bare country ISO ("US") if MaxMind can
place the IP in a country but not a subdivision (common for mobile-carrier
IPs used across a whole state); returns None if MaxMind can't place it at
all. Matches the 2026-09-01 ruling; supersedes the mixed on-wire format
("IN" vs "US-IN") the parked CF sources produced.

Refresh policy: one fetch per container start. Render redeploys weekly-ish;
MaxMind refreshes GeoLite2-City twice-weekly (Tue/Fri). Steady-state
staleness is a few days at most — a rounding error for state-level
attribution.
"""

import logging
import os
import tarfile
import tempfile
import urllib.request
from urllib.error import HTTPError, URLError

log = logging.getLogger(__name__)

_MAXMIND_URL_TEMPLATE = (
    "https://download.maxmind.com/app/geoip_download"
    "?edition_id=GeoLite2-City&license_key={key}&suffix=tar.gz"
)
_DEFAULT_MMDB_PATH = "/tmp/GeoLite2-City.mmdb"
_FETCH_TIMEOUT_S = 30

_reader = None


def _download_and_extract(license_key, dest_path):
    """Fetch the GeoLite2-City tarball, extract the .mmdb into dest_path.

    Returns True on success. Never raises — logs failures at WARNING so
    a bad key or MaxMind outage is visible without killing app startup.
    Uses tempfile for the intermediate tarball so a partial download can't
    leave stale bytes at dest_path.
    """
    url = _MAXMIND_URL_TEMPLATE.format(key=license_key)
    tarball_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
            tarball_path = tmp.name
        with urllib.request.urlopen(url, timeout=_FETCH_TIMEOUT_S) as resp:
            with open(tarball_path, "wb") as f:
                f.write(resp.read())
        with tarfile.open(tarball_path, "r:gz") as tf:
            member = next(
                (m for m in tf.getmembers()
                 if m.name.endswith("GeoLite2-City.mmdb")),
                None,
            )
            if member is None:
                log.warning("geoip_state: GeoLite2-City.mmdb not found in tarball")
                return False
            src = tf.extractfile(member)
            if src is None:
                log.warning("geoip_state: could not extract member from tarball")
                return False
            with src, open(dest_path, "wb") as dst:
                dst.write(src.read())
        return True
    except (HTTPError, URLError, OSError, tarfile.TarError) as e:
        log.warning("geoip_state: fetch/extract failed: %r", e)
        return False
    finally:
        if tarball_path:
            try:
                os.unlink(tarball_path)
            except OSError:
                pass


def _initialize():
    """Fetch the database and build a Reader. Returns Reader or None."""
    key = (os.environ.get("MAXMIND_LICENSE_KEY") or "").strip()
    if not key:
        log.info("geoip_state: MAXMIND_LICENSE_KEY unset — state lookup disabled")
        return None
    dest_path = os.environ.get("GEOIP_MMDB_PATH") or _DEFAULT_MMDB_PATH
    if not _download_and_extract(key, dest_path):
        return None
    try:
        import geoip2.database
        reader = geoip2.database.Reader(dest_path)
        log.info("geoip_state: reader initialized from %s", dest_path)
        return reader
    except Exception as e:
        log.warning("geoip_state: reader init failed: %r", e)
        return None


_reader = _initialize()


def available():
    """True when the reader is ready and lookup_region_code will work."""
    return _reader is not None


def lookup_region_code(ip):
    """Return "US-CA" style region code for the given IP, or None.

    Returns None on: empty/unset IP, reader unavailable, private/loopback
    IP (MaxMind raises AddressNotFoundError), or any unexpected error.
    Never raises.
    """
    if _reader is None or not ip:
        return None
    try:
        response = _reader.city(ip)
        country = response.country.iso_code
        sub = response.subdivisions.most_specific.iso_code
        if country and sub:
            return f"{country}-{sub}"
        return country or None
    except Exception:
        return None
