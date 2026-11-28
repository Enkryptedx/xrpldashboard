"""The ledger-wide census line must guard every field it formats.

A presence-only guard over a PROXY field is a render-killer in this
codebase: `escrowed_xrp` being set says nothing about whether
`escrow_object_count`, `escrow_account_count` or the stamp are populated --
they are independent columns on the singleton, and a partial read (empty
table, no DATABASE_URL, a half-written row) can leave any of them None.
`"{:,}".format(None)` raises TypeError, which inside Jinja is a 500 on the
homepage.

So the condition is asserted against the REAL template source rather than a
copy: the block is located in templates/index.html and rendered, so if
someone later relaxes the guard this test follows the file and fails.

Checked with `is not none` rather than truthiness on purpose -- a genuine
ZERO count is a real census result and must still render.

No DB, no network.
"""

import os
import re

import pytest
from jinja2 import Environment

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TPL = os.path.join(HERE, "templates", "index.html")


def _census_block() -> str:
    """The real {% if %}…{% endif %} block that renders the census line."""
    src = open(TPL, encoding="utf-8").read()
    i = src.index("_('Ledger-wide')")
    start = src.rindex("{% if", 0, i)
    end = src.index("{% endif %}", i) + len("{% endif %}")
    return src[start:end]


def _render(dist: dict) -> str:
    env = Environment()
    env.globals["_"] = lambda s: s
    return env.from_string(_census_block()).render(xrp_distribution=dist).strip()


def base(**kw) -> dict:
    d = {
        "escrowed_xrp": 31245182712.1952,
        "escrow_object_count": 15869,
        "escrow_account_count": 1925,
        "escrow_census_as_of": "as of ledger 108,632,950 · Nov 28, 1:16 PM ET (18:16 UTC)",
        "escrow_stale": False,
    }
    d.update(kw)
    return d


def test_the_complete_row_renders_the_line():
    out = _render(base())
    assert "Ledger-wide: 31.25B XRP" in out
    assert "15,869 escrow objects" in out
    assert "1,925 holder accounts" in out
    assert "Nov 28, 1:16 PM ET" in out


# ── the partial reads: each must render NOTHING, not raise ───────────

@pytest.mark.parametrize("missing", [
    "escrow_object_count",
    "escrow_account_count",
    "escrow_census_as_of",
])
def test_a_none_field_withholds_the_line_instead_of_500ing(missing):
    """escrowed_xrp present, a formatted field None -- the exact shape that
    would have raised TypeError under a presence-only guard."""
    assert _render(base(**{missing: None})) == ""


def test_counts_present_but_no_amount_withholds_the_line():
    """The reverse direction: the counts are there, the headline number is
    not. Rendering 'Ledger-wide: ' with no figure asserts nothing."""
    assert _render(base(escrowed_xrp=None)) == ""


def test_both_counts_none_withholds_the_line():
    assert _render(base(escrow_object_count=None,
                        escrow_account_count=None)) == ""


def test_an_entirely_empty_distribution_renders_nothing():
    assert _render({}) == ""


def test_a_stale_census_withholds_the_line_even_when_complete():
    """Staleness is a separate axis from completeness."""
    assert _render(base(escrow_stale=True)) == ""


# ── a real zero is a result, not a missing value ─────────────────────

def test_a_zero_count_still_renders():
    """`is not none`, not truthiness: a census that genuinely found zero
    escrow objects is a finding and must publish."""
    out = _render(base(escrow_object_count=0, escrow_account_count=0))
    assert "0 escrow objects" in out
    assert "0 holder accounts" in out


# ── the guard itself ────────────────────────────────────────────────

def test_the_guard_names_every_formatted_field():
    """Guards against the proxy-guard regression returning: any field the
    block FORMATS must also appear in its condition."""
    block = _census_block()
    cond = block[:block.index("%}") + 2]
    for field in ("escrowed_xrp", "escrow_object_count",
                  "escrow_account_count", "escrow_census_as_of"):
        assert field in cond, f"{field} is formatted but not guarded"


def test_the_guard_uses_is_not_none_for_the_counts():
    cond = _census_block()
    cond = cond[:cond.index("%}") + 2]
    assert re.search(r"escrow_object_count\s+is\s+not\s+none", cond)
    assert re.search(r"escrow_account_count\s+is\s+not\s+none", cond)
