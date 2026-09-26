"""geoip_state: local → our Postgres store → MaxMind direct (only when the
store can't serve) → stale local; one fetch per host under a lock; fail-open;
visible status. Regression for the 2026-09-26 MaxMind daily-download-limit
incident (3 workers × ~40 deploys/day on a 30/24h GeoLite limit → region_code
NULL for every visit, invisible for 5 h)."""
from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import importlib
import os
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import db  # noqa: E402

PAYLOAD = b"\0" * 2_000_000


@pytest.fixture
def G(monkeypatch, tmp_path):
    """Fresh module import: no key, no retry thread, temp mmdb path, and the
    store stubbed EMPTY (never touch a real Postgres from tests)."""
    monkeypatch.delenv("MAXMIND_LICENSE_KEY", raising=False)
    monkeypatch.setenv("GEOIP_DISABLE_RETRY", "1")
    monkeypatch.setenv("GEOIP_MMDB_PATH", str(tmp_path / "GeoLite2-City.mmdb"))
    monkeypatch.setattr(db, "pg_available", lambda: False)
    monkeypatch.setattr(db, "read_geoip_db_blob_meta", lambda edition="GeoLite2-City": None)
    monkeypatch.setattr(db, "read_geoip_db_blob", lambda edition="GeoLite2-City": None)
    writes = []
    monkeypatch.setattr(db, "write_geoip_db_blob",
                        lambda edition, gz, sha, source, fetched_at=None: (writes.append((edition, len(gz), sha, source)) or True))
    sys.modules.pop("geoip_state", None)
    mod = importlib.import_module("geoip_state")
    mod._test_store_writes = writes
    return mod


def _fake_download(calls, succeed=True):
    def _dl(key, dest):
        calls.append(dest)
        if succeed:
            with open(dest, "wb") as f:
                f.write(PAYLOAD)
        return succeed
    return _dl


def _stub_store(monkeypatch, age_s=60):
    fetched = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=age_s)
    sha = hashlib.sha256(PAYLOAD).hexdigest()
    gz = gzip.compress(PAYLOAD, compresslevel=1)
    monkeypatch.setattr(db, "read_geoip_db_blob_meta", lambda edition="GeoLite2-City": (fetched, sha, len(gz)))
    monkeypatch.setattr(db, "read_geoip_db_blob", lambda edition="GeoLite2-City": (gz, fetched, sha))


def test_no_key_no_store_no_file_is_disabled_and_fail_open(G):
    assert G.available() is False
    assert G.lookup_region_code("8.8.8.8") is None
    s = G.status()
    assert s["available"] is False and "MAXMIND_LICENSE_KEY unset" in (s["last_error"] or "")


def test_fresh_local_file_is_reused_without_any_fetch(G, monkeypatch):
    calls = []
    monkeypatch.setenv("MAXMIND_LICENSE_KEY", "k")
    monkeypatch.setattr(G, "_download_and_extract", _fake_download(calls))
    with open(G._mmdb_path(), "wb") as f:
        f.write(PAYLOAD)
    assert G.ensure_database() is True
    assert calls == []
    assert G.status()["source"] == "reused"


def test_store_serves_boot_with_zero_maxmind_calls(G, monkeypatch):
    calls = []
    monkeypatch.setenv("MAXMIND_LICENSE_KEY", "k")
    monkeypatch.setattr(G, "_download_and_extract", _fake_download(calls))
    _stub_store(monkeypatch, age_s=3600)
    assert G.ensure_database() is True
    assert calls == [], "store must be preferred over MaxMind"
    assert G.status()["source"] == "pg"
    assert os.path.getsize(G._mmdb_path()) == len(PAYLOAD)
    # And with NO key at all the store still serves.
    monkeypatch.delenv("MAXMIND_LICENSE_KEY")
    os.unlink(G._mmdb_path())
    assert G.ensure_database() is True and G.status()["source"] == "pg"


def test_store_too_old_falls_to_direct_and_seeds_store(G, monkeypatch):
    calls = []
    monkeypatch.setenv("MAXMIND_LICENSE_KEY", "k")
    monkeypatch.setattr(G, "_download_and_extract", _fake_download(calls))
    _stub_store(monkeypatch, age_s=G.BLOB_MAX_AGE_S + 100)
    assert G.ensure_database() is True
    assert len(calls) == 1 and G.status()["source"] == "downloaded"
    assert len(G._test_store_writes) == 1 and G._test_store_writes[0][3] == "direct_download"


def test_missing_everything_downloads_once_and_second_worker_reuses(G, monkeypatch):
    calls = []
    monkeypatch.setenv("MAXMIND_LICENSE_KEY", "k")
    monkeypatch.setattr(G, "_download_and_extract", _fake_download(calls))
    assert G.ensure_database() is True
    assert G.ensure_database() is True
    assert len(calls) == 1


def test_store_sha_mismatch_is_rejected(G, monkeypatch):
    fetched = dt.datetime.now(dt.timezone.utc)
    gz = gzip.compress(PAYLOAD, compresslevel=1)
    monkeypatch.setattr(db, "read_geoip_db_blob_meta", lambda edition="GeoLite2-City": (fetched, "bad", len(gz)))
    monkeypatch.setattr(db, "read_geoip_db_blob", lambda edition="GeoLite2-City": (gz, fetched, "bad"))
    assert G.ensure_database() is False
    assert "sha256 mismatch" in (G.status()["last_error"] or "")


def test_download_failure_keeps_stale_file_and_reports_error(G, monkeypatch):
    calls = []
    monkeypatch.setenv("MAXMIND_LICENSE_KEY", "k")
    monkeypatch.setattr(G, "_download_and_extract", _fake_download(calls, succeed=False))
    p = G._mmdb_path()
    with open(p, "wb") as f:
        f.write(PAYLOAD)
    old = time.time() - G.MMDB_MAX_AGE_S - 10
    os.utime(p, (old, old))
    assert G.ensure_database() is True
    assert G.status()["source"] == "reused-stale"
    os.unlink(p)
    assert G.ensure_database() is False


def test_build_script_never_fails_the_build(G, tmp_path):
    import subprocess
    env = dict(os.environ, GEOIP_MMDB_PATH=str(tmp_path / "x.mmdb"), GEOIP_DISABLE_RETRY="1", DATABASE_URL="")
    env.pop("MAXMIND_LICENSE_KEY", None)
    r = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "fetch_geoip_db.py")],
                       capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0, r.stderr
    assert "available=False" in r.stdout and "WARNING" in r.stdout
