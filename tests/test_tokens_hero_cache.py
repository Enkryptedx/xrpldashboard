"""/tokens hero: lifted into a pure function and cached, because it was the
largest live sub-stage.

LIVE BEFORE PROBES (70dbe96, from Lenovo, 2026-10-19 13:12 ET):
  enrich.hero  531-1734 ms   sum 4372 ms over 4 probes  <- biggest
  rows.stats   780-1388 ms   sum 3956 ms
Warm floor ~530 ms was pure compute over the 1,500-row 30-day aggregate on
EVERY request; the cold extra was the aggregate refetch per gunicorn
worker (3 workers, 60 s per-process TTL).

WHY IT IS CACHEABLE. The hero is request-INDEPENDENT by design -- a fixed
30-day window regardless of the page's range selector (the comment above
it says so, and the AST confirms it reads no request-scoped name). The
only `enriched` reference in the old block was the error-path fallback.

THE SPLIT, and what each test pins:
  _build_hero_enriched_30d(tokens_meta)      fetch + shape the 30-day list
  _compute_hero_30d(hero_enriched, snapshot)  PURE: six outputs from a list
  _cached_hero_30d()                          ttl_cache(300) over both; RAISES
                                              on fetch failure so the route
                                              recomputes from `enriched`
Route: normal path uses the cache; fallback path calls the same pure
function with the request's own list -- identical semantics to the old
inline try/except, verified by running the lifted code and the original
origin/main text in one process on the same input (equal on both the
30-day input and a 50-row fallback input).

Sub-stage `enrich.hero` now times only the cached call; the per-request
hex grid that used to share the mark is `enrich.hex`.

Hermetic: loaders monkeypatched; no network, no DB.
"""
from __future__ import annotations

import copy
import os
import pathlib
import re
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DATABASE_URL", "")

import app as app_mod  # noqa: E402


def row(cur, iss, trades, hours=24):
    return (cur, iss, trades, hours)


def enriched_item(display, category, labeled, trades):
    return {"display": display, "category": category, "labeled": labeled, "trades": trades}


SNAPSHOT = {"floor": {"pct": 20.5}}


# ── _build_hero_enriched_30d ────────────────────────────────────────
class TestBuild:
    def test_shapes_rows_using_tokens_meta(self, monkeypatch):
        monkeypatch.setattr(app_mod, "_cached_token_volume_aggregates",
                            lambda **k: [row("RLUSD", "rR", 100), row("DEADBEEF" * 5, "rX", 5)])
        meta = {("RLUSD", "rR"): {"currency_display": "RLUSD", "category": "stablecoin"}}
        out = app_mod._build_hero_enriched_30d(meta)
        assert out[0] == {"display": "RLUSD", "category": "stablecoin", "labeled": True, "trades": 100}
        assert out[1]["labeled"] is False and out[1]["category"] is None

    def test_requests_the_fixed_30_day_window(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(app_mod, "_cached_token_volume_aggregates",
                            lambda **k: (seen.update(k) or []))
        app_mod._build_hero_enriched_30d({})
        assert seen == {"hours_back": 24 * 30, "limit": 1500}

    def test_fetch_failure_propagates(self, monkeypatch):
        """The route relies on this to fall back; swallowing would hide it."""
        def boom(**k): raise RuntimeError("neon down")
        monkeypatch.setattr(app_mod, "_cached_token_volume_aggregates", boom)
        with pytest.raises(RuntimeError):
            app_mod._build_hero_enriched_30d({})


# ── _compute_hero_30d: pure ─────────────────────────────────────────
class TestComputePure:
    def _sample(self):
        return [
            enriched_item("RLUSD", "stablecoin", True, 600),
            enriched_item("SOLO", "other", True, 300),
            enriched_item("MEME1", "unlabeled", True, 50),
            enriched_item("????", None, False, 50),
        ]

    def test_returns_exactly_the_six_outputs(self):
        out = app_mod._compute_hero_30d(self._sample(), SNAPSHOT)
        assert set(out) == {"category_bars", "unlabeled_bar", "zone_b_lp",
                            "tier_summary", "rwa_caption", "floor_pct"}

    def test_does_not_mutate_its_input(self):
        src = self._sample(); before = copy.deepcopy(src)
        app_mod._compute_hero_30d(src, SNAPSHOT)
        assert src == before

    def test_deterministic(self):
        a = app_mod._compute_hero_30d(self._sample(), SNAPSHOT)
        b = app_mod._compute_hero_30d(self._sample(), SNAPSHOT)
        assert a == b

    def test_floor_pct_from_snapshot_with_default(self):
        assert app_mod._compute_hero_30d(self._sample(), {"floor": {"pct": 33.3}})["floor_pct"] == 33.3
        assert app_mod._compute_hero_30d(self._sample(), {})["floor_pct"] == 20.5

    def test_empty_input_does_not_crash(self):
        out = app_mod._compute_hero_30d([], SNAPSHOT)
        assert "category_bars" in out


# ── _cached_hero_30d ────────────────────────────────────────────────
class TestCached:
    def test_hits_loaders_once_within_ttl(self, monkeypatch):
        calls = []
        monkeypatch.setattr(app_mod, "_cached_token_volume_aggregates",
                            lambda **k: (calls.append("agg") or [row("RLUSD", "rR", 10)]))
        monkeypatch.setattr(app_mod, "_load_token_names_dict", lambda: {})
        monkeypatch.setattr(app_mod, "_load_d1_hero_snapshot", lambda: SNAPSHOT)
        f = app_mod.ttl_cache(seconds=300)(lambda: app_mod._compute_hero_30d(
            app_mod._build_hero_enriched_30d(app_mod._load_token_names_dict()),
            app_mod._load_d1_hero_snapshot()))
        a = f(); b = f()
        assert a is b and calls == ["agg"]

    def test_is_ttl_cache_wrapped(self):
        assert app_mod._cached_hero_30d.__closure__ is not None


# ── the route: normal path vs fallback path ─────────────────────────
class TestRouteWiring:
    @pytest.fixture
    def base(self, monkeypatch):
        monkeypatch.setattr(app_mod.db, "pg_available", lambda: True)
        monkeypatch.setattr(app_mod, "_cached_tier_lookup_map", lambda: ({}, "test"))
        monkeypatch.setattr(app_mod, "_cached_token_prices_map", lambda: {})
        monkeypatch.setattr(app_mod, "_cached_token_category_inferred_map", lambda: {})
        monkeypatch.setattr(app_mod, "_cached_token_volume_aggregates",
                            lambda **k: [row("RLUSD", "rR", 10)])
        # These two wrappers arrive with the stats/warnings cache PR; stub the
        # raw db calls too so the fixture works on either base.
        monkeypatch.setattr(app_mod, "_cached_token_volume_bucket_stats", lambda: (1, 2, 3), raising=False)
        monkeypatch.setattr(app_mod, "_cached_token_warnings_recent", lambda **k: [], raising=False)
        monkeypatch.setattr(app_mod.db, "read_token_volume_bucket_stats", lambda: (1, 2, 3))
        monkeypatch.setattr(app_mod.db, "read_token_warnings_recent", lambda **k: [])
        monkeypatch.setattr(app_mod, "_volumes_db_age_seconds", lambda: None)
        return app_mod.app.test_client()

    def test_normal_path_uses_cached_hero(self, base, monkeypatch):
        calls = []
        monkeypatch.setattr(app_mod, "_cached_hero_30d",
                            lambda: (calls.append(1) or app_mod._compute_hero_30d([], SNAPSHOT)))
        r = base.get("/tokens")
        assert r.status_code == 200 and calls == [1]

    def test_fallback_when_cached_hero_raises(self, base, monkeypatch):
        """Old semantics: on failure, build the hero from the request's list."""
        def boom(): raise RuntimeError("neon down")
        monkeypatch.setattr(app_mod, "_cached_hero_30d", boom)
        seen = []
        real = app_mod._compute_hero_30d
        monkeypatch.setattr(app_mod, "_compute_hero_30d",
                            lambda he, hs: (seen.append(len(he)) or real(he, hs)))
        r = base.get("/tokens")
        assert r.status_code == 200
        assert seen == [1], "fallback must compute from the request's enriched list (1 row here)"

    def test_substages_split_hero_and_hex(self, base):
        st = base.get("/tokens").headers["Server-Timing"]
        names = [n for n, _ in re.findall(r"([\w.]+);dur=(\d+)", st)]
        assert "enrich.hero" in names and "enrich.hex" in names
        assert names.index("enrich.hero") < names.index("enrich.hex")


def test_route_no_longer_inlines_the_hero():
    src = (ROOT / "app.py").read_text(encoding="utf-8")
    m = re.search(r'\n@app\.route\("/tokens"\)\ndef tokens\(\):(.*?)\n@app\.route', src, re.S)
    body = m.group(1)
    assert "_cached_hero_30d()" in body and "_compute_hero_30d(hero_enriched, hero_snapshot)" in body
    assert "zone_a_total =" not in body and "cat_order =" not in body, "hero math must live in _compute_hero_30d"
