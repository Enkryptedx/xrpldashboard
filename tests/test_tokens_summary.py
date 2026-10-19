"""/tokens precomputed summary: walker, row, route, labels endpoint.

House pattern from /nfts, applied to /tokens on 2026-10-19 after live
Server-Timing showed cold-worker TTFB of 7-12 s: 3 dynos x 3 gunicorn
workers each held a private ttl_cache, so most visitors paid full compute.

WHAT THIS PINS
  db        per-column content hashes gate the write: unchanged columns are
            NOT rewritten (the 1.3 MB label blob stays in TOAST), computed_at
            advances on every tick regardless; ensure_*_table sets
            lock_timeout BEFORE the DDL and returns False on LockNotAvailable;
            read skips the label blob unless asked.
  route     three states from one read -- ok / stale (still served, labelled)
            / warming (live path) -- and that the page takes rows, stats,
            maps, hero and warnings from the row when present.
  labels    /tokens/labels.json serves the walker's blob with a public
            5-min Cache-Control and an ETag that 304s; builds live when
            warming; the page inlines only the visible rows.
  walker    _jsonable normalises Decimal / tuple keys / tuple rows; the
            contract check that every db.* and app_mod.* name it references
            exists (a misspelled helper is swallowed by the write guard and
            shows up as a row that never appears).

Hermetic: cursors are fakes, loaders are stubbed. No network, no DB.
"""
from __future__ import annotations

import datetime
import decimal
import importlib.util
import json
import os
import pathlib
import re
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DATABASE_URL", "")

import db  # noqa: E402
import app as app_mod  # noqa: E402

NOW = datetime.datetime(2026, 10, 19, 23, 0, tzinfo=datetime.timezone.utc)


class FakeCursor:
    def __init__(self, fetch=None):
        self.executed = []
        self._fetch = list(fetch or [])

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self._fetch.pop(0) if self._fetch else None

    def __enter__(self): return self
    def __exit__(self, *a): return False


class FakeConn:
    def __init__(self, cur):
        self.cur = cur; self.committed = 0
    def cursor(self): return self.cur
    def commit(self): self.committed += 1
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _use(monkeypatch, cur):
    conn = FakeConn(cur)
    monkeypatch.setattr(db, "pg_available", lambda: True)
    monkeypatch.setattr(db, "pg_connect", lambda: conn)
    return conn


# ── db: hashing + gated write ───────────────────────────────────────
class TestHashing:
    def test_jsonb_hash_ignores_key_order(self):
        a = db._tokens_summary_hash("hero", {"x": 1, "y": [1, 2]})
        b = db._tokens_summary_hash("hero", {"y": [1, 2], "x": 1})
        assert a == b

    def test_text_hash_is_content(self):
        assert db._tokens_summary_hash("label_lookup_json", "abc") != db._tokens_summary_hash("label_lookup_json", "abd")

    def test_none_hashes_distinct_from_empty(self):
        assert db._tokens_summary_hash("hero", None) != db._tokens_summary_hash("hero", {})


class TestWriteGating:
    VALUES = {c: {"v": c} if c in db._TOKENS_SUMMARY_JSONB else ("blob" if c == "label_lookup_json" else 7)
              for c in db.TOKENS_SUMMARY_COLUMNS}

    def test_first_write_inserts_every_column(self, monkeypatch):
        cur = FakeCursor(fetch=[None])            # SELECT 1 -> no row
        conn = _use(monkeypatch, cur)
        changed = db.write_tokens_summary(self.VALUES, prior_hashes={})
        assert set(changed) == set(db.TOKENS_SUMMARY_COLUMNS)
        sql = cur.executed[-1][0]
        assert sql.startswith("INSERT INTO tokens_summary")
        assert "col_hashes" in sql and "computed_at" in sql
        assert conn.committed == 1

    def test_unchanged_touches_only_computed_at(self, monkeypatch):
        prior = {c: db._tokens_summary_hash(c, v) for c, v in self.VALUES.items()}
        cur = FakeCursor(fetch=[(1,)])            # row exists
        _use(monkeypatch, cur)
        changed = db.write_tokens_summary(self.VALUES, prior_hashes=prior)
        assert changed == []
        sql = cur.executed[-1][0]
        assert sql == "UPDATE tokens_summary SET computed_at = now() WHERE id = 1"
        assert "label_lookup_json" not in sql

    def test_partial_change_writes_only_changed_columns(self, monkeypatch):
        prior = {c: db._tokens_summary_hash(c, v) for c, v in self.VALUES.items()}
        new = dict(self.VALUES); new["hero"] = {"v": "different"}
        cur = FakeCursor(fetch=[(1,)])
        _use(monkeypatch, cur)
        changed = db.write_tokens_summary(new, prior_hashes=prior)
        assert changed == ["hero"]
        sql, params = cur.executed[-1]
        assert sql.startswith("UPDATE tokens_summary SET hero = %s::jsonb, col_hashes")
        assert "label_lookup_json" not in sql, "the 1.3 MB blob must not be rewritten when unchanged"
        assert "changed_at = now()" in sql and "computed_at = now()" in sql
        assert json.loads(params[-1])["hero"] == db._tokens_summary_hash("hero", new["hero"])


class TestEnsureTable:
    def test_lock_timeout_precedes_ddl(self, monkeypatch):
        cur = FakeCursor(); conn = _use(monkeypatch, cur)
        assert db.ensure_tokens_summary_table() is True
        assert cur.executed[0][0] == "SET lock_timeout = '3s'"
        assert cur.executed[1][0].startswith("CREATE TABLE IF NOT EXISTS tokens_summary")
        assert conn.committed == 1

    def test_lock_not_available_returns_false(self, monkeypatch):
        import psycopg
        class Cur(FakeCursor):
            def execute(self, sql, params=None):
                super().execute(sql, params)
                if "CREATE TABLE" in sql:   # DDL string starts with a newline
                    raise psycopg.errors.LockNotAvailable("canceling statement due to lock timeout")
        conn = _use(monkeypatch, Cur())
        assert db.ensure_tokens_summary_table() is False
        assert conn.committed == 0

    def test_pg_unavailable_is_false_not_raise(self, monkeypatch):
        monkeypatch.setattr(db, "pg_available", lambda: False)
        assert db.ensure_tokens_summary_table() is False


class TestRead:
    def test_default_skips_label_blob(self, monkeypatch):
        cur = FakeCursor(fetch=[tuple(range(9)) + (None, NOW)])
        _use(monkeypatch, cur)
        row = db.read_tokens_summary()
        assert "label_lookup_json" not in cur.executed[0][0]
        assert row["label_lookup_json"] is None and row["computed_at"] == NOW

    def test_include_labels_reads_it(self, monkeypatch):
        cur = FakeCursor(fetch=[tuple(range(10)) + (None, NOW)])
        _use(monkeypatch, cur)
        db.read_tokens_summary(include_labels=True)
        assert "label_lookup_json" in cur.executed[0][0]

    def test_read_never_raises(self, monkeypatch):
        class Boom(FakeCursor):
            def execute(self, *a): raise RuntimeError("relation does not exist")
        _use(monkeypatch, Boom())
        assert db.read_tokens_summary() is None


# ── route state machine ─────────────────────────────────────────────
class TestState:
    def test_ok(self, monkeypatch):
        monkeypatch.setattr(app_mod.db, "pg_available", lambda: True)
        monkeypatch.setattr(app_mod.db, "read_tokens_summary",
                            lambda include_labels=False: {"computed_at": datetime.datetime.now(datetime.timezone.utc)})
        row, state, age = app_mod._tokens_summary_state()
        assert state == "ok" and age < 5

    def test_stale_past_ceiling(self, monkeypatch):
        monkeypatch.setattr(app_mod.db, "pg_available", lambda: True)
        old = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=app_mod._TOKENS_SUMMARY_STALE_S + 60)
        monkeypatch.setattr(app_mod.db, "read_tokens_summary", lambda include_labels=False: {"computed_at": old})
        assert app_mod._tokens_summary_state()[1] == "stale"

    def test_warming_when_no_row(self, monkeypatch):
        monkeypatch.setattr(app_mod.db, "pg_available", lambda: True)
        monkeypatch.setattr(app_mod.db, "read_tokens_summary", lambda include_labels=False: None)
        assert app_mod._tokens_summary_state() == (None, "warming", None)

    def test_warming_when_pg_down(self, monkeypatch):
        monkeypatch.setattr(app_mod.db, "pg_available", lambda: False)
        assert app_mod._tokens_summary_state()[1] == "warming"


def test_decode_pair_keys():
    assert app_mod._decode_pair_keys({"RLUSD|rA": 1.5, "5841|rB": 2}) == {("RLUSD", "rA"): 1.5, ("5841", "rB"): 2}
    assert app_mod._decode_pair_keys(None) == {}


def test_inferred_lookup_matches_old_inline_rule():
    inferred = {("ABC", "rI"): {"display_labelled": "ABC · self-described", "category": "meme", "source": "toml"},
                ("DEF", "rJ"): {"currency_display": "DEF"}}
    tiers = {"ABC|rI": "verified"}
    out = app_mod._build_inferred_label_lookup(inferred, tiers)
    assert out["ABC|rI"] == {"display": "ABC · self-described", "category": "meme",
                             "category_source": "toml", "attestation": "verified"}
    assert out["DEF|rJ"] == {"display": "DEF", "category": "other",
                             "category_source": "toml", "attestation": "self-described"}


# ── the route, fed by a summary row ─────────────────────────────────
ROW = {
    "aggregates_24h": [["RLUSD", "rR", 10.0, 24]], "aggregates_7d": [["RLUSD", "rR", 70.0, 168]],
    "aggregates_all": [["RLUSD", "rR", 700.0, 1000]],
    "hero": None, "bucket_stats": [1, 2, 3], "warnings_recent": [],
    "tier_lookup": {"RLUSD|rR": "verified"}, "price_map": {"RLUSD|rR": 1.0},
    "label_lookup_json": None, "latest_bucket": 497903,
    "changed_at": NOW, "computed_at": None,
}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(app_mod.db, "pg_available", lambda: True)
    hero = app_mod._compute_hero_30d([], {"floor": {"pct": 20.5}})
    row = dict(ROW); row["hero"] = hero
    row["computed_at"] = datetime.datetime.now(datetime.timezone.utc)
    monkeypatch.setattr(app_mod.db, "read_tokens_summary", lambda include_labels=False: dict(row))
    # every live loader explodes: the row must be enough
    for name in ("_cached_token_volume_aggregates", "_cached_token_volume_bucket_stats",
                 "_cached_tier_lookup_map", "_cached_token_prices_map", "_cached_hero_30d",
                 "_cached_token_warnings_recent", "_cached_token_category_inferred_map"):
        monkeypatch.setattr(app_mod, name, lambda *a, **k: (_ for _ in ()).throw(AssertionError(f"{name} called")))
    monkeypatch.setattr(app_mod, "_volumes_db_age_seconds", lambda: (_ for _ in ()).throw(AssertionError("live age")))
    return app_mod.app.test_client()


class TestRouteFromRow:
    def test_renders_from_row_without_live_loaders(self, client):
        r = client.get("/tokens")
        assert r.status_code == 200
        assert r.headers["X-Tokens-Summary-State"] == "ok"
        assert b"Summary warming up" not in r.data and b"running behind" not in r.data

    def test_range_selector_picks_the_matching_list(self, client):
        for rng in ("24h", "7d", "all"):
            assert client.get(f"/tokens?range={rng}").status_code == 200

    def test_inline_labels_are_only_the_visible_rows(self, client):
        body = client.get("/tokens").data.decode()
        inl = re.search(r'id="token-labels-data">(.*?)</script>', body, re.S).group(1)
        assert len(inl) < 20_000, len(inl)
        assert "RLUSD|rR" in inl

    def test_page_fetches_the_labels_endpoint(self, client):
        assert b"/tokens/labels.json" in client.get("/tokens").data

    def test_stale_row_still_served_with_banner(self, client, monkeypatch):
        old = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=5000)
        row = dict(ROW); row["hero"] = app_mod._compute_hero_30d([], {}); row["computed_at"] = old
        monkeypatch.setattr(app_mod.db, "read_tokens_summary", lambda include_labels=False: dict(row))
        r = client.get("/tokens")
        assert r.status_code == 200 and r.headers["X-Tokens-Summary-State"] == "stale"
        assert b"running behind" in r.data


class TestLabelsEndpoint:
    def test_serves_blob_with_cache_and_etag(self, monkeypatch):
        monkeypatch.setattr(app_mod.db, "pg_available", lambda: True)
        row = dict(ROW); row["label_lookup_json"] = '{"A|r":{"display":"A"}}'
        row["computed_at"] = datetime.datetime.now(datetime.timezone.utc)
        monkeypatch.setattr(app_mod.db, "read_tokens_summary", lambda include_labels=False: dict(row))
        c = app_mod.app.test_client()
        r = c.get("/tokens/labels.json")
        assert r.status_code == 200 and r.get_json() == {"A|r": {"display": "A"}}
        assert r.headers["Cache-Control"] == "public, max-age=300, s-maxage=300"
        et = r.headers["ETag"]; assert et.startswith('"')
        assert c.get("/tokens/labels.json", headers={"If-None-Match": et}).status_code == 304

    def test_warming_builds_live(self, monkeypatch):
        monkeypatch.setattr(app_mod.db, "pg_available", lambda: True)
        monkeypatch.setattr(app_mod.db, "read_tokens_summary", lambda include_labels=False: None)
        monkeypatch.setattr(app_mod, "_cached_tier_lookup_map", lambda: ({}, "t"))
        monkeypatch.setattr(app_mod, "_cached_token_category_inferred_map",
                            lambda: {("Z", "rZ"): {"currency_display": "Z"}})
        r = app_mod.app.test_client().get("/tokens/labels.json")
        assert r.status_code == 200 and "Z|rZ" in r.get_json()
        assert r.headers["X-Tokens-Summary-State"] == "warming"


# ── the walker ──────────────────────────────────────────────────────
def _walker():
    spec = importlib.util.spec_from_file_location("tsw", ROOT / "scripts" / "tokens_summary_walker.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


def test_walker_imports_hermetically():
    assert _walker().WALKER_NAME == "tokens_summary_walker"


def test_jsonable_normalises_everything():
    w = _walker()
    out = w._jsonable({("CUR", "rI"): decimal.Decimal("1.5"), "k": (1, decimal.Decimal("2")),
                       "d": datetime.date(2026, 10, 19)})
    assert out == {"CUR|rI": 1.5, "k": [1, 2.0], "d": "2026-10-19"}
    assert json.dumps(out)  # round-trips


def test_walker_references_only_existing_helpers():
    """A misspelled db./app_mod. name is swallowed by the write guard and
    surfaces as a row that never appears. Catch it here."""
    src = (ROOT / "scripts" / "tokens_summary_walker.py").read_text(encoding="utf-8")
    for mod, prefix in ((db, "db"), (app_mod, "app_mod")):
        names = set(re.findall(rf"\b{prefix}\.([A-Za-z_]\w*)", src))
        missing = [n for n in names if not hasattr(mod, n)]
        assert missing == [], f"{prefix}.{missing} do not exist"


def test_compute_summary_fills_every_column(monkeypatch):
    w = _walker()
    monkeypatch.setattr(app_mod, "_cached_token_volume_aggregates", lambda **k: [("A", "r", decimal.Decimal("1"), 1)])
    monkeypatch.setattr(app_mod, "_cached_token_volume_bucket_stats", lambda: (1, 9, 3))
    monkeypatch.setattr(app_mod, "_load_token_names_dict", lambda: {})
    monkeypatch.setattr(app_mod, "_load_d1_hero_snapshot", lambda: {})
    monkeypatch.setattr(app_mod, "_build_hero_enriched_30d", lambda tm: [])
    monkeypatch.setattr(app_mod, "_cached_tier_lookup_map", lambda: ({"A|r": "verified"}, "t"))
    monkeypatch.setattr(app_mod, "_cached_token_prices_map", lambda: {("A", "r"): 2.0})
    monkeypatch.setattr(app_mod, "_cached_token_category_inferred_map", lambda: {("B", "s"): {"currency_display": "B"}})
    monkeypatch.setattr(app_mod, "_cached_token_warnings_recent", lambda **k: [])
    values, stages = w.compute_summary(app_mod, db)
    assert set(values) == set(db.TOKENS_SUMMARY_COLUMNS)
    assert values["aggregates_24h"] == [["A", "r", 1.0, 1]]
    assert values["price_map"] == {"A|r": 2.0}
    assert values["latest_bucket"] == 9
    assert json.loads(values["label_lookup_json"])["B|s"]["attestation"] == "self-described"
    assert set(stages) == {"aggregates", "stats", "hero", "maps", "labels", "warnings"}
