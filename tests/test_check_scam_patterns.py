"""Test set for the /check pre-send scam-catch detectors (2026-09-07).

Two detectors in check_data.py:
  _detect_seed_phrase(text) — user pasted their OWN mnemonic
  _detect_scam_patterns(text) — inbound message hits one or more of
    seed_request / giveaway_double / fake_support_urgency

Test set is deliberately small + human-authored (10 benign + 5 scam,
per Charlie 2026-09-07). The goal is regression protection, not
statistical coverage — the pattern list is small and fits in memory.

If a false positive lands in prod, add the trigger phrase to BENIGN
here and tighten the pattern in check_data.py. If a false negative
lands in prod, add the phrase to SCAM here and broaden the pattern.
The tests are the ratchet — the patterns become the artifact.
"""
import unittest

import check_data


BENIGN = [
    "Did you see the XRP price today? Up 5% since morning.",
    "The RLUSD launch is a big deal for XRPL adoption.",
    "I moved my tokens to a new wallet yesterday, the fees are so low.",
    "Whats your favorite XRP explorer? Ive been using XRPScan.",
    "The AMM feature is finally live and I am loving the low fees.",
    "Any recommendations for XRPL cold storage hardware?",
    "The signed snapshot on xrpldashboard is a cool feature honestly.",
    "Just staked some CTF tokens for the first time this week.",
    "How do I use xrpldashboard/tokens to see the top pools by TVL?",
    "The new registry taxonomy for tokens is a great step forward.",
]

SCAM = [
    (
        "seed_request",
        "Ripple Wallet Verification: Please enter your 12-word recovery "
        "phrase below to unlock your wallet.",
    ),
    (
        "giveaway_double",
        "OFFICIAL RIPPLE GIVEAWAY: Send 100 XRP to r-address and receive "
        "200 XRP back within 5 minutes!",
    ),
    (
        "fake_support_urgency",
        "Your XRPL wallet has been compromised. Immediate action required. "
        "Click here to verify your account within 24 hours.",
    ),
    (
        "compound_urgency_and_seed",
        "Support here — your wallet will be locked in 24 hours. To secure "
        "it, verify your recovery phrase now.",
    ),
    (
        "compound_three_hits",
        "Ripple Team notice: Your account has been flagged. Enter your "
        "recovery phrase to verify, or send 10 XRP to receive 20 XRP as "
        "compensation.",
    ),
]


class ScamPatternDetectorTests(unittest.TestCase):
    def test_benign_produces_no_scam_pattern_hits(self):
        for i, msg in enumerate(BENIGN, 1):
            with self.subTest(i=i, msg=msg[:60]):
                self.assertEqual(check_data._detect_scam_patterns(msg), [],
                                 f"Benign msg {i} triggered a scam pattern")

    def test_benign_does_not_trigger_seed_detector(self):
        # None of these are 12+ short lowercase words in a row — the seed
        # detector should stay silent on ordinary chat.
        for i, msg in enumerate(BENIGN, 1):
            with self.subTest(i=i, msg=msg[:60]):
                self.assertFalse(check_data._detect_seed_phrase(msg),
                                 f"Benign msg {i} tripped the seed detector")

    def test_seed_request_scam_is_detected(self):
        _, msg = SCAM[0]
        self.assertIn("seed_request", check_data._detect_scam_patterns(msg))

    def test_giveaway_double_scam_is_detected(self):
        _, msg = SCAM[1]
        self.assertIn("giveaway_double", check_data._detect_scam_patterns(msg))

    def test_fake_support_urgency_scam_is_detected(self):
        _, msg = SCAM[2]
        self.assertIn(
            "fake_support_urgency",
            check_data._detect_scam_patterns(msg),
        )

    def test_compound_urgency_plus_seed_returns_both_categories(self):
        _, msg = SCAM[3]
        hits = set(check_data._detect_scam_patterns(msg))
        self.assertIn("seed_request", hits)
        self.assertIn("fake_support_urgency", hits)

    def test_compound_three_categories_returns_all_three(self):
        _, msg = SCAM[4]
        hits = set(check_data._detect_scam_patterns(msg))
        self.assertEqual(
            hits,
            {"seed_request", "giveaway_double", "fake_support_urgency"},
        )

    def test_check_message_surfaces_scam_hits_and_overrides_summary(self):
        _, msg = SCAM[0]
        result = check_data.check_message(msg)
        self.assertEqual(result["scam_patterns_matched"], ["seed_request"])
        self.assertIn("scam patterns", result["status_line"].lower())

    def test_pasted_seed_phrase_still_wins_over_scam_pattern_render(self):
        # 12-word mnemonic-shape input should trigger the seed detector's
        # STOP wording, which takes precedence over the scam-pattern
        # summary override. Both fields still surface in the payload so
        # the caller can render both if desired.
        seed = ("abandon ability able about above absent absorb "
                "abstract absurd abuse access accident")
        result = check_data.check_message(seed)
        self.assertTrue(result["seed_phrase_detected"])
        self.assertIn("STOP", result["status_line"])


if __name__ == "__main__":
    unittest.main()
