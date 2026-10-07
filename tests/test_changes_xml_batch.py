"""/changes.xml must build in at most 2 DB queries, byte-identically.

Why this exists (2026-10-07): the feed used to call read_changes_envelope_dates()
once and then read_changes_envelope() per date -- 1 + 30 = 31 round-trips, each
on its OWN short-lived connection (db.pg_connect does not pool). From Render to
Neon that cost ~15s per cold build, which raced the route canary's 15.0s timeout
and produced network_ReadTimeout at 06:06:30 ET on 2026-10-07.

Two independent things are pinned here:
  1. query COUNT -- a future refactor that reintroduces a per-date read fails.
  2. output BYTES -- the batched path must produce exactly what the per-date
     path produced, including the disclosed-corrections overlay.

Hermetic: no DB, no network. The db module is monkeypatched.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app  # noqa: E402

# Two dates with envelopes, newest first -- mirrors what PG returns.
FIXTURE = [
    ("2026-10-06", {"date": "2026-10-06",
                    "changes": [{"category": "rlusd", "line": "RLUSD XRPL supply grew by $25.9M"}],
                    "categories_no_change": ["amm", "nft"]}),
    ("2026-10-05", {"date": "2026-10-05",
                    "changes": [{"category": "amm", "line": "AMM TVL fell by $1.2M"}],
                    "categories_no_change": []}),
]


@pytest.fixture(autouse=True)
def _no_corrections(monkeypatch):
    """Default: no disclosed corrections. Individual tests override."""
    monkeypatch.setattr(app, "_load_disclosed_corrections_for", lambda d: [])


def _install_batch(monkeypatch, rows=None, counter=None):
    rows = FIXTURE if rows is None else rows

    def fake_batch(limit=30):
        if counter is not None:
            counter["batch"] += 1
        return list(rows)[:limit]
    monkeypatch.setattr(app.db, "read_changes_envelopes_batch", fake_batch)


def _forbid_per_date(monkeypatch, counter):
    def boom(*a, **k):
        counter["per_date"] += 1
        raise AssertionError("per-date read_changes_envelope must not be called")
    monkeypatch.setattr(app.db, "read_changes_envelope", boom)

    def boom_dates(*a, **k):
        counter["dates"] += 1
        raise AssertionError("read_changes_envelope_dates must not be called")
    monkeypatch.setattr(app.db, "read_changes_envelope_dates", boom_dates)


# ------------------------------------------------------------ query count

def test_build_uses_at_most_two_queries(monkeypatch):
    """THE regression. 31 queries -> 1."""
    c = {"batch": 0, "per_date": 0, "dates": 0}
    _install_batch(monkeypatch, counter=c)
    _forbid_per_date(monkeypatch, c)

    body = app._build_changes_atom_body()

    total = c["batch"] + c["per_date"] + c["dates"]
    assert total <= 2, f"build made {total} queries, budget is 2"
    assert c["batch"] == 1
    assert c["per_date"] == 0, "a per-date read reintroduces the 31-query fanout"
    assert c["dates"] == 0
    assert body.startswith('<?xml version="1.0" encoding="UTF-8"?>')


def test_scales_flat_with_date_count(monkeypatch):
    """30 dates must still be one query -- not one per date."""
    rows = [(f"2026-09-{d:02d}", {"date": f"2026-09-{d:02d}", "changes": []})
            for d in range(30, 0, -1)]
    c = {"batch": 0, "per_date": 0, "dates": 0}
    _install_batch(monkeypatch, rows=rows, counter=c)
    _forbid_per_date(monkeypatch, c)
    app._build_changes_atom_body()
    assert c["batch"] == 1, "query count must not grow with the number of dates"


# ------------------------------------------------------------ output bytes

def _reference_body(rows, corrections_for):
    """Rebuild the feed the way the PER-DATE path would have, as an independent
    oracle. Deliberately not a call into the batched code."""
    entries = []
    dates = [d for d, _ in rows]
    for d, raw in rows:
        env = app._overlay_disclosed_corrections(d, raw)
        if not env:
            continue
        parts = [f"[{c.get('category','')}] {c.get('line','')}"
                 for c in (env.get("changes") or [])]
        nc = env.get("categories_no_change") or []
        if nc:
            parts.append(f"No change: {', '.join(nc)}")
        summary = "\n".join(parts) if parts else "No changes recorded."

        def _esc(s):
            return (str(s).replace("&", "&amp;").replace("<", "&lt;")
                    .replace(">", "&gt;").replace('"', "&quot;"))
        entries.append(
            f"  <entry>\n"
            f"    <id>{app.SITE_URL}/changes/{d}</id>\n"
            f"    <title>What&#39;s new — {d}</title>\n"
            f"    <link href='{app.SITE_URL}/changes/{d}'/>\n"
            f"    <updated>{d}T00:00:00Z</updated>\n"
            f"    <author><name>xrpldashboard</name></author>\n"
            f"    <summary type='text'>{_esc(summary)}</summary>\n"
            f"  </entry>")
    return dates, entries


def test_batched_output_is_byte_identical(monkeypatch):
    _install_batch(monkeypatch)
    body = app._build_changes_atom_body()
    _, entries = _reference_body(FIXTURE, lambda d: [])
    for e in entries:
        assert e in body, "entry block differs from the per-date rendering"
    assert body.count("<entry>") == len(FIXTURE)
    assert body.endswith("\n</feed>\n")


def test_corrections_overlay_is_applied_in_the_batched_path(monkeypatch):
    """A disclosed correction must still be prepended -- the overlay reads a
    local JSON file, so it stays in Python after batching."""
    corr = [{"category": "correction", "line": "Corrected: earlier figure wrong",
             "as_of_utc": "2026-10-06T12:00:00Z"}]
    monkeypatch.setattr(app, "_load_disclosed_corrections_for",
                        lambda d: corr if d == "2026-10-06" else [])
    _install_batch(monkeypatch)
    body = app._build_changes_atom_body()
    assert "[correction] Corrected: earlier figure wrong" in body
    i_corr = body.index("[correction]")
    i_rlusd = body.index("[rlusd]")
    assert i_corr < i_rlusd, "corrections must be prepended, as before"


def test_empty_db_yields_valid_empty_feed(monkeypatch):
    _install_batch(monkeypatch, rows=[])
    body = app._build_changes_atom_body()
    assert body.startswith('<?xml version="1.0"')
    assert "<entry>" not in body
    assert body.endswith("\n</feed>\n")


# ------------------------------------------- stale-while-revalidate contract

def _reset_cache(body=None, built_at=0.0):
    app._changes_xml_cache["body"] = body
    app._changes_xml_cache["built_at"] = built_at
    app._changes_xml_cache["refreshing"] = False


def test_stale_body_is_served_without_blocking(monkeypatch):
    """A stale cache must return the OLD body immediately and refresh in the
    background -- no request pays the build cost."""
    _reset_cache(body="STALE", built_at=0.0)
    called = {"n": 0}

    def never_inline():
        called["n"] += 1
        raise AssertionError("must not build inline when a stale body exists")
    monkeypatch.setattr(app, "_build_changes_atom_body", never_inline)
    spawned = {"n": 0}
    monkeypatch.setattr(app, "_start_changes_xml_refresh",
                        lambda: spawned.__setitem__("n", spawned["n"] + 1))

    out = app._changes_atom_body_cached(now=10_000.0)
    assert out == "STALE"
    assert spawned["n"] == 1, "a background refresh should have been kicked"
    assert called["n"] == 0
    _reset_cache()


def test_cold_cache_builds_synchronously(monkeypatch):
    """Nothing cached: must build inline rather than serve an empty feed."""
    _reset_cache(body=None)
    _install_batch(monkeypatch)
    out = app._changes_atom_body_cached(now=10_000.0)
    assert out.startswith('<?xml version="1.0"')
    assert app._changes_xml_cache["body"] is not None
    _reset_cache()


def test_failure_is_never_cached(monkeypatch):
    """A raised rebuild serves the last good body and leaves it in place."""
    _reset_cache(body="GOOD", built_at=0.0)

    def boom():
        raise RuntimeError("pg down")
    monkeypatch.setattr(app, "_build_changes_atom_body", boom)

    out = app._changes_xml_rebuild_blocking(now=10_000.0)
    assert out == "GOOD"
    assert app._changes_xml_cache["body"] == "GOOD", "failure must not poison cache"
    _reset_cache()


def test_failure_with_no_good_copy_raises(monkeypatch):
    _reset_cache(body=None)

    def boom():
        raise RuntimeError("pg down")
    monkeypatch.setattr(app, "_build_changes_atom_body", boom)
    with pytest.raises(RuntimeError):
        app._changes_xml_rebuild_blocking(now=10_000.0)
    _reset_cache()


def test_refresh_is_single_flight(monkeypatch):
    """Two bursts must not spawn two refresh threads."""
    _reset_cache(body="STALE", built_at=0.0)
    app._changes_xml_cache["refreshing"] = True
    started = {"n": 0}

    class FakeThread:
        def __init__(self, *a, **k):
            started["n"] += 1

        def start(self):
            pass
    monkeypatch.setattr(app.threading, "Thread", FakeThread)
    app._start_changes_xml_refresh()
    assert started["n"] == 0, "refresh already in flight; must not spawn another"
    _reset_cache()


def test_canary_timeout_was_not_raised():
    """The fix must be the build, not a looser canary."""
    import public_route_200_canary as canary
    assert canary.TIMEOUT_S == 15.0, "canary timeout must stay at 15.0s"
