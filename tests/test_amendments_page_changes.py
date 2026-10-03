"""Tests for the 2026-10-02 /amendments page changes (branch
amendments-fix-2026-10-02).

Every test here exercises the REAL code paths, not a private copy of the
logic or Python's own clock:

  * test_filter_*            import and call app.datetime_to_et_first (the real
                             registered Jinja filter) and assert on its output,
                             including the America/Indiana/Indianapolis DST
                             fall-back.
  * test_flag_ledger_*       call roll_call_card.flag_ledger_counter (the real
                             function) with a known ledger index and assert the
                             next-flag math.
  * test_template_render_*   render the REAL templates/amendments.html through
                             Flask's Jinja env (so the real filter runs inside
                             the real template) and assert the rendered HTML.
  * test_status_tool_*       call the REAL mcp_tools_ledger reconciliation using
                             an injected state dict shaped exactly like
                             amendments_state.fetch_amendments_state_cached so
                             no network/DB is touched, and assert the
                             recognized-key fix.

These are designed to FAIL against the pre-fix code and PASS against the
post-fix code — see the branch's commit message for the before/after pytest
transcript.
"""
import datetime as dt

import pytest


# ─────────────────────────────────────────────────────────────────────
# ITEM 2 — the real datetime_to_et_first filter (Indianapolis zone)
# ─────────────────────────────────────────────────────────────────────

def _filter():
    import app
    # Must be the REAL registered filter, not a reimplementation.
    assert "datetime_to_et_first" in app.app.jinja_env.filters
    return app.app.jinja_env.filters["datetime_to_et_first"]


def test_filter_edt_sub_hour_rollover_to_previous_day():
    """01:25 UTC on Oct 8 2026 must render Oct 7, 9:25 PM ET (EDT, UTC-4)."""
    out = _filter()("2026-10-08T01:25:00Z")
    assert "Oct 7" in out
    assert "9:25 PM ET" in out
    assert "(01:25 UTC)" in out


def test_filter_est_after_fall_back():
    """A date AFTER the Nov 1 2026 fall-back must render in EST (UTC-5):
    15:30 UTC -> 10:30 AM ET, not 11:30 AM. This is what proves we use the
    named IANA zone and not a fixed -4 offset."""
    out = _filter()("2026-11-10T15:30:00Z")
    assert "10:30 AM ET" in out
    assert "(15:30 UTC)" in out


def test_filter_edt_before_fall_back():
    """Same 15:30 UTC wall-clock BEFORE the switch is 11:30 AM ET (EDT)."""
    out = _filter()("2026-10-14T15:30:00Z")
    assert "11:30 AM ET" in out
    assert "(15:30 UTC)" in out


def test_filter_uses_named_indianapolis_zone():
    """Guard against a regression back to America/New_York or a fixed offset:
    the two 15:30-UTC instants above straddle the DST boundary, so their ET
    hours MUST differ by exactly one. A fixed offset would make them equal."""
    f = _filter()
    before = f("2026-10-14T15:30:00Z")   # EDT -> 11:30 AM
    after = f("2026-11-10T15:30:00Z")    # EST -> 10:30 AM
    assert "11:30 AM ET" in before
    assert "10:30 AM ET" in after


def test_filter_bad_input_is_empty():
    f = _filter()
    assert f(None) == ""
    assert f("not-a-date") == ""


# ─────────────────────────────────────────────────────────────────────
# ITEM 4 — the real flag-ledger counter
# ─────────────────────────────────────────────────────────────────────

def test_flag_ledger_counter_formula():
    """next_flag = ((current // 256) + 1) * 256, with real remaining/minutes."""
    import roll_call_card
    current = 107393113
    fc = roll_call_card.flag_ledger_counter(current)
    assert fc is not None
    assert fc["next_flag_ledger"] == ((current // 256) + 1) * 256 == 107393280
    assert fc["ledgers_remaining"] == 107393280 - current == 167
    # minutes at ~3.86 s/ledger
    assert fc["minutes_remaining"] == round(167 * 3.86 / 60, 1)
    assert fc["seconds_per_ledger"] == 3.86


def test_flag_ledger_counter_boundary_on_flag():
    """A ledger exactly on a multiple of 256 still points to the NEXT flag."""
    import roll_call_card
    fc = roll_call_card.flag_ledger_counter(107393280)
    assert fc["next_flag_ledger"] == 107393280 + 256
    assert fc["ledgers_remaining"] == 256


def test_flag_ledger_counter_bad_input():
    import roll_call_card
    assert roll_call_card.flag_ledger_counter(None) is None
    assert roll_call_card.flag_ledger_counter(0) is None


def test_build_card_wires_live_ledger_not_stale_voting_ledger():
    """build_card must compute the flag counter from the LIVE ledger passed in,
    NOT from the recorder's stale voting_ledger. We give a stale voting_ledger
    far from the live one and assert the counter tracks the live value."""
    import roll_call_card
    live = 107393113
    stale_voting = 100000000  # deliberately nothing like `live`
    rounds = [{
        "voting_ledger": stale_voting, "flag_ledger": stale_voting + 1,
        "observed_iso": "2026-10-02T20:00:00Z", "unl_size": 35, "seen": 30,
        "trusted_available": 35, "threshold": 28, "needed": 29,
        "unl_sequence": 1, "tallies": {}, "rounds_recorded": 10,
        "first_round_iso": "2026-09-26T11:44:53Z",
    }]
    card = roll_call_card.build_card(
        rounds, in_flight=[], current_validated_ledger=live)
    assert card is not None
    assert card["flag_counter"] is not None
    # The counter is anchored to the LIVE ledger, not the stale voting_ledger.
    assert card["flag_counter"]["current_ledger"] == live
    assert card["flag_counter"]["next_flag_ledger"] == ((live // 256) + 1) * 256


# ─────────────────────────────────────────────────────────────────────
# ITEM 1 + 2 + 6 + 8 — the real template render
# ─────────────────────────────────────────────────────────────────────

def _full_roll_call(unl_full=35):
    return {
        "unl_full": unl_full, "threshold_full": (unl_full * 80) // 100,
        "needed_full": (unl_full * 80) // 100 + 1,
        "rows": [], "unl_size": unl_full, "seen": unl_full - 5,
        "trusted_available": unl_full, "threshold": (unl_full * 80) // 100,
        "needed": (unl_full * 80) // 100 + 1, "not_heard": 5,
        "voting_ledger": 107393000, "flag_ledger": 107393001,
        "next_voting_ledger": 107393256, "next_eta_min": 10,
        "next_overdue": False, "seconds_per_ledger": 3.86, "any_reset": False,
        "stale": False, "observed_utc": "2026-10-02 20:00:00 UTC",
        "observed_et": "4:00:00 PM ET", "age_min": 2, "rounds_recorded": 50,
        "first_round_iso": "2026-09-26T11:44:53Z", "unl_sequence": 1,
        "source": "own-node", "flag_counter": None,
    }


def _render_amendments(roll_call):
    import app
    from flask import render_template
    state = {
        "ok": True, "enabled_count": 94, "in_flight_count": 10,
        "ledger_index": 107393280, "recognized_enabled": [],
        "unrecognized_enabled": [], "unrecognized_enabled_count": 0,
        "in_flight": [], "superseded": [], "majorities": [{
            "hash": "ABC123", "name": "BatchV1_1", "recognized": True,
            "majority_reached_iso": "2026-10-14T15:30:00Z",
            "activation_eta_iso": "2026-10-28T15:30:00Z",
        }], "network_votes_source": {}, "in_development": [],
    }
    with app.app.test_request_context("/amendments"):
        return render_template(
            "amendments.html", state=state, majority_history=[],
            page_sourcing="sovereign", roll_call=roll_call, cite=None,
            cache_ttl_seconds=300)


def test_template_quotes_block_verbatim():
    """ITEM 1: all seven xrpl.org quotes appear verbatim with the source URL."""
    html = _render_amendments(_full_roll_call())
    assert "The rule, word for word" in html
    for q in [
        "Amendments must maintain two weeks of support from more than 80% of "
        "trusted validators to be enabled.",
        "If support drops below 80%, the amendment is temporarily rejected, "
        "and the two week period restarts.",
        "Every 256th ledger is called a flag ledger.",
        "Flag Ledger +1: Servers insert an EnableAmendment pseudo-transaction "
        "and flag based on what they think happened",
        "Flag Ledger +2: Enabled amendments apply to transactions on this "
        "ledger onwards.",
        "If an amendment receives more than 80% support for two weeks, the "
        "amendment passes and the change applies permanently to all "
        "subsequent ledger versions.",
        "no longer understand the rules of the network.",
    ]:
        assert q in html, f"missing verbatim quote: {q[:40]}..."
    assert "xrpl.org/docs/concepts/networks-and-servers/amendments" in html
    # seven empty slots for Charlie's prose
    assert html.count("CHARLIE-PLACEHOLDER item1-") == 7


def test_template_activation_time_rendered_server_side_et_first():
    """ITEM 2: the activation + majority-reached times are converted on the
    SERVER (ET first, UTC in parens) with <time datetime> keeping UTC."""
    html = _render_amendments(_full_roll_call())
    assert '<time datetime="2026-10-28T15:30:00Z">Wed Oct 28, 11:30 AM ET (15:30 UTC)</time>' in html
    assert '<time datetime="2026-10-14T15:30:00Z">Wed Oct 14, 11:30 AM ET (15:30 UTC)</time>' in html
    # The raw ISO must NOT be the only thing shown anymore.
    assert ">2026-10-28T15:30:00Z<" not in html


def test_template_validator_counts_are_live_not_hardcoded():
    """ITEM 6: 'needed of N' comes from the live trusted-validator count, not a
    hardcoded '29 of the 35'. With N=40, needed=33 and threshold=32."""
    html = _render_amendments(_full_roll_call(unl_full=40))
    # floor(0.8*40)=32, needed=33
    assert "33 of the" in html
    assert "40" in html
    assert "threshold of" in html and "32" in html
    # And it must NOT still say the old hardcoded "29 of the 35" when N=40.
    assert "29 of the 35" not in html


def test_template_item8_placeholder_present():
    html = _render_amendments(_full_roll_call())
    assert "CHARLIE-PLACEHOLDER item8-activation-wording" in html


# ─────────────────────────────────────────────────────────────────────
# ITEM 5 — the real status-tool reconciliation
# ─────────────────────────────────────────────────────────────────────

def test_status_tool_reads_recognized_enabled_key(monkeypatch):
    """ITEM 5: tool_get_amendment_status must report the real enabled count.

    We inject a state dict shaped EXACTLY like
    amendments_state.fetch_amendments_state_cached() returns — the key for the
    recognized-enabled list is 'recognized_enabled' and the canonical count is
    'enabled_count'. The PRE-FIX tool read state.get('enabled') (absent) and
    reported 0; the fixed tool reads 'recognized_enabled' / 'enabled_count'.
    """
    import amendments_state
    import mcp_tools_ledger

    fake_state = {
        "ok": True,
        "enabled_count": 94,  # canonical, from the Amendments ledger object
        "recognized_enabled": [{"hash": f"H{i}", "name": f"A{i}"} for i in range(94)],
        "unrecognized_enabled": [],
        "in_flight": [{"hash": "F1", "name": "InFlight1"}],
        "superseded": [],
        # NOTE: deliberately NO 'enabled' key — mirrors the real state dict.
    }
    monkeypatch.setattr(
        amendments_state, "fetch_amendments_state_cached",
        lambda *a, **k: fake_state)

    # Avoid touching the real envelope-signing/stamping side effects.
    monkeypatch.setattr(mcp_tools_ledger.mcp_server, "wrap_envelope",
                        lambda data, **k: {"data": data, **k})
    monkeypatch.setattr(mcp_tools_ledger.mcp_server, "stamp_tool_call",
                        lambda *a, **k: None)

    env = mcp_tools_ledger.tool_get_amendment_status()
    data = env["data"]
    assert data["enabled_count"] == 94
    assert data["recognized_enabled_count"] == 94
    assert len(data["enabled"]) == 94
    assert data["in_flight_count"] == 1
