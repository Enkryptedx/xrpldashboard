"""Single definition of the XRPL amendment activation bar.

WHY THIS MODULE EXISTS
----------------------
rippled stores an 80% *line* and then requires STRICTLY MORE than it. Those
are two different numbers and we published the wrong one for 54 days.

From rippled 3.2.0:

  include/xrpl/protocol/SystemParameters.h:59
      constexpr std::ratio<80, 100> kAmendmentMajorityCalcThreshold;

  src/xrpld/app/misc/detail/AmendmentTable.cpp  (AmendmentSet)
      threshold_ = std::max(1L, static_cast<long>(
          (trustedValidations_ * kAmendmentMajorityCalcThreshold.num) /
          kAmendmentMajorityCalcThreshold.den));
      ...
      // One validator is an exception, otherwise it is not possible
      // to gain majority.
      if (trustedValidations_ == 1)
          return it->second >= threshold_;
      return it->second > threshold_;

So with 35 trusted validations the line is (35*80)/100 = 28 (integer
division), and an amendment needs 29 yes votes. 28 does NOT pass -- confirmed
on-ledger by PermissionDelegationV1_1, which sat at 28/35 and LOST its
majority at flag ledger 107181569 on 2026-09-23.

THE ONE-VALIDATOR EXCEPTION IS REAL, NOT THEORETICAL PEDANTRY
-------------------------------------------------------------
`votes_needed = threshold + 1` is wrong when trustedValidations == 1: the
line is max(1, 0) = 1 and rippled switches to `>=`, so one vote is enough.
A naive +1 would claim 2 of 1 are needed, which is unreachable. Our UNL is
35 so it never fires today, but this module is the place the rule is written
down, and a hardcoded +1 would be a landmine for anyone testing against a
single-validator network.

WHAT IS SIGNED VS WHAT IS DERIVED
---------------------------------
The signed leaf records rippled's RAW `threshold` (the 80% line) because
that is the observed protocol value. `votes_needed` is DERIVED from it at
display time. Anchored leaves are never rewritten -- the 54 leaves dated
2026-09-22..2026-11-15 keep their raw 28 and still verify byte-for-byte;
the surfaces that render them now derive and label the needed count, and
carry a note saying so.
"""

from __future__ import annotations

# rippled's kAmendmentMajorityCalcThreshold, as a ratio over the validations
# it actually heard. Integer division, matching the C++.
MAJORITY_NUM = 80
MAJORITY_DEN = 100


def eighty_percent_line(validations):
    """rippled's `threshold_`: max(1, (validations*80)/100), integer division.

    Returns None when validations is missing/unusable, so callers render a
    dash rather than inventing a bar.
    """
    try:
        v = int(validations)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    return max(1, (v * MAJORITY_NUM) // MAJORITY_DEN)


def votes_needed(threshold, validations):
    """Yes votes required to GAIN majority, guarded for the 1-validator case.

    `threshold` is rippled's raw 80% line as recorded in the leaf. We take it
    as given rather than recomputing, so a leaf whose recorded line disagrees
    with our arithmetic is reported as rippled saw it, not as we would.
    """
    try:
        thr = int(threshold)
    except (TypeError, ValueError):
        return None
    try:
        v = int(validations)
    except (TypeError, ValueError):
        v = None
    # rippled: `>=` with a single trusted validation, `>` otherwise.
    if v == 1:
        return thr
    return thr + 1


def needed_display(threshold, validations):
    """"29/35" -- the activation bar, not the 80% line. None if unknown."""
    need = votes_needed(threshold, validations)
    if need is None or validations in (None, ""):
        return None
    return f"{need}/{validations}"


def correction_note(threshold, validations):
    """The reader-facing note for a leaf that recorded only the 80% line.

    Templated from the row's OWN values -- never a hardcoded 28/35, because
    `validations` varies per amendment (the 2026-09-22 leaf carries both a 28
    and a 29 line) and a fixed sentence would misreport those rows.
    """
    need = votes_needed(threshold, validations)
    if need is None or threshold is None or validations in (None, ""):
        return None
    return (
        f"The signed snapshot for this date recorded the 80% line "
        f"({threshold} of {validations}); activation requires more than 80%, "
        f"so {need} are needed. The leaf is unchanged and still verifies."
    )
