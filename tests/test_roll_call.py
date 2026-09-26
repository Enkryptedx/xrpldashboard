"""Roll-call arithmetic locks — pure, no network, no DB.

Every expected number here is derived from rippled source (AmendmentTable.cpp,
SystemParameters.h, RCLConsensus.cpp), not from what the recorder happens
to print. If rippled changes its rule these tests are the ones to update.
"""
import roll_call as rc

# Public UNL manifest for one Ripple-list validator (vl.ripple.com seq 85,
# fetched 2026-09-25). Master + signing keys are public information.
MANIFEST_B64 = ("JAAAAAFxIe0Tqvy2qHvLXQk8LvN/BEMcKREm1nQpMwUVLZd2xquk1nMhA9RioHJW8Kz6IjnHOOkt"
                "bvbaHsZqwJb8otgoIu+46QbWdkYwRAIgE0pz8HpSKrUsJ8E390K8KCwmvExB00jLvqPv9LZr6roC"
                "IAl9zLWeIRSsBRIaOl5alblYMYMXrpbxJZ7t+jtbiT9Ldwd4cnAudmV0cBJADEZOQPQJcWj0zPju"
                "lcvH1o8WhQ9jrKzWV/mkXSHGjmzIiekkOzUcEnzmJXwJYWZZnA0jTLE30OYmxCRXfCm9Bg==")
MASTER_HEX = "ED13AAFCB6A87BCB5D093C2EF37F04431C291126D674293305152D9776C6ABA4D6"
MASTER_B58 = "nHBWa56Vr7csoFcCnEPzCCKVvnDQw3L28mATgHYQMGtbEfUjuYyB"
SIGNING_B58 = "n94a894ARPe5RdcaRgdMBB9gG9ukS5mqsd7q2oNmC1NKqtZqEJnb"

A1 = "A" * 64
A2 = "B" * 64


# --- ledger arithmetic -------------------------------------------------------

def test_voting_ledger_is_the_one_before_a_flag_ledger():
    assert rc.is_voting_ledger(107238655)           # 107238655 % 256 == 255
    assert not rc.is_voting_ledger(107238656)       # flag ledger itself (% 256 == 0)
    assert not rc.is_voting_ledger(107238657)       # pseudo-tx ledger (% 256 == 1)
    assert rc.flag_ledger_for(107238655) == 107238656


def test_threshold_matches_rippled_integer_division():
    # max(1, trusted * 80 / 100), C++ long division
    assert rc.rippled_threshold(35) == 28
    assert rc.rippled_threshold(34) == 27
    assert rc.rippled_threshold(1) == 1
    assert rc.rippled_threshold(0) == 1        # max(1, 0)
    assert rc.rippled_threshold(4) == 3
    assert rc.rippled_threshold(5) == 4


def test_passes_is_strictly_greater_than_threshold():
    # 35 trusted → threshold 28 → needs 29 yes votes
    assert not rc.rippled_passes(28, 35)
    assert rc.rippled_passes(29, 35)
    # 0 trusted → threshold 1, nothing can pass
    assert not rc.rippled_passes(1, 0)


def test_single_trusted_validator_exception_uses_greater_or_equal():
    assert rc.rippled_passes(1, 1)
    assert not rc.rippled_passes(0, 1)


def test_votes_needed_is_threshold_plus_one_and_matches_our_sep_23_record():
    # PermissionDelegationV1_1 held 28/35 at the 2026-09-23 roll call and lost
    # its majority at flag ledger 107181569 — 28 is NOT enough, 29 is.
    assert rc.votes_needed(35) == 29
    assert not rc.rippled_passes(28, 35)
    assert rc.rippled_passes(rc.votes_needed(35), 35)
    assert rc.votes_needed(1) == 1
    assert rc.votes_needed(0) == 2          # nothing can pass with no trusted validations


# --- UNL maps ----------------------------------------------------------------

def _blob():
    return {"sequence": 85, "validators": [
        {"validation_public_key": MASTER_HEX, "manifest": MANIFEST_B64}]}


def test_unl_key_maps_decodes_manifest_signing_key_to_master():
    maps = rc.unl_key_maps(_blob(), source="vl.ripple.com")
    assert maps.size == 1
    assert maps.sequence == 85
    assert MASTER_B58 in maps.master_keys
    assert maps.signing_to_master[SIGNING_B58] == MASTER_B58
    assert maps.skipped == 0


def test_unl_key_maps_keeps_member_when_manifest_is_garbage():
    blob = {"sequence": 1, "validators": [
        {"validation_public_key": MASTER_HEX, "manifest": "not-base64!!"}]}
    maps = rc.unl_key_maps(blob, source="x")
    assert MASTER_B58 in maps.master_keys
    assert maps.signing_to_master == {}
    assert maps.skipped == 1


# --- wire → vote -------------------------------------------------------------

def _msg(**over):
    base = {"type": "validationReceived", "full": True, "ledger_index": "107238655",
            "validation_public_key": SIGNING_B58, "signing_time": 843700000,
            "server_version": "1745990606786789376", "amendments": [A2.lower(), A1]}
    base.update(over)
    return base


def test_vote_from_message_maps_wire_signing_key_to_master_and_normalizes_hashes():
    maps = rc.unl_key_maps(_blob(), source="vl.ripple.com")
    v = rc.vote_from_message(_msg(), maps)
    assert v is not None
    assert v.master_key == MASTER_B58
    assert v.signing_key == SIGNING_B58
    assert v.ledger_index == 107238655
    assert v.amendments == (A1, A2)          # sorted, upper-cased


def test_vote_from_message_prefers_wire_master_key_when_present():
    maps = rc.unl_key_maps(_blob(), source="vl.ripple.com")
    v = rc.vote_from_message(_msg(master_key=MASTER_B58, validation_public_key="n9unknown"), maps)
    assert v is not None and v.master_key == MASTER_B58


def test_vote_from_message_rejects_partial_and_non_unl():
    maps = rc.unl_key_maps(_blob(), source="vl.ripple.com")
    assert rc.vote_from_message(_msg(full=False), maps) is None
    assert rc.vote_from_message(_msg(validation_public_key="n9NotOnTheList"), maps) is None
    assert rc.vote_from_message({"type": "ledgerClosed"}, maps) is None


def test_vote_with_no_amendments_field_is_a_yes_on_nothing():
    maps = rc.unl_key_maps(_blob(), source="vl.ripple.com")
    m = _msg(); m.pop("amendments")
    v = rc.vote_from_message(m, maps)
    assert v is not None and v.amendments == ()


# --- TrustedVotes carry-forward ---------------------------------------------

def _vote(master, ams, idx=107238655, st=843700000):
    return rc.Vote(master_key=master, signing_key=None, ledger_index=idx,
                   signing_time=st, server_version=None, amendments=tuple(ams))


def test_trusted_votes_carry_forward_and_denominator():
    tv = rc.TrustedVotes()
    tv.trust_changed({"m1", "m2", "m3"})
    tv.record_votes([_vote("m1", [A1]), _vote("m2", [A1])], close_time=1000)
    available, counts = tv.get_votes()
    assert available == 2                     # m3 never heard → not available
    assert counts == {A1: 2}
    # next round only m1 speaks; m2's vote is CARRIED (rippled anti-flap)
    tv.record_votes([_vote("m1", [A1])], close_time=1000 + 900)
    available, counts = tv.get_votes()
    assert available == 2 and counts == {A1: 2}


def test_trusted_votes_expire_after_24h():
    tv = rc.TrustedVotes()
    tv.trust_changed({"m1", "m2"})
    tv.record_votes([_vote("m1", [A1]), _vote("m2", [A1])], close_time=1000)
    # m1 keeps talking, m2 goes silent for > 24h
    tv.record_votes([_vote("m1", [A1])], close_time=1000 + rc.VOTE_EXPIRY_SECONDS + 1)
    available, counts = tv.get_votes()
    assert available == 1 and counts == {A1: 1}


def test_trusted_votes_ignores_untrusted_and_drops_removed_validators():
    tv = rc.TrustedVotes()
    tv.trust_changed({"m1"})
    tv.record_votes([_vote("m1", [A1]), _vote("stranger", [A1])], close_time=1000)
    assert tv.get_votes() == (1, {A1: 1})
    tv.trust_changed({"m2"})                  # m1 left the list, m2 joined as "no"
    assert tv.get_votes() == (0, {})


def test_newest_validation_replaces_previous_upvotes():
    tv = rc.TrustedVotes()
    tv.trust_changed({"m1"})
    tv.record_votes([_vote("m1", [A1, A2])], close_time=1000)
    tv.record_votes([_vote("m1", [A2])], close_time=2000)
    assert tv.get_votes() == (1, {A2: 1})


# --- round tally -------------------------------------------------------------

def test_tally_round_stores_both_denominators_and_both_counts():
    maps = rc.UnlKeyMaps(source="vl.ripple.com", sequence=85,
                         master_keys={f"m{i}" for i in range(35)}, signing_to_master={})
    tv = rc.TrustedVotes()
    # round 1: 29 of 35 say yes to A1 → threshold(29)=23 → passes
    r1 = rc.tally_round(107238655, [_vote(f"m{i}", [A1]) for i in range(29)], maps, tv, "t1")
    assert r1.round_row["unl_size"] == 35
    assert r1.round_row["validations_seen"] == 29
    assert r1.round_row["trusted_available"] == 29
    assert r1.round_row["threshold_rippled"] == 23
    assert r1.round_row["votes_needed"] == 24
    assert r1.round_row["flag_ledger_index"] == 107238656
    assert r1.tally_rows == [{"voting_ledger_index": 107238655, "amendment_hash": A1,
                              "yes_votes_round": 29, "yes_votes_carried": 29,
                              "passes_rippled": True}]
    assert len(r1.vote_rows) == 29
    # round 2 (next voting ledger): only 5 heard, all yes → round=5, carried=29
    r2 = rc.tally_round(107238911, [_vote(f"m{i}", [A1], idx=107238911, st=843700900)
                                    for i in range(5)], maps, tv, "t2")
    t = r2.tally_rows[0]
    assert t["yes_votes_round"] == 5
    assert t["yes_votes_carried"] == 29
    assert r2.round_row["validations_seen"] == 5
    assert r2.round_row["trusted_available"] == 29
    assert t["passes_rippled"] is True


def test_tally_round_filters_votes_for_other_ledgers_and_dedups_by_master():
    maps = rc.UnlKeyMaps(source="x", sequence=1, master_keys={"m1", "m2"}, signing_to_master={})
    tv = rc.TrustedVotes()
    r = rc.tally_round(107238655, [_vote("m1", [A1]), _vote("m1", [A1]),
                                   _vote("m2", [A1], idx=107238911)], maps, tv, "t")
    assert r.round_row["validations_seen"] == 1
    assert r.round_row["trusted_available"] == 1
    assert r.tally_rows[0]["yes_votes_round"] == 1


def test_tally_round_with_no_votes_is_an_honest_empty_round():
    maps = rc.UnlKeyMaps(source="x", sequence=1, master_keys={"m1"}, signing_to_master={})
    r = rc.tally_round(107238655, [], maps, rc.TrustedVotes(), "t")
    assert r.round_row["validations_seen"] == 0
    assert r.round_row["trusted_available"] == 0
    assert r.round_row["threshold_rippled"] == 1
    assert r.round_row["signing_time_max"] is None
    assert r.tally_rows == [] and r.vote_rows == []
