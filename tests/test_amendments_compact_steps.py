"""Compact stepper on /amendments (branch amendments-compact-steps-2026-10-04).

The six activation-timeline steps in each "in countdown" card were a tall
stacked <li> list (1 NOW + 5 WAITING per card, three cards). This change
restructures them into ONE WRAPPING ROW of native <details> stops: each step's
EXISTING badge + EXISTING title become the <summary>, and that step's same full
body opens underneath. No JavaScript, no wording change, no data removed.

What these tests prove (Charlie's four required assertions, 2026-10-04):

  (a) test_six_compact_stops_per_countdown_card
      All six steps still render, for each of the three countdown amendments.

  (b) test_step_wording_identical_to_origin_main
      The strongest guard: renders the SAME context through origin/main's
      template AND the working-tree template, then compares the visible text
      of every step block word for word. Fails if a single word differs.
      This is what makes "no data removed" a test result instead of a claim.

  (c) test_only_the_now_step_is_open_by_default
      Exactly one stop per card carries `open`, and it is the NOW step.

  (d) test_no_invalid_color_functions_in_css
      Guards the CSS added here against malformed color functions
      (e.g. a stray space producing `rg ba(` / `rgba (`), and checks every
      rgb()/rgba()/hsl()/hsla() has a legal argument count.

Hermetic: no network, no DB. The state/roll_call/timeline context is injected
in the shape app.py's /amendments route builds, so the real template renders
through the real Flask Jinja env (custom filters + includes included).
"""
from __future__ import annotations

import html as _html
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)


# --------------------------------------------------------------------------
# Context fixtures — shaped like the real route's render kwargs.
# --------------------------------------------------------------------------

# Three amendments in countdown, matching the live page. Hashes are 64-char
# hex placeholders (shape-correct; the template only echoes and slices them).
_MAJORITIES = [
    {
        "name": "PermissionDelegationV1_1",
        "hash": "0F48FF561C7095403288F31F1C97FD512ACC8B4E42138A161CB0B1A2C3D4E5F6",
        "recognized": True,
        "activation_eta_iso": "2026-10-08T21:25:00Z",
        "majority_reached_iso": "2026-09-24T21:25:00Z",
    },
    {
        "name": "fixBatchV1_2",
        "hash": "1A2B3C4D5E6F70819200A1B2C3D4E5F60718293A4B5C6D7E8F90A1B2C3D4E5F6",
        "recognized": True,
        "activation_eta_iso": "2026-10-09T15:00:00Z",
        "majority_reached_iso": "2026-09-25T15:00:00Z",
    },
    {
        "name": "BatchV1_1",
        "hash": "9F8E7D6C5B4A39281716253443526170FFEEDDCCBBAA99887766554433221100",
        "recognized": True,
        "activation_eta_iso": "2026-10-10T12:00:00Z",
        "majority_reached_iso": "2026-09-26T12:00:00Z",
    },
]

_NAMES = [m["name"] for m in _MAJORITIES]


def _state():
    return {
        "ok": True,
        "enabled_count": 80,
        "in_flight_count": 10,
        "ledger_index": 107_437_000,
        "majorities": _MAJORITIES,
        "in_flight": [],
        "enabled": [],
        "unrecognized_enabled_count": 0,
        "unrecognized_enabled": [],
    }


def _roll_call():
    return {
        "unl_full": 35,
        "needed_full": 29,
        "unl_size": 35,
        "seen": 33,
        "observed_iso": "2026-10-04T23:02:00Z",
        "observed_et": "Sun Oct 4, 2026, 7:02 PM ET (11:02 PM UTC)",
        "next_eta_min": 3,
        "voting_ledger": 107_437_056,
        "flag_ledger": 107_437_056,
        "next_voting_ledger": 107_437_312,
        "source": "own-node validations stream",
        "rounds_recorded": 42,
        "first_round_iso": "2026-09-26T00:00:00Z",
        "any_reset": False,
        "stale": False,
        "rows": [
            {
                "hash": m["hash"],
                "name": m["name"],
                "yes_carried": 29,
                "count_state": "passing",
                "not_heard": 2,
                "best_possible": 31,
            }
            for m in _MAJORITIES
        ],
    }


def _timeline_by_hash():
    """One NOW + five WAITING — the live 1-NOW/5-WAITING shape."""
    base = {
        "enabled": False,
        "restarted": False,
        "restarted_iso": None,
        "days_elapsed": 10,
        "hours_elapsed": 1,
        "ledgers_remaining": 41,
        "next_flag_ledger": 107_437_056,
        "flag_plus_1": 107_437_057,
        "flag_plus_2": 107_437_058,
        "steps": {
            1: "now", 2: "waiting", 3: "waiting",
            4: "waiting", 5: "waiting", 6: "waiting",
        },
    }
    return {
        m["hash"]: dict(base, activation_eta_iso=m["activation_eta_iso"])
        for m in _MAJORITIES
    }


_CTX = dict(
    majority_history=None,
    page_sourcing=None,
    flag_counter=None,
    cite=None,
    cache_ttl_seconds=300,
)


def _render(template_name="amendments.html"):
    import app

    with app.app.test_request_context("/amendments"):
        return app.render_template(
            template_name,
            state=_state(),
            roll_call=_roll_call(),
            timeline_by_hash=_timeline_by_hash(),
            **_CTX,
        )


def _render_origin_main():
    """Render origin/main's amendments.html with the SAME injected context.

    origin/main's copy is dropped into a temp dir that is searched FIRST, so
    its {% include %} partials and the custom Jinja filters still resolve from
    the real app. Returns None (skip) when origin/main is not fetched.
    """
    import jinja2

    import app

    try:
        blob = subprocess.run(
            ["git", "show", "origin/main:templates/amendments.html"],
            cwd=REPO, capture_output=True, check=True, text=True,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None

    original_loader = app.app.jinja_loader
    with tempfile.TemporaryDirectory() as td:
        with open(os.path.join(td, "amendments.html"), "w", encoding="utf-8") as fh:
            fh.write(blob)
        app.app.jinja_loader = jinja2.ChoiceLoader(
            [jinja2.FileSystemLoader(td), original_loader]
        )
        try:
            app.app.jinja_env.cache = None
            return _render("amendments.html")
        finally:
            app.app.jinja_loader = original_loader
            app.app.jinja_env.cache = None


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _countdown_cards(page):
    """The three `<div class="countdown" id="...">` blocks, each isolated.

    re.split drops the delimiter, so each returned chunk contains exactly one
    card's markup and cannot bleed into the next card.
    """
    parts = re.split(r'<div class="countdown" id="', page)
    return parts[1:]


def _step_blocks(card):
    """Each `<li class="timeline-step ...">...</li>` in document order."""
    return re.findall(
        r'<li class="timeline-step[^"]*"[^>]*>(.*?)</li>', card, re.S
    )


def _visible_text(fragment):
    """Strip tags/entities and collapse whitespace -> comparable visible text."""
    txt = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", fragment)
    txt = re.sub(r"(?s)<!--.*?-->", " ", txt)
    txt = re.sub(r"(?s)<[^>]+>", " ", txt)
    return re.sub(r"\s+", " ", _html.unescape(txt)).strip()


def _style_css(page):
    return "\n".join(
        re.findall(r"(?is)<style[^>]*>(.*?)</style>", page)
    )


# --------------------------------------------------------------------------
# (a) all six steps still render, for each countdown amendment
# --------------------------------------------------------------------------

def test_six_compact_stops_per_countdown_card():
    page = _render()
    cards = _countdown_cards(page)
    assert len(cards) == len(_MAJORITIES), (
        f"expected {len(_MAJORITIES)} countdown cards, got {len(cards)}"
    )

    expected_titles = [
        "Support holds for two weeks",
        "Wait for the next flag ledger",
        "The network records the change",
        "The amendment turns on",
        "It becomes permanent",
        "Old servers fall behind",
    ]

    for card, name in zip(cards, _NAMES):
        assert card.lstrip().startswith(name), (
            f"card order unexpected; wanted {name!r}"
        )

        steps = _step_blocks(card)
        assert len(steps) == 6, (
            f"{name}: expected 6 timeline steps, got {len(steps)}"
        )

        stops = re.findall(r'<details class="timeline-stop"', card)
        assert len(stops) == 6, (
            f"{name}: expected 6 compact <details> stops, got {len(stops)}"
        )

        # every stop carries a <summary> (the clickable compact row)
        summaries = re.findall(r'<summary class="timeline-summary">', card)
        assert len(summaries) == 6, (
            f"{name}: expected 6 <summary> rows, got {len(summaries)}"
        )

        # the row must be the wrapping-compact variant, not the tall list
        assert 'class="timeline-steps timeline-steps-compact"' in card, (
            f"{name}: compact stepper class missing"
        )

        # each step's existing title is present, in order, inside its summary
        for idx, (step, title) in enumerate(zip(steps, expected_titles), 1):
            summary = re.search(r"(?s)<summary.*?</summary>", step)
            assert summary, f"{name}: step {idx} has no <summary>"
            assert title in summary.group(0), (
                f"{name}: step {idx} summary missing existing title {title!r}"
            )

    # no JavaScript was introduced for the stepper
    assert "timeline-stop" not in "\n".join(
        re.findall(r"(?is)<script[^>]*>(.*?)</script>", page)
    ), "the compact stepper must not depend on JavaScript"


# --------------------------------------------------------------------------
# (b) step wording identical to origin/main, word for word
# --------------------------------------------------------------------------

def test_step_wording_identical_to_origin_main():
    before = _render_origin_main()
    if before is None:
        pytest.skip("origin/main not available (run `git fetch origin`)")
    after = _render()

    cards_before = _countdown_cards(before)
    cards_after = _countdown_cards(after)
    assert len(cards_before) == len(cards_after) == len(_MAJORITIES)

    for name, cb, ca in zip(_NAMES, cards_before, cards_after):
        steps_before = _step_blocks(cb)
        steps_after = _step_blocks(ca)
        assert len(steps_before) == 6, f"{name}: origin/main had no 6 steps"
        assert len(steps_after) == len(steps_before), (
            f"{name}: step count changed "
            f"{len(steps_before)} -> {len(steps_after)}"
        )

        for idx, (sb, sa) in enumerate(zip(steps_before, steps_after), 1):
            tb = _visible_text(sb)
            ta = _visible_text(sa)
            assert ta == tb, (
                f"{name}: step {idx} WORDING CHANGED.\n"
                f"  origin/main: {tb!r}\n"
                f"  working tree: {ta!r}"
            )

    # and nothing was dropped page-wide: every word of the old step text
    # must still be somewhere in the new render
    for cb, ca in zip(cards_before, cards_after):
        for sb in _step_blocks(cb):
            assert _visible_text(sb) in _visible_text(ca), (
                "a step's text is missing from the new card render"
            )


# --------------------------------------------------------------------------
# (c) only the NOW step is open by default
# --------------------------------------------------------------------------

def test_only_the_now_step_is_open_by_default():
    page = _render()
    for card, name in zip(_countdown_cards(page), _NAMES):
        steps = _step_blocks(card)
        assert len(steps) == 6, f"{name}: expected 6 steps"

        card_head = re.findall(
            r'<li class="timeline-step[^"]*"[^>]*data-status="([a-z]+)"', card
        )
        assert len(card_head) == 6, f"{name}: could not read 6 step statuses"

        open_count = 0
        for idx, (step, status) in enumerate(zip(steps, card_head), 1):
            stop = re.search(r"<details[^>]*>", step)
            assert stop, f"{name}: step {idx} has no <details> stop"
            is_open = " open" in stop.group(0)
            if status == "now":
                assert is_open, (
                    f"{name}: step {idx} is NOW but is not open by default"
                )
                open_count += 1
            else:
                assert not is_open, (
                    f"{name}: step {idx} is {status.upper()} but is open "
                    f"by default; only NOW may be open"
                )
        assert open_count == 1, (
            f"{name}: expected exactly 1 open (NOW) stop, got {open_count}"
        )

    # exactly one open stop per countdown card page-wide
    assert len(re.findall(r'<details class="timeline-stop" open>', page)) >= 3


# --------------------------------------------------------------------------
# (d) no invalid colour functions in the CSS
# --------------------------------------------------------------------------

_COLOR_ARGS = {"rgb": (3,), "rgba": (3, 4), "hsl": (3,), "hsla": (3, 4)}


def test_no_invalid_color_functions_in_css():
    css = _style_css(_render())
    assert css.strip(), "no <style> CSS found in the rendered page"

    # 1. a space between the function name and "(" is invalid CSS
    broken = re.findall(r"\b(rgba?|hsla?)\s+\(", css)
    assert not broken, f"colour function with a stray space before '(': {broken}"

    # 2. a space *inside* the function name (the 'rg ba(' class of typo)
    typos = re.findall(r"\b(?:r\s+gba?|rg\s+ba?|rgb\s+a|h\s+sla?|hs\s+la?)\s*\(", css)
    assert not typos, f"malformed colour function name: {typos}"

    # 3. every colour function must have a legal argument count
    bad = []
    for fn, args in re.findall(r"\b(rgba?|hsla?)\(([^()]*)\)", css):
        n = len([a for a in args.split(",") if a.strip()])
        if n not in _COLOR_ARGS[fn]:
            bad.append(f"{fn}({args}) -> {n} args")
    assert not bad, f"colour functions with an illegal argument count: {bad}"

    # 4. the stepper CSS this change adds must actually be present
    for rule in (
        ".timeline-steps-compact",
        ".timeline-summary",
        ".timeline-stop",
    ):
        assert rule in css, f"expected stepper CSS rule {rule!r} in the page"

    # 5. the row wraps instead of scrolling sideways (no horizontal scroll)
    compact = re.search(
        r"\.activation-timeline \.timeline-steps-compact\s*\{([^}]*)\}", css
    )
    assert compact, "missing .timeline-steps-compact rule"
    assert "flex-wrap: wrap" in compact.group(1), (
        "the compact stepper row must wrap, never scroll sideways"
    )
    assert "overflow-x" not in compact.group(1), (
        "the compact stepper row must not introduce horizontal scrolling"
    )


# --------------------------------------------------------------------------
# ITEM 2 (Charlie 2026-10-04): step 2's ledger numbers must be gated exactly
# like steps 3/4 by show_flag_numbers — visible ONLY once the 14-day clock is
# done and the amendment is not yet enabled.
#
# Why: roll_call_card.flag_ledger_counter() reports the next flag ledger off
# the CURRENT validated ledger (minutes away). That is not the flag ledger
# that activates THIS amendment, which is the first flag ledger after its own
# 14 days end — days away. Showing it mid-countdown made a days-away
# activation look imminent.
# --------------------------------------------------------------------------

_NOW = datetime(2026, 10, 4, 21, 0, 0, tzinfo=timezone.utc)
_FLAG_COUNTER = {"ledgers_remaining": 41, "next_flag_ledger": 107_437_056}


def _tl(days_ago, enabled=False):
    """Real _activation_timeline_ctx with a live flag counter attached.

    days_ago = how long ago the 14-day majority clock started, so 10 -> clock
    still running, 15 -> clock done.
    """
    import app

    reached = _NOW - timedelta(days=days_ago)
    return app._activation_timeline_ctx(
        majority_reached_iso=reached.strftime("%Y-%m-%dT%H:%M:%SZ"),
        activation_eta_iso=(reached + timedelta(days=14)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        flag_counter=_FLAG_COUNTER,
        enabled=enabled,
        now=_NOW,
    )


def test_step2_numbers_hidden_while_clock_not_done():
    """Clock still running (10 of 14 days) -> no step-2 ledger numbers."""
    tl = _tl(days_ago=10)

    assert tl["steps"][1] == "now", "step 1 should still be the NOW step"
    assert tl["steps"][2] == "waiting", "step 2 status must be unchanged"

    assert tl["ledgers_remaining"] is None, (
        "step 2 must not expose 'ledgers remaining' while the 14-day clock "
        f"is still running; got {tl['ledgers_remaining']!r}"
    )
    assert tl["next_flag_ledger"] is None, (
        "step 2 must not expose 'next flag ledger' while the 14-day clock "
        f"is still running; got {tl['next_flag_ledger']!r}"
    )
    # gated on the SAME condition as steps 3/4, which were already correct
    assert tl["flag_plus_1"] is None and tl["flag_plus_2"] is None


def test_step2_numbers_shown_once_clock_done():
    """Clock done (15 days) and not yet enabled -> numbers appear."""
    tl = _tl(days_ago=15)

    assert tl["steps"][1] == "done", "step 1 should be DONE after 14 days"
    assert tl["steps"][2] == "now", "step 2 should be the NOW step"

    assert tl["ledgers_remaining"] == _FLAG_COUNTER["ledgers_remaining"], (
        "step 2 must show 'ledgers remaining' once the clock is done"
    )
    assert tl["next_flag_ledger"] == _FLAG_COUNTER["next_flag_ledger"], (
        "step 2 must show 'next flag ledger' once the clock is done"
    )
    # steps 3/4 light up on the same condition
    assert tl["flag_plus_1"] == _FLAG_COUNTER["next_flag_ledger"] + 1
    assert tl["flag_plus_2"] == _FLAG_COUNTER["next_flag_ledger"] + 2


def test_step2_numbers_hidden_once_enabled():
    """Already enabled -> the next flag ledger is a future, unrelated one."""
    tl = _tl(days_ago=30, enabled=True)
    assert tl["ledgers_remaining"] is None
    assert tl["next_flag_ledger"] is None


def test_step2_gate_is_identical_to_steps_3_and_4():
    """The step-2 numbers appear in exactly the same window as steps 3/4."""
    for days_ago, enabled in (
        (0, False), (10, False), (13, False),
        (15, False), (30, False),
        (15, True), (30, True),
    ):
        tl = _tl(days_ago=days_ago, enabled=enabled)
        step2_shown = tl["ledgers_remaining"] is not None
        step34_shown = tl["flag_plus_1"] is not None
        assert step2_shown == step34_shown, (
            f"days_ago={days_ago} enabled={enabled}: step 2 shown="
            f"{step2_shown} but steps 3/4 shown={step34_shown} — step 2 must "
            "be gated on the same show_flag_numbers window"
        )


def _render_with_timeline(tl_overrides):
    """Render the page with every countdown card carrying tl_overrides."""
    import app

    base = {
        "enabled": False,
        "restarted": False,
        "restarted_iso": None,
        "days_elapsed": 10,
        "hours_elapsed": 2,
        "ledgers_remaining": None,
        "next_flag_ledger": None,
        "flag_plus_1": None,
        "flag_plus_2": None,
        "steps": {
            1: "now", 2: "waiting", 3: "waiting",
            4: "waiting", 5: "waiting", 6: "waiting",
        },
    }
    base.update(tl_overrides)
    tbh = {
        m["hash"]: dict(base, activation_eta_iso=m["activation_eta_iso"])
        for m in _MAJORITIES
    }
    with app.app.test_request_context("/amendments"):
        return app.render_template(
            "amendments.html",
            state=_state(),
            roll_call=_roll_call(),
            timeline_by_hash=tbh,
            **_CTX,
        )


def _step2_text(page):
    """Visible text of step 2's live-data line, per countdown card."""
    out = []
    for card in _countdown_cards(page):
        steps = _step_blocks(card)
        assert len(steps) == 6, "expected 6 steps per card"
        data = re.search(
            r'<div class="timeline-data dim">(.*?)</div>', steps[1], re.S
        )
        out.append(_visible_text(data.group(1)) if data else "")
    return out


def test_render_step2_shows_no_numbers_while_clock_not_done():
    """Gated context -> step 2's data line renders empty, no digits at all."""
    page = _render_with_timeline(
        {"ledgers_remaining": None, "next_flag_ledger": None}
    )
    for txt in _step2_text(page):
        assert not re.search(r"\d", txt), (
            f"step 2 rendered a ledger number while gated off: {txt!r}"
        )
        assert "ledgers remaining" not in txt
        assert "next flag ledger" not in txt


def test_render_step2_shows_numbers_once_clock_done():
    """Ungated context -> the existing labels and numbers render as before."""
    page = _render_with_timeline({
        "ledgers_remaining": 41,
        "next_flag_ledger": 107_437_056,
        "steps": {
            1: "done", 2: "now", 3: "waiting",
            4: "waiting", 5: "waiting", 6: "waiting",
        },
    })
    for txt in _step2_text(page):
        assert "41" in txt, f"step 2 lost 'ledgers remaining' count: {txt!r}"
        assert "107,437,056" in txt, (
            f"step 2 lost the next flag ledger number: {txt!r}"
        )
        assert "ledgers remaining" in txt
        assert "next flag ledger" in txt
