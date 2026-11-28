"""The env drift guard must not be complete by assumption.

Two faults are fixed here, and they are worth keeping apart.

FAULT 1 - the guard pinned a dead variable to a wrong value.
xrpl_env_drift_guard shipped 2026-08-31 13:10 with FIVE vars pinned to one
address, hours after the Wave-0 re-point that had just broken the MPT
holders walk. One of the five was named XRPL_CLIO_NODE, and `mpt_holders`
is a Clio-only method that rippled 3.4.1 answers with unknownCmd. So the
guard PAGED IF A CLIO VARIABLE EVER POINTED AT CLIO, and reported clean for
88 days while the walk was dead. Audited 2026-11-27: XRPL_CLIO_NODE was read
by no production code at all -- only by this guard, which pinned it.
docs/SOVEREIGNTY_AUDIT.md GAP-3 had already recorded "no longer read by
code" in September.

FAULT 2 - the guard could not see what it was not already looking for.
The old loop iterated EXPECTED and never read the file's own key set, so an
XRPL_ variable absent from EXPECTED was invisible: a sixth var could be
added and the guard would still report "clean: all 5 XRPL_ vars" forever.
That is the same shape as a walker exiting 0 while doing nothing -- the one
visible number describes something other than the question being asked.

So XRPL_CLIO_NODE is RETIRED, not merely deleted. Retirement is an
assertion: if it reappears, either an old env file was restored (precisely
the 2026-08-31 incident class this guard exists for) or someone re-added a
Clio knob no code reads. Silently dropping it from EXPECTED would have left
the guard indifferent to its return.

These tests call evaluate() directly rather than re-deriving the findings,
so they cannot pass by agreeing with a copy of the logic.
"""

import re

import pytest

import xrpl_env_drift_guard as g


V = g.EXPECTED_VALUE
SOVEREIGN = ("XRPL_LOCAL_NODE", "XRPL_NODE", "XRPL_MPT_NODE", "XRPL_RPC")


def env_body(keys, overrides=None, style="export-double"):
    """Build an env-file body. Values exist only so the parser has something
    to read; no test asserts on a value."""
    overrides = overrides or {}
    out = []
    for k in keys:
        val = overrides.get(k, V)
        if style == "export-double":
            out.append(f'export {k}="{val}"')
        elif style == "bare":
            out.append(f"{k}={val}")
        elif style == "single":
            out.append(f"export {k}='{val}'")
    return "\n".join(out) + "\n"


# ── the retirement itself ────────────────────────────────────────────

def test_clio_node_is_not_guarded_any_more():
    assert "XRPL_CLIO_NODE" not in g.EXPECTED


def test_clio_node_is_retired_with_a_stated_reason():
    """Not a silent deletion: the guard still has an opinion about it."""
    assert "XRPL_CLIO_NODE" in g.RETIRED
    reason = g.RETIRED["XRPL_CLIO_NODE"]
    assert isinstance(reason, str) and len(reason) > 40
    assert "MPT_HOLDERS_NODES" in reason


def test_exactly_the_four_sovereign_vars_are_guarded():
    assert set(g.EXPECTED) == set(SOVEREIGN)
    assert all(v == V for v in g.EXPECTED.values())


def test_the_single_clio_setting_is_not_pinned_to_our_own_node():
    """MPT_HOLDERS_NODES must NOT equal our node -- our node cannot serve
    mpt_holders. Guarding it to EXPECTED_VALUE would recreate fault 1."""
    assert "MPT_HOLDERS_NODES" not in g.EXPECTED
    assert "MPT_HOLDERS_NODES" not in g.RETIRED


# ── the end state is clean ───────────────────────────────────────────

def test_post_retirement_env_is_clean():
    assert g.evaluate(env_body(SOVEREIGN)) == []


@pytest.mark.parametrize("style", ["export-double", "bare", "single"])
def test_clean_in_every_assignment_form(style):
    """The key scanner and the value parser must agree about what counts as
    an assignment, in all three forms the env file is allowed to use."""
    assert g.evaluate(env_body(SOVEREIGN, style=style)) == []


# ── fault 1: the retired variable coming back ────────────────────────

def test_clio_node_present_again_is_a_finding():
    msgs = g.evaluate(env_body(list(SOVEREIGN) + ["XRPL_CLIO_NODE"]))
    assert len(msgs) == 1
    assert msgs[0] == "XRPL_CLIO_NODE=<RETIRED-BUT-PRESENT>"


def test_the_pre_edit_env_would_now_be_a_finding():
    """Documents the transition: the file as it stood before 2026-11-27 is
    exactly one finding under the new guard, not silently acceptable."""
    msgs = g.evaluate(env_body(list(SOVEREIGN) + ["XRPL_CLIO_NODE"]))
    assert msgs == ["XRPL_CLIO_NODE=<RETIRED-BUT-PRESENT>"]


# ── fault 2: guard-the-guard ─────────────────────────────────────────

def test_an_unguarded_sixth_variable_is_a_finding():
    """THE regression this closes. Under the old guard this body reported
    clean, because nothing ever compared the key sets."""
    msgs = g.evaluate(env_body(list(SOVEREIGN) + ["XRPL_SHINY_NEW_NODE"]))
    assert msgs == ["XRPL_SHINY_NEW_NODE=<UNGUARDED>"]


def test_several_unguarded_variables_are_each_reported():
    msgs = g.evaluate(env_body(list(SOVEREIGN) + ["XRPL_AAA", "XRPL_BBB"]))
    assert msgs == ["XRPL_AAA=<UNGUARDED>", "XRPL_BBB=<UNGUARDED>"]


def test_key_sets_match_in_the_clean_case():
    """The property Charlie asked for, asserted directly: the guard's
    expected keys equal the XRPL_ variables actually present."""
    body = env_body(SOVEREIGN)
    assert g._env_xrpl_keys(body) == set(g.EXPECTED)


def test_env_key_scanner_ignores_comments_and_other_prefixes():
    body = (
        "# export XRPL_COMMENTED_OUT=http://nope\n"
        "DATABASE_URL=postgres://x\n"
        "MPT_HOLDERS_NODES=https://s2-clio.ripple.com:51234\n"
        + env_body(SOVEREIGN)
    )
    assert g._env_xrpl_keys(body) == set(SOVEREIGN)
    assert g.evaluate(body) == []


# ── the original axes still work ─────────────────────────────────────

def test_a_drifted_sovereign_var_is_still_caught():
    body = env_body(SOVEREIGN, overrides={"XRPL_LOCAL_NODE": "http://192.168.40.95:5005"})
    msgs = g.evaluate(body)
    assert len(msgs) == 1 and msgs[0].startswith("XRPL_LOCAL_NODE=")


def test_a_missing_sovereign_var_is_still_caught():
    msgs = g.evaluate(env_body([k for k in SOVEREIGN if k != "XRPL_MPT_NODE"]))
    assert msgs == ["XRPL_MPT_NODE=<MISSING>"]


def test_an_empty_env_file_is_four_findings_not_silence():
    """A truncated or unreadable-but-parseable env file must not read as
    clean. Four missing sovereign vars, no unguarded noise."""
    msgs = g.evaluate("")
    assert sorted(msgs) == sorted(f"{k}=<MISSING>" for k in SOVEREIGN)


# ── no config values leak through the new findings ───────────────────

def test_retired_and_unguarded_findings_name_keys_only():
    """These two axes have no expected value to compare against, so the
    guard has no reason to copy unknown config values into walker_health.
    Asserted on the message text, which is what lands in the DB."""
    secretish = "http://10.9.9.9:9999/super-secret-path"
    body = env_body(
        list(SOVEREIGN) + ["XRPL_CLIO_NODE", "XRPL_WEIRD_NODE"],
        overrides={"XRPL_CLIO_NODE": secretish, "XRPL_WEIRD_NODE": secretish},
    )
    msgs = g.evaluate(body)
    assert len(msgs) == 2
    joined = " ".join(msgs)
    assert secretish not in joined
    assert "10.9.9.9" not in joined
    assert not re.search(r"https?://", joined)
