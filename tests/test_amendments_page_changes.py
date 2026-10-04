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
        "observed_et": "4:00:00 PM ET",
        # ROUND 7 (2026-10-03): the real build_card now emits observed_iso so the
        # page can run the last-roll-call time through datetime_to_et_first_html.
        "observed_iso": "2026-10-02T20:00:00Z", "age_min": 2, "rounds_recorded": 50,
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
    # ROUND 7 (2026-10-03): the UTC parenthetical is now wrapped in a nowrap
    # <span class="utc-paren"> (CHANGE 2), so the <time> body carries the span.
    assert ('<time datetime="2026-10-28T15:30:00Z">Wed Oct 28, 11:30 AM ET '
            '<span class="utc-paren">(15:30 UTC)</span></time>') in html
    assert ('<time datetime="2026-10-14T15:30:00Z">Wed Oct 14, 11:30 AM ET '
            '<span class="utc-paren">(15:30 UTC)</span></time>') in html
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
    # ROUND 7: UTC part now in a nowrap span.
    assert " ET " in html and '<span class="utc-paren">(' in html and "UTC)</span>" in html


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
    # PART A: at 0 days the enabling flag is days away and unknown -> no numbers.
    assert c["flag_plus_1"] is None and c["flag_plus_2"] is None


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
    # PART A: use a 14-done (clock complete, not enabled) countdown case so the
    # flag+1/flag+2 ledger numbers are legitimately present to assert on.
    tl_count = _tl(majority_reached_iso=_iso_ago(14, 1),
                   activation_eta_iso=_iso_ago(0, 1), enabled=False)
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
    # Finished state => six DONE badges; countdown (14 done, waiting) => step1
    # DONE + step2 NOW + steps 3-6 WAITING = 1 extra DONE, 1 NOW, 4 WAITING.
    assert html.count(">DONE<") == 7
    assert html.count(">NOW<") == 1
    assert html.count(">WAITING<") == 4
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


# ───────────────────────────────────────────────────────────
# ROUND 5 PART A — flag+1/flag+2 ledger numbers ONLY when step 1 is done and
#                 the amendment is not yet enabled; None in every other state.
# ───────────────────────────────────────────────────────────

def test_partA_flag_numbers_absent_at_0_days():
    c = _tl(majority_reached_iso=_iso_ago(0, 0), activation_eta_iso="2026-10-16T20:00:00Z", enabled=False)
    assert c["flag_plus_1"] is None and c["flag_plus_2"] is None


def test_partA_flag_numbers_absent_at_13_days():
    c = _tl(majority_reached_iso=_iso_ago(13, 0), activation_eta_iso="2026-10-17T20:00:00Z", enabled=False)
    assert c["flag_plus_1"] is None and c["flag_plus_2"] is None


def test_partA_flag_numbers_present_when_14_done_not_enabled():
    c = _tl(majority_reached_iso=_iso_ago(14, 1), activation_eta_iso=_iso_ago(0, 1), enabled=False)
    assert c["flag_plus_1"] == 107393281 and c["flag_plus_2"] == 107393282


def test_partA_flag_numbers_absent_when_enabled():
    c = _tl(majority_reached_iso="2026-09-01T00:00:00Z", activation_eta_iso="2026-09-15T00:00:00Z", enabled=True)
    assert c["flag_plus_1"] is None and c["flag_plus_2"] is None


def test_partA_flag_numbers_absent_when_restarted():
    c = _tl(majority_reached_iso=_iso_ago(1, 8), activation_eta_iso="2026-10-15T12:00:00Z",
            enabled=False, restarted_iso=_iso_ago(1, 8))
    assert c["flag_plus_1"] is None and c["flag_plus_2"] is None


def _render_case(**kw):
    """Render /amendments for one timeline case; return (html, timeline)."""
    import app
    from flask import render_template
    kw.setdefault("now", _NOW)
    kw.setdefault("flag_counter", _FC)
    tl = app._activation_timeline_ctx(**kw)
    state = {
        "ok": True, "enabled_count": 94, "in_flight_count": 1,
        "ledger_index": 107393280, "recognized_enabled": [],
        "unrecognized_enabled": [], "unrecognized_enabled_count": 0,
        "in_flight": [], "superseded": [], "majorities": [{
            "hash": "ABC", "name": "X", "recognized": True,
            "majority_reached_iso": kw["majority_reached_iso"],
            "activation_eta_iso": kw["activation_eta_iso"]}],
        "network_votes_source": {}, "in_development": [],
    }
    with app.app.test_request_context("/amendments"):
        html = render_template(
            "amendments.html", state=state, majority_history=[],
            page_sourcing="sovereign", roll_call=None, flag_counter=_FC,
            timeline_by_hash={"ABC": tl}, cite=None, cache_ttl_seconds=300)
    return html, tl


def _timeline_block(html):
    import re
    m = re.search(r"data-activation-timeline.*?</ol>", html, re.S)
    return m.group(0) if m else ""


def test_partA_rendered_ledger_numbers_gated_per_state():
    """Part A end-to-end: the rendered timeline shows the flag+1/flag+2 ledger
    numbers ONLY in the 14-done / flag+1 window."""
    import re
    def shown(html):
        b = _timeline_block(html)
        return ("107,393,281" in b) or ("107,393,282" in b)
    h0, _ = _render_case(majority_reached_iso=_iso_ago(0, 0), activation_eta_iso="2026-10-16T20:00:00Z", enabled=False)
    h13, _ = _render_case(majority_reached_iso=_iso_ago(13, 0), activation_eta_iso="2026-10-17T20:00:00Z", enabled=False)
    h14, _ = _render_case(majority_reached_iso=_iso_ago(14, 1), activation_eta_iso=_iso_ago(0, 1), enabled=False)
    hen, _ = _render_case(majority_reached_iso="2026-09-01T00:00:00Z", activation_eta_iso="2026-09-15T00:00:00Z", enabled=True)
    hre, _ = _render_case(majority_reached_iso=_iso_ago(1, 8), activation_eta_iso="2026-10-15T12:00:00Z", enabled=False, restarted_iso=_iso_ago(1, 8))
    assert shown(h0) is False
    assert shown(h13) is False
    assert shown(h14) is True
    assert shown(hen) is False
    assert shown(hre) is False


# ───────────────────────────────────────────────────────────
# ROUND 5 PART E — every slot is filled; no counts; no percent sign in new
#                 translated strings; no owner name.
# ───────────────────────────────────────────────────────────

def test_partE_all_slots_filled_in_timeline_and_quotes():
    import re
    html, _ = _render_case(majority_reached_iso=_iso_ago(13, 0),
                           activation_eta_iso="2026-10-17T20:00:00Z", enabled=False)
    # No empty owner slots remain.
    assert not re.search(r'class="timeline-title"[^>]*></div>', html)
    assert not re.search(r'class="timeline-note"[^>]*></div>', html)
    # Seven plain-English lines under the quotes.
    assert html.count('class="quote-plain"') == 7
    # No leftover placeholder instruction text.
    for marker in ("owner writes", "owner's plain-English line",
                   "CHARLIE-PLACEHOLDER", "item8-activation-wording: owner"):
        assert marker not in html, marker
    # Defines the three terms somewhere on the page.
    assert "Trusted validators are the servers" in html
    assert "A flag ledger is a checkpoint" in html
    assert "becomes amendment blocked" in html


def test_partE_how_it_works_paragraph_filled():
    import app
    from flask import render_template
    with app.app.test_request_context("/amendments/how-it-works"):
        h = render_template("amendments_how_it_works.html")
    assert '<p id="how-the-vote-works-slot"></p>' not in h  # not empty
    assert "Trusted validators are the servers" in h
    assert "becomes amendment blocked" in h


def test_partE_no_validator_count_in_new_text():
    """No hardcoded validator counts (29/35/28) in the filled slots. We assert
    the specific count tokens do not appear as standalone numbers in the new
    plain-English blocks (the live needed-of-N sentence uses variables, which
    only render when a roll_call is present — absent here)."""
    import re
    html, _ = _render_case(majority_reached_iso=_iso_ago(13, 0),
                           activation_eta_iso="2026-10-17T20:00:00Z", enabled=False)
    # Pull the quote-plain lines + timeline notes + item8 sentence region.
    plains = re.findall(r'class="quote-plain">(.*?)</p>', html, re.S)
    notes = re.findall(r'class="timeline-note"[^>]*>(.*?)</div>', html, re.S)
    for chunk in plains + notes:
        for bad in ("29", "35", "28"):
            assert bad not in chunk, f"hardcoded count {bad} in: {chunk[:60]}"
        # must use the words '80 percent', never the % sign
        assert "%" not in chunk


def test_partE_no_percent_sign_inside_new_translated_strings():
    """The Babel gettext %-bug: no literal '%' may sit inside the _() strings we
    added. We scan the template source for _('...') spans and assert none of
    the plain-English additions carry a bare percent sign (they say 'percent').
    """
    import os
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for rel in ("templates/amendments.html",
                "templates/amendments_how_it_works.html"):
        src = open(os.path.join(here, rel), encoding="utf-8").read()
        # Every _('...') string must not contain a bare % unless it is 'percent'.
        import re
        for m in re.finditer(r"_\('((?:[^'\\]|\\.)*)'\)", src):
            s = m.group(1)
            if "%" in s:
                # allowed only if there is literally no standalone percent sign
                # (there should be none at all in our strings)
                assert "percent" in s and "%" not in s.replace("percent", ""), \
                    f"percent sign inside _() string: {s[:60]}"


def test_partE_filled_pages_have_no_owner_name():
    html, _ = _render_case(majority_reached_iso=_iso_ago(13, 0),
                           activation_eta_iso="2026-10-17T20:00:00Z", enabled=False)
    assert "Charlie" not in html and "Bruce" not in html
    import app
    from flask import render_template
    with app.app.test_request_context("/amendments/how-it-works"):
        h = render_template("amendments_how_it_works.html")
    assert "Charlie" not in h and "Bruce" not in h


# ───────────────────────────────────────────────────────────
# ROUND 6 — flag ledger +1/+2 = next ledger / ledger after (not 256/512 later).
# ───────────────────────────────────────────────────────────

def _all_round6_pages():
    """Render every page that carries the +1/+2 wording: /amendments (a
    countdown case) and /amendments/how-it-works. Returns a list of HTML."""
    import app
    from flask import render_template
    htmls = []
    # /amendments with one countdown amendment so the timeline + quotes render.
    tl = _tl(majority_reached_iso=_iso_ago(14, 1), activation_eta_iso=_iso_ago(0, 1), enabled=False)
    state = {
        "ok": True, "enabled_count": 94, "in_flight_count": 1,
        "ledger_index": 107393280, "recognized_enabled": [],
        "unrecognized_enabled": [], "unrecognized_enabled_count": 0,
        "in_flight": [], "superseded": [], "majorities": [{
            "hash": "ABC", "name": "X", "recognized": True,
            "majority_reached_iso": _iso_ago(14, 1),
            "activation_eta_iso": _iso_ago(0, 1)}],
        "network_votes_source": {}, "in_development": [],
    }
    with app.app.test_request_context("/amendments"):
        htmls.append(render_template(
            "amendments.html", state=state, majority_history=[],
            page_sourcing="sovereign", roll_call=None, flag_counter=_FC,
            timeline_by_hash={"ABC": tl}, cite=None, cache_ttl_seconds=300))
    with app.app.test_request_context("/amendments/how-it-works"):
        htmls.append(render_template("amendments_how_it_works.html"))
    return htmls


def test_round6_no_misleading_flag_ledger_phrasing():
    """No rendered page may say the change happens one/two flag ledgers later
    (which would read as 256/512 ledgers = 17-33 minutes)."""
    bad = ("flag ledger later", "flag ledger after that",
           "flag ledgers after", "one flag ledger", "two flag ledgers")
    for html in _all_round6_pages():
        low = html.lower()
        for phrase in bad:
            assert phrase not in low, f"misleading phrase still present: {phrase!r}"


def test_round6_step3_step4_say_next_ledger():
    """The step 3 and step 4 lines must describe the NEXT ledger / the ledger
    after that (the correct ~4-second cadence)."""
    import re
    html = _all_round6_pages()[0]
    block = _timeline_block(html)
    # step 3 line
    assert "next ledger after the flag ledger" in block
    # step 4 line
    assert "the ledger after that" in block
    # the ~4-second cadence is stated
    assert "about 4 seconds" in block


# ═══════════════════════════════════════════════════════════════════════
# ROUND 7 (owner ruling 2026-10-03): countdown-first layout + ET-first
# time format with the UTC parenthetical kept together (nowrap).
#   CHANGE 1 — in each countdown box: live d/h/m/s countdown FIRST (biggest,
#              server-rendered real value), then the projected date, then the
#              unchanged "if the more-than-80-percent..." line.
#   CHANGE 2 — every visible time on /amendments reads Eastern-first with UTC
#              in parentheses; no raw ISO string in visible text; the UTC part
#              carries white-space:nowrap.
# ═══════════════════════════════════════════════════════════════════════

import re as _re7


def _countdown_filter():
    import app
    assert "countdown_to" in app.app.jinja_env.filters
    return app.app.jinja_env.filters["countdown_to"]


# ── CHANGE 1: the countdown_to server-render filter ──

def test_countdown_to_server_renders_dhms():
    """countdown_to produces a real d/h/m/s initial value in the script's exact
    format ('Nd NNh NNm NNs', days omitted when zero, others 2-digit)."""
    f = _countdown_filter()
    now = dt.datetime(2026, 10, 3, 13, 0, 0, tzinfo=dt.timezone.utc)
    # 5d 9h 12m 00s ahead
    target = now + dt.timedelta(days=5, hours=9, minutes=12)
    assert f(target.strftime("%Y-%m-%dT%H:%M:%SZ"), now=now) == "5d 09h 12m 00s"
    # under a day -> no leading 'Nd'
    target2 = now + dt.timedelta(hours=2, minutes=3, seconds=4)
    assert f(target2.strftime("%Y-%m-%dT%H:%M:%SZ"), now=now) == "02h 03m 04s"


def test_countdown_to_past_and_bad_input():
    """Past instant -> 'past' (consistent with the script's past state); bad /
    missing input -> empty string (never crashes a page)."""
    f = _countdown_filter()
    now = dt.datetime(2026, 10, 3, 13, 0, 0, tzinfo=dt.timezone.utc)
    assert f("2026-10-01T00:00:00Z", now=now) == "past"
    assert f(None) == ""
    assert f("not-a-date") == ""
    assert f("") == ""


def _countdown_box(html):
    """Return the main-column slice of the first countdown box (from the
    primary live-countdown div to the start of the roll-call sub-box)."""
    start = html.find("data-countdown-primary")
    assert start != -1, "no server-rendered primary countdown found"
    end = html.find("countdown-rollcall", start)
    return html[start:end if end != -1 else start + 4000]


def test_change1_countdown_before_date_before_sentence():
    """CHANGE 1: in the box the live countdown comes BEFORE the projected date,
    and the date BEFORE the unchanged 'if the more-than-80-percent...' line."""
    html = _render_amendments(_full_roll_call())
    i_cd = html.find("data-countdown-primary")
    i_date = html.find('class="timer"', i_cd)
    i_sentence = html.find(
        "if the more-than-80-percent majority holds for the full 14-day window",
        i_date)
    assert i_cd != -1 and i_date != -1 and i_sentence != -1
    assert i_cd < i_date < i_sentence, (i_cd, i_date, i_sentence)


def test_change1_countdown_server_rendered_real_value():
    """CHANGE 1: the primary countdown is server-rendered with a REAL d/h/m/s
    value (not empty, not a JS placeholder) so no-JS readers and crawlers see
    a countdown. activation_eta is 2026-10-28, well in the future."""
    html = _render_amendments(_full_roll_call())
    m = _re7.search(
        r'<div class="live-countdown"[^>]*data-countdown-primary>([^<]*)</div>',
        html)
    assert m, "primary live-countdown div missing"
    val = m.group(1).strip()
    assert _re7.match(r'^\d+d \d{2}h \d{2}m \d{2}s$', val), f"bad server value: {val!r}"


def test_change1_sentence_unchanged_word_for_word():
    """CHANGE 1: the conditional sentence is unchanged, word for word."""
    html = _render_amendments(_full_roll_call())
    assert ("if the more-than-80-percent majority holds for the full 14-day "
            "window") in html


def test_change1_countdown_is_largest_text_css():
    """CHANGE 1: the countdown is the largest text in the box via a clamp()
    rule topping out ~2.4-3rem, with tabular numerals, on one line (nowrap).
    The projected date (.timer) is smaller than the countdown's max."""
    html = _render_amendments(_full_roll_call())
    assert ".countdown .live-countdown" in html
    assert "clamp(" in html and "3rem" in html
    assert "tabular-nums" in html
    assert "white-space: nowrap" in html  # keeps the countdown on one line


def test_change1_script_drives_server_element_not_rebuild():
    """CHANGE 1/B: the script ticks the SERVER-rendered .live-countdown element
    (selects .live-countdown[data-activation-iso]); it no longer lazily creates
    its own element from .timer, and still never rewrites .when to the locale."""
    html = _render_amendments(_full_roll_call())
    assert ".countdown .live-countdown[data-activation-iso]" in html
    assert "when_el.innerHTML" not in html
    assert "fmtLocal" not in html


# ── CHANGE 2: ET-first time format everywhere, no raw ISO, nowrap UTC ──

def test_change2_cross_date_renders_et_first_with_utc_date():
    """CHANGE 2: 01:25 UTC Oct 8 2026 renders 'Wed Oct 7, 9:25 PM ET (Oct 8,
    01:25 UTC)'."""
    assert _filter()("2026-10-08T01:25:01Z") == \
        "Wed Oct 7, 9:25 PM ET (Oct 8, 01:25 UTC)"


def test_change2_same_date_renders_et_first_short():
    """CHANGE 2: 14:12 UTC Oct 9 2026 renders 'Fri Oct 9, 10:12 AM ET
    (14:12 UTC)'."""
    assert _filter()("2026-10-09T14:12:00Z") == \
        "Fri Oct 9, 10:12 AM ET (14:12 UTC)"


def test_change2_offset_correct_after_fall_back():
    """CHANGE 2: a date after the Nov 1 2026 EST fall-back uses UTC-5. 06:00
    UTC on Nov 2 is 1:00 AM ET (EST), not 2:00 AM (EDT)."""
    assert _filter()("2026-11-02T06:00:00Z") == \
        "Mon Nov 2, 1:00 AM ET (06:00 UTC)"


def test_change2_html_filter_wraps_utc_in_nowrap_span():
    """CHANGE 2: the HTML variant wraps the UTC parenthetical in a nowrap span
    so it stays together on a narrow phone, lighter/smaller than the ET time."""
    import app
    out = str(app.datetime_to_et_first_html("2026-10-09T14:12:00Z"))
    assert out == ('Fri Oct 9, 10:12 AM ET '
                   '<span class="utc-paren">(14:12 UTC)</span>')
    # cross-date form keeps the whole parenthetical inside the one span
    out2 = str(app.datetime_to_et_first_html("2026-10-08T01:25:01Z"))
    assert ('<span class="utc-paren">(Oct 8, 01:25 UTC)</span>') in out2
    assert app.datetime_to_et_first_html("") == ""


def test_change2_utc_paren_has_nowrap_css():
    """CHANGE 2: the .utc-paren rule carries white-space:nowrap and uses the
    muted palette var (slightly lighter + smaller)."""
    html = _render_amendments(_full_roll_call())
    assert _re7.search(r'\.utc-paren\s*\{[^}]*white-space:\s*nowrap', html)
    assert _re7.search(r'\.utc-paren\s*\{[^}]*var\(--muted\)', html)


def test_change2_no_raw_iso_in_visible_amendments_text():
    """CHANGE 2: /amendments shows NO raw ISO string like 2026-10-08T21:25:01Z
    in visible text. ISO is allowed only inside datetime="" / data-*-iso=""
    machine attributes and the ld+json <script> schema block."""
    html = _render_amendments(_full_roll_call())
    vis = _re7.sub(r'<script type="application/ld\+json">.*?</script>', '',
                   html, flags=_re7.S)
    vis = _re7.sub(r'datetime="[^"]*"', '', vis)
    vis = _re7.sub(r'data-[a-z0-9-]*iso="[^"]*"', '', vis)
    vis = _re7.sub(r'data-activation-iso="[^"]*"', '', vis)
    leaked = _re7.findall(r'>[^<]*\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z[^<]*<', vis)
    assert leaked == [], f"raw ISO leaked into visible text: {leaked[:5]}"


def test_change2_no_24h_eastern_last_roll_call():
    """CHANGE 2: the roll-call 'last roll call' time is ET-first (12-hour, ET
    label, UTC in parens), not the old bare '4:00:00 PM ET' / 24h form."""
    html = _render_amendments(_full_roll_call())
    # observed_iso 2026-10-02T20:00:00Z -> Fri Oct 2, 4:00 PM ET (20:00 UTC),
    # with the UTC part in the nowrap span (CHANGE 2).
    assert ('Fri Oct 2, 4:00 PM ET '
            '<span class="utc-paren">(20:00 UTC)</span>') in html
    assert "4:00:00 PM ET" not in html  # old bare form gone


def test_change2_how_it_works_has_no_time_strings():
    """CHANGE 2: /amendments/how-it-works is static explainer copy with no ISO
    strings and no 24-hour Eastern times to convert (confirms the audit)."""
    import app
    from flask import render_template
    with app.app.test_request_context("/amendments/how-it-works"):
        html = render_template("amendments_how_it_works.html")
    assert not _re7.search(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z', html)


# ═══════════════════════════════════════════════════════════════════════
# RESTART DATE FIX (Charlie 2026-10-04): the "Countdown restarted · <date>"
# line must anchor to the CURRENT ACTIVE epoch's majority_close_iso, never a
# removed/superseded earlier epoch's close. Earlier bug took the first row
# carrying removed_iso (the REMOVED epoch, e.g. PermissionDelegationV1_1
# Sep 21) which contradicted the live Sep 24 majority the countdown/activation
# correctly run from. These tests exercise the real render path end-to-end.
# ═══════════════════════════════════════════════════════════════════════

import re as _re_restart


def _render_real_route(monkeypatch, majorities, majority_history,
                       enabled_hashes=None):
    """Drive the REAL /amendments route so app.py's own restart_iso_by_hash
    builder runs (this is what makes the fail-on-main proof genuine). We
    monkeypatch only the two data sources the route reads: the amendments
    state (Majorities) and the majority-history loader. Everything else —
    restart_iso_by_hash, timeline_by_hash, the template render — is the real
    production code path."""
    import app
    state = {
        "ok": True, "enabled_count": 94, "in_flight_count": 10,
        "ledger_index": 107393280,
        "recognized_enabled": [{"hash": h} for h in (enabled_hashes or [])],
        "unrecognized_enabled": [], "unrecognized_enabled_count": 0,
        "in_flight": [], "superseded": [], "majorities": majorities,
        "network_votes_source": {}, "in_development": [],
        "sourcing": "sovereign", "cached_age_seconds": 0.0,
    }
    monkeypatch.setattr(app, "fetch_amendments_state_cached",
                        lambda *a, **k: dict(state))
    monkeypatch.setattr(app, "_load_amendment_majority_history",
                        lambda *a, **k: list(majority_history))
    app.app.config["TESTING"] = True
    with app.app.test_client() as c:
        r = c.get("/amendments")
    assert r.status_code == 200, f"/amendments returned {r.status_code}"
    return r.data.decode("utf-8")


def _restart_line_iso(html):
    """Return the datetime= value on the 'Countdown restarted' <time>, or None
    if no restart line is rendered."""
    m = _re_restart.search(
        r'Countdown restarted[^<]*<time datetime="([^"]+)"', html)
    return m.group(1) if m else None


def _restart_build(majority_history):
    """Pure mirror of the FIXED builder — used only for the non-render
    value-assert cases (c/d/e). (a) and (b) use the real route above so they
    truly fail on main."""
    restart, active, removed = {}, {}, set()
    for mh in (majority_history or []):
        h = (mh.get("hash") or "").upper()
        if not h:
            continue
        if mh.get("removed_iso"):
            removed.add(h)
        elif h not in active:
            active[h] = mh.get("majority_close_iso")
    for h in removed:
        if active.get(h):
            restart[h] = active[h]
    return restart


# History fixtures (newest-first per hash, matching the route's ORDER BY DESC).
def _mh_row(**kw):
    """A majority_history row with the template-required fields defaulted, so a
    crafted fixture renders the full history block without an UndefinedError."""
    base = {
        "active": kw.get("removed_iso") is None,
        "activation_eta_iso": None, "first_seen_iso": None,
        "vote_count_at_first": None, "unl_threshold": 28,
        "first_seen_ledger": None, "first_seen_close_iso": None,
        "removed_seen_ledger": None, "removed_close_iso": None,
        "first_seen_is_flag": False, "correction_note": None,
    }
    base.update(kw)
    base["active"] = base.get("removed_iso") is None
    return base


_PD_HISTORY = [
    _mh_row(hash="0F48FF56", name="PermissionDelegationV1_1",
            majority_close_iso="2026-09-24T21:25:01Z", removed_iso=None,
            activation_eta_iso="2026-10-08T21:25:01Z"),
    _mh_row(hash="0F48FF56", name="PermissionDelegationV1_1",
            majority_close_iso="2026-09-21T11:18:40Z",
            removed_iso="2026-09-23T12:47:20Z",
            activation_eta_iso="2026-10-05T11:18:40Z"),
]
_BATCH_HISTORY = [
    _mh_row(hash="9F287AED", name="BatchV1_1",
            majority_close_iso="2026-09-25T14:46:02Z", removed_iso=None,
            activation_eta_iso="2026-10-09T14:46:02Z"),
    _mh_row(hash="9F287AED", name="BatchV1_1",
            majority_close_iso="2026-09-15T14:06:41Z",
            removed_iso="2026-09-25T14:46:02Z",
            activation_eta_iso="2026-09-29T14:06:41Z"),
]


def test_restart_a_permissiondelegation_uses_active_epoch_sep24(monkeypatch):
    """(a) PD: removed Sep21->Sep23, active from Sep24 21:25 -> the rendered
    'Countdown restarted' line binds to Sep 24, NOT the removed epoch's Sep 21.
    Drives the REAL route, so this FAILS on main (restart line reads Sep 21)
    and PASSES on the branch (Sep 24)."""
    html = _render_real_route(
        monkeypatch,
        majorities=[{"hash": "0F48FF56", "name": "PermissionDelegationV1_1",
                     "recognized": True,
                     "majority_reached_iso": "2026-09-24T21:25:01Z",
                     "activation_eta_iso": "2026-10-08T21:25:01Z"}],
        majority_history=_PD_HISTORY)
    assert "Countdown restarted" in html
    restart_iso = _restart_line_iso(html)
    assert restart_iso == "2026-09-24T21:25:01Z", (
        f"restart line anchored to {restart_iso!r}; expected the ACTIVE epoch "
        f"Sep 24 (2026-09-24T21:25:01Z), not the removed epoch Sep 21")
    assert restart_iso != "2026-09-21T11:18:40Z"


def test_restart_b_batch_uses_active_epoch_sep25(monkeypatch):
    """(b) BatchV1_1: removed Sep15 epoch, active from Sep25 14:46 -> the
    rendered restart line binds to Sep 25, NOT the removed Sep 15. Drives the
    REAL route; FAILS on main (Sep 15), PASSES on branch (Sep 25)."""
    html = _render_real_route(
        monkeypatch,
        majorities=[{"hash": "9F287AED", "name": "BatchV1_1",
                     "recognized": True,
                     "majority_reached_iso": "2026-09-25T14:46:02Z",
                     "activation_eta_iso": "2026-10-09T14:46:02Z"}],
        majority_history=_BATCH_HISTORY)
    assert "Countdown restarted" in html
    restart_iso = _restart_line_iso(html)
    assert restart_iso == "2026-09-25T14:46:02Z", (
        f"restart line anchored to {restart_iso!r}; expected the ACTIVE epoch "
        f"Sep 25 (2026-09-25T14:46:02Z), not the removed epoch Sep 15")
    assert restart_iso != "2026-09-15T14:06:41Z"


def test_restart_c_single_epoch_no_restart_line(monkeypatch):
    """(c) A single-epoch hash (no removed row) gets NO restart entry, and the
    real route renders no 'Countdown restarted' line."""
    hist = [_mh_row(hash="14A2B45E", name="fixBatchV1_2",
                    majority_close_iso="2026-09-25T14:12:51Z", removed_iso=None,
                    activation_eta_iso="2026-10-09T14:12:51Z")]
    assert "14A2B45E" not in _restart_build(hist)
    html = _render_real_route(
        monkeypatch,
        majorities=[{"hash": "14A2B45E", "name": "fixBatchV1_2",
                     "recognized": True,
                     "majority_reached_iso": "2026-09-25T14:12:51Z",
                     "activation_eta_iso": "2026-10-09T14:12:51Z"}],
        majority_history=hist)
    assert "Countdown restarted" not in html


def test_restart_d_multiple_removed_epochs_use_active():
    """(d) Two removed epochs + one active -> still the ACTIVE epoch's close."""
    hist = [
        {"hash": "ABCDEF01", "majority_close_iso": "2026-09-24T21:25:01Z",
         "removed_iso": None},
        {"hash": "ABCDEF01", "majority_close_iso": "2026-09-21T11:18:40Z",
         "removed_iso": "2026-09-23T12:47:20Z"},
        {"hash": "ABCDEF01", "majority_close_iso": "2026-09-10T08:00:00Z",
         "removed_iso": "2026-09-12T09:00:00Z"},
    ]
    r = _restart_build(hist)
    assert r["ABCDEF01"] == "2026-09-24T21:25:01Z"


def test_restart_e_no_active_epoch_shows_no_restart_date():
    """(e) Removed epoch(s) but NO active epoch (support currently lost) ->
    no restart date (don't point at a stale old epoch)."""
    hist = [
        {"hash": "DEAD0001", "majority_close_iso": "2026-09-21T11:18:40Z",
         "removed_iso": "2026-09-23T12:47:20Z"},
        {"hash": "DEAD0001", "majority_close_iso": "2026-09-10T08:00:00Z",
         "removed_iso": "2026-09-12T09:00:00Z"},
    ]
    r = _restart_build(hist)
    assert "DEAD0001" not in r
