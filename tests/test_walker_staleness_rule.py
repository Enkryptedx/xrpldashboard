"""A job that stops succeeding must be noticed within two of its own cycles.

The meta-watch JOBS list is hand-written, so it cannot see the problem it
has: a walker absent from the list is unwatched forever and nothing notices
the absence. The 2026-11-28 silence audit measured it -- seven walkers in
walker_health declare NO cadence, and six had not succeeded in 43-65 days,
none of them in JOBS. Because they declared no cadence, no staleness rule
could fire on them however long they stayed quiet. Same shape as the MPT
holders walk sitting dead for 88 days while the trusted signal measured
something else.

So the rule under test reads walker_health directly and judges each row by
its OWN declaration. Two findings: UNDECLARED-CADENCE (the omission is the
defect, because it makes the job permanently unwatchable) and STALE beyond
STALENESS_MULTIPLE x cadence.

Charlie's rulings of 2026-11-28, both pinned here:
  * demand-driven counters are exempt from staleness but MUST still declare
    themselves as demand-driven -- membership of DEMAND_DRIVEN is that
    declaration.
  * the five silent jobs must NOT be pre-exempted; they show as findings
    until each one is decided. test_the_five_silent_jobs_are_not_exempt
    is the guard on that instruction.

Pure: evaluate_walker_declarations() takes rows as an argument, so none of
this needs a database.
"""

import pytest

import dockvault_mirror_freshness_canary as c


HOUR = 3600


def row(name, cadence=HOUR, age=0.0, **kw):
    d = {"walker_name": name, "cadence_seconds": cadence,
         "last_success_age_seconds": age}
    d.update(kw)
    return d


def base():
    """Rows for every really-exempt walker.

    Guard-the-guard 2 reports an exemption naming a walker that is absent
    from walker_health, which is correct in production but means any test
    passing a small subset of rows would otherwise trip it. Including the
    real exempt names keeps each test measuring the thing it names.
    """
    return [
        row("mcp_connect_redirect", cadence=2592000, age=100.0),
        row("mcp_session_start", cadence=86400, age=100.0),
        row("supply_escrow_walker", cadence=None, age=100.0),
    ]


def evaluate(rows):
    """Evaluate `rows` on top of base(), overriding by walker_name.

    A test that supplies its own mcp_connect_redirect row must REPLACE the
    base one, not sit alongside it -- two rows for one name would produce
    two results and make assertions ambiguous. Malformed entries pass
    through untouched so the skip-don't-crash case stays testable.
    """
    merged = {r["walker_name"]: r for r in base()}
    passthrough = []
    for r in rows:
        if isinstance(r, dict) and r.get("walker_name"):
            merged[r["walker_name"]] = r
        else:
            passthrough.append(r)
    return c.evaluate_walker_declarations(list(merged.values()) + passthrough)


def findings(rows):
    return [r for r in evaluate(rows) if not r["ok"]]


def reasons_for(rows, label):
    return [r["reason"] for r in evaluate(rows)
            if r["label"] == f"declare:{label}"]


# ── the two findings ─────────────────────────────────────────────────

def test_a_fresh_job_is_not_a_finding():
    assert findings([row("w", cadence=HOUR, age=1800)]) == []


def test_undeclared_cadence_is_itself_a_finding():
    """Not merely a missing input -- a job that declares no cadence can
    never be judged stale, so the omission IS the defect."""
    assert reasons_for([row("w", cadence=None, age=10)], "w") == \
        ["UNDECLARED-CADENCE"]


@pytest.mark.parametrize("cadence", [0, None, ""])
def test_falsy_cadences_all_count_as_undeclared(cadence):
    assert reasons_for([row("w", cadence=cadence)], "w") == \
        ["UNDECLARED-CADENCE"]


def test_undeclared_is_a_finding_even_when_the_job_is_healthy():
    """nft_activity_backfill's real shape: succeeded minutes ago, but
    declares nothing, so it is still unwatchable."""
    assert reasons_for([row("w", cadence=None, age=60)], "w") == \
        ["UNDECLARED-CADENCE"]


def test_stale_beyond_two_cycles_is_a_finding():
    r = reasons_for([row("w", cadence=HOUR, age=2.5 * HOUR)], "w")
    assert r and r[0].startswith("STALE ")
    assert "2.0h" in r[0]  # the limit is shown, not just the age


def test_exactly_two_cycles_is_not_yet_stale():
    """Boundary: the rule fires BEYOND two cycles, so a job landing exactly
    on 2x has not missed a second cycle yet."""
    assert findings([row("w", cadence=HOUR, age=2 * HOUR)]) == []


def test_a_hair_over_two_cycles_is_stale():
    assert len(findings([row("w", cadence=HOUR, age=2 * HOUR + 1)])) == 1


def test_never_succeeded_is_the_loudest_case():
    """A declared job with no success ever must not read as fresh."""
    assert reasons_for([row("w", cadence=HOUR, age=None)], "w") == \
        ["STALE never_succeeded"]


def test_two_cycles_is_derived_from_each_jobs_own_cadence():
    """A daily job and a 5-min job get different limits from one rule."""
    daily, fast = row("d", cadence=86400, age=90000), row("f", cadence=300, age=900)
    got = {r["label"]: r["ok"] for r in c.evaluate_walker_declarations([daily, fast])}
    assert got["declare:d"] is True    # 25h < 48h limit
    assert got["declare:f"] is False   # 15min > 10min limit


def test_the_multiple_is_two():
    assert c.STALENESS_MULTIPLE == 2


# ── Charlie ruling: demand-driven ────────────────────────────────────

def test_demand_driven_counters_are_exempt_from_staleness():
    """mcp_connect_redirect really sits at ~2.05x its 30-day cadence; it is
    a request-time stamp, not a schedule, so 2x-cadence asks the wrong
    question and would be a standing false finding."""
    rows = [row("mcp_connect_redirect", cadence=2592000, age=5317000)]
    assert findings(rows) == []
    assert reasons_for(rows, "mcp_connect_redirect") == ["exempt:demand-driven"]


def test_both_mcp_counters_declare_themselves_demand_driven():
    """Membership IS the declaration -- the other half of the ruling."""
    assert set(c.DEMAND_DRIVEN) == {"mcp_connect_redirect", "mcp_session_start"}


def test_a_demand_driven_counter_is_still_exempt_with_no_cadence():
    """Being demand-driven answers the cadence question outright."""
    rows = [row("mcp_session_start", cadence=None, age=None)]
    assert findings(rows) == []


# ── Charlie ruling: do NOT pre-exempt the five silent jobs ───────────

FIVE_SILENT = [
    "escrow_supply_walker",
    "rwa_nav_transparency_walker",
    "supply_block_walker",
    "nft_activity_reconcile-type-rollup",
    "escrow_seed_walk",
]


@pytest.mark.parametrize("name", FIVE_SILENT)
def test_the_five_silent_jobs_are_not_exempt(name):
    """Charlie 2026-11-28: they must SHOW as findings until each is decided.

    Guards against a future 'tidy up the noise' commit quietly exempting
    them, which would recreate the exact invisibility being fixed.
    """
    assert name not in c.DEMAND_DRIVEN
    assert name not in c.PAUSED
    assert name not in c.RETIRED
    assert reasons_for([row(name, cadence=None, age=1_500_000)], name) == \
        ["UNDECLARED-CADENCE"]


def test_retired_is_deliberately_empty():
    """openclaw_cli_backend_drift looks retired (.retired-2026-09-25 files)
    but pre-exempting it would be deciding on Charlie's behalf."""
    assert c.RETIRED == {}


def test_paused_holds_only_the_job_charlie_confirmed():
    assert set(c.PAUSED) == {"supply_escrow_walker"}


def test_paused_job_is_exempt():
    rows = [row("supply_escrow_walker", cadence=None, age=4_000_000)]
    assert findings(rows) == []
    assert reasons_for(rows, "supply_escrow_walker") == ["exempt:paused"]


# ── guard-the-guard: the exemption maps cannot rot ───────────────────

def test_every_exemption_states_a_reason():
    for table in (c.DEMAND_DRIVEN, c.PAUSED, c.RETIRED):
        for name, reason in table.items():
            assert reason and reason.strip(), f"{name} exempt with no reason"
            assert len(reason) > 30, f"{name} reason is too thin to review"


def test_a_name_in_two_maps_is_a_finding(monkeypatch):
    monkeypatch.setitem(c.PAUSED, "mcp_session_start", "contradictory")
    f = findings([row("mcp_session_start")])
    assert any("exempt_in_multiple_maps" in r["reason"] for r in f)


def test_an_exemption_for_an_unknown_walker_is_a_finding(monkeypatch):
    """A RENAMED job would otherwise keep its old name exempt forever while
    the new name went unnoticed -- the escrow_supply/supply_escrow hazard."""
    monkeypatch.setitem(c.PAUSED, "ghost_walker_that_does_not_exist", "x" * 40)
    f = findings([row("w")])
    assert any(r["label"] == "declare:ghost_walker_that_does_not_exist"
               and r["reason"] == "exemption_for_unknown_walker" for r in f)


def test_a_blank_reason_is_a_finding(monkeypatch):
    """An exemption with no stated reason is a silence with extra steps."""
    monkeypatch.setitem(c.RETIRED, "some_walker", "   ")
    f = findings([row("some_walker")])
    assert any("exemption_without_reason" in r["reason"] for r in f)


# ── a check that cannot see must not report health ──────────────────

def test_pg_unavailable_is_a_finding_not_silence(monkeypatch):
    import db
    monkeypatch.setattr(db, "pg_available", lambda: False, raising=False)
    res = c.check_walker_declarations()
    assert len(res) == 1 and res[0]["ok"] is False
    assert res[0]["reason"] == "pg_unavailable"


def test_a_read_failure_is_a_finding_not_silence(monkeypatch):
    import db
    monkeypatch.setattr(db, "pg_available", lambda: True, raising=False)

    def boom():
        raise RuntimeError("neon gone")
    monkeypatch.setattr(db, "read_walker_health_all", boom, raising=False)
    res = c.check_walker_declarations()
    assert len(res) == 1 and res[0]["ok"] is False
    assert res[0]["reason"].startswith("read_failed:")


def test_an_empty_walker_health_yields_no_false_health():
    """No rows means nothing was verified; it must not add ok_count.

    It must ALSO not emit bogus exemption_for_unknown_walker findings: an
    empty or unavailable walker_health is a single different fault, already
    reported as pg_unavailable / read_failed, and dressing it up as one
    finding per exemption would bury the real cause.
    """
    assert c.evaluate_walker_declarations([]) == []


# ── result shape must match check_one so main() needs no special case ──

def test_results_carry_the_same_keys_as_check_one():
    res = c.evaluate_walker_declarations([row("w"), row("x", cadence=None)])
    for r in res:
        for key in ("label", "ok", "reason", "path", "age_seconds",
                    "ceiling_seconds"):
            assert key in r, f"missing {key}; main() would KeyError"
        assert isinstance(r["ceiling_seconds"], int)


def test_labels_are_namespaced_so_they_cannot_collide_with_jobs():
    """JOBS labels and walker names share one results list, and several
    names appear in both -- an un-namespaced label would make the printed
    output ambiguous about which check failed."""
    res = evaluate([row("dockvault_mirror")])
    labels = {r["label"] for r in res}
    assert "declare:dockvault_mirror" in labels
    assert not (labels & {j[0] for j in c.JOBS})


def test_malformed_rows_are_skipped_not_crashed():
    res = evaluate([None, "junk", {}, {"walker_name": ""}, row("good")])
    assert "declare:good" in {r["label"] for r in res}
    assert all(r["label"].startswith("declare:") for r in res)
    # Nothing was invented for the four malformed entries.
    assert len(res) == len(base()) + 1
