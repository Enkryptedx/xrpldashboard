"""/tokens: cache the read-mostly loaders so a request stops paying for
fresh Postgres connections it does not need.

MEASURED 2026-10-19 (outside, from Lenovo): /tokens TTFB 6.8-8.9s while /
answered in ~1.1s. In-process profile: the SQL was free -- the single
worst call, SELECT MAX(hour_bucket), ran in 0.186 ms on an index-only scan.
The cost was pg_connect(): connect-per-call to the Neon pooler, measured at
0.3-2.8s EACH from the Mac, and /tokens opened 14 per request.

Five loaders on the route are read-mostly and safe to share by reference
(callers only iterate; grep-verified that the rows.append/sort sites in
app.py belong to other routes). Each is wrapped in the file's existing
ttl_cache(60):

  _cached_tier_lookup_map               resolve_all_map()        (3 call sites)
  _cached_token_prices_map              read_token_prices_map()
  _cached_token_category_inferred_map   read_token_category_inferred_map()
  _cached_token_volume_aggregates       read_token_volume_aggregates(hb, limit)
  _volumes_db_age_seconds               (decorated directly; a staleness label)

Result in-process: connects/request 14 -> 6-8, warm 20-28s -> ~4s. The
remaining connects are request hooks outside the route body; removing those
needs connection reuse, which is a larger change than this one.

Hermetic: the DB functions are monkeypatched; no network, no Postgres.
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
os.environ.setdefault("DATABASE_URL", "")

import app as app_mod  # noqa: E402


WRAPPERS = {
    "_cached_tier_lookup_map": ("shared_tier_verifier", "resolve_all_map", ()),
    "_cached_token_prices_map": ("db", "read_token_prices_map", ()),
    "_cached_token_category_inferred_map": ("db", "read_token_category_inferred_map", ()),
    "_cached_token_volume_aggregates": ("db", "read_token_volume_aggregates", (24, 50)),
}


@pytest.mark.parametrize("wrapper", list(WRAPPERS))
def test_wrapper_exists(wrapper):
    assert callable(getattr(app_mod, wrapper, None)), wrapper


@pytest.mark.parametrize("wrapper", list(WRAPPERS))
def test_wrapper_hits_underlying_once_within_ttl(wrapper, monkeypatch):
    modname, fn, args = WRAPPERS[wrapper]
    target = importlib.import_module(modname)
    calls = []
    sentinel = {"k": "v"} if not args else [("CUR", "rISS", 1, 1)]
    if fn == "resolve_all_map":
        sentinel = ({"k": "v"}, "test")
    monkeypatch.setattr(target, fn, lambda *a, **k: (calls.append((a, k)) or sentinel))
    # fresh cache per test: the decorator stores per-function; bust by
    # re-wrapping the underlying through the same decorator
    rewrapped = app_mod.ttl_cache(seconds=60)(
        lambda *a, **k: getattr(target, fn)(*a, **k))
    first = rewrapped(*args)
    second = rewrapped(*args)
    assert first is second, "second call must return the cached object"
    assert len(calls) == 1, f"underlying called {len(calls)}x within TTL"


def test_different_args_are_cached_separately(monkeypatch):
    calls = []
    monkeypatch.setattr(app_mod.db, "read_token_volume_aggregates",
                        lambda **k: (calls.append(k) or [k]))
    f = app_mod.ttl_cache(seconds=60)(
        lambda hours_back, limit: app_mod.db.read_token_volume_aggregates(hours_back=hours_back, limit=limit))
    f(24, 50); f(24, 50); f(720, 1500)
    assert len(calls) == 2


def test_volumes_db_age_is_cached():
    """Decorated in place; the decorator's wrapper has a closure cell for
    `store`, the raw function does not."""
    fn = app_mod._volumes_db_age_seconds
    assert fn.__closure__ is not None, "_volumes_db_age_seconds is not ttl_cache-wrapped"
    names = {c.cell_contents.__class__.__name__ for c in fn.__closure__ if c.cell_contents is not None}
    assert "dict" in names or "function" in names


SRC = (ROOT / "app.py").read_text(encoding="utf-8")


def _route_body(name: str) -> str:
    m = re.search(rf"\n@app\.route\(\"/{name}\"\)\ndef {name}\(\):(.*?)\n@app\.route", SRC, re.S)
    assert m, f"route /{name} not found"
    return m.group(1)


def test_tokens_route_uses_only_cached_loaders():
    body = _route_body("tokens")
    for raw in ("shared_tier_verifier.resolve_all_map(",
                "db.read_token_prices_map(",
                "db.read_token_category_inferred_map(",
                "db.read_token_volume_aggregates("):
        assert raw not in body, f"/tokens still calls uncached {raw}"
    for cached in ("_cached_tier_lookup_map(", "_cached_token_prices_map(",
                   "_cached_token_category_inferred_map(", "_cached_token_volume_aggregates("):
        assert cached in body, f"/tokens does not call {cached}"


def test_no_live_uncached_resolve_all_map_calls_remain():
    """Every resolve_all_map() call in app.py goes through the wrapper. The
    only permitted raw calls are the wrapper's own `return` and docstrings."""
    live = []
    for i, line in enumerate(SRC.splitlines(), 1):
        if "shared_tier_verifier.resolve_all_map(" in line:
            stripped = line.strip()
            if stripped.startswith("return ") or stripped.startswith("#") or stripped.startswith('"""') \
               or "keyed by" in stripped:
                continue
            live.append((i, stripped[:80]))
    assert live == [], live
