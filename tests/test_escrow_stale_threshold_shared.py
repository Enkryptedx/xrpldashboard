"""One escrow staleness threshold, three consumers, no copies.

The page gate, the API redaction gate and the freshness canary all answer
the same question: is the ledger-wide escrow census too old to publish? If
they disagree, the page withholds a number the API is still serving, or the
canary reports clean while both surfaces hide the figure -- three answers to
one question and no way for a reader or an operator to tell which is right.

So the value lives in exactly one place, supply_block, and these tests fail
if anyone reintroduces a local copy.

26h = a 24h cadence plus ~2h grace. It was 3600 (60 min) until 2026-11-28,
sized for the hourly walker supply_escrow_walker used to be; under the daily
cadence that would mark the census stale for 23 of every 24 hours and the
API would redact almost permanently.

Each import happens in its own subprocess. escrow_census_freshness_canary
inserts the main checkout onto sys.path inside its functions, and importing
modules together in one process let that leak redirect a later import to a
different checkout -- which is exactly how a first verification of this
change reported a false mismatch.
"""

import subprocess
import sys
import os

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

EXPECTED_SECONDS = 26 * 3600


def _read(snippet: str) -> int:
    out = subprocess.run(
        [sys.executable, "-c", snippet],
        cwd=REPO, capture_output=True, text=True,
        env={**os.environ, "DATABASE_URL": ""},
    )
    for line in reversed(out.stdout.strip().splitlines()):
        line = line.strip()
        if line.isdigit():
            return int(line)
    raise AssertionError(f"no value read: {out.stdout[-400:]} {out.stderr[-400:]}")


def test_supply_block_is_the_single_source():
    assert _read(
        "import supply_block as s;"
        "print(s.ESCROW_CENSUS_STALE_AFTER_SECONDS)"
    ) == EXPECTED_SECONDS


def test_the_canary_reads_the_shared_value():
    assert _read(
        "import escrow_census_freshness_canary as c;"
        "print(c.MAX_AGE_SECONDS)"
    ) == EXPECTED_SECONDS


def test_the_page_and_api_gate_reads_the_shared_value():
    """app._XRP_ESCROW_STALE_AFTER drives BOTH the page gate (the homepage
    constellation printing 'paused') and the API redaction."""
    assert _read(
        "import app;print(app._XRP_ESCROW_STALE_AFTER)"
    ) == EXPECTED_SECONDS


def test_all_three_consumers_agree():
    """The property, asserted directly rather than inferred from three
    separate equality checks against a constant."""
    a = _read("import supply_block as s;print(s.ESCROW_CENSUS_STALE_AFTER_SECONDS)")
    b = _read("import escrow_census_freshness_canary as c;print(c.MAX_AGE_SECONDS)")
    c = _read("import app;print(app._XRP_ESCROW_STALE_AFTER)")
    assert a == b == c, f"threshold drifted: supply_block={a} canary={b} app={c}"


def test_no_module_redefines_the_threshold_locally():
    """A local literal would silently win over the shared import."""
    import re
    for name in ("app.py", "escrow_census_freshness_canary.py"):
        src = open(os.path.join(REPO, name)).read()
        # An assignment to the gate name with a numeric literal is a copy.
        assert not re.search(
            r"^_XRP_ESCROW_STALE_AFTER\s*=\s*\d", src, re.M), name
        assert not re.search(
            r"^MAX_AGE_SECONDS\s*=\s*\d", src, re.M), name


def test_the_threshold_is_longer_than_the_walker_cadence():
    """It must exceed the cadence or the census is stale before its next
    scheduled run, which would make the gate permanently closed."""
    cadence = _read(
        "import supply_escrow_walker as w;print(w.CADENCE_SECONDS)")
    assert EXPECTED_SECONDS > cadence
