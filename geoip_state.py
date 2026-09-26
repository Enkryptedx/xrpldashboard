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
still fills from CF-IPCountry). Source chain now (Charlie ruling
2026-09-26 14:47 ET — "stop downloading from MaxMind on every container
boot; keep a copy in our own store; refresh once a week by one walker"):

  1. Local file younger than GEOIP_MMDB_MAX_AGE_S (7 d) → reuse. Workers on
     one host share it; the Render build step (scripts/fetch_geoip_db.py)
     creates it once per deploy.
  2. OUR STORE: Postgres `geoip_db_blob` (gzip'd .mmdb, written weekly by
     the Mac walker geoip_db_refresh_walker, or seeded by step 3) → gunzip
     to the local path. Zero MaxMind traffic. Accepted while younger than
     GEOIP_BLOB_MAX_AGE_S (14 d = two missed weekly refreshes).
  3. MaxMind direct — ONLY when the store is empty/too old and a key is
     present. On success the blob is uploaded to the store (seed), so the
     next boot anywhere takes step 2.
  4. A stale local file beats nothing. Otherwise boot "disabled" (fail-open:
     region_code None, callers keep their header fallback) and a daemon
     thread retries steps 2–3 every GEOIP_RETRY_S (1 h).
  5. `status()` is exposed on /healthz; geoip_health_canary pages on it.

Format: ISO 3166-2 short form "US-CA" (country-dash-subdivision) when both
values are present; falls back to bare country ISO ("US") if MaxMind can
place the IP in a country but not a subdivision; None if it can't place it
at all. Matches the 2026-09-01 ruling.
"""

import gzip
import hashlib
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
EDITION = "GeoLite2-City"
_MAXMIND_URL_TEMPLATE = (
    "https://download.maxmind.com/app/geoip_download"
    "?edition_id=GeoLite2-City&license_key={key}&suffix=tar.gz"
)
# Inside the repo dir (gitignored) so the build-step fetch persists into
# the running container on Render. /tmp did not: it is per-container.
_DEFAULT_MMDB_PATH = os.path.join(HERE, "GeoLite2-City.mmdb")
_FETCH_TIMEOUT_S = 30
MMDB_MAX_AGE_S = int(os.environ.get("GEOIP_MMDB_MAX_AGE_S", str(7 * 86400)))
BLOB_MAX_AGE_S = int(os.environ.get("GEOIP_BLOB_MAX_AGE_S", str(14 * 86400)))
# Retry is store-only (cheap: one metadata SELECT), so poll every 10 min —
# a freshly seeded geoip_db_blob is picked up without a redeploy.
RETRY_S = int(os.environ.get("GEOIP_RETRY_S", "600"))
MIN_MMDB_BYTES = 1_000_000
# Charlie ruling 2026-09-26 15:55 ET: the app NEVER calls MaxMind on boot or
# retry. Direct download is opt-in (GEOIP_ALLOW_DIRECT=1) for the weekly
# walker / a manual seed only. Boot + retry = local file → our store.
ALLOW_DIRECT = os.environ.get("GEOIP_ALLOW_DIRECT", "0") == "1"

_reader = None
_state = {
    "available": False,
    "path": None,
    "file_age_s": None,
    "source": None,          # reused | pg | downloaded | reused-stale | None
    "last_error": None,
    "last_attempt_iso": None,
    "downloads_this_process": 0,
    "blob_fetched_at": None,
}
_lock = threading.Lock()


def _mmdb_path() -> str:
    return os.environ.get("GEOIP_MMDB_PATH") or _DEFAULT_MMDB_PATH


def _file_age_s(path: str):
    try:
        return time.time() - os.path.getmtime(path)
    except OSError:
        return None


def _usable(path: str) -> bool:
    try:
        return os.path.getsize(path) > MIN_MMDB_BYTES
    except OSError:
        return False


def _fresh(path: str) -> bool:
    age = _file_age_s(path)
    return age is not None and age <= MMDB_MAX_AGE_S and _usable(path)


def _atomic_write(dest_path: str, data: bytes) -> None:
    tmp = dest_path + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, dest_path)


def _download_mmdb_bytes(license_key: str):
    """Fetch the GeoLite2-City tarball from MaxMind and return the raw
    .mmdb bytes, or None. Never raises; records last_error. HTTP 429 here
    = MaxMind's daily download limit (30 per rolling 24 h on GeoLite)."""
    url = _MAXMIND_URL_TEMPLATE.format(key=license_key)
    tarball_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
            tarball_path = tmp.name
        with urllib.request.urlopen(url, timeout=_FETCH_TIMEOUT_S) as resp:
            with open(tarball_path, "wb") as f:
                f.write(resp.read())
        with tarfile.open(tarball_path, "r:gz") as tf:
            member = next((m for m in tf.getmembers() if m.name.endswith("GeoLite2-City.mmdb")), None)
            if member is None:
                _state["last_error"] = "GeoLite2-City.mmdb not found in tarball"
                log.warning("geoip_state: %s", _state["last_error"])
                return None
            src = tf.extractfile(member)
            if src is None:
                _state["last_error"] = "could not extract member from tarball"
                log.warning("geoip_state: %s", _state["last_error"])
                return None
            with src:
                data = src.read()
        _state["downloads_this_process"] += 1
        _state["last_error"] = None
        return data
    except (HTTPError, URLError, OSError, tarfile.TarError) as e:
        _state["last_error"] = f"fetch/extract failed: {e!r}"[:200]
        log.warning("geoip_state: %s", _state["last_error"])
        return None
    finally:
        if tarball_path:
            try:
                os.unlink(tarball_path)
            except OSError:
                pass


def _download_and_extract(license_key, dest_path):
    """Direct MaxMind fetch → dest_path (atomic). Returns True on success.
    Kept as a function so tests and the walker can stub it."""
    data = _download_mmdb_bytes(license_key)
    if not data:
        return False
    try:
        _atomic_write(dest_path, data)
        return True
    except OSError as e:
        _state["last_error"] = f"write failed: {e!r}"[:200]
        log.warning("geoip_state: %s", _state["last_error"])
        return False


def _from_store(dest_path: str) -> bool:
    """Step 2: pull the gzip'd .mmdb from geoip_db_blob if young enough."""
    try:
        import db
        meta = db.read_geoip_db_blob_meta(EDITION)
        if not meta:
            return False
        fetched_at, sha256, _size = meta
        age = time.time() - fetched_at.timestamp()
        if age > BLOB_MAX_AGE_S:
            _state["last_error"] = f"store blob too old ({int(age)}s > {BLOB_MAX_AGE_S}s)"
            return False
        got = db.read_geoip_db_blob(EDITION)
        if not got:
            return False
        gz, fetched_at, sha256 = got
        data = gzip.decompress(gz)
        if hashlib.sha256(data).hexdigest() != sha256 or len(data) < MIN_MMDB_BYTES:
            _state["last_error"] = "store blob sha256 mismatch or too small"
            log.warning("geoip_state: %s", _state["last_error"])
            return False
        _atomic_write(dest_path, data)
        _state["blob_fetched_at"] = fetched_at.isoformat()
        _state["last_error"] = None
        return True
    except Exception as e:  # noqa: BLE001
        _state["last_error"] = f"store read failed: {e!r}"[:200]
        log.warning("geoip_state: %s", _state["last_error"])
        return False


def _seed_store(dest_path: str, source: str) -> bool:
    """After a successful direct download: upload the gz to our store so
    no other boot needs MaxMind. Best-effort."""
    try:
        import db
        with open(dest_path, "rb") as f:
            data = f.read()
        gz = gzip.compress(data, compresslevel=6)
        ok = db.write_geoip_db_blob(EDITION, gz, hashlib.sha256(data).hexdigest(), source)
        if ok:
            log.info("geoip_state: seeded geoip_db_blob (%d gz bytes, source=%s)", len(gz), source)
        return ok
    except Exception as e:  # noqa: BLE001
        log.warning("geoip_state: store seed failed: %r", e)
        return False


def _store_needs_direct() -> bool:
    """Direct MaxMind only when the store is empty or older than BLOB_MAX_AGE_S."""
    try:
        import db
        meta = db.read_geoip_db_blob_meta(EDITION)
        if not meta:
            return True
        return (time.time() - meta[0].timestamp()) > BLOB_MAX_AGE_S
    except Exception:  # noqa: BLE001
        return True


def ensure_database(force: bool = False) -> bool:
    """Make a fresh .mmdb exist at the configured path via the source chain
    (local → store → MaxMind → stale local). Serialized across processes
    with a lock file so N workers => 1 fetch. Returns True when a usable
    file is present afterwards. Never raises."""
    path = _mmdb_path()
    _state["path"] = path
    _state["last_attempt_iso"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if not force and _fresh(path):
        _state["source"] = "reused"
        _state["file_age_s"] = round(_file_age_s(path) or 0)
        return True
    lock_path = path + ".lock"
    ok = False
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(lock_path, "w") as lf:
            try:
                import fcntl
                fcntl.flock(lf, fcntl.LOCK_EX)
            except Exception:  # noqa: BLE001 — no fcntl: best effort
                pass
            if not force and _fresh(path):          # another worker finished first
                _state["source"] = "reused"
                _state["file_age_s"] = round(_file_age_s(path) or 0)
                return True
            # Step 2: our store.
            if _from_store(path):
                _state["source"] = "pg"
                ok = True
            else:
                # Step 3: MaxMind direct — opt-in only (GEOIP_ALLOW_DIRECT=1),
                # and only if the store can't serve. Containers never take it.
                key = (os.environ.get("MAXMIND_LICENSE_KEY") or "").strip()
                if not ALLOW_DIRECT:
                    _state["last_error"] = ((_state.get("last_error") or "store empty")
                                            + " | direct download disabled (GEOIP_ALLOW_DIRECT!=1)")[:200]
                    log.warning("geoip_state: store cannot serve and direct download is disabled — "
                                "state lookup disabled until geoip_db_blob is refreshed")
                elif not key:
                    _state["last_error"] = (_state.get("last_error") or "") + " | MAXMIND_LICENSE_KEY unset"
                    log.info("geoip_state: no store blob and MAXMIND_LICENSE_KEY unset — state lookup disabled")
                elif force or _store_needs_direct():
                    if _download_and_extract(key, path):
                        _state["source"] = "downloaded"
                        ok = True
                        _seed_store(path, "direct_download")
    except OSError as e:
        _state["last_error"] = f"lock/dir failed: {e!r}"[:200]
        log.warning("geoip_state: %s", _state["last_error"])
    if ok:
        _state["file_age_s"] = 0
        return True
    if _usable(path):                              # step 4: stale beats none
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
                log.info("geoip_state: recovered after retry (%s)", _state["source"])
                return
        except Exception as e:  # noqa: BLE001
            log.warning("geoip_state: retry failed: %r", e)


def _start_retry_thread():
    if _reader is None and os.environ.get("GEOIP_DISABLE_RETRY") != "1":
        t = threading.Thread(target=_retry_loop, name="geoip-retry", daemon=True)
        t.start()


_initialize()
_start_retry_thread()


def available():
    """True when the reader is ready and lookup_region_code will work."""
    return _reader is not None


def status() -> dict:
    """Small, JSON-safe view for /healthz + geoip_health_canary."""
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
