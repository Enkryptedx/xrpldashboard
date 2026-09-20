"""Weekly-post typed weekday+date pairs must match the calendar.

Filed 2026-09-19 after the sub-agent typed-date sweep caught
`docs/thisweek/2026-09-20.md` saying "Wednesday 2026-09-17" — Sept 17
was a Thursday. Weekly-post markdown is authored copy so the general
`_regulation_freshness` computed-weekday pattern (weekday derived from
a constant in code, never typed) doesn't apply. But the visitor-facing
result still has to be correct, and the class of drift — human types
a weekday next to a date and the calendar moved under them — is real.

This test scans every `docs/thisweek/*.md` and `*.summary.md`, extracts
every typed `<weekday> <date>` pair (long-form date like `2026-09-17`
or short-form like `Sept 17` where the year is inferable from the
filename), computes the actual weekday from the calendar, and asserts
match.

Charlie ruling 2026-09-19: the render/preview test fails on any typed
weekday next to a date that doesn't match the calendar.
"""
from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

import pytest


THISWEEK_DIR = Path(__file__).resolve().parent.parent / "docs" / "thisweek"

WEEKDAY_LONG = {
    "Monday": 0, "Tuesday": 1, "Wednesday": 2, "Thursday": 3,
    "Friday": 4, "Saturday": 5, "Sunday": 6,
}
WEEKDAY_SHORT = {
    "Mon": 0, "Tue": 1, "Tues": 1, "Wed": 2, "Weds": 2, "Thu": 3,
    "Thur": 3, "Thurs": 3, "Fri": 4, "Sat": 5, "Sun": 6,
}
WEEKDAY_ALL = {**WEEKDAY_LONG, **WEEKDAY_SHORT}
WEEKDAY_NAMES = "|".join(sorted(WEEKDAY_ALL, key=len, reverse=True))

MONTHS = {
    "Jan": 1, "January": 1, "Feb": 2, "February": 2, "Mar": 3, "March": 3,
    "Apr": 4, "April": 4, "May": 5, "Jun": 6, "June": 6, "Jul": 7, "July": 7,
    "Aug": 8, "August": 8, "Sep": 9, "Sept": 9, "September": 9,
    "Oct": 10, "October": 10, "Nov": 11, "November": 11, "Dec": 12, "December": 12,
}
MONTH_NAMES = "|".join(sorted(MONTHS, key=len, reverse=True))

# Pattern A — long-form: `Weekday[,] YYYY-MM-DD`
PAT_LONG = re.compile(
    rf"\b({WEEKDAY_NAMES})[,]?\s+(\d{{4}})-(\d{{2}})-(\d{{2}})\b"
)
# Pattern B — short-form: `Weekday[,] (Month) N` (year inferred from filename)
PAT_SHORT = re.compile(
    rf"\b({WEEKDAY_NAMES})[,]?\s+({MONTH_NAMES})\s+(\d{{1,2}})\b"
)


def _iter_docs():
    """Yield (path, text) for every .md file we author under docs/thisweek/."""
    if not THISWEEK_DIR.is_dir():
        return
    for p in sorted(THISWEEK_DIR.glob("*.md")):
        yield p, p.read_text(encoding="utf-8", errors="replace")


def _year_from_filename(p: Path) -> int | None:
    """Weekly-post filenames are `YYYY-MM-DD.md` or `YYYY-MM-DD.summary.md`.
    Extract the year to disambiguate short-form 'Sept 17' style pairs."""
    m = re.match(r"(\d{4})-\d{2}-\d{2}", p.name)
    return int(m.group(1)) if m else None


def _collect_pairs(path: Path, text: str):
    """Return list of (line_no, weekday_typed, actual_date, matched_snippet).
    Only pairs where we can pin an actual date are returned."""
    pairs = []
    file_year = _year_from_filename(path)
    for line_no, line in enumerate(text.splitlines(), start=1):
        for m in PAT_LONG.finditer(line):
            weekday_typed, y, mo, d = m.group(1), int(m.group(2)), int(m.group(3)), int(m.group(4))
            try:
                actual = dt.date(y, mo, d)
            except ValueError:
                continue
            pairs.append((line_no, weekday_typed, actual, m.group(0)))
        for m in PAT_SHORT.finditer(line):
            weekday_typed, month_name, day = m.group(1), m.group(2), int(m.group(3))
            if file_year is None:
                continue
            try:
                actual = dt.date(file_year, MONTHS[month_name], day)
            except ValueError:
                continue
            pairs.append((line_no, weekday_typed, actual, m.group(0)))
    return pairs


def test_every_typed_weekday_matches_calendar():
    """One assertion across all thisweek docs so a bad pair fails loud
    with every offending line shown at once — humans fix them in one
    edit rather than N re-runs."""
    if not THISWEEK_DIR.is_dir():
        pytest.skip(f"{THISWEEK_DIR} does not exist in this checkout")

    errors: list[str] = []
    total_pairs = 0
    for path, text in _iter_docs():
        for line_no, typed, actual, snippet in _collect_pairs(path, text):
            total_pairs += 1
            expected = actual.strftime("%A")
            if WEEKDAY_ALL[typed] != actual.weekday():
                rel = path.relative_to(THISWEEK_DIR.parent.parent)
                errors.append(
                    f"  {rel}:{line_no} '{snippet}' — {actual.isoformat()} "
                    f"is a {expected}, not {typed}"
                )

    assert not errors, (
        f"typed weekday+date pairs disagree with the calendar in "
        f"docs/thisweek/ (Charlie ruling 2026-09-19 — the calendar wins):\n"
        + "\n".join(errors)
    )


def test_scanner_actually_finds_pairs():
    """Guard against silent test-passes because the regex broke. If
    docs/thisweek/ has any .md files at all, we expect at least one
    typed weekday+date pair to exist across them."""
    if not THISWEEK_DIR.is_dir() or not list(THISWEEK_DIR.glob("*.md")):
        pytest.skip("no thisweek docs present")

    found_any = False
    for path, text in _iter_docs():
        if _collect_pairs(path, text):
            found_any = True
            break
    assert found_any, (
        "regex found ZERO typed weekday+date pairs in docs/thisweek/*.md — "
        "the scanner is silently passing everything. Regex broke."
    )
