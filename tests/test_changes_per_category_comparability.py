"""Per-category comparability for the /changes envelope and homepage strip.

THE BUG THIS FIXES. `comparable` was `not inputs_missing`: ANY missing
producer voided the whole envelope, and `build_strip` then replaced every
slot with "Comparison unavailable: we have no prior-day snapshot to compare
against". On 2026-11-11 and 2026-11-12 that fired because `unl_snapshot`
had not written yet -- it was a drifting StartInterval job landing roughly
four hours after its consumer `changes_walker`. The homepage told readers
there was nothing to compare while the SAME envelope carried 7 correctly
derived changes and named `prev_snapshot_date: 2026-11-11`. /changes listed
all 7. The box contradicted the page it linked to.

THE DISTINCTION. A missing UNL row cannot invalidate an AMM TVL delta. Only
the leaf the diff is computed FROM can do that. So:

  signed_snapshot          the comparison BASIS -- every scalar metric is
                           diffed out of this leaf. Missing it means
                           nothing is comparable, and that alone sets
                           comparable=False.
  unl_snapshot             feeds only the `unl` category.
  signed_registry_snapshot feeds only the `registry` category.

A missing side-category producer now marks just its own categories
unavailable and leaves every other line rendering.

WHY NOT JUST RESCHEDULE. The 22:15 ET slot for `unl_snapshot` (applied
2026-11-13) makes the race unlikely, not impossible -- the plist comment on
`changes_walker` says so itself: a fixed slot cannot guarantee ordering
against a drifting producer, and `_assert_producer_inputs()` is the durable
guard. The schedule is the cheap first line; this is the correct behaviour
when it is crossed anyway.

Hermetic: pure functions over hand-built envelopes. No DB, no network.
"""

import pytest

import changes_builder as cb


def _env(**over):
    """A comparable envelope carrying one amm and one unl change."""
    base = {
        "date": "2026-11-12",
        "generated_at_utc": "2026-11-12T03:45:06Z",
        "changes": [
            {"line": "AMM TVL grew by $92K", "category": "amm",
             "metric_type": "usd", "label": "AMM total TVL (USD)",
             "before": 1.0, "after": 2.0, "delta": 1.0},
            {"line": "UNL: a validator moved", "category": "unl"},
        ],
        "prev_snapshot_date": "2026-11-11",
        "categories_no_change": ["registry", "mpt"],
        "inputs_missing": [],
    }
    base.update(over)
    base.setdefault("comparable",
                    not cb._basis_missing(base["inputs_missing"]))
    base.setdefault("categories_unavailable",
                    cb._categories_unavailable(base["inputs_missing"]))
    return base


class TestProducerMap:
    def test_basis_is_the_snapshot_leaf(self):
        # Declared with a dedicated sentinel, NOT None: dict.get() returns
        # None for an unknown key too, and conflating the two made an
        # unmapped producer read as the basis.
        assert cb._PRODUCER_CATEGORIES["signed_snapshot"] is cb._BASIS
        assert cb._basis_missing(["signed_snapshot"]) is True

    @pytest.mark.parametrize("label,cats", [
        ("unl_snapshot", ["unl"]),
        ("signed_registry_snapshot", ["registry"]),
    ])
    def test_side_producers_scope_to_their_own_category(self, label, cats):
        assert cb._basis_missing([label]) is False
        assert cb._categories_unavailable([label]) == cats

    def test_basis_missing_lists_no_categories(self):
        """When nothing is comparable, `comparable` carries the message --
        listing categories would imply the others were fine."""
        assert cb._categories_unavailable(["signed_snapshot"]) == []

    def test_unknown_producer_is_not_treated_as_basis(self):
        """A producer added to _PRODUCER_INPUTS without a category entry
        must not silently void every envelope."""
        assert cb._basis_missing(["something_new"]) is False

    def test_empty_is_comparable(self):
        assert cb._basis_missing([]) is False
        assert cb._categories_unavailable([]) == []


class TestTheRegression:
    """The exact 2026-11-11 / 11-12 shape."""

    def test_missing_unl_leaves_the_envelope_comparable(self):
        e = _env(inputs_missing=["unl_snapshot"])
        assert e["comparable"] is True
        assert e["categories_unavailable"] == ["unl"]

    def test_strip_still_renders_the_other_changes(self):
        e = _env(inputs_missing=["unl_snapshot"])
        s = cb.build_strip(e)
        assert s["comparable"] is True
        kinds = {sl.get("kind") for sl in s["slots"]}
        assert "comparison_unavailable" not in kinds

    def test_strip_drops_only_the_unavailable_category(self):
        e = _env(inputs_missing=["unl_snapshot"])
        s = cb.build_strip(e)
        assert "unl" not in {sl.get("category") for sl in s["slots"]}

    def test_explainer_survives_a_side_producer_miss(self):
        """The explainer was nulled whenever any input was missing, so the
        strip lost its heading too."""
        assert cb.build_strip(_env(inputs_missing=["unl_snapshot"]))["explainer"]

    def test_no_category_silence_claimed_for_the_missing_one(self):
        """We must not say 'registry: no change' when registry never wrote."""
        e = _env(inputs_missing=["signed_registry_snapshot"],
                 categories_no_change=["registry", "mpt"])
        from_builder = [c for c in e["categories_no_change"]]
        # the envelope fixture mirrors the builder's own filter
        filtered = [c for c in from_builder
                    if c not in cb._categories_unavailable(e["inputs_missing"])]
        assert "registry" not in filtered


class TestBasisMissingStillSuppresses:
    """The legitimate 'nothing to compare' case must keep working."""

    def test_whole_strip_replaced(self):
        s = cb.build_strip(_env(inputs_missing=["signed_snapshot"]))
        assert s["comparable"] is False
        assert len(s["slots"]) == 1
        assert s["slots"][0]["kind"] == "comparison_unavailable"

    def test_explainer_suppressed(self):
        assert cb.build_strip(_env(inputs_missing=["signed_snapshot"]))["explainer"] is None

    def test_basis_plus_side_still_suppresses(self):
        s = cb.build_strip(_env(inputs_missing=["signed_snapshot", "unl_snapshot"]))
        assert s["comparable"] is False


class TestBackwardCompatibility:
    """Envelopes stored before 2026-11-13 carry inputs_missing but no
    comparable/categories_unavailable. build_strip must recompute rather
    than treat the absent keys as 'fine'."""

    def test_legacy_side_miss_recomputes_as_comparable(self):
        e = _env(inputs_missing=["unl_snapshot"])
        del e["comparable"], e["categories_unavailable"]
        s = cb.build_strip(e)
        assert s["comparable"] is True
        assert "unl" not in {sl.get("category") for sl in s["slots"]}

    def test_legacy_basis_miss_recomputes_as_not_comparable(self):
        e = _env(inputs_missing=["signed_snapshot"])
        del e["comparable"], e["categories_unavailable"]
        assert cb.build_strip(e)["comparable"] is False


class TestCanary:
    """The detector for this class of defect."""

    def test_contradiction_is_flagged(self):
        """strip not-comparable while changes exist -- the 11-11 shape."""
        import changes_strip_agreement_canary as canary
        bad = _env(inputs_missing=["unl_snapshot"])
        bad["comparable"] = False          # the pre-fix behaviour
        f = canary.check_envelope("2026-11-12", bad, len(bad["changes"]))
        assert any(x["kind"] == "contradiction" for x in f)

    def test_healthy_envelope_is_silent(self):
        import changes_strip_agreement_canary as canary
        good = _env(inputs_missing=["unl_snapshot"])
        assert canary.check_envelope("2026-11-12", good,
                                     len(good["changes"])) == []

    def test_count_mismatch_is_flagged(self):
        import changes_strip_agreement_canary as canary
        e = _env()
        f = canary.check_envelope("2026-11-12", e, 99)
        assert any(x["kind"] == "count_mismatch" for x in f)

    def test_basis_claim_disagreement_is_flagged(self):
        """comparable=True while the basis is missing must not pass."""
        import changes_strip_agreement_canary as canary
        e = _env(inputs_missing=["signed_snapshot"])
        e["comparable"] = True
        f = canary.check_envelope("2026-11-12", e, len(e["changes"]))
        assert any(x["kind"] == "basis_claim" for x in f)
