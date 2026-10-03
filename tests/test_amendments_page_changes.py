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
    """01:25 UTC on Oct 8 2026 must render Oct 7, 9:25 PM ET (EDT, UTC-4).
    Item E: because the UTC date (Oct 8) differs from the ET date (Oct 7), the
    parenthetical now carries the UTC date too."""
    out = _filter()("2026-10-08T01:25:00Z")
    assert "Oct 7" in out
    assert "9:25 PM ET" in out
    assert "(Oct 8, 01:25 UTC)" in out


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


def _flag_counter(current=107393113):
    import roll_call_card
    return roll_call_card.flag_ledger_counter(current)


def _render_amendments(roll_call, flag_counter="__default__"):
    import app
    from flask import render_template
    if flag_counter == "__default__":
        flag_counter = _flag_counter()
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
            page_sourcing="sovereign", roll_call=roll_call,
            flag_counter=flag_counter, cite=None, cache_ttl_seconds=300)


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
    # ITEM C: placeholders are now Jinja comments, so they must NOT appear in
    # the rendered HTML at all.
    assert "CHARLIE-PLACEHOLDER" not in html


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


def test_template_item8_placeholder_is_jinja_comment_not_in_output():
    """ITEM 8 + C: the item-8 activation-wording placeholder is a Jinja comment,
    so it must NOT render into the public HTML."""
    html = _render_amendments(_full_roll_call())
    assert "item8-activation-wording" not in html


# ───────────────────────────────────────────────────────────
# ITEM A — flag counter renders, WITH and WITHOUT roll_call
# ───────────────────────────────────────────────────────────

def test_flag_counter_renders_with_roll_call():
    """ITEM A: with a roll-call card present, the live flag-counter data labels
    (next flag ledger w/ commas, ledgers remaining, minutes remaining, and the
    ET-first next-flag time) render on the page."""
    fc = _flag_counter(107393113)
    html = _render_amendments(_full_roll_call(), flag_counter=fc)
    nf = f'{fc["next_flag_ledger"]:,}'
    assert nf in html                     # comma-formatted next flag ledger
    assert str(fc["ledgers_remaining"]) in html
    assert str(fc["minutes_remaining"]) in html
    # ET-first <time> for the next flag, server-rendered via the filter.
    assert f'<time datetime="{fc["next_flag_iso"]}">' in html
    assert " ET (" in html and "UTC)" in html


def test_flag_counter_renders_when_roll_call_is_none():
    """ITEM A: the counter MUST still render when roll_call is None (Postgres
    down / card disabled), from the independently-passed flag_counter var."""
    fc = _flag_counter(107393113)
    html = _render_amendments(None, flag_counter=fc)
    assert "data-flag-counter-standalone" in html
    assert f'{fc["next_flag_ledger"]:,}' in html
    assert str(fc["ledgers_remaining"]) in html
    assert f'<time datetime="{fc["next_flag_iso"]}">' in html


def test_flag_counter_absent_hides_block():
    """No live ledger -> flag_counter None -> no counter block, no crash."""
    html = _render_amendments(None, flag_counter=None)
    assert "data-flag-counter-standalone" not in html


# ───────────────────────────────────────────────────────────
# ITEM B — the browser script does not undo the server ET text
# ───────────────────────────────────────────────────────────

def test_script_uses_indianapolis_zone():
    """ITEM B: if the script formats any absolute time it uses the Indianapolis
    zone, never the visitor's locale."""
    html = _render_amendments(_full_roll_call())
    assert "America/Indiana/Indianapolis" in html


def test_script_does_not_rewrite_when_line_to_locale():
    """ITEM B: the old script replaced .when's text with the visitor's locale
    time (when_el.innerHTML = 'projected activation · ' + fmtLocal(iso)). That
    line must be gone, and there must be no fmtLocal locale formatter."""
    html = _render_amendments(_full_roll_call())
    assert "when_el.innerHTML" not in html
    assert "fmtLocal" not in html
    # The countdown must write into its own element, not the server <time>.
    assert "live-countdown" in html


# ───────────────────────────────────────────────────────────
# ITEM C — no owner name / instructions leak into public source
# ───────────────────────────────────────────────────────────

def test_rendered_page_has_no_placeholder_or_instruction_leak():
    """ITEM C: none of JJ's placeholder slots, author-note comments, or
    instruction text ship in the public HTML (they are all Jinja comments now).
    The one remaining 'Charlie' substring in the rendered page is the site-wide
    <meta name="author" content="Charlie Bruce"> partial (_head_meta.html) — an
    intentional, owner-authored attribution, NOT a leak of JJ's notes, so it is
    flagged in the report rather than stripped from a shared partial unasked."""
    html = _render_amendments(_full_roll_call())
    # JJ's placeholder markers / instruction comments must be gone entirely.
    assert "CHARLIE-PLACEHOLDER" not in html
    assert "CHARLIE" not in html
    assert "item1-" not in html
    assert "item8-activation-wording" not in html
    assert "owner's plain-English line" not in html
    # No instruction text JJ wrote leaks (e.g. the item-8 FACTS note or the
    # 'owner writes the public' phrasing JJ authored this round).
    assert "owner writes the public" not in html
    assert "EnableAmendment at flag+1" not in html
    # The remaining 'Charlie' substrings all come from PRE-EXISTING, owner-
    # authored material in SHARED partials this task did not own: the site-wide
    # <meta name="author"> and dated '/* Charlie ruling ... */' notes inside the
    # _head_meta.html live-stream CSS/JS block. None are JJ's round-2/3 notes.
    # They are enumerated in the report for the owner to decide on, not stripped
    # from a shared partial unasked.
    import re
    occurrences = re.findall(r".{0,30}Charlie.{0,20}", html)
    allowed = ('name="author"', "Charlie Bruce", "Charlie ruling",
               "BEHIND the page content")
    leaked = [o for o in occurrences if not any(a in o for a in allowed)]
    assert leaked == [], f"unexpected Charlie leak(s) from this task: {leaked}"


# ───────────────────────────────────────────────────────────
# ITEM E — cross-date UTC shown in the filter
# ───────────────────────────────────────────────────────────

def test_filter_cross_date_shows_utc_date():
    """ITEM E: when the UTC date differs from the ET date, the parenthetical
    includes the UTC date: 'Wed Oct 7, 9:25 PM ET (Oct 8, 01:25 UTC)'."""
    out = _filter()("2026-10-08T01:25:00Z")
    assert out == "Wed Oct 7, 9:25 PM ET (Oct 8, 01:25 UTC)"


def test_filter_same_date_omits_utc_date():
    """ITEM E: same ET/UTC date keeps the short '(HH:MM UTC)' form."""
    out = _filter()("2026-10-14T15:30:00Z")
    assert out == "Wed Oct 14, 11:30 AM ET (15:30 UTC)"
    assert "Oct 14, 15:30 UTC" not in out  # no redundant date


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


# ───────────────────────────────────────────────────────────
# PART 3 — activation timeline: the real app._activation_timeline_ctx
#          computes DONE/NOW/WAITING + ledger numbers from live data, and the
#          real template renders the macro. Six cases.
# ───────────────────────────────────────────────────────────

_NOW = dt.datetime(2026, 10, 2, 20, 0, tzinfo=dt.timezone.utc)
_FC = {"ledgers_remaining": 167, "next_flag_ledger": 107393280}


def _tl(**kw):
    import app
    kw.setdefault("now", _NOW)
    kw.setdefault("flag_counter", _FC)
    return app._activation_timeline_ctx(**kw)


def _iso_ago(days=0, hours=0):
    return (_NOW - dt.timedelta(days=days, hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_timeline_case_just_reached():
    c = _tl(majority_reached_iso=_iso_ago(0, 0), activation_eta_iso="2026-10-16T20:00:00Z", enabled=False)
    assert c["steps"] == {1: "now", 2: "waiting", 3: "waiting", 4: "waiting", 5: "waiting", 6: "waiting"}
    assert c["days_elapsed"] == 0
    assert c["ledgers_remaining"] == 167
    assert c["flag_plus_1"] == 107393281 and c["flag_plus_2"] == 107393282


def test_timeline_case_13_days_in():
    c = _tl(majority_reached_iso=_iso_ago(13, 0), activation_eta_iso="2026-10-17T20:00:00Z", enabled=False)
    assert c["steps"][1] == "now" and c["steps"][2] == "waiting"
    assert c["days_elapsed"] == 13


def test_timeline_case_14_done_waiting_for_flag():
    c = _tl(majority_reached_iso=_iso_ago(14, 1), activation_eta_iso=_iso_ago(0, 1), enabled=False)
    assert c["steps"][1] == "done"   # 14-day clock complete
    assert c["steps"][2] == "now"    # now waiting for the flag ledger
    assert c["steps"][3] == "waiting" and c["steps"][4] == "waiting"
    assert c["days_elapsed"] == 14


def test_timeline_case_flag_plus_1_passed_not_enabled():
    # Represented the same as 14-done-and-waiting until the enabled set flips;
    # the ledger numbers for flag+1/flag+2 are exposed as data labels.
    c = _tl(majority_reached_iso=_iso_ago(14, 2), activation_eta_iso=_iso_ago(0, 2), enabled=False)
    assert c["steps"][1] == "done" and c["steps"][2] == "now"
    assert c["flag_plus_1"] == 107393281
    assert c["flag_plus_2"] == 107393282


def test_timeline_case_enabled_all_done():
    c = _tl(majority_reached_iso="2026-09-01T00:00:00Z", activation_eta_iso="2026-09-15T00:00:00Z", enabled=True)
    assert c["steps"] == {1: "done", 2: "done", 3: "done", 4: "done", 5: "done", 6: "done"}
    assert c["enabled"] is True


def test_timeline_case_restarted():
    c = _tl(majority_reached_iso=_iso_ago(1, 8), activation_eta_iso="2026-10-15T12:00:00Z",
            enabled=False, restarted_iso=_iso_ago(1, 8))
    assert c["restarted"] is True
    assert c["steps"][1] == "now"   # clock running again after the restart
    assert c["days_elapsed"] == 1


def test_timeline_renders_in_template_countdown_and_finished():
    """The real template renders the macro for a countdown amendment AND a
    finished (enabled) state, with DONE/NOW/WAITING badges and the reused
    verified quotes; the owner title/note slots do NOT render any placeholder."""
    import app
    from flask import render_template
    tl_count = _tl(majority_reached_iso=_iso_ago(13, 0),
                   activation_eta_iso="2026-10-17T20:00:00Z", enabled=False)
    tl_enabled = _tl(majority_reached_iso=None, activation_eta_iso=None, enabled=True)
    state = {
        "ok": True, "enabled_count": 94, "in_flight_count": 10,
        "ledger_index": 107393280,
        "recognized_enabled": [{"hash": "EN1", "name": "Done1"}],
        "unrecognized_enabled": [], "unrecognized_enabled_count": 0,
        "in_flight": [], "superseded": [], "majorities": [{
            "hash": "ABC", "name": "BatchV1_1", "recognized": True,
            "majority_reached_iso": _iso_ago(13, 0),
            "activation_eta_iso": "2026-10-17T20:00:00Z",
        }], "network_votes_source": {}, "in_development": [],
    }
    tbh = {"ABC": tl_count, "__enabled_example__": tl_enabled}
    with app.app.test_request_context("/amendments"):
        html = render_template(
            "amendments.html", state=state, majority_history=[],
            page_sourcing="sovereign", roll_call=None, flag_counter=_FC,
            timeline_by_hash=tbh, cite=None, cache_ttl_seconds=300)
    # Two timelines: the countdown one and the finished one.
    assert html.count("data-activation-timeline") == 2
    # Finished state => six DONE badges; countdown (13 days in) => 1 NOW, 5 WAITING.
    assert html.count(">DONE<") == 6
    assert html.count(">NOW<") == 1
    assert html.count(">WAITING<") == 5
    # Reused verified quotes appear in the timeline (steps 2 and 6).
    assert "Every 256th ledger is called a flag ledger." in html
    assert "no longer understand the rules of the network." in html
    # Ledger numbers as data labels.
    assert "107,393,281" in html and "107,393,282" in html
    # Owner slots are empty; no placeholder marker leaks.
    assert "item-timeline-title-" not in html
    assert "item-timeline-note-" not in html
    # No-JS safe: the timeline is pure server HTML (no <script> needed to show).
    assert "<ol class=\"timeline-steps\">" in html


def test_timeline_quotes_do_not_drift_from_word_for_word_block():
    """The timeline reuses the SAME quote_* Jinja vars as the word-for-word
    block, so each step quote must appear with the identical verbatim text."""
    import app
    from flask import render_template
    tl = _tl(majority_reached_iso=_iso_ago(1, 0), activation_eta_iso="2026-10-16T20:00:00Z", enabled=False)
    state = {
        "ok": True, "enabled_count": 1, "in_flight_count": 0,
        "ledger_index": 107393280, "recognized_enabled": [],
        "unrecognized_enabled": [], "unrecognized_enabled_count": 0,
        "in_flight": [], "superseded": [], "majorities": [{
            "hash": "ABC", "name": "X", "recognized": True,
            "majority_reached_iso": _iso_ago(1, 0),
            "activation_eta_iso": "2026-10-16T20:00:00Z"}],
        "network_votes_source": {}, "in_development": [],
    }
    with app.app.test_request_context("/amendments"):
        html = render_template(
            "amendments.html", state=state, majority_history=[],
            page_sourcing="sovereign", roll_call=None, flag_counter=_FC,
            timeline_by_hash={"ABC": tl}, cite=None, cache_ttl_seconds=300)
    # Each of these verified strings appears at least twice: once in the
    # word-for-word block and once in the timeline (proving no drift).
    for q in ["Every 256th ledger is called a flag ledger.",
              "Flag Ledger +1: Servers insert an EnableAmendment pseudo-transaction",
              "no longer understand the rules of the network."]:
        assert html.count(q) >= 2, (q, html.count(q))


# ───────────────────────────────────────────────────────────
# PART 1 — the owner's name does not render on public pages (except about + the
#          terms.html operator line). Renders the REAL templates via routes.
# ───────────────────────────────────────────────────────────

def test_no_owner_name_on_amendments_and_contact_pages():
    """PART 1: the owner's name must not render on /amendments, contact,
    institutional, institutional_contact, security, or health. (about.html and
    the terms.html operator line are the allowed exceptions and are not
    rendered here.)"""
    import app
    from flask import render_template
    # /amendments full render (no DB).
    state = {
        "ok": True, "enabled_count": 94, "in_flight_count": 0,
        "ledger_index": 107393280, "recognized_enabled": [],
        "unrecognized_enabled": [], "unrecognized_enabled_count": 0,
        "in_flight": [], "superseded": [], "majorities": [],
        "network_votes_source": {}, "in_development": [],
    }
    with app.app.test_request_context("/amendments"):
        amd = render_template(
            "amendments.html", state=state, majority_history=[],
            page_sourcing="sovereign", roll_call=None, flag_counter=_FC,
            timeline_by_hash={}, cite=None, cache_ttl_seconds=300)
    for name in ("Charlie", "Bruce"):
        assert name not in amd, f"{name} leaked into /amendments"
    # The static-content contact-style pages that only needed a name swap.
    for tpl in ("institutional.html", "institutional_contact.html",
                "security.html"):
        with app.app.test_request_context("/"):
            h = render_template(tpl)
        assert "Charlie" not in h and "Bruce" not in h, f"name leaked in {tpl}"
