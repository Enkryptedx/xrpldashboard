"""/amendments "Just went LIVE" box — real route + real template render.

Branch amendments-live-banner-2026-10-09. Drives the REAL /amendments route
and the REAL templates/_amendments_live_banner.html through Flask with
fakes only: no DB, no network, no production DATABASE_URL (owner rule
2026-10-08). Also covers the light poll endpoint the 30s script calls.
"""
from __future__ import annotations

import os
import re
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import app as app_mod  # noqa: E402

PD = "0F48FF561C709540328F31F1C97FD512ACC8B4E42138A161CB0E21ECA292540B"
PD_TX = "478284066BA0B83CC35CA1174F667F43D9577782F221440AFCC5541788F25A77"
OTHER = "56B241D7A43D40E7E93C9CF7E8F2A5B4F51D0DC4F7A95B6A4CAA0E4C3B1D2E5F"


def _state():
    """Shaped exactly like amendments_state.fetch_amendments_state_cached."""
    return {
        "ok": True, "enabled_count": 95, "in_flight_count": 12,
        "ledger_index": 107529999,
        "recognized_enabled": [{"hash": PD, "name": "PermissionDelegationV1_1"}],
        "unrecognized_enabled": [], "unrecognized_enabled_count": 0,
        "in_flight": [{"hash": OTHER, "name": "fixBatchV1_2"}],
        "superseded": [], "superseded_count": 0,
        "majorities": [{
            "hash": OTHER, "name": "fixBatchV1_2", "recognized": True,
            "known_meta": None,
            "majority_reached_iso": "2026-09-25T14:12:51Z",
            "activation_eta_iso": "2026-10-09T14:12:51Z",
        }],
        "network_votes_source": {}, "in_development": [],
        "in_development_count": 0, "sourcing": "sovereign",
        "cached_age_seconds": 0.0,
        "fetched_at_iso": "2026-10-09T10:30:00Z",
    }


def _pd_enabled_row(enabled_iso="2026-10-08T21:29:50Z"):
    """amendment_majority_history row shaped like
    app._load_amendment_majority_history() returns, post true-enable fix."""
    return {
        "name": "PermissionDelegationV1_1", "hash": PD,
        "majority_close_iso": "2026-09-24T21:25:01Z",
        "activation_eta_iso": "2026-10-08T21:25:01Z",
        "first_seen_iso": "2026-09-24T21:25:01Z", "removed_iso": None,
        "active": False, "enabled": True,
        "enabled_seen_ledger": 107524865,
        "enabled_close_iso": enabled_iso,
        "enabled_iso": enabled_iso,
        "enabled_tx_hash": PD_TX,
        "vote_count_at_first": 30, "unl_threshold": 28,
    }


def _get(monkeypatch, history, path="/amendments"):
    monkeypatch.setattr(app_mod, "fetch_amendments_state_cached",
                        lambda *a, **k: _state())
    monkeypatch.setattr(app_mod, "_load_amendment_majority_history",
                        lambda *a, **k: list(history))
    import roll_call_card as C
    monkeypatch.setattr(C, "is_enabled", lambda *a, **k: False)
    app_mod.app.config["TESTING"] = True
    with app_mod.app.test_client() as c:
        return c.get(path)


def _banner(html):
    """The rendered box only. Starts at the container's data-live-banner
    attribute (the <style> block above it uses .live-banner-* class names,
    so never anchor on those) and ends where the poll <script> begins."""
    i = html.find("data-live-banner")
    assert i != -1, "live banner box not rendered"
    j = html.find("<script", i)
    return html[i:j if j != -1 else len(html)]


def _items(html):
    """Just the per-amendment rows, excluding the container tag — whose
    data-live-fingerprint attribute legitimately contains amendment
    hashes and so must not be searched for 'a bare hash leaked'."""
    box = _banner(html)
    k = box.find("data-live-banner-item")
    assert k != -1, "no banner items rendered"
    # Back up to the enclosing tag's '<' so a tag-stripping regex sees
    # complete tags; slicing mid-tag would leave attribute text behind.
    return box[box.rfind("<", 0, k):]


# ── the motivating case: PD must be visible, not vanished ──

def test_pd_shows_with_name_live_tag_time_ledger_and_tx(monkeypatch):
    import datetime as dt

    import amendments_live_banner as B
    # Freeze "now" so the 48h window is deterministic.
    real = B.recently_enabled
    monkeypatch.setattr(
        B, "recently_enabled",
        lambda h, now=None, window_hours=B.WINDOW_HOURS: real(
            h, now=dt.datetime(2026, 10, 9, 10, 30, tzinfo=dt.timezone.utc),
            window_hours=window_hours))
    r = _get(monkeypatch, [_pd_enabled_row()])
    assert r.status_code == 200
    html = r.data.decode()
    box = _banner(html)
    assert "Just went LIVE" in html
    assert "PermissionDelegationV1_1" in box
    assert ">LIVE<" in box
    # ET first, UTC in parens — via the page's shared filter.
    assert "5:29" in box and "PM ET" in box
    assert "21:29 UTC" in box
    # True enable ledger, comma-formatted.
    assert "107,524,865" in box
    # EnableAmendment tx link to the real tx.
    assert "livenet.xrpl.org/transactions/" + PD_TX in box
    assert "EnableAmendment" in box


def test_box_hidden_when_nothing_enabled_recently_but_poll_still_runs(monkeypatch):
    r = _get(monkeypatch, [])
    assert r.status_code == 200
    html = r.data.decode()
    assert "Just went LIVE" not in html
    assert "data-live-banner" not in html
    # The poll must still run so a viewer sitting on the page sees an
    # activation happen without touching anything.
    assert "data-live-fingerprint-only" in html
    assert "/api/amendments/live.json" in html


def test_old_activation_not_in_box(monkeypatch):
    """Enabled well over 48h ago -> no box (it belongs to history, not
    "just went live")."""
    r = _get(monkeypatch, [_pd_enabled_row(enabled_iso="2026-01-01T00:00:00Z")])
    assert r.status_code == 200
    assert "Just went LIVE" not in r.data.decode()


def test_page_embeds_fingerprint_and_polls_every_30s(monkeypatch):
    r = _get(monkeypatch, [_pd_enabled_row()])
    html = r.data.decode()
    assert re.search(r'data-live-fingerprint="[^"]+"', html)
    assert "/api/amendments/live.json" in html
    assert "30000" in html
    assert "location.reload()" in html
    # Exactly one polling timer on the page from this feature.
    assert html.count("setInterval(check, 30000)") == 1


def test_no_bare_hash_shown_when_name_present(monkeypatch):
    """The VISIBLE row reads as the amendment's name, not a raw hash.
    Machine-readable attributes (data-amendment-hash, the container's
    data-live-fingerprint) legitimately carry hashes — same pattern the
    existing countdown cards use — so assert on rendered text only."""
    r = _get(monkeypatch, [_pd_enabled_row()])
    visible = re.sub(r"<[^>]+>", " ", _items(r.data.decode()))
    assert "PermissionDelegationV1_1" in visible
    assert PD not in visible, "amendment hash shown as visible text"


# ── the poll endpoint itself ──

def test_poll_endpoint_is_light_and_cached(monkeypatch):
    r = _get(monkeypatch, [_pd_enabled_row()], path="/api/amendments/live.json")
    assert r.status_code == 200
    assert r.headers["Cache-Control"] == "public, max-age=30, s-maxage=30"
    j = r.get_json()
    assert j["ok"] is True
    assert j["enabled_count"] == 95
    assert isinstance(j["fingerprint"], str) and j["fingerprint"]


def test_poll_endpoint_never_500s_on_a_bad_read(monkeypatch):
    """A DB hiccup must return ok:false, never a 500 — the script then
    simply doesn't reload."""
    def _boom(*a, **k):
        raise RuntimeError("pg down")
    monkeypatch.setattr(app_mod, "_load_amendment_majority_history", _boom)
    monkeypatch.setattr(app_mod, "fetch_amendments_state_cached",
                        lambda *a, **k: _state())
    app_mod.app.config["TESTING"] = True
    with app_mod.app.test_client() as c:
        r = c.get("/api/amendments/live.json")
    assert r.status_code == 200
    assert r.get_json()["ok"] is False


def test_fingerprint_matches_between_page_and_endpoint(monkeypatch):
    """The reload decision compares these two; if they disagree for the
    same data the page would reload forever."""
    history = [_pd_enabled_row()]
    page = _get(monkeypatch, history)
    m = re.search(r'data-live-fingerprint="([^"]+)"', page.data.decode())
    assert m
    poll = _get(monkeypatch, history, path="/api/amendments/live.json")
    assert poll.get_json()["fingerprint"] == m.group(1)


def test_page_still_renders_when_banner_helper_fails(monkeypatch):
    """Render-killer rule: the box failing must not 500 /amendments."""
    import amendments_live_banner as B
    monkeypatch.setattr(B, "recently_enabled",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    r = _get(monkeypatch, [_pd_enabled_row()])
    assert r.status_code == 200
    assert "Just went LIVE" not in r.data.decode()
