"""geoip_state: one download per deploy, reuse across workers, fail-open,
visible status. Regression for the 2026-09-26 MaxMind daily-download-limit
incident (3 workers × ~40 deploys/day on a 30/24h GeoLite limit → region_code
NULL for every visit, invisible for 5 h)."""
from __future__ import annotations

import importlib
import os
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


@pytest.fixture
def G(monkeypatch, tmp_path):
    """Fresh module import with no key, no retry thread, temp mmdb path."""
    monkeypatch.delenv("MAXMIND_LICENSE_KEY", raising=False)
    monkeypatch.setenv("GEOIP_DISABLE_RETRY", "1")
    monkeypatch.setenv("GEOIP_MMDB_PATH", str(tmp_path / "GeoLite2-City.mmdb"))
    sys.modules.pop("geoip_state", None)
    mod = importlib.import_module("geoip_state")
    return mod


def _fake_download(calls, succeed=True, size=2_000_000):
    def _dl(key, dest):
        calls.append(dest)
        if succeed:
            with open(dest, "wb") as f:
                f.write(b"\0" * size)
        return succeed
    return _dl


def test_no_key_no_file_is_disabled_and_fail_open(G):
    assert G.available() is False
    assert G.lookup_region_code("8.8.8.8") is None
    s = G.status()
    assert s["available"] is False and "MAXMIND_LICENSE_KEY unset" in (s["last_error"] or "")


def test_fresh_file_is_reused_without_download(G, monkeypatch):
    calls = []
    monkeypatch.setenv("MAXMIND_LICENSE_KEY", "k")
    monkeypatch.setattr(G, "_download_and_extract", _fake_download(calls))
    p = G._mmdb_path()
    with open(p, "wb") as f:
        f.write(b"\0" * 2_000_000)
    assert G.ensure_database() is True
    assert calls == [], "fresh file must not trigger a download"
    assert G.status()["source"] == "reused"


def test_missing_file_downloads_once_and_second_worker_reuses(G, monkeypatch):
    calls = []
    monkeypatch.setenv("MAXMIND_LICENSE_KEY", "k")
    monkeypatch.setattr(G, "_download_and_extract", _fake_download(calls))
    assert G.ensure_database() is True
    assert len(calls) == 1
    # A second worker (same host, same path) reuses the file.
    assert G.ensure_database() is True
    assert len(calls) == 1


def test_stale_file_triggers_one_download(G, monkeypatch):
    calls = []
    monkeypatch.setenv("MAXMIND_LICENSE_KEY", "k")
    monkeypatch.setattr(G, "_download_and_extract", _fake_download(calls))
    p = G._mmdb_path()
    with open(p, "wb") as f:
        f.write(b"\0" * 2_000_000)
    old = time.time() - G.MMDB_MAX_AGE_S - 10
    os.utime(p, (old, old))
    assert G.ensure_database() is True
    assert len(calls) == 1


def test_download_failure_keeps_stale_file_and_reports_error(G, monkeypatch):
    calls = []
    monkeypatch.setenv("MAXMIND_LICENSE_KEY", "k")
    monkeypatch.setattr(G, "_download_and_extract", _fake_download(calls, succeed=False))
    p = G._mmdb_path()
    with open(p, "wb") as f:
        f.write(b"\0" * 2_000_000)
    old = time.time() - G.MMDB_MAX_AGE_S - 10
    os.utime(p, (old, old))
    assert G.ensure_database() is True          # stale beats none
    assert G.status()["source"] == "reused-stale"
    # And with no file at all: honest False, no exception.
    os.unlink(p)
    assert G.ensure_database() is False


def test_build_script_never_fails_the_build(G, tmp_path):
    import subprocess
    env = dict(os.environ, GEOIP_MMDB_PATH=str(tmp_path / "x.mmdb"), GEOIP_DISABLE_RETRY="1")
    env.pop("MAXMIND_LICENSE_KEY", None)
    r = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "fetch_geoip_db.py")],
                       capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0, r.stderr
    assert "available=False" in r.stdout and "WARNING" in r.stdout
