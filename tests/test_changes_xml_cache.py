"""In-process cache for /changes.xml (Charlie 2026-10-03).

The live feed took ~14s on every hit because it does up to 31 serial remote
Postgres round-trips (one dates query + one envelope read per date) and the HTTP
cache header is bypassed at the Cloudflare edge (DYNAMIC). The envelopes change
once a day, so the route now caches the finished body in-process for 15 minutes.

These tests prove:
  (a) a second call within the window does NOT hit the DB again,
  (b) after the window it rebuilds,
  (c) a failed rebuild does NOT overwrite a good cached copy,
  (d) the cached body is byte-identical to the uncached build.
"""

import importlib

import pytest

import app as app_module


@pytest.fixture(autouse=True)
def _reset_changes_cache():
    """Each test starts with an empty cache and restores builder/state after."""
    app_module._changes_xml_cache["body"] = None
    app_module._changes_xml_cache["built_at"] = 0.0
    orig_list = app_module._list_changes_dates
    orig_load = app_module._load_changes_envelope
    yield
    app_module._list_changes_dates = orig_list
    app_module._load_changes_envelope = orig_load
    app_module._changes_xml_cache["body"] = None
    app_module._changes_xml_cache["built_at"] = 0.0


def _fake_feed_data(monkeypatch):
    """Point the builder at deterministic fake envelopes + count DB reads.
    Returns a dict with the 'reads' counter so a test can assert no re-read.
    """
    calls = {"dates": 0, "envelope": 0}
    dates = ["2026-10-03", "2026-10-02", "2026-10-01"]

    def fake_list(limit=30):
        calls["dates"] += 1
        return list(dates)[:limit]

    def fake_load(d):
        calls["envelope"] += 1
        return {
            "date": d,
            "changes": [{"category": "amendments", "line": f"thing on {d}"}],
            "categories_no_change": ["fees"],
        }

    monkeypatch.setattr(app_module, "_list_changes_dates", fake_list)
    monkeypatch.setattr(app_module, "_load_changes_envelope", fake_load)
    return calls


def test_a_second_call_within_window_does_not_hit_db(monkeypatch):
    calls = _fake_feed_data(monkeypatch)
    b1 = app_module._changes_atom_body_cached(now=1000.0)
    reads_after_first = calls["envelope"]
    assert reads_after_first == 3  # one per date, first build
    # Second call 5 minutes later — within the 15-min window.
    b2 = app_module._changes_atom_body_cached(now=1000.0 + 300)
    assert b2 == b1
    assert calls["envelope"] == reads_after_first, (
        "second call within the window re-read the DB")
    assert calls["dates"] == 1, "dates query ran again within the window"


def test_b_rebuilds_after_window(monkeypatch):
    calls = _fake_feed_data(monkeypatch)
    app_module._changes_atom_body_cached(now=1000.0)
    first_reads = calls["envelope"]
    # 16 minutes later — past the 15-min TTL.
    app_module._changes_atom_body_cached(now=1000.0 + app_module._CHANGES_XML_TTL_S + 60)
    assert calls["envelope"] > first_reads, "cache did not rebuild after the window"
    assert calls["dates"] == 2


def test_c_failed_rebuild_keeps_good_copy(monkeypatch):
    calls = _fake_feed_data(monkeypatch)
    good = app_module._changes_atom_body_cached(now=1000.0)
    assert "thing on 2026-10-03" in good

    # Now make the builder fail, and advance past the TTL to force a rebuild.
    def boom(limit=30):
        raise RuntimeError("DB down")

    monkeypatch.setattr(app_module, "_list_changes_dates", boom)
    served = app_module._changes_atom_body_cached(
        now=1000.0 + app_module._CHANGES_XML_TTL_S + 60)
    assert served == good, "failed rebuild did not serve the last good copy"
    # The failure must NOT have been cached as the new body.
    assert app_module._changes_xml_cache["body"] == good
    # And a fresh process with no good copy re-raises instead of caching failure.
    app_module._changes_xml_cache["body"] = None
    app_module._changes_xml_cache["built_at"] = 0.0
    with pytest.raises(RuntimeError):
        app_module._changes_atom_body_cached(now=2000.0)
    assert app_module._changes_xml_cache["body"] is None


def test_d_cached_body_byte_identical_to_uncached(monkeypatch):
    _fake_feed_data(monkeypatch)
    uncached = app_module._build_changes_atom_body()
    cached = app_module._changes_atom_body_cached(now=1000.0)
    assert cached == uncached, "cached body differs from the direct build"
    # Byte-for-byte.
    assert cached.encode("utf-8") == uncached.encode("utf-8")


def test_e_route_returns_cached_body_200(monkeypatch):
    """End-to-end: the route serves the cached body with the unchanged header."""
    _fake_feed_data(monkeypatch)
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        r = c.get("/changes.xml")
    assert r.status_code == 200
    assert r.mimetype == "application/atom+xml"
    assert r.headers["Cache-Control"] == "public, max-age=1800, s-maxage=1800"
    assert b"thing on 2026-10-03" in r.data
