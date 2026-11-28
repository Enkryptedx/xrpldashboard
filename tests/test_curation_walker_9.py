"""Curation of the 9 walker-discovered entries (2026-10-17).

A walker fired overnight and added 9 entries to token_names.json. Under the
automatic MPT rule they would all have rendered -- bare, with no curation --
because none of them contains a curated ticker, so the lookalike rule stays
silent on all 9. Rendering is therefore the DEFAULT for a freshly-walked
name, which is exactly why each one needs an explicit decision.

Decisions pinned here:

  10x SCPO (rM75v2Ssm6W7...) -> suppressed, never render.
     Six declare "test" in their own on-ledger metadata. The other four do
     NOT -- the name predicate misses them -- and are suppressed on
     ISSUER-CLUSTER evidence instead: same issuer, same SC.PO issuer_name,
     identical CanEscrow+CanClawback flags, and sequential issuance IDs
     interleaved with the six. That reason is recorded as its own string so
     the judgement is auditable rather than hidden behind a substring match.

     "Refresh Timing 1" (seq 107538940) was the first such case, curated
     2026-10-17.

     "Refresh Timing 2" (107538943), "Oil Sample" (107538948) and "Nails"
     (107538951) joined it on 2026-10-30, under the same reason, after the
     carve-out for anchor #13 surfaced them unsuppressed. The evidence the
     owner approved them on, read from our own node and both full-history
     witnesses:

       - the issuer has NO Domain field, so no xrp-ledger.toml is
         discoverable; it appears in no toml we track at either issuer or
         currency level, and carries no XRPScan label (accountName null);
       - the account was created 2026-10-09 and funded from Uphold --
         21 days old at curation time, not an institutional issuer;
       - all ten carry "i":"https://example.com/scpo.png", the RFC 2606
         reserved placeholder domain, as their icon;
       - all ten have MaximumAmount 1 -- a one-unit cap, not a fungible
         instrument -- and an "ext" purchase-order blob whose buyer "b" is
         the issuing account itself;
       - all three have OutstandingAmount 0: zero supply, therefore zero
         holders. "Refresh Timing 2" has a lone MPTokenAuthorize (an
         opt-in, not a holding); the other two have nothing past their
         MPTokenIssuanceCreate;
       - the issuer has never placed a DEX order: zero OfferCreate /
         OfferCancel / AMM* across its 121 transactions, and none of its
         58 Payments touches these three issuance IDs. No trades exist.

     Rendering is the DEFAULT for a freshly-walked name, so each of these
     needed an explicit decision; the walker found them and nothing in code
     re-applies an earlier cluster judgement to a later sibling. That gap is
     deliberately NOT closed here -- it is a separate change under the
     token-trust rules. This file only pins the decisions.

  CERT -> self_described. "CERTCRYPT Capacity", no issuer Domain,
     OutstandingAmount 0. No first-party source exists, so it renders with
     the unverified qualifier.

  XAGa -> tier toml. Its [[CURRENCIES]] block pairs code
     5841476100000000000000000000000000000000 with issuer
     rQJ1EbtepKKzx4sBCUtPjvSk8CfiAx2H8e and ticker XAGa -- a CURRENCY-level
     citation, not merely issuer-level, so it earns the full toml tier and
     renders bare like any other verified token.

TWO NEAR-MISSES that shaped this, both false negatives from mechanical
failure rather than from evidence:
  1. The first TOML fetch returned URLError. One transient blip would have
     demoted a legitimately sourced token to self-described.
  2. A parser looking for `currency =` found nothing, because this TOML uses
     `code =` / `ticker =`. That one fails DETERMINISTICALLY and looks like
     a clean negative -- a retry would not have saved it.
Both are addressed in the follow-up PR (shared fetch-with-retry + shared
[[CURRENCIES]] parser). This PR records the verdicts they produced.

Hermetic: reads token_names.json and pure token_label functions. No network,
no DB -- deliberately, since a CI test that fetches a third-party TOML would
be flaky and would re-create near-miss #1 inside the test suite.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
import token_label as tl  # noqa: E402

NAMES = os.path.join(REPO_ROOT, "token_names.json")
SCPO_ISSUER = "rM75v2Ssm6W7ZP3iKi8FCaMbSsUaiQhYKC"
XAGA_ISSUER = "rQJ1EbtepKKzx4sBCUtPjvSk8CfiAx2H8e"
XAGA_HEX = "5841476100000000000000000000000000000000"
XAGA_TOML = "https://silver.assetiko.com/.well-known/xrp-ledger.toml"
CLUSTER_REASON = "issuer-cluster: same issuer/test pattern"


@pytest.fixture(scope="module")
def raw():
    return json.load(open(NAMES, encoding="utf-8"))


def entries(raw, issuer=None, display=None):
    out = []
    for key, v in raw.items():
        if not isinstance(v, dict):
            continue
        if issuer and key.split(":", 1)[1] != issuer:
            continue
        if display and v.get("currency_display") != display:
            continue
        out.append(v)
    return out


# ── the SCPO cluster ────────────────────────────────────────────────
class TestScpoCluster:
    def test_all_ten_are_suppressed(self, raw):
        """Every entry under this issuer is suppressed and never renders.

        The count is pinned deliberately. A new sibling arriving from the
        walker must FAIL here rather than quietly render -- that is exactly
        how "Refresh Timing 2", "Oil Sample" and "Nails" were caught on
        2026-10-30. If this count moves, the new entries need a curation
        decision, not a re-baseline.
        """
        got = entries(raw, issuer=SCPO_ISSUER)
        # 10 -> 14 on 2026-11-06: the guard fired as designed. Four new
        # siblings arrived from the walker ("Bikes", "Blue Bikes", "First RFQ
        # Agent to PO test", "$1M - Car Parts - Working Capital") and were
        # each given a curation decision before this count moved -- one on
        # the name predicate, three on issuer-cluster evidence. NOT a
        # re-baseline: the per-entry suppression loop below is unchanged and
        # still proves every one of the 14 is suppressed.
        assert len(got) == 14, [v.get("name") for v in got]
        for v in got:
            assert v.get("suppressed"), v.get("name")
            assert tl.render_decision(v) == (False, None)

    def test_six_cite_their_own_test_metadata(self, raw):
        got = entries(raw, issuer=SCPO_ISSUER)
        by_name = {v.get("name"): v.get("suppressed") for v in got}
        test_named = [n for n, r in by_name.items()
                      if r == "self_declared_test"]
        assert len(test_named) == 6, by_name

    def test_refresh_timing_uses_the_cluster_reason(self, raw):
        """The name predicate misses this one. The reason must say so
        explicitly, not borrow the self_declared_test label it did not earn."""
        got = [v for v in entries(raw, issuer=SCPO_ISSUER)
               if v.get("name") == "Refresh Timing 1"]
        assert len(got) == 1
        assert got[0]["suppressed"] == CLUSTER_REASON
        blob = " ".join(str(got[0].get(f, "")) for f in
                        ("name", "desc", "issuer_name")).lower()
        assert "test" not in blob, (
            "if the name now says 'test', use self_declared_test instead")

    def test_cluster_reason_count_is_pinned(self, raw):
        """The cluster reason must not become a catch-all.

        It is an explicit human judgement, so its use is counted rather
        than left open-ended: 1 from 2026-10-17 ("Refresh Timing 1") plus
        the 3 approved 2026-10-30 ("Refresh Timing 2", "Oil Sample",
        "Nails"). Raising this number requires the same evidence trail as
        the header documents -- never a silent bump to make a red run go
        green.
        """
        n = sum(1 for v in raw.values() if isinstance(v, dict)
                and v.get("suppressed") == CLUSTER_REASON)
        assert n == 4


# ── CERT ────────────────────────────────────────────────────────────
class TestCert:
    def test_renders_with_the_unverified_qualifier(self, raw):
        got = entries(raw, display="CERT")
        assert len(got) == 1
        v = got[0]
        assert v.get("tier") == tl.SELF_DESCRIBED
        assert not v.get("suppressed")
        ok, label = tl.render_decision(v)
        assert ok is True
        assert label == "self-described (unverified)"

    def test_is_not_a_lookalike(self, raw):
        """No curated ticker in "CERT", so the automatic rule stays silent."""
        assert tl.lookalike_asset(entries(raw, display="CERT")[0]) is None


# ── XAGa ────────────────────────────────────────────────────────────
class TestXaga:
    def test_tier_is_toml_not_domain_fallback(self, raw):
        """Currency-level citation: the [[CURRENCIES]] block pairs this exact
        code WITH this exact issuer, so it is not merely issuer-level."""
        got = [v for v in raw.values() if isinstance(v, dict)
               and v.get("currency_hex") == XAGA_HEX]
        assert len(got) == 1
        v = got[0]
        assert v.get("issuer") == XAGA_ISSUER
        assert v.get("tier") == "toml"
        assert v.get("verified_via") == XAGA_TOML

    def test_citation_is_the_subdomain_not_the_apex(self, raw):
        """silver.assetiko.com names the issuer 4x; assetiko.com names it 0x.
        Citing the apex would be an unreciprocated claim."""
        got = [v for v in raw.values() if isinstance(v, dict)
               and v.get("currency_hex") == XAGA_HEX][0]
        assert "silver.assetiko.com" in got["verified_via"]

    def test_renders_bare_like_any_verified_token(self, raw):
        got = [v for v in raw.values() if isinstance(v, dict)
               and v.get("currency_hex") == XAGA_HEX][0]
        ok, label = tl.render_decision(got)
        assert ok is True
        assert label is None, "a toml-tier token needs no qualifier"


# ── the Licuido issuer ──────────────────────────────────────────────
LICUIDO_ISSUER = "rLr3ZxWy7owtSwaCH2n7tCauQENrt9MVgU"
LRC_REASON_CLASS = "placeholder_unfinished_issuance"


class TestLicuidoLrcReasonClass:
    """LRC is suppressed as a PLACEHOLDER, not as an imitation.

    It was first suppressed impersonation_risk_pending_review on 2026-11-27
    and re-classed the same day. The impersonation reading did not fit: the
    display "LRC" is an initialism of the issuance's own name, "Licuido RC
    Issuance", in the same series as its siblings at this issuer (LMI =
    Licuido MPT Issuance, LKT = Licuido Komainu Test Issuance). The clash
    with an established ticker is coincidental rather than borrowed, and
    tl.lookalike_asset() never fired on it because that ticker is not in
    tl.CURATED_ASSETS -- so nothing in code ever supported the first call.

    The outcome is unchanged: still suppressed, still not rendering. Only
    the evidence class moved, which is skill section 5 -- never borrow a
    predicate the entry fails.
    """

    def _lrc(self, raw):
        got = [v for v in raw.values() if isinstance(v, dict)
               and v.get("issuer") == LICUIDO_ISSUER
               and v.get("currency_display") == "LRC"]
        assert len(got) == 1, got
        return got[0]

    def test_suppressed_under_the_placeholder_class(self, raw):
        v = self._lrc(raw)
        assert v.get("suppressed", "").startswith(LRC_REASON_CLASS), \
            v.get("suppressed")
        assert tl.render_decision(v) == (False, None)

    def test_the_placeholder_evidence_is_still_true(self, raw):
        """The reason cites desc == "TEXT". If a walk ever fills in a real
        description, this fails and the class gets re-chosen rather than
        silently becoming a claim the data no longer supports."""
        assert self._lrc(raw).get("desc") == "TEXT"

    def test_it_does_not_borrow_a_predicate_it_fails(self, raw):
        """LRC's own fields never say "test", so it may not wear
        self_declared_test -- that belongs to its sibling LKT, whose
        metadata does say it. Nor may it wear the SCPO cluster reason,
        whose use is pinned at 4 elsewhere in this module."""
        v = self._lrc(raw)
        blob = " ".join(str(v.get(f, "")) for f in
                        ("name", "desc", "issuer_name")).lower()
        assert "test" not in blob, (
            "if LRC now says 'test', use self_declared_test instead")
        assert not v.get("suppressed", "").startswith("self_declared_test")
        assert v.get("suppressed") != CLUSTER_REASON

    def test_the_test_sibling_is_the_one_that_says_test(self, raw):
        """The issuer-cluster half of LRC's evidence: LKT at the same issuer
        self-declares a test, which is why a cluster reading is available
        here at all."""
        got = [v for v in raw.values() if isinstance(v, dict)
               and v.get("issuer") == LICUIDO_ISSUER
               and v.get("currency_display") == "LKT"]
        assert len(got) == 1, got
        assert "test" in str(got[0].get("name", "")).lower()
        assert got[0].get("suppressed", "").startswith("self_declared_test")


# ── file-level invariants ───────────────────────────────────────────
class TestFileInvariants:
    def test_suppressed_total(self, raw):
        """5 from the earlier curation pass + 14 SCPO + 16 from 2026-11-06.

        Was 12 (5 + 7) until 2026-10-30, when "Refresh Timing 2", "Oil
        Sample" and "Nails" were suppressed on issuer-cluster evidence --
        see this module's header for the evidence the owner approved.

        15 -> 35 on 2026-11-06 (anchor #14 gate-5 carve-out): 20 pending
        entries were decided -- 10 VQUOTE (per-quote identifiers), 2
        self-declared tests, 4 issuer-cluster (3 SCPO siblings + YBB, which
        shares its issuer with a self-declared test fund and has zero
        OutstandingAmount), 1 nXRP (XRP-adjacent name whose issuer returns
        actNotFound), and 3 'pending_review' holds (SCPO "12222", BOTC,
        DATEC) which are reversible and rest on no evidence against them.
        35 -> 33 on 2026-11-12: of those 3 'pending_review' holds, BOTC
        and DATEC were PUBLISHED (owner decision; first-party TOML names
        both issuance ids) and so leave the suppressed set, while SCPO
        "12222" stays suppressed with its reason restated as
        issuer_cluster_test. Net -2.

        33 -> 34 the same day: the sibling 060B0AE4 (a second renderable
        "BOTC" from the same issuer) was suppressed display_collision_
        superseded, the owner's verdict on the collision that publishing
        060B0AE9 exposed. See test_curate_botc_datec_scpo.py.

        34 -> 115 on 2026-11-13: the anchor #15 gate-5 carve-out decided 81
        new walker-discovered entries, all suppressed. 67
        bulk_serial_mint_unreviewed (one undomained issuer minting a
        machine-numbered series), 7 pending_review, 4 self_declared_test,
        1 issuer_cluster_test, 1 display_collision_unreviewed and
        1 impersonation_risk_pending_review. Nothing was published: the
        count rose because decisions were ADDED, not because a guard was
        relaxed -- the per-entry suppression assertion below is unchanged
        and still passes on every one of them.

        115 -> 140 on 2026-11-20: the anchor #16 gate-5 carve-out decided
        the 28 walker-discovered entries undecided since anchor #15. 25 of
        the 28 join the suppressed set -- 12 display_collision_unreviewed
        (one issuer minting 12 issuances that all render the identical
        "WFWD"), 4 self_declared_test (the HVI*/CGG* Test family on
        rKVVothwXXUQ..., absent from the hvi3.com TOML whose reciprocated
        HVI3 names they shadow), 3 bulk_serial_mint_unreviewed (the same
        undomained serial minter already suppressed 67x at anchor #15),
        1 self_declared_test (TEMP01, whose own fields read temp0123 /
        faskdjfakdfha / johndoe), and 5 pending_review (CRRVN1/CRXVN2/
        CRXVN3 vault receipt notes and SWT01/SWR01 RWA lease notes, both
        issuers undomained, recheck 2026-11-27).

        +25 not +20: a `pending_review` hold is RECORDED IN THE SAME
        `suppressed` FIELD, so the five holds count here exactly as the
        seven anchor #15 holds already did. The other 3 of the 28 were
        PUBLISHED (YELLOW at currency level, LOOM and HVI3 at issuer
        level) and so never enter this set.

        140 -> 223 on 2026-11-22: the 83 SIVAX serials. One publisher
        (www.sivax.io) self-attests 83 MPT serials across two issuers,
        rM7ffj9GZV41K8fWUhtpfZSZvYoZB2yA4t (80) and rNyrL3hjvM3mDYtTDhHLWMiGtbnV2wdBv6
        (1). Their citation is REAL -- they reached issuer level in the
        Domain-first sweep, 83 of its 90 issuer-level passes -- but our own
        node reads zero holders and zero outstanding obligations for all 83
        (rM7ffj9 has a single trustline and it is RLUSD, not any SIV), they
        are MPT keys so `toml` is unreachable, and SIV1 is duplicated at
        rM7ffj9 (0601A58E.../0601A594...) which trips the
        (currency_display, issuer) collision guard. Suppression is not a
        finding against the publisher: it keeps 83 holderless serials from
        one operator out of a shared namespace. Count rose because
        decisions were ADDED. See tests/test_sivax_suppression.py.

        This is the one authoritative home for the census count.
        """
        n = sum(1 for v in raw.values()
                if isinstance(v, dict) and v.get("suppressed"))
        # 223 -> 226 on 2026-11-22: XRP NFT, XRP+ and XRP3D, suppressed
        # impersonation_risk_pending_review on the PUBXRP precedent of
        # 2026-11-13. All three imitate XRP itself and had been parked on an
        # explicit owner decision since 2026-10-16 (lookalike_review =
        # pending_owner_decision_2026-10-16), rendering BARE in the meantime.
        # Displaying a name that imitates XRP amplifies the imitation, which
        # is a different question from provenance -- so the bare-is-honest
        # argument that applies to an unexamined SOURCE does not apply here.
        # See tests/test_bare_display_sites.py.
        #
        # 226 -> 240 on 2026-11-27 (anchor #17 carve-out), +14, measured as
        # the suppressed-key delta against origin/main, with NOTHING
        # un-suppressed:
        #   impersonation_risk_pending_review (5) -- two NXRP records
        #   ("Native XRP Yield Shares", "Native XRP Yield") and NRLUSD
        #   imitate XRP and Ripple's RLUSD; LRC and the Korean-script name
        #   imitate a consumer brand. Same PUBXRP precedent as above.
        #   bulk_serial_mint_unreviewed (5) -- FIT25/FIT26/FIT27/TOSB29/
        #   LYNV21, machine-numbered serial mints from one issuer.
        #   issuer-cluster (2) -- TSWT01/TSWR01, siblings of the already
        #   suppressed SWT01/SWR01 under the SAME issuer
        #   r4V8kJBEzjBNXbTc5nuRLFwzWFo8ZsiYu7. The cluster reason is stored
        #   per entry with no rule in code to re-apply it, which is why each
        #   new sibling arrives unsuppressed and must be caught here.
        #   owner_suppressed_unreviewed (2) -- BESTV, MPTOK1.
        # LKT (publish_decision) and WRENCH (tier=domain_fallback) were
        # decided the OTHER way in the same pass and are correctly not
        # counted here -- see tests/test_display_labelled.py for WRENCH's
        # label.
        #
        # 240 -> 241 on 2026-11-27, +1, measured as the suppressed-key
        # delta against origin/main (added: the Korean-script utility name
        # at rDXwXPcbfupmFRtSye7E5pe1xdpGeNPB3W; removed: nothing). It takes
        # impersonation_risk_pending_review on the same evidence as its
        # sibling at that issuer, decided in the #17 carve-out.
        #
        # It was NOT found by the new-entry check, and that is the point
        # worth recording: the entry predates the carve-out's base, so
        # `set(new) - set(old)` could never see it and the anchor gate-5
        # new-entry scan structurally could not raise it. It surfaced only
        # from a whole-issuer sweep -- for every issuer holding at least one
        # impersonation suppression, every OTHER entry at that issuer still
        # rendering, regardless of age. A sibling OLDER than the suppressed
        # member is invisible to every age-based check we run, so the sweep
        # is the guard, not the diff.
        #
        # 241 -> 242 on 2026-11-27, +1, measured as the suppressed-key delta
        # against origin/main (added: LKT; removed: nothing). LKT is a
        # REVERSAL, not a new arrival: it was published untiered earlier the
        # same day, and the sweep that followed read its own on-ledger
        # metadata -- name "Licuido Komainu Test Issuance", desc "Testing
        # along with partners for sanity". The self-declared-test predicate
        # fires on that, and skill §5 rejects such a token rather than
        # holding it pending, so the publish_decision was withdrawn and
        # replaced with self_declared_test.
        #
        # The lesson the reversal records: the first decision rested only on
        # "no citation is reachable", which was true but not the whole
        # question. Absent provenance and a self-declared test are different
        # findings, and the second one decides the entry on its own.
        #
        # LMI, its sibling at the same issuer, moved the OTHER way in the
        # same commit and is correctly absent from this count: it carries a
        # publish_decision, not a suppression. Sharing an issuer with a
        # suppressed entry is not evidence against a token.
        #
        # 242 -> 245 on 2026-11-27, +3, measured as the suppressed-key delta
        # against origin/main (added: TST1, TST2, LTP; removed: nothing).
        # All three self-declare a test in their own on-ledger metadata, so
        # skill section 5 rejects them rather than holding them pending.
        #
        # They arrived via a class this file had never been able to see. The
        # walker's Part A inserts a token_names entry for every NAMED MPT
        # issuance, but 121 of 533 issuances in mpt_snapshot had no entry at
        # all, because Part A had not run since they appeared. The MPT
        # surfaces build their rows from the LEDGER snapshot, not from this
        # file, so those names were already published while carrying no
        # suppression, no tier and no publish_decision -- invisible to every
        # curation check, including the whole-issuer sweep, which reads this
        # file. Running the walker created 32 stub entries for the named
        # ones; the other 89 publish no name and need nothing.
        #
        # 29 of those 32 stubs are deliberately left UNDECIDED. They are now
        # visible to the anchor gate-5 new-entry check, which is the point:
        # the next pre-flight halts on them until they are judged, instead of
        # them publishing unseen.
        #
        # 245 -> 274 on 2026-11-27, +29, measured as the suppressed-key delta
        # against origin/main. These are the 29 walker-written stubs for
        # named ledger-only issuances, left undecided when they were created
        # and now decided in three groups:
        #   display_collision_unreviewed (24) -- every one of the 24 shares
        #     the identical rendered ledger name "Receivable principal" at
        #     one issuer. The count is MEASURED from the data, not carried
        #     over from a hand-written list: a first pass described this
        #     group as 14 while naming 15 members.
        #   owner_suppressed_unreviewed (4) -- M3D467, M77354, MC4896,
        #     SE5FAB, distinctly named instruments at the same issuer that
        #     no pass has assessed.
        #   bulk_serial_mint_unreviewed (1) -- FLW159, from the undomained
        #     bulk serial minter already suppressed on this class 75 times.
        #
        # Both issuers were read from our own node at ledger_index
        # validated and carry NO Domain field, so no xrp-ledger.toml is
        # discoverable for either and no citation is reachable.
        #
        # Suppression here does NOT hide these names: only the impersonation
        # class withholds a name. All 29 keep publishing on the MPT surfaces
        # with the `unverified` caveat, which is confirmed from rendered HTML
        # in tests/test_decided_stubs_still_publish.py rather than inferred.
        #
        # 274 -> 275 on 2026-11-27, +1, measured as the suppressed-key delta
        # against origin/main (added: ZUVMP; removed: nothing).
        # bulk_serial_mint_unreviewed at the undomained minter
        # r3iMKCiKqu522oRGQcAdyvwoRUaTn7s8fi, which already carried that
        # class 76 times. Its name equals its ticker, it has no
        # description, and the issuer has no on-ledger Domain.
        #
        # Recorded because of HOW it was found: the whole-issuer sweep, not
        # the new-entry check. ZUVMP predates the anchor #17 base, so
        # `set(new) - set(old)` could never see it and the gate-5 scan could
        # not raise it -- the same blind spot that hid the Korean-script
        # sibling earlier the same day. It was rendering with supply
        # 154,785 the whole time.
        assert n == 275

    def test_review_markers_still_three(self, raw):
        """This PR must not disturb the XRP NFT / XRP+ / XRP3D hold."""
        marked = sorted(v.get("currency_display") for v in raw.values()
                        if isinstance(v, dict) and v.get(tl.REVIEW_MARKER))
        assert marked == ["XRP NFT", "XRP+", "XRP3D"]

    def test_none_of_the_nine_is_a_lookalike(self, raw):
        """SCPO / CERT / XAGa contain no curated ticker, so rendering was the
        DEFAULT for all 9 -- the reason each needed an explicit decision."""
        for v in entries(raw, issuer=SCPO_ISSUER):
            assert tl.lookalike_asset(v) is None
        assert tl.lookalike_asset(entries(raw, display="CERT")[0]) is None

    def test_every_suppressed_entry_has_a_reason_string(self, raw):
        for v in raw.values():
            if isinstance(v, dict) and v.get("suppressed"):
                assert isinstance(v["suppressed"], str) and v["suppressed"]
