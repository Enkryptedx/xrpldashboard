"""geoip_health_canary.evaluate — OPEN + CLOSED per the publish-gate rule."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import geoip_health_canary as C  # noqa: E402


def test_healthy_is_zero_findings():
    h = {"status": "ok", "geoip": {"available": True, "source": "pg", "last_error": None}}
    assert C.evaluate(h, rows=200, null_rows=5) == []


def test_unavailable_geoip_is_a_finding_with_reason():
    h = {"status": "ok", "geoip": {"available": False, "source": None,
                                   "last_error": "fetch/extract failed: <HTTPError 429: 'Too Many Requests'>"}}
    f = C.evaluate(h, rows=200, null_rows=5)
    assert [x["reason"] for x in f] == ["geoip_unavailable"]
    assert "429" in f[0]["last_error"]


def test_null_ratio_pages_even_when_healthz_looks_fine():
    h = {"status": "ok", "geoip": {"available": True}}
    f = C.evaluate(h, rows=100, null_rows=95)
    assert [x["reason"] for x in f] == ["region_null_ratio"]
    assert f[0]["ratio"] == 0.95


def test_low_traffic_window_does_not_page_on_ratio():
    h = {"status": "ok", "geoip": {"available": True}}
    assert C.evaluate(h, rows=5, null_rows=5) == []


def test_missing_geoip_field_is_a_finding():
    assert [x["reason"] for x in C.evaluate({"status": "ok"}, 100, 0)] == ["healthz_no_geoip_field"]
    assert [x["reason"] for x in C.evaluate(None, None, None)] == ["healthz_no_geoip_field"]
