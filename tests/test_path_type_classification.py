"""Tests for xrpl_stream.classify_payment_path and the path_type
migration surface.

Locks the taxonomy shipped 2026-08-02 for the value-weighted CLOB/AMM
floor analysis. Fixtures represent the four Payment shapes we
classify: AMM-only, CLOB-only, MIXED, DIRECT. The AMM_LP tag applies
to AMMDeposit/AMMWithdraw (handled by token_event_handler, not the
classifier). UNKNOWN is the honest fallback for malformed / missing
meta — never guessed as DIRECT.

Denominator statement enforced here: the classifier considers only
LedgerEntryType == 'AMM' / 'Offer' / 'AccountRoot' (with AMM-set
fallback). Pure DEX OfferCreate fills that never become a Payment are
out of token_volume scope — that gap is documented in the handler
docstring and is a deliberate follow-up ship.
"""

import pytest

import xrpl_stream
from xrpl_stream import classify_payment_path


# ─────────────────────────────────────────────────────────────────────
# Fixtures — realistic meta.AffectedNodes shapes
# ─────────────────────────────────────────────────────────────────────


def _node(change_type, entry_type, fields=None):
    """Build a single AffectedNode dict of the requested shape."""
    return {
        change_type: {
            "LedgerEntryType": entry_type,
            "FinalFields": fields or {},
        }
    }


AMM_ONLY_META = {
    "AffectedNodes": [
        _node("ModifiedNode", "AMM", {"Asset": {"currency": "XRP"}}),
        _node("ModifiedNode", "AccountRoot", {"Account": "rSender"}),
        _node("ModifiedNode", "AccountRoot", {"Account": "rReceiver"}),
    ]
}

CLOB_ONLY_META = {
    "AffectedNodes": [
        _node("ModifiedNode", "Offer", {"Account": "rMaker"}),
        _node("DeletedNode", "Offer", {"Account": "rMaker2"}),
        _node("ModifiedNode", "AccountRoot", {"Account": "rSender"}),
    ]
}

MIXED_META = {
    "AffectedNodes": [
        _node("ModifiedNode", "AMM", {"Asset": {"currency": "USD"}}),
        _node("DeletedNode", "Offer", {"Account": "rMaker"}),
        _node("ModifiedNode", "AccountRoot", {"Account": "rSender"}),
    ]
}

DIRECT_META = {
    "AffectedNodes": [
        _node("ModifiedNode", "AccountRoot", {"Account": "rSender"}),
        _node("ModifiedNode", "AccountRoot", {"Account": "rReceiver"}),
    ]
}

# Fallback path: AMM is detected via an AccountRoot whose Account is a
# known AMM account, not via a dedicated LedgerEntryType == "AMM" node.
AMM_ACCOUNT_FALLBACK_META = {
    "AffectedNodes": [
        _node("ModifiedNode", "AccountRoot", {"Account": "rAMMPool123"}),
        _node("ModifiedNode", "AccountRoot", {"Account": "rSender"}),
    ]
}


# ─────────────────────────────────────────────────────────────────────
# classify_payment_path — the taxonomy
# ─────────────────────────────────────────────────────────────────────


class TestClassifyPaymentPath:
    def test_amm_only(self):
        assert classify_payment_path(AMM_ONLY_META, set()) == "AMM"

    def test_clob_only(self):
        assert classify_payment_path(CLOB_ONLY_META, set()) == "CLOB"

    def test_mixed(self):
        assert classify_payment_path(MIXED_META, set()) == "MIXED"

    def test_direct_no_amm_no_clob(self):
        assert classify_payment_path(DIRECT_META, set()) == "DIRECT"

    def test_amm_via_account_set_fallback(self):
        # AMM presence detected via AccountRoot match against the loaded
        # AMM account set, even when no LedgerEntryType == "AMM" node
        # appears in meta.
        amm_set = {"rAMMPool123"}
        assert classify_payment_path(AMM_ACCOUNT_FALLBACK_META, amm_set) == "AMM"

    def test_amm_via_account_set_ignored_when_set_empty(self):
        # No fallback when we have no AMM account set loaded — the row
        # falls to DIRECT rather than being silently mislabeled AMM.
        assert (
            classify_payment_path(AMM_ACCOUNT_FALLBACK_META, set()) == "DIRECT"
        )

    def test_created_and_deleted_nodes_count(self):
        # Both CreatedNode and DeletedNode entries participate in
        # classification (not just ModifiedNode).
        meta = {
            "AffectedNodes": [
                _node("CreatedNode", "AMM", {"Asset": {"currency": "USD"}}),
                _node("DeletedNode", "Offer", {"Account": "rMaker"}),
            ]
        }
        assert classify_payment_path(meta, set()) == "MIXED"

    # ─── UNKNOWN cases: honest fallback, never guessed as DIRECT ──────

    def test_unknown_when_meta_is_none(self):
        assert classify_payment_path(None, set()) == "UNKNOWN"

    def test_unknown_when_meta_is_not_dict(self):
        assert classify_payment_path("not a dict", set()) == "UNKNOWN"
        assert classify_payment_path([], set()) == "UNKNOWN"

    def test_unknown_when_affected_nodes_missing(self):
        # Meta present but the AffectedNodes key isn't there — we can't
        # know whether it was AMM/CLOB/DIRECT, so it's UNKNOWN. Never
        # silently DIRECT (that would corrupt the DIRECT-share numerator).
        assert classify_payment_path({}, set()) == "UNKNOWN"
        assert classify_payment_path({"other_key": "value"}, set()) == "UNKNOWN"

    def test_unknown_when_affected_nodes_wrong_type(self):
        assert (
            classify_payment_path({"AffectedNodes": "not-a-list"}, set())
            == "UNKNOWN"
        )

    def test_direct_when_affected_nodes_empty_list(self):
        # AffectedNodes present but empty is a distinct case from missing.
        # Empty list is what you'd see for a Payment with no ledger-effect
        # nodes beyond the sender/dest which are still ordinary accounts —
        # classify as DIRECT.
        assert classify_payment_path({"AffectedNodes": []}, set()) == "DIRECT"

    # ─── Robustness against malformed sub-entries ─────────────────────

    def test_malformed_node_skipped(self):
        meta = {
            "AffectedNodes": [
                "not-a-dict",
                {"ModifiedNode": "not-a-dict-either"},
                _node("ModifiedNode", "AMM"),
            ]
        }
        assert classify_payment_path(meta, set()) == "AMM"

    def test_missing_ledger_entry_type_skipped(self):
        meta = {
            "AffectedNodes": [
                {"ModifiedNode": {"FinalFields": {}}},  # no LedgerEntryType
                _node("ModifiedNode", "Offer"),
            ]
        }
        assert classify_payment_path(meta, set()) == "CLOB"


# ─────────────────────────────────────────────────────────────────────
# Enum sentinel exposure — pinned so downstream code can import them
# ─────────────────────────────────────────────────────────────────────


class TestPathTypeSentinels:
    def test_all_six_sentinels_exposed(self):
        assert xrpl_stream._PATH_TYPE_AMM == "AMM"
        assert xrpl_stream._PATH_TYPE_CLOB == "CLOB"
        assert xrpl_stream._PATH_TYPE_MIXED == "MIXED"
        assert xrpl_stream._PATH_TYPE_DIRECT == "DIRECT"
        assert xrpl_stream._PATH_TYPE_AMM_LP == "AMM_LP"
        assert xrpl_stream._PATH_TYPE_UNKNOWN == "UNKNOWN"


# ─────────────────────────────────────────────────────────────────────
# DDL structural check — the migration statements are declared
# ─────────────────────────────────────────────────────────────────────


class TestPathTypeDdl:
    """SCHEMA_DDL in db.py must declare the path_type column, drop the
    old PK, and create the new UNIQUE INDEX. Fresh installs and existing
    installs both apply the same DDL block — these assertions guard the
    idempotent migration statements from silent regression."""

    def test_path_type_column_declared(self):
        import db
        assert "ADD COLUMN IF NOT EXISTS path_type TEXT" in db.SCHEMA_DDL

    def test_old_pk_dropped(self):
        import db
        assert "DROP CONSTRAINT IF EXISTS token_volume_pkey" in db.SCHEMA_DDL

    def test_new_unique_index_created(self):
        import db
        # The index name is stable and referenced by upsert ON CONFLICT.
        assert "token_volume_path_uniq_idx" in db.SCHEMA_DDL
        assert (
            "currency, issuer, hour_bucket, path_type" in db.SCHEMA_DDL
        )
