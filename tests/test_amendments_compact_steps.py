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
import json
import os
import re
import sys
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
                # Required by _amendments_rollcall_verdict.html (e31d3ee,
                # 2026-10-09), which landed AFTER this file (2026-10-04).
                # Jinja RAISES on `Undefined > 0`, so a missing spare_votes in
                # `{% if rc_row.spare_votes > 0 %}` killed the ENTIRE
                # /amendments render -- every test here read as "nothing
                # rendered". Pinned by tests/test_rollcall_verdict_contract.py.
                "needed": 29,
                "spare_votes": 0,
                "short_by": 0,
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


# ITEM 5 (Charlie-approved 2026-10-04): the ONLY sanctioned wording change on
# this branch is the singular at a count of 1 — "1 hours" -> "1 hour" and
# "1 days" -> "1 day". The word-for-word guard below must keep catching every
# OTHER difference, so it normalizes just these two back to plural before
# comparing. Applied to BOTH sides: origin/main only ever emits the plural, so
# it is a no-op there and merely reverses the approved fix on the working tree.
# Anything else that differs by a single word still fails the guard.
_ITEM5_SANCTIONED = ((r"\b1 hour(?!s)", "1 hours"), (r"\b1 day(?!s)", "1 days"))


def _normalize_item5(text):
    for pattern, plural in _ITEM5_SANCTIONED:
        text = re.sub(pattern, plural, text)
    return text


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
# (b) step wording pinned to a COMMITTED golden baseline, word for word
#
# These two tests used to shell out to
#     git show origin/main:templates/amendments.html
# and pytest.skip() when that failed. ci.yml sets no fetch-depth, so
# actions/checkout@v4 hands the runner a depth-1 clone with NO origin/main ref
# at all -> both tests SKIPPED SILENTLY in CI while the job stayed green. They
# were the only guard on the step wording, so the wording was effectively
# unguarded on every push.
#
# A committed fixture means they always run: no network, no git history, any
# clone, any depth. There is no pytest.skip() left anywhere in this file.
# --------------------------------------------------------------------------

GOLDEN_PATH = os.path.join(
    REPO, "tests", "fixtures", "amendments_step_wording.json"
)

_GOLDEN_REFRESH_CMD = (
    "UPDATE_STEP_WORDING_GOLDEN=1 pytest "
    "tests/test_amendments_compact_steps.py -k wording -q"
)

_GOLDEN_META = {
    "_what": (
        "Golden baseline of the /amendments compact-stepper step wording, as "
        "rendered under the fixed test context in "
        "tests/test_amendments_compact_steps.py. Visible text only: tags and "
        "entities stripped, whitespace collapsed."
    ),
    "_why": (
        "The two tests that consume this file used to shell out to `git show "
        "origin/main:templates/amendments.html` and pytest.skip() when that "
        "failed. ci.yml sets no fetch-depth, so actions/checkout@v4 gives a "
        "depth-1 clone with no origin/main ref - both tests SKIPPED SILENTLY "
        "on the runner while the job looked green, leaving the step wording "
        "unguarded on every push. Committing the baseline means they always "
        "run."
    ),
    "_how_to_refresh": [
        "ONLY refresh when a wording change is intentional and approved.",
        "A failure here is the guard doing its job. Refreshing to silence a",
        "diff you did not intend defeats the entire point of the file.",
        "",
        "1. Make the approved wording change in templates/amendments.html.",
        "2. Regenerate from the working-tree render:",
        f"     {_GOLDEN_REFRESH_CMD}",
        "   (inside the usual hermetic env:",
        '     env -i PATH="$PWD/.civ/bin:/usr/bin:/bin" HOME="$PWD" \\',
        "       DATABASE_URL=\"\" bash -c 'cd \"$PWD\" && <command above>' )",
        "3. Re-run WITHOUT the env var; both wording tests must pass.",
        "4. git diff this file, read every changed string, and commit it in",
        "   the SAME commit as the template change so the pair is reviewable.",
        "",
        "Do not hand-edit the strings below - regenerate, so the baseline",
        "always reflects a real render rather than someone's expectation.",
        "",
        "NOTE on the _item5 test: the Item 5 singular fix ('1 hours' ->",
        "'1 hour') is ALREADY merged into main, so the baseline already holds",
        "the singular and there is no raw diff left for that test to explain.",
        "Its loop is therefore empty by design now; it keeps working because",
        "its second half pins the singular wording positively instead.",
    ],
}


def _golden_cards(page):
    """[{name, steps: [6 visible-text strings]}] for each countdown card."""
    cards = _countdown_cards(page)
    assert len(cards) == len(_MAJORITIES), (
        f"expected {len(_MAJORITIES)} countdown cards, got {len(cards)}"
    )
    out = []
    for name, card in zip(_NAMES, cards):
        steps = _step_blocks(card)
        assert len(steps) == 6, f"{name}: expected 6 steps, got {len(steps)}"
        out.append({"name": name, "steps": [_visible_text(s) for s in steps]})
    return out


def _maybe_refresh_golden(page):
    """Opt-in maintenance mode. Never silent - you must set the env var."""
    if os.environ.get("UPDATE_STEP_WORDING_GOLDEN") != "1":
        return
    doc = dict(_GOLDEN_META)
    doc["_captured_from"] = "working-tree render at refresh time"
    doc["cards"] = _golden_cards(page)
    os.makedirs(os.path.dirname(GOLDEN_PATH), exist_ok=True)
    with open(GOLDEN_PATH, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    print(f"\nREFRESHED golden baseline -> {GOLDEN_PATH}")


def _load_golden():
    """The committed baseline. HARD FAILS - there is deliberately no skip.

    Every failure mode here raises instead of skipping, because a skip is how
    this guard went silent in CI in the first place.
    """
    if not os.path.exists(GOLDEN_PATH):
        raise AssertionError(
            f"missing golden baseline: {GOLDEN_PATH}\n"
            "It is committed on purpose so these tests cannot skip in CI.\n"
            f"If the wording change is intentional: {_GOLDEN_REFRESH_CMD}"
        )
    with open(GOLDEN_PATH, encoding="utf-8") as fh:
        try:
            doc = json.load(fh)
        except json.JSONDecodeError as exc:
            raise AssertionError(
                f"golden baseline is not valid JSON ({GOLDEN_PATH}): {exc}"
            ) from exc

    cards = doc.get("cards")
    assert isinstance(cards, list) and cards, (
        f"golden baseline has no 'cards' list: {GOLDEN_PATH}"
    )
    assert len(cards) == len(_MAJORITIES), (
        f"golden baseline holds {len(cards)} cards, expected "
        f"{len(_MAJORITIES)}: {GOLDEN_PATH}"
    )
    for card in cards:
        steps = card.get("steps")
        assert isinstance(steps, list) and len(steps) == 6, (
            f"golden card {card.get('name')!r} must hold 6 steps"
        )
        # an empty/blank baseline would make both tests pass on nothing
        assert all(isinstance(s, str) and s.strip() for s in steps), (
            f"golden card {card.get('name')!r} has a BLANK step string; a "
            "vacuous baseline would make these tests pass on nothing"
        )
    return cards


def test_step_wording_identical_to_origin_main():
    """Step wording must match the committed baseline, word for word.

    Re-pointed 2026-10-10 off `git show origin/main` onto
    tests/fixtures/amendments_step_wording.json - see the section comment
    above for why the old form skipped silently in CI. The baseline was
    captured from a render that is byte-identical to origin/main @ 45c5489.
    """
    after = _render()
    _maybe_refresh_golden(after)

    golden = _load_golden()
    actual = _golden_cards(after)

    assert [c["name"] for c in actual] == [c["name"] for c in golden], (
        "countdown card order changed: "
        f"{[c['name'] for c in golden]} -> {[c['name'] for c in actual]}"
    )

    for exp, got in zip(golden, actual):
        name = exp["name"]
        assert len(got["steps"]) == len(exp["steps"]) == 6, (
            f"{name}: step count changed "
            f"{len(exp['steps'])} -> {len(got['steps'])}"
        )
        for idx, (sb, sa) in enumerate(zip(exp["steps"], got["steps"]), 1):
            tb = _normalize_item5(sb)
            ta = _normalize_item5(sa)
            assert ta == tb, (
                f"{name}: step {idx} WORDING CHANGED.\n"
                f"  baseline:     {tb!r}\n"
                f"  working tree: {ta!r}\n"
                f"If intentional, refresh: {_GOLDEN_REFRESH_CMD}"
            )


def test_item5_is_the_only_wording_difference_from_origin_main():
    """Without the Item 5 allowance, the ONLY diff is the singular fix.

    Guards the normalization itself: if a future change sneaks in another
    wording difference, the raw (un-normalized) comparison will differ in
    some way the sanctioned singular substitutions cannot explain.
    """
    after = _render()
    _maybe_refresh_golden(after)

    golden = _load_golden()
    actual = _golden_cards(after)

    raw_diffs = []
    for exp, got in zip(golden, actual):
        for idx, (sb, sa) in enumerate(zip(exp["steps"], got["steps"]), 1):
            if sb != sa:
                raw_diffs.append((exp["name"], idx, sb, sa))

    # every raw difference must be explained purely by the singular fix
    for name, idx, tb, ta in raw_diffs:
        assert _normalize_item5(ta) == _normalize_item5(tb), (
            f"{name}: step {idx} differs from the baseline in a way the Item 5 "
            f"singular fix does not explain.\n  baseline:     {tb!r}\n"
            f"  working tree: {ta!r}"
        )
        assert "1 hours" in tb or "1 days" in tb, (
            f"{name}: step {idx} changed but the baseline had no '1 hours'/"
            f"'1 days' to fix: {tb!r}"
        )

    # ---- NON-VACUITY HALF (added 2026-10-10) ----------------------------
    # The Item 5 singular fix is already merged into main, so the baseline
    # holds the singular too and raw_diffs is now EMPTY - the loop above
    # inspects nothing and would pass on anything. That is the same
    # empty-loop trap test_official_check_mark_is_pure_css was sitting in.
    # So pin the Item 5 wording positively: it must be LIVE, not merely
    # undisputed. The fixture renders days_elapsed=10, hours_elapsed=1.
    step1 = " | ".join(_step1_text(after))
    assert re.search(r"\b1 hour\b", step1), (
        f"Item 5's singular '1 hour' is not in step 1 any more: {step1!r}"
    )
    assert not re.search(r"\b1 hours\b", step1), (
        f"Item 5 REGRESSED - step 1 reads the plural '1 hours': {step1!r}"
    )
    assert not re.search(r"\b1 days\b", step1), (
        f"Item 5 REGRESSED - step 1 reads the plural '1 days': {step1!r}"
    )


# --------------------------------------------------------------------------
# ITEM 5 (Charlie 2026-10-04): singular at a count of 1 on step 1's elapsed
# line. "10 days 1 hours of 14" must read "10 days 1 hour of 14".
# Two new strings only: "day" and "hour".
# --------------------------------------------------------------------------

def _step1_text(page):
    """Visible text of step 1's live-data line, per countdown card."""
    out = []
    for card in _countdown_cards(page):
        steps = _step_blocks(card)
        assert len(steps) == 6, "expected 6 steps per card"
        data = re.search(
            r'<div class="timeline-data dim">(.*?)</div>', steps[0], re.S
        )
        out.append(_visible_text(data.group(1)) if data else "")
    return out


@pytest.mark.parametrize(
    "days,hours,expected",
    [
        (1, 1, "1 day 1 hour of 14"),
        (10, 1, "10 days 1 hour of 14"),
        (1, 5, "1 day 5 hours of 14"),
        (2, 1, "2 days 1 hour of 14"),
        (0, 0, "0 days 0 hours of 14"),
        (13, 23, "13 days 23 hours of 14"),
    ],
)
def test_step1_day_hour_pluralization(days, hours, expected):
    """Singular only at exactly 1; plural at 0 and at everything above 1."""
    page = _render_with_timeline(
        {"days_elapsed": days, "hours_elapsed": hours}
    )
    for txt in _step1_text(page):
        assert expected in txt, (
            f"days={days} hours={hours}: expected {expected!r} in step 1's "
            f"line, got {txt!r}"
        )


def test_no_one_hours_or_one_days_anywhere():
    """The reported bug strings must not survive for any count of 1."""
    page = _render_with_timeline(
        {"days_elapsed": 1, "hours_elapsed": 1}
    )
    for txt in _step1_text(page):
        assert "1 hours" not in txt, f"'1 hours' still rendered: {txt!r}"
        assert "1 days" not in txt, f"'1 days' still rendered: {txt!r}"


def test_item5_adds_only_day_and_hour_strings():
    """Step 1's line gains no vocabulary beyond 'day' and 'hour'.

    Compares the word set of the singular render against the plural render:
    the only difference may be day/days and hour/hours.
    """
    singular = _step1_text(
        _render_with_timeline({"days_elapsed": 1, "hours_elapsed": 1})
    )[0]
    plural = _step1_text(
        _render_with_timeline({"days_elapsed": 3, "hours_elapsed": 4})
    )[0]

    def _words(text):
        return {w for w in re.findall(r"[A-Za-z]+", text)}

    added = _words(singular) - _words(plural)
    removed = _words(plural) - _words(singular)
    assert added == {"day", "hour"}, (
        f"the singular render must add exactly 'day' and 'hour'; added {added}"
    )
    assert removed == {"days", "hours"}, (
        f"the singular render must drop exactly 'days' and 'hours'; "
        f"removed {removed}"
    )


# --------------------------------------------------------------------------
# PIECE A (Charlie 2026-10-04): one short timing line inside steps 2-6.
#
# Meaning: that step finishes within about 17 minutes after the 14 days end.
# Derived in app._activation_timeline_ctx as activation_eta_iso + 17 min and
# exposed as steps_complete_eta_iso. Wording approved by Charlie: "by about".
# Step 1 and every existing word are untouched.
# --------------------------------------------------------------------------

_ETA_ISO = "2026-10-08T21:25:01Z"
_ETA_PLUS_17 = "2026-10-08T21:42:01Z"


def test_steps_complete_eta_is_activation_plus_17_minutes():
    """The shared anchor is exactly the 14-day end + 17 minutes."""
    import app

    tl = app._activation_timeline_ctx(
        majority_reached_iso="2026-09-24T21:25:01Z",
        activation_eta_iso=_ETA_ISO,
        flag_counter=_FLAG_COUNTER,
        enabled=False,
        now=_NOW,
    )
    assert tl["activation_eta_iso"] == _ETA_ISO
    assert tl["steps_complete_eta_iso"] == _ETA_PLUS_17, (
        "steps_complete_eta_iso must be activation_eta_iso + 17 minutes; "
        f"got {tl['steps_complete_eta_iso']!r}"
    )


def test_steps_complete_eta_is_none_without_activation_eta():
    """No activation ETA (e.g. the finished example) -> line self-hides."""
    import app

    tl = app._activation_timeline_ctx(
        majority_reached_iso=None, activation_eta_iso=None,
        flag_counter=_FLAG_COUNTER, enabled=True, now=_NOW,
    )
    assert tl["steps_complete_eta_iso"] is None


def _eta_lines(page):
    """The data-step-eta timing lines per countdown card, keyed by step."""
    out = []
    for card in _countdown_cards(page):
        found = {}
        for n, body in re.findall(
            r'<div class="timeline-data dim" data-step-eta="(\d)">(.*?)</div>',
            card, re.S,
        ):
            found[int(n)] = _visible_text(body)
        out.append(found)
    return out


def test_timing_line_appears_on_steps_2_to_6_only():
    """Steps 2-6 carry the line; step 1 must not."""
    page = _render_with_timeline({"steps_complete_eta_iso": _ETA_PLUS_17})
    lines = _eta_lines(page)
    assert len(lines) == len(_MAJORITIES)
    for found in lines:
        assert sorted(found) == [2, 3, 4, 5, 6], (
            "the timing line must render on steps 2-6 and nowhere else; "
            f"found it on steps {sorted(found)}"
        )


def test_timing_line_wording_and_eastern_first():
    """Reads 'by about', Eastern first, UTC in parentheses."""
    page = _render_with_timeline({"steps_complete_eta_iso": _ETA_PLUS_17})
    for found in _eta_lines(page):
        for n, txt in found.items():
            assert txt.startswith("by about"), (
                f"step {n}: timing line must start with 'by about'; got {txt!r}"
            )
            assert "ET" in txt and "UTC" in txt, (
                f"step {n}: need both ET and UTC; got {txt!r}"
            )
            # Eastern first: the ET reading precedes the parenthesised UTC
            assert txt.index(" ET") < txt.index("("), (
                f"step {n}: Eastern must come first, UTC in parentheses; "
                f"got {txt!r}"
            )
            assert "UTC)" in txt, (
                f"step {n}: UTC must be inside the parentheses; got {txt!r}"
            )


def test_timing_line_hidden_when_no_eta():
    """Without steps_complete_eta_iso nothing renders — no empty divs."""
    page = _render_with_timeline({})
    for found in _eta_lines(page):
        assert found == {}, f"timing line rendered with no ETA: {found}"
    assert "data-step-eta" not in page


def test_timing_line_absent_from_finished_stepper():
    """An enabled amendment has nothing left to time, so no timing line.

    Re-pointed 2026-10-10 from _finished_block(_render_enabled_example()) to
    the real enabled render; the old vehicle was a no-op (see _render_enabled).
    The CLAIM is unchanged and still live - it is now checked on all three
    cards instead of one synthetic block.
    """
    for name, card in zip(_NAMES, _countdown_cards(_render_enabled())):
        assert "data-step-eta" not in card, (
            f"{name}: the enabled stepper must not carry the timing line"
        )


def test_timing_line_leaves_step1_and_existing_wording_alone():
    """Step 1's line is untouched and 'by about' is the only text added."""
    page = _render_with_timeline({"steps_complete_eta_iso": _ETA_PLUS_17})

    # step 1 still carries its own elapsed line, with no timing line added
    for txt in _step1_text(page):
        assert "of 14" in txt, f"step 1's elapsed line changed: {txt!r}"
        assert "by about" not in txt, (
            f"the timing line must not be added to step 1: {txt!r}"
        )

    # strip the timing lines back out: the card text must then match a render
    # with no timing line at all, proving nothing else moved or changed
    stripped = re.sub(
        r'<div class="timeline-data dim" data-step-eta="\d">.*?</div>',
        "", page, flags=re.S,
    )
    baseline = _render_with_timeline({})
    for a, b in zip(_countdown_cards(stripped), _countdown_cards(baseline)):
        for sa, sb in zip(_step_blocks(a), _step_blocks(b)):
            assert _visible_text(sa) == _visible_text(sb), (
                "removing the timing line must restore the previous text "
                "exactly — something else changed"
            )


# --------------------------------------------------------------------------
# Charlie's item 3 (2026-10-04): prove the card HEADLINE activation time
# equals step 1's "activates" time, THROUGH THE REAL ROUTE.
#
# The headline reads m.activation_eta_iso (from state.majorities) while step 1
# reads tl.activation_eta_iso (from the timeline context). They are only equal
# because the route wires one from the other; nothing tested that until now,
# so an independent recompute of the timeline ETA could silently drift them.
#
# Hermetic: the route's data sources are patched, so this exercises the real
# view function and real template with no DB and no network.
# --------------------------------------------------------------------------

_ROUTE_MAJORITIES = [
    {"name": "AlphaAmendment", "hash": "A" * 64, "recognized": True,
     "majority_reached_iso": "2026-09-24T21:25:01Z",
     "activation_eta_iso": "2026-10-08T21:25:01Z"},
    {"name": "BetaAmendment", "hash": "B" * 64, "recognized": True,
     "majority_reached_iso": "2026-09-25T14:12:51Z",
     "activation_eta_iso": "2026-10-09T14:12:51Z"},
    {"name": "GammaAmendment", "hash": "C" * 64, "recognized": True,
     "majority_reached_iso": "2026-09-25T14:46:02Z",
     "activation_eta_iso": "2026-10-09T14:46:02Z"},
]


def _route_page(monkeypatch):
    """GET /amendments through the real route with patched data sources.

    raising=True on every target on purpose: if one of these is renamed, the
    patch must FAIL LOUDLY rather than silently let the route reach the live
    node/DB, which would make this test non-hermetic and flaky in CI.

    roll_call_card is patched on the MODULE, not on `app` — the route does a
    function-local `import roll_call_card`, so `app.roll_call_card` does not
    exist (verified 2026-10-04).
    """
    import app
    import roll_call_card

    state = dict(_state(), majorities=_ROUTE_MAJORITIES)
    monkeypatch.setattr(app, "fetch_amendments_state_cached",
                        lambda *a, **k: state)
    monkeypatch.setattr(app, "_load_amendment_majority_history",
                        lambda *a, **k: [])
    monkeypatch.setattr(app, "fetch_pulse_cached", lambda *a, **k: None)
    monkeypatch.setattr(app, "_amendments_cite_this_day", lambda *a, **k: None)
    monkeypatch.setattr(roll_call_card, "load_for_page", lambda *a, **k: None)

    app.app.config["TESTING"] = True
    resp = app.app.test_client().get("/amendments")
    assert resp.status_code == 200, (
        f"/amendments returned {resp.status_code} through the real route"
    )
    return resp.data.decode()


def test_headline_activation_equals_step1_activation_via_route(monkeypatch):
    """Per card: headline ISO == step 1 'activates' ISO == the source field."""
    page = _route_page(monkeypatch)

    cards = _countdown_cards(page)
    assert len(cards) == len(_ROUTE_MAJORITIES), (
        f"expected {len(_ROUTE_MAJORITIES)} countdown cards, got {len(cards)}"
    )

    for m, card in zip(_ROUTE_MAJORITIES, cards):
        name = m["name"]
        head = re.search(
            r'<div class="timer"[^>]*>\s*<time datetime="([^"]+)"', card
        )
        assert head, f"{name}: no headline activation <time> found"

        steps = _step_blocks(card)
        assert len(steps) == 6, f"{name}: expected 6 steps, got {len(steps)}"
        act = re.search(r'activates\s*<time datetime="([^"]+)"', steps[0])
        assert act, f"{name}: step 1 has no 'activates' <time>"

        assert head.group(1) == act.group(1), (
            f"{name}: headline and step 1 activation times DIFFER.\n"
            f"  headline: {head.group(1)}\n"
            f"  step 1  : {act.group(1)}"
        )
        # and both must be the field the route was given, not a recompute
        assert head.group(1) == m["activation_eta_iso"], (
            f"{name}: rendered activation {head.group(1)} does not match the "
            f"source activation_eta_iso {m['activation_eta_iso']}"
        )


def test_headline_and_step1_display_text_match_via_route(monkeypatch):
    """The human-readable strings match too, not just the ISO attributes."""
    page = _route_page(monkeypatch)

    for m, card in zip(_ROUTE_MAJORITIES, _countdown_cards(page)):
        name = m["name"]
        head = re.search(
            r'<div class="timer"[^>]*>\s*<time datetime="[^"]+">(.*?)</time>',
            card, re.S,
        )
        steps = _step_blocks(card)
        act = re.search(
            r'activates\s*<time datetime="[^"]+">(.*?)</time>', steps[0], re.S
        )
        assert head and act, f"{name}: missing headline or step 1 time text"

        head_txt = _visible_text(head.group(1))
        act_txt = _visible_text(act.group(1))
        assert head_txt == act_txt, (
            f"{name}: headline and step 1 display text DIFFER.\n"
            f"  headline: {head_txt!r}\n  step 1  : {act_txt!r}"
        )
        # Eastern first, UTC in parentheses
        assert " ET" in head_txt and "UTC)" in head_txt, (
            f"{name}: expected Eastern-first with UTC in parens; {head_txt!r}"
        )
        assert head_txt.index(" ET") < head_txt.index("("), (
            f"{name}: Eastern must come first; {head_txt!r}"
        )


def test_route_timing_line_derives_from_the_same_activation(monkeypatch):
    """Through the route, each card's timing line is its own ETA + 17 min."""
    from datetime import datetime, timedelta, timezone as _tz

    page = _route_page(monkeypatch)

    for m, card in zip(_ROUTE_MAJORITIES, _countdown_cards(page)):
        name = m["name"]
        # NOTE: the approved wording "by about " sits between the div's `>`
        # and the <time> tag, so this must span text, not just whitespace.
        # An earlier `\s*` here matched nothing and looked like a missing
        # timing line when the route was in fact rendering it correctly.
        etas = set(
            re.findall(
                r'data-step-eta="\d">[^<]*<time datetime="([^"]+)"', card
            )
        )
        assert etas, f"{name}: no timing line rendered through the route"
        assert len(etas) == 1, (
            f"{name}: steps 2-6 must share ONE timing value; got {etas}"
        )

        src = datetime.fromisoformat(
            m["activation_eta_iso"].replace("Z", "+00:00")
        )
        want = (src.astimezone(_tz.utc) + timedelta(minutes=17)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        assert etas.pop() == want, (
            f"{name}: timing line must be activation + 17 min ({want})"
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


# --------------------------------------------------------------------------
# ITEM 3 (Charlie 2026-10-04): the ENABLED (finished) stepper.
#
# In that render all six steps are DONE and none is NOW, so the
# "only the NOW step is open" rule from bcddb5d left EVERY stop collapsed.
# Fix: open step 5, "It becomes permanent", and mark it with a green
# check-mark badge labelled OFFICIAL (pure CSS check, no icon font/image).
#
# RE-POINTED 2026-10-10. These tests used to reach the enabled state through
# a synthetic timeline_by_hash['__enabled_example__'] demo block. 56c6025
# deleted that block, so the injection became a no-op and all four tests here
# failed looking exactly as if the FEATURE had been deleted. It had not: the
# enabled branch is live at amendments.html:546 (_open_n) and :557 (the badge),
# and is reached normally by flipping tl.enabled. They now render that.
# --------------------------------------------------------------------------

_PERMANENT_TITLE = "It becomes permanent"


def _render_enabled(**tl_overrides):
    """Render with every countdown card in the ENABLED (finished) state.

    Reaches the real enabled branch through the NORMAL timeline_by_hash path,
    so this is the markup a recently-enabled amendment actually gets:
      amendments.html:557  {% if tl.enabled and n == 5 %} -> OFFICIAL badge
      amendments.html:546  _open_n = 5 if (tl.enabled and not _has_now)

    Replaces _render_enabled_example(), which injected a synthetic
    timeline_by_hash['__enabled_example__']. 56c6025 deleted the generic
    DONE-steps block that was the ONLY consumer of that key, so the injection
    silently became a NO-OP - it rendered the same thing a plain _render()
    does, with zero OFFICIAL badges and no timeline-finished anchor. The tests
    below therefore failed for the wrong reason and read as "this feature was
    deleted" when only the demo vehicle had been. The feature is live.
    """
    steps = {n: "done" for n in (1, 2, 3, 4, 5, 6)}
    page = _render_with_timeline(
        {"enabled": True, "steps": steps, **tl_overrides}
    )

    # Guard the premise. Without these two asserts a future template change
    # could quietly stop rendering the enabled branch and every test below
    # would pass VACUOUSLY on markup that is not there - exactly the trap
    # test_official_check_mark_is_pure_css was already sitting in.
    assert 'data-status="now"' not in page, (
        "the enabled state must have no NOW step"
    )
    badges = page.count('class="timeline-official"')
    assert badges == len(_MAJORITIES), (
        f"the enabled branch did not render: expected {len(_MAJORITIES)} "
        f"OFFICIAL badges (one per card), got {badges}"
    )
    return page


def test_enabled_stepper_opens_exactly_the_permanent_step():
    """Exactly one stop open in the enabled stepper: 'It becomes permanent'.

    Guards amendments.html:546, which is LIVE:
        {% set _open_n = 5 if (tl.enabled and not _has_now) else 0 %}
    Re-pointed 2026-10-10 onto the real enabled render (see _render_enabled).
    """
    for name, card in zip(_NAMES, _countdown_cards(_render_enabled())):
        steps = _step_blocks(card)
        assert len(steps) == 6, (
            f"{name}: enabled stepper should have 6 steps, got {len(steps)}"
        )

        open_steps = []
        for idx, step in enumerate(steps, 1):
            stop = re.search(r"<details[^>]*>", step)
            assert stop, f"{name}: enabled step {idx} has no <details> stop"
            if " open" in stop.group(0):
                open_steps.append(idx)

        assert len(open_steps) == 1, (
            f"{name}: the enabled stepper must have EXACTLY one stop open by "
            f"default (bcddb5d left all six collapsed); open: {open_steps}"
        )

        opened = steps[open_steps[0] - 1]
        summary = re.search(r"(?s)<summary.*?</summary>", opened)
        assert summary, f"{name}: the opened stop has no <summary>"
        assert _PERMANENT_TITLE in summary.group(0), (
            f"{name}: the opened stop must be {_PERMANENT_TITLE!r}; opened "
            f"step {open_steps[0]}: {_visible_text(summary.group(0))!r}"
        )


def test_enabled_stepper_has_official_badge_on_permanent_step():
    """The OFFICIAL badge is present, and only on 'It becomes permanent'.

    Guards amendments.html:557, which is LIVE:
        {% if tl.enabled and n == 5 %}<span class="timeline-official" ...>
    Re-pointed 2026-10-10 onto the real enabled render (see _render_enabled).
    """
    for name, card in zip(_NAMES, _countdown_cards(_render_enabled())):
        steps = _step_blocks(card)
        assert len(steps) == 6, f"{name}: expected 6 steps, got {len(steps)}"

        badged = [
            idx for idx, step in enumerate(steps, 1)
            if "data-step-official" in step
        ]
        assert badged == [5], (
            f"{name}: the OFFICIAL badge must sit on step 5 only; "
            f"found on {badged}"
        )

        step5 = steps[4]
        assert _PERMANENT_TITLE in step5, f"{name}: step 5 is not permanent"
        assert "OFFICIAL" in _visible_text(step5), (
            f"{name}: the OFFICIAL label did not render on the permanent step"
        )
        assert 'class="timeline-official"' in step5, (
            f"{name}: the badge must carry the .timeline-official class it is "
            "styled by"
        )

        # the badge lives in the SUMMARY (the always-visible compact row)
        summary = re.search(r"(?s)<summary.*?</summary>", step5)
        assert summary and "data-step-official" in summary.group(0), (
            f"{name}: the OFFICIAL badge must be in the always-visible "
            "<summary> row"
        )


def test_official_badge_absent_from_countdown_cards():
    """OFFICIAL is an enabled-state marker only — never while in countdown.

    Re-pointed 2026-10-10. The old version rendered the synthetic
    __enabled_example__ page and truncated it before the `timeline-finished`
    anchor; both are gone, so it now asserts the live countdown render
    (enabled=False) carries no badge. The negative is guarded against passing
    vacuously: _render_enabled() proves the SAME template does emit the badge
    once tl.enabled flips, so a zero here means "correctly absent", not
    "never renders".
    """
    cards = _countdown_cards(_render())
    assert len(cards) == len(_MAJORITIES), (
        f"expected {len(_MAJORITIES)} countdown cards, got {len(cards)}"
    )
    for card, name in zip(cards, _NAMES):
        assert len(_step_blocks(card)) == 6, (
            f"{name}: card slice should hold exactly its own 6 steps"
        )
        assert "data-step-official" not in card, (
            f"{name}: OFFICIAL badge must not appear while in countdown"
        )
        assert "OFFICIAL" not in _visible_text(card), (
            f"{name}: the word OFFICIAL leaked into a countdown card"
        )


def test_official_check_mark_is_pure_css():
    """The check mark is drawn in CSS — no icon font, no image, no emoji.

    Re-pointed AND strengthened 2026-10-10. This test was PASSING VACUOUSLY:
    it only ever read the static <style> block, which every render emits
    regardless of state, so it asserted the rule was well-formed while never
    checking that a single element uses the class. It would have kept passing
    with the badge markup deleted outright. The badge-count assertion below is
    the missing half - the styled class must actually be applied to real
    markup, once per card, or the "pure CSS check mark" styles nothing.
    """
    page = _render_enabled()

    # the styled class must really be USED in markup, not merely defined
    body = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", page)
    used = len(re.findall(r'class="timeline-official"', body))
    assert used == len(_MAJORITIES), (
        "the .timeline-official class must be applied to real markup once per "
        f"card (expected {len(_MAJORITIES)}), found {used}"
    )

    css = _style_css(page)

    badge = re.search(
        r"\.activation-timeline \.timeline-official\s*\{([^}]*)\}", css
    )
    assert badge, "missing .timeline-official rule"

    mark = re.search(
        r"\.activation-timeline \.timeline-official::before\s*\{([^}]*)\}", css
    )
    assert mark, "missing .timeline-official::before (the pure-CSS check mark)"
    body = mark.group(1)

    # a rotated two-border corner = a check mark, with no glyph content
    assert re.search(r"content:\s*''", body), (
        "the check mark must use empty content, not a text/emoji glyph"
    )
    assert "transform" in body and "rotate" in body, (
        "the check mark must be a rotated border corner"
    )
    assert "border-right" in body and "border-bottom" in body, (
        "the check mark must be built from two borders"
    )
    assert "url(" not in body, "the check mark must not load an image"
    assert "font-family" not in body, "the check mark must not need an icon font"

    # the badge reads green, matching the existing DONE badge colour
    assert "#22c55e" in badge.group(1), (
        "the OFFICIAL badge must use the existing DONE green"
    )
    assert "#22c55e" in body, "the check mark itself must be green"


def test_enabled_stepper_wording_unchanged():
    """OFFICIAL is the only text added; the six step titles are untouched.

    Re-pointed 2026-10-10 onto the real enabled render (see _render_enabled).
    The six step_title[n] strings are live template content, so this is real
    wording coverage, not a guard on the deleted demo block.
    """
    expected_titles = [
        "Support holds for two weeks",
        "Wait for the next flag ledger",
        "The network records the change",
        "The amendment turns on",
        "It becomes permanent",
        "Old servers fall behind",
    ]
    for name, card in zip(_NAMES, _countdown_cards(_render_enabled())):
        steps = _step_blocks(card)
        assert len(steps) == 6, f"{name}: expected 6 steps, got {len(steps)}"

        for idx, (step, title) in enumerate(zip(steps, expected_titles), 1):
            summary = re.search(r"(?s)<summary.*?</summary>", step)
            assert summary, f"{name}: enabled step {idx} has no <summary>"
            assert title in summary.group(0), (
                f"{name}: enabled step {idx} lost its title {title!r}"
            )

        # strip the badge back out and the visible text must match the stepper
        # with no badge at all — i.e. OFFICIAL is the ONLY word added
        step5 = steps[4]
        without_badge = re.sub(
            r'<span class="timeline-official"[^>]*>.*?</span>', "",
            step5, flags=re.S,
        )
        assert "OFFICIAL" not in _visible_text(without_badge), (
            f"{name}: OFFICIAL must come only from the badge span"
        )
        assert _PERMANENT_TITLE in _visible_text(without_badge)
