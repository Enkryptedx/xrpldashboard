"""The four held entries are destroyed issuances, not unassessed ones.

Until 2026-11-27 the last four entries in `token_names.json` carrying the
reason class `pending_review` all said the same thing: "not yet assessed",
which in practice meant "waiting on a holder walk". That hold could never
resolve, because all four issuances NO LONGER EXIST on-ledger. A holder
walk enumerates the holders of a live MPTokenIssuance node; there is no
node to enumerate, so the queue would have sat there forever describing a
review that was not pending but impossible.

MEASURED 2026-11-27 from our own node (192.168.40.95:5006, rippled 3.4.1)
at ledger_index validated 108617992, `ledger_entry(mpt_issuance)` for all
four returns entryNotFound. They split two ways on whether the ISSUER
survived:

  issuance_destroyed_then_issuer_deleted (2)
    TVV   rUF9TCVU7x8e2Z6vGAiLwwyZpjykzZGaiR -> account_info actNotFound
    BESTV rNBr9RnUnZrJnAZrwJZtAX6jJ1wbHwx5N1 -> account_info actNotFound
    A full-history read the same day placed MPTokenIssuanceDestroy then
    AccountDelete on 2026-11-19 ~17:00Z for both.

  issuance_destroyed_on_ledger (2)
    DZD  rKRQCkc9XWuCgYSY2nF5Hxhz6kTknZFz72 -> alive, OwnerCount 33, and
         account_objects(type=mpt_issuance) returns ZERO live issuances
    WFWD rDT55fnKurcFPZTjZkmUf9CBo19SBAh2Q1 -> alive, OwnerCount 14, and
         account_objects returns 14 live issuances, this one NOT among them

WHAT OUR OWN NODE CAN AND CANNOT PROVE. It retains only ledgers
108520005-108617992, so it proves the PRESENT ABSENCE of these objects, not
the November destroy/delete transactions. The transaction-level history came
from a separate full-history read. The reason strings say so explicitly
rather than crediting our node with history it does not hold -- the same
discipline as not asserting rendering behaviour in a reason string.

SCOPE. Classification only. All four stay suppressed, the census is
unchanged, and the suppressed key set is identical. None of these is a
withholding class, so all four keep publishing their names with the
`unverified` caveat exactly as before -- `is_withheld()` is true only for
the impersonation class, and that is deliberately not what these are. A
destroyed object is a fact about the ledger, not a judgement about a name.
"""

import collections
import json
import os

import pytest

import token_label as tl


HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DESTROYED_AND_DELETED = "issuance_destroyed_then_issuer_deleted"
DESTROYED = "issuance_destroyed_on_ledger"

# key -> (expected class, issuer address that the evidence must name)
EXPECTED = {
    "06715B138193FEE2597B39E2507F760355A878EC9F3AF361:rUF9TCVU7x8e2Z6vGAiLwwyZpjykzZGaiR":
        (DESTROYED_AND_DELETED, "rUF9TCVU7x8e2Z6vGAiLwwyZpjykzZGaiR"),
    "06716565907DEA79B217A14C5DF06FAEE136D96B8287A049:rNBr9RnUnZrJnAZrwJZtAX6jJ1wbHwx5N1":
        (DESTROYED_AND_DELETED, "rNBr9RnUnZrJnAZrwJZtAX6jJ1wbHwx5N1"),
    "0649B40ACA10011D695BFE3939CB59EDEB4E1EE73FC9F0B3:rKRQCkc9XWuCgYSY2nF5Hxhz6kTknZFz72":
        (DESTROYED, "rKRQCkc9XWuCgYSY2nF5Hxhz6kTknZFz72"),
    "062B592588AF241E0720C1D54DD12798D8B0A05E7B147EE0:rDT55fnKurcFPZTjZkmUf9CBo19SBAh2Q1":
        (DESTROYED, "rDT55fnKurcFPZTjZkmUf9CBo19SBAh2Q1"),
}

# Measured suppressed-key count on origin/main before and after this change.
# Unchanged by construction: this PR re-classes, it does not suppress or
# un-suppress anything.
CENSUS = 275


@pytest.fixture(scope="module")
def raw():
    with open(os.path.join(HERE, "token_names.json")) as f:
        return json.load(f)


def test_the_four_carry_their_measured_class(raw):
    for key, (expected_class, _issuer) in EXPECTED.items():
        assert key in raw, f"{key} vanished from token_names.json"
        assert tl.reason_class(raw[key]) == expected_class


def test_the_two_classes_partition_the_four_two_and_two(raw):
    """Not a blanket re-label: whether the ISSUER survived is the split."""
    got = collections.Counter(tl.reason_class(raw[k]) for k in EXPECTED)
    assert got == {DESTROYED_AND_DELETED: 2, DESTROYED: 2}


def test_the_pending_review_queue_is_empty(raw):
    """The point of the change: nothing is left waiting on an impossible walk.

    Reads the CLASS, so the 10 `impersonation_risk_pending_review` entries
    are correctly not counted here -- that is a different class that happens
    to end in the same two words.
    """
    still_held = sorted(
        v.get("currency_display") or k
        for k, v in raw.items()
        if isinstance(v, dict) and tl.reason_class(v) == "pending_review"
    )
    assert still_held == []


def test_census_and_suppression_are_untouched(raw):
    suppressed = {k for k, v in raw.items()
                  if isinstance(v, dict) and v.get("suppressed")}
    assert len(suppressed) == CENSUS
    for key in EXPECTED:
        assert raw[key].get("suppressed"), f"{key} must stay suppressed"


def test_none_of_the_four_is_withheld(raw):
    """A destroyed object is a ledger fact, not a judgement about the name.

    Pins that this re-class did NOT quietly change what readers see: only
    the impersonation class withholds a name, so all four keep publishing
    theirs with the `unverified` caveat, as they did while held.
    """
    for key in EXPECTED:
        assert tl.is_withheld(raw[key]) is False


def test_each_reason_names_its_own_issuer_and_the_measured_absence(raw):
    """Evidence must be anchored to the account it was read from.

    Guards the copy-paste failure mode for four near-identical strings:
    an entry whose prose cites a DIFFERENT issuer's address reads as
    verified when nothing about it was.
    """
    for key, (_cls, issuer) in EXPECTED.items():
        reason = raw[key]["suppressed"]
        assert issuer in reason
        assert "entryNotFound" in reason
        for _k, (_c, other) in EXPECTED.items():
            if other != issuer:
                assert other not in reason.split(issuer, 1)[0], (
                    f"{key} cites {other} before its own issuer {issuer}"
                )


def test_the_deleted_pair_claim_a_delete_and_the_others_do_not(raw):
    """The class distinction has to be visible in the evidence, not just
    in the label. An `issuance_destroyed_on_ledger` entry must not claim
    the issuer is gone -- for DZD and WFWD the issuer is demonstrably
    alive and, in WFWD's case, still issuing 14 other instruments."""
    for key, (cls, _issuer) in EXPECTED.items():
        reason = raw[key]["suppressed"]
        if cls == DESTROYED_AND_DELETED:
            assert "AccountDelete" in reason
            assert "actNotFound" in reason
        else:
            assert "AccountDelete" not in reason
            assert "actNotFound" not in reason
            assert "still" in reason and "alive" in reason


def test_no_reason_credits_our_node_with_history_it_lacks(raw):
    """Our node retains ~98k ledgers, not full history. Any entry citing
    the November transactions must also say where that came from, so a
    later reader does not re-run the claim against our node and conclude
    the evidence was fabricated when it simply is not in the window."""
    for key, (cls, _issuer) in EXPECTED.items():
        reason = raw[key]["suppressed"]
        if "AccountDelete" in reason or "create and four destroy" in reason:
            assert "full-history" in reason
