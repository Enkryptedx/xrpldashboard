"""Prove which build answered, and where /tokens spends its time.

WHY. A cache PR on 2026-10-19 cut /tokens' Postgres connects 14 -> 6-8 and
moved outside TTFB not at all (6.8-8.9s before, 7.2-8.8s after). Two
blind spots made that possible: the profile was taken on the Mac, where
connect latency dominates and on Render does not; and nothing exposed the
running commit, so "has the deploy rolled?" was an assumption. This closes
both.

  X-Commit          every response; RENDER_GIT_COMMIT[:12] or "unknown"
  /healthz.commit   same value, both the ok and unhealthy branches
  Server-Timing     /tokens only: rows, maps, enrich, render, total (ms)
  X-Response-Bytes  /tokens only: exact UTF-8 body length

Server-Timing is the W3C header; browsers surface it in devtools and curl
shows it with -D. The stage boundaries are the route's own: rows = the
ranked aggregate fetch; maps = tier/price lookups (cached, expect ~0);
enrich = the per-row loop incl. the 30d hero and inferred map; render =
Jinja. Hermetic: DB loaders are monkeypatched; no network.
"""
from __future__ import annotations

import importlib
import os
import pathlib
import re
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _fresh_app(monkeypatch, commit):
    """Import app with a chosen RENDER_GIT_COMMIT (module constant is read
    at import time, so reload)."""
    if commit is None:
        monkeypatch.delenv("RENDER_GIT_COMMIT", raising=False)
    else:
        monkeypatch.setenv("RENDER_GIT_COMMIT", commit)
    monkeypatch.setenv("DATABASE_URL", "")
    sys.modules.pop("app", None)
    return importlib.import_module("app")


class TestCommitIdentity:
    def test_truncated_to_12_from_render_env(self, monkeypatch):
        a = _fresh_app(monkeypatch, "0123456789abcdef0123456789abcdef01234567")
        assert a._GIT_COMMIT == "0123456789ab"

    def test_unknown_off_render(self, monkeypatch):
        a = _fresh_app(monkeypatch, None)
        assert a._GIT_COMMIT == "unknown"

    def test_whitespace_env_is_unknown(self, monkeypatch):
        a = _fresh_app(monkeypatch, "   ")
        assert a._GIT_COMMIT == "unknown"

    def test_x_commit_on_every_response(self, monkeypatch):
        a = _fresh_app(monkeypatch, "feedfacefeedface")
        c = a.app.test_client()
        for path in ("/robots.txt", "/healthz", "/nonexistent-404-path"):
            r = c.get(path)
            assert r.headers.get("X-Commit") == "feedfacefeed", path

    def test_healthz_ok_branch_carries_commit(self, monkeypatch):
        a = _fresh_app(monkeypatch, "cafebabecafebabe")
        monkeypatch.setattr(a.db, "ping", lambda *x, **k: True, raising=False)
        r = a.app.test_client().get("/healthz")
        body = r.get_json()
        assert body.get("commit") == "cafebabecafe"

    def test_healthz_unhealthy_branch_carries_commit(self, monkeypatch):
        a = _fresh_app(monkeypatch, "cafebabecafebabe")
        src = (ROOT / "app.py").read_text(encoding="utf-8")
        # the 503 branch is a literal dict; pin that it includes the key
        m = re.search(r'return \{"status": "unhealthy".*?\}, 503', src)
        assert m and '"commit": _GIT_COMMIT' in m.group(0)


class TestTokensServerTiming:
    @pytest.fixture
    def client(self, monkeypatch):
        a = _fresh_app(monkeypatch, "abc123abc123abc1")
        # stub every DB-touching loader the route uses
        monkeypatch.setattr(a.db, "pg_available", lambda: False)
        monkeypatch.setattr(a, "_cached_tier_lookup_map", lambda: ({}, "test"))
        monkeypatch.setattr(a, "_cached_token_prices_map", lambda: {})
        monkeypatch.setattr(a, "_cached_token_category_inferred_map", lambda: {})
        monkeypatch.setattr(a, "_cached_token_volume_aggregates", lambda **k: [])
        monkeypatch.setattr(a, "_volumes_db_age_seconds", lambda: None)
        return a.app.test_client()

    def test_header_shape(self, client):
        r = client.get("/tokens")
        assert r.status_code == 200
        st = r.headers.get("Server-Timing")
        assert st, "Server-Timing missing"
        names = [n for n, _ in re.findall(r"([\w.]+);dur=(\d+)", st)]
        assert names[:5] == ["rows", "maps", "enrich", "render", "total"]
        # sub-stages follow the five stage totals (added 2026-10-19 so the
        # live site names the slow CALL, not just the slow stage)
        subs = set(names[5:])
        # Always present (this client stubs pg_available=False):
        assert {"rows.files", "enrich.loop", "enrich.hero", "enrich.hex", "enrich.inferred", "enrich.tail"} <= subs, subs
        # Postgres-only marks must NOT appear when Postgres is off -- a
        # sub-stage that did not run must not report 0 and look cheap:
        assert not ({"rows.agg", "rows.stats", "enrich.ticker", "enrich.warnings"} & subs), subs

    def test_substages_sum_to_their_stage(self, client):
        st = client.get("/tokens").headers["Server-Timing"]
        d = {n: int(v) for n, v in re.findall(r"([\w.]+);dur=(\d+)", st)}
        rows_sub = sum(v for k, v in d.items() if k.startswith("rows."))
        enr_sub = sum(v for k, v in d.items() if k.startswith("enrich."))
        assert abs(d["rows"] - rows_sub) <= 3, (d["rows"], rows_sub)
        assert abs(d["enrich"] - enr_sub) <= 3, (d["enrich"], enr_sub)

    def test_total_is_sum_of_stages_within_rounding(self, client):
        st = client.get("/tokens").headers["Server-Timing"]
        d = {n: int(v) for n, v in re.findall(r"([\w.]+);dur=(\d+)", st)}
        assert abs(d["total"] - (d["rows"] + d["maps"] + d["enrich"] + d["render"])) <= 4

    def test_response_bytes_is_exact(self, client):
        r = client.get("/tokens")
        assert r.headers.get("X-Response-Bytes") == str(len(r.data))

    def test_commit_also_on_tokens(self, client):
        assert client.get("/tokens").headers.get("X-Commit") == "abc123abc123"

    def test_only_tokens_emits_server_timing(self, client):
        assert "Server-Timing" not in client.get("/robots.txt").headers
