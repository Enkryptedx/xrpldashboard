"""Brand protection is a declaration, not a side effect of a citation.

THE BUG THIS REMOVES. load_verified_brands() used to decide protection by a
substring test on verified_via:

    if not any(t in ver for t in ("xrp-ledger.toml", "xrpscan@")): continue

So writing a citation onto an entry SILENTLY enrolled its currency_hex as a
protected brand. Measured 2026-11-22: of the 73 families, only 3 were
deliberate (the xrpscan@brand-protected sentinels MIDAS, MTBILL, SOIL); 63
owed their existence purely to a .../xrp-ledger.toml curation citation --
ONDO's sentinel among them -- and 7 to an xrpscan@<other> provenance note.
All but 3 were enrolled as a side effect of describing provenance.

It also blocked ordinary labelling: six issuer-level passes could not be
tiered, because the verified_via their label needs would have added six
protected families (73 -> 79).

Enrolment is now `brand_protected: true` plus `brand_allowlist`.
"""

import json
import os

import pytest

import rank_amms
import token_label as TL

NAMES_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                          "token_names.json")
SOIL_HEX = "534F494C00000000000000000000000000000000"
SOIL_ISSUER = "rfmS3zqrQrka8wVyhXifEeyTwe8AMz2Yhw"


@pytest.fixture(scope="module")
def names():
    with open(NAMES_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def test_family_census_is_exact():
    """Still 73. The migration changed the MECHANISM, not the outcome."""
    assert len(rank_amms.load_verified_brands()) == 73


def test_every_family_is_declared(names):
    """No family may exist implicitly. Exactly 73 carrier records declare
    brand_protected, and every family in the loader traces to one."""
    carriers = [v for v in names.values()
                if isinstance(v, dict) and v.get("brand_protected")]
    assert len(carriers) == 73, len(carriers)
    declared = {(v.get("currency_hex") or "").strip() for v in carriers}
    assert declared == set(rank_amms.load_verified_brands())


def test_a_citation_alone_no_longer_enrols(names):
    """The regression guard. An entry citing a TOML but NOT declaring
    brand_protected must not appear as a family -- that is exactly what
    used to happen, and what blocked the six labels."""
    fam = rank_amms.load_verified_brands()
    cited_only = [v for v in names.values()
                  if isinstance(v, dict) and not v.get("brand_protected")
                  and "xrp-ledger.toml" in (v.get("verified_via") or "")]
    assert cited_only, "no cited-but-undeclared entries left to prove this"
    for v in cited_only:
        hexc = (v.get("currency_hex") or "").strip()
        if hexc in fam:
            # allowed only when a DIFFERENT record declares that hex
            assert any(o.get("brand_protected") and
                       (o.get("currency_hex") or "").strip() == hexc
                       for o in names.values() if isinstance(o, dict)), hexc


# ── the two kinds of sentinel ───────────────────────────────────────
def test_canonical_soil_is_allowlisted_and_never_warns(names):
    """SOIL's sentinel says in prose 'canonical issuer is the Axelar
    bridge; any OTHER issuer is a spoof', but encoded an EMPTY allowlist,
    which means nobody is legitimate. An ungated check would therefore have
    warned on the canonical issuer itself -- 719,697 outstanding, 16
    trustlines, two-way verified against axelar.foundation. One false
    positive, zero true positives. The allowlist now names it."""
    fam = rank_amms.load_verified_brands()
    assert SOIL_ISSUER in fam[SOIL_HEX]
    real = [v for v in names.values() if isinstance(v, dict)
            and v.get("currency_hex") == SOIL_HEX
            and v.get("issuer") == SOIL_ISSUER]
    assert len(real) == 1
    label = TL.label_for(real[0]) or ""
    assert "\u26a0" not in label, label
    assert "not " not in label, label


def test_ondo_style_sentinels_keep_an_empty_allowlist(names):
    """The other kind: no canonical XRPL issuer EXISTS (ONDO is
    Ethereum-only), so an empty allowlist is correct and every issuer of
    that code should fail membership. Distinguishing the two kinds is the
    point -- SOIL was mis-encoded as this one."""
    fam = rank_amms.load_verified_brands()
    for hexc in ("4F4E444F00000000000000000000000000000000",   # ONDO
                 "4D49444153000000000000000000000000000000",   # MIDAS
                 "4D5442494C4C0000000000000000000000000000"):  # MTBILL
        assert fam[hexc] == set(), (hexc, fam[hexc])


def test_soil_sentinel_records_which_kind_it_is(names):
    note = (names.get(SOIL_HEX + ":") or {}).get("brand_note") or ""
    assert "canonical issuer" in note, note
    assert "ONDO" in note, note
