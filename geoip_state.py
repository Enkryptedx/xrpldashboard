"""Self-hosted MaxMind GeoLite2 City lookup for state-level analytics.

Replaces the parked Cloudflare Worker / Managed Transform paths for
region_code. Holds a process-wide geoip2.database.Reader over a local
GeoLite2-City.mmdb; lookups are local-file only — no network I/O on the
request hot path, sub-microsecond per call at our request volume.

Where the .mmdb comes from (INCIDENT 2026-09-26, MaxMind "Daily GeoIP
Database Download Limit Reached", account 1403113, 09:48 ET): the previous
version downloaded the tarball ON IMPORT in EVERY gunicorn worker on EVERY
container start — 3 workers × ~40 Render deploys in 24 h = well past the
GeoLite tier's limit of 30 direct downloads per rolling 24 h. From the
first post-limit deploy (~13:00Z) every container booted with the lookup
disabled and page_views.region_code went NULL for every visit (country
still fills from CF-IPCountry). Refresh policy now:

  1. The Render build step (`scripts/fetch_geoip_db.py`) downloads ONCE per
     deploy into the repo directory, which persists from build to runtime.
  2. Workers reuse that file if it is younger than GEOIP_MMDB_MAX_AGE_S
     (default 7 days; GeoLite City updates Tue/Fri). No per-worker download.
  3. Only if the file is missing/stale does a worker download — under a
     file lock so 3 workers make 1 request, not 3.
  4. If the download fails (limit, key, outage) the module boots "disabled"
     (fail-open, region_code None, callers keep their header fallback) and
     a daemon thread retries every GEOIP_RETRY_S (default 3600 s), so the
     lookup recovers when the rolling window frees without a redeploy.
  5. `status()` is exposed on /healthz so a disabled lookup is visible.

Format: ISO 3166-2 short form "US-CA" (country-dash-subdivision) when both
values are present; falls back to bare country ISO ("US") if MaxMind can
place the IP in a country but not a subdivision; None if it can't place it
at all. Matches the 2026-09-01 ruling.
"""

import logging
import os
import tarfile
import tempfile
import threading
import time
import urllib.request
from urllib.error import HTTPError, URLError

log = logging.getLogger(__name__)

HERE = os.path.dirname(os.path.abspath(__file__))
_MAXMIND_URL_TEMPLATE = (
    "https://download.maxmind.com/app/geoip_download"
    "?edition_id=GeoLite2-City&license_key={key}&suffix=tar.gz"
)
# Inside the repo dir (gitignored) so the build-step download persists into
# the running container on Render. /tmp did not: it is per-container.
_DEFAULT_MMDB_PATH = os.path.join(HERE, "GeoLite2-City.mmdb")
_FETCH_TIMEOUT_S = 30
MMDB_MAX_AGE_S = int(os.environ.get("GEOIP_MMDB_MAX_AGE_S", str(7 * 86400)))
RETRY_S = int(os.environ.get("GEOIP_RETRY_S", "3600"))

_reader = None
_state = {
    "available": False,
    "path": None,
    "file_age_s": None,
    "source": None,          # "reused" | "downloaded" | None
    "last_error": None,
    "last_attempt_iso": None,
    "downloads_this_process": 0,
}
_lock = threading.Lock()


def _mmdb_path() -> str:
    return os.environ.get("GEOIP_MMDB_PATH") or _DEFAULT_MMDB_PATH


def _file_age_s(path: str):
    try:
        return time.time() - os.path.getmtime(path)
    except OSError:
        return None


def _fresh(path: str) -> bool:
    age = _file_age_s(path)
    return age is not None and age <= MMDB_MAX_AGE_S and os.path.getsize(path) > 1_000_000


def _download_and_extract(license_key, dest_path):
    """Fetch the GeoLite2-City tarball, extract the .mmdb into dest_path
    (atomic: written to a temp file next to dest, then os.replace).
    Returns True on success. Never raises — logs failures at WARNING so a
    bad key, the daily download limit, or a MaxMind outage is visible
    without killing app startup."""
    url = _MAXMIND_URL_TEMPLATE.format(key=license_key)
    tarball_path = None
    tmp_dest = dest_path + ".part"
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
                _state["last_error"] = "GeoLite2-City.mmdb not found in tarball"
                log.warning("geoip_state: %s", _state["last_error"])
                return False
            src = tf.extractfile(member)
            if src is None:
                _state["last_error"] = "could not extract member from tarball"
                log.warning("geoip_state: %s", _state["last_error"])
                return False
            with src, open(tmp_dest, "wb") as dst:
                dst.write(src.read())
        os.replace(tmp_dest, dest_path)
        _state["downloads_this_process"] += 1
        _state["last_error"] = None
        return True
    except (HTTPError, URLError, OSError, tarfile.TarError) as e:
        # HTTP 401/403 here after a burst of deploys = MaxMind's daily
        # download limit (30 per rolling 24 h on GeoLite). Loud, not fatal.
        _state["last_error"] = f"fetch/extract failed: {e!r}"[:200]
        log.warning("geoip_state: %s", _state["last_error"])
        return False
    finally:
        for p in (tarball_path, tmp_dest):
            if p:
                try:
                    os.unlink(p)
                except OSError:
                    pass


def ensure_database(force: bool = False) -> bool:
    """Make a fresh .mmdb exist at the configured path, downloading at most
    once per call and only when missing/stale (or `force`). Serialized
    across processes with a lock file so N workers => 1 download. Returns
    True when a usable file is present afterwards. Never raises."""
    path = _mmdb_path()
    _state["path"] = path
    _state["last_attempt_iso"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if not force and _fresh(path):
        _state["source"] = "reused"
        _state["file_age_s"] = round(_file_age_s(path) or 0)
        return True
    key = (os.environ.get("MAXMIND_LICENSE_KEY") or "").strip()
    if not key:
        _state["last_error"] = "MAXMIND_LICENSE_KEY unset"
        log.info("geoip_state: MAXMIND_LICENSE_KEY unset — state lookup disabled")
        return os.path.exists(path) and os.path.getsize(path) > 1_000_000
    lock_path = path + ".lock"
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(lock_path, "w") as lf:
            try:
                import fcntl
                fcntl.flock(lf, fcntl.LOCK_EX)
            except Exception:  # noqa: BLE001 — no fcntl (Windows): best effort
                pass
            # Another worker may have finished the download while we waited.
            if not force and _fresh(path):
                _state["source"] = "reused"
                _state["file_age_s"] = round(_file_age_s(path) or 0)
                return True
            ok = _download_and_extract(key, path)
    except OSError as e:
        _state["last_error"] = f"lock/dir failed: {e!r}"[:200]
        log.warning("geoip_state: %s", _state["last_error"])
        ok = False
    if ok:
        _state["source"] = "downloaded"
        _state["file_age_s"] = 0
        return True
    # Download failed: a stale-but-present file is still better than None.
    if os.path.exists(path) and os.path.getsize(path) > 1_000_000:
        _state["source"] = "reused-stale"
        _state["file_age_s"] = round(_file_age_s(path) or 0)
        return True
    return False


def _open_reader():
    global _reader
    path = _mmdb_path()
    try:
        import geoip2.database
        _reader = geoip2.database.Reader(path)
        _state["available"] = True
        log.info("geoip_state: reader initialized from %s (%s)", path, _state["source"])
        return True
    except Exception as e:  # noqa: BLE001
        _state["last_error"] = f"reader init failed: {e!r}"[:200]
        _state["available"] = False
        log.warning("geoip_state: %s", _state["last_error"])
        return False


def _initialize() -> bool:
    with _lock:
        if ensure_database():
            return _open_reader()
        _state["available"] = False
        return False


def _retry_loop():
    while _reader is None:
        time.sleep(RETRY_S)
        try:
            if _initialize():
                log.info("geoip_state: recovered after retry")
                return
        except Exception as e:  # noqa: BLE001
            log.warning("geoip_state: retry failed: %r", e)


def _start_retry_thread():
    if _reader is None and (os.environ.get("MAXMIND_LICENSE_KEY") or "").strip() \
            and os.environ.get("GEOIP_DISABLE_RETRY") != "1":
        t = threading.Thread(target=_retry_loop, name="geoip-retry", daemon=True)
        t.start()


_initialize()
_start_retry_thread()


def available():
    """True when the reader is ready and lookup_region_code will work."""
    return _reader is not None


def status() -> dict:
    """Small, JSON-safe view for /healthz: is state-level lookup live, and
    if not, why (the 2026-09-26 outage was invisible for 5 h)."""
    out = dict(_state)
    out["available"] = _reader is not None
    p = out.get("path")
    out["file_age_s"] = round(_file_age_s(p)) if p and os.path.exists(p) else None
    return out


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
