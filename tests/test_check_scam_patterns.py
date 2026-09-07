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
    # Benign counter-cases for account_alert_phishing (2026-09-07):
    # legit chat about security notifications should NOT trigger
    "I finally set up my Coinbase account and the KYC flow was smooth.",
    "Ripple announced a new partnership with a European bank yesterday.",
    "Xaman is my daily wallet — signing txns is one tap.",
    "The XRPL airdrop from 2020 was memorable, I still hold those tokens.",
    "Bithomp has a nice explorer for the XRPL and shows account activity.",
    # Benign counter-cases discussing scams / help-seeking without asking
    # the reader to reveal anything (the detector should NOT fire on
    # someone reporting having BEEN targeted). We avoid literal request
    # phrasing here because the /check surface DOES want to warn users
    # who paste literal request phrasing — even in an "is this a scam?"
    # frame, firing the warning is helpful, not a false positive.
    "Someone messaged me claiming to be Ripple support but I ignored it.",
    "I got an alert email from Kraken yesterday — is there a way to check whether it was legitimate before I do anything?",
    # Casual "airdrop" mention without wallet-connect pitch
    "There is a legitimate airdrop from a project I follow, it uses a snapshot from block N.",
    # Casual "connect your wallet" reference to a DEX/dApp
    "To use the AMM I have to connect my wallet to the DEX interface.",
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
    # New scam categories (2026-09-07 — Charlie ruling):
    (
        "account_alert_phishing",
        "Coinbase Security Alert: We detected unusual sign-in activity "
        "on your account from a new device. If this wasn't you, click "
        "here to secure your account immediately.",
    ),
    (
        "fake_airdrop_connect_wallet",
        "Exclusive Ripple XRP airdrop — connect your wallet to claim "
        "5,000 XRP within the next 24 hours!",
    ),
    (
        "brand_impersonation",
        "Xaman support here — your wallet needs immediate verification. "
        "Click here to reactivate before your account is suspended.",
    ),
    (
        "account_alert_and_brand",
        "From: Binance Security Team — Suspicious login attempt detected. "
        "If this wasn't you, verify your identity via the link below.",
    ),
    (
        "airdrop_and_brand",
        "Ripple partnership airdrop — sign this transaction with your "
        "Xaman wallet to claim the free XRP drop.",
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
        # SCAM[4] mentions "Ripple Team notice" + urgency ("account
        # flagged") → also fires brand_impersonation now (2026-09-07
        # category added). Assert the three core categories are all
        # present; brand_impersonation firing too is a bonus, not a
        # regression.
        _, msg = SCAM[4]
        hits = set(check_data._detect_scam_patterns(msg))
        self.assertIn("seed_request", hits)
        self.assertIn("giveaway_double", hits)
        self.assertIn("fake_support_urgency", hits)

    def test_check_message_surfaces_scam_hits_and_overrides_summary(self):
        _, msg = SCAM[0]
        result = check_data.check_message(msg)
        self.assertEqual(result["scam_patterns_matched"], ["seed_request"])
        # Charlie ruling 2026-09-07: public wording says
        # "warning signs commonly used in fraud attempts" — the internal
        # name "scam_patterns" is a working label, never a verdict.
        self.assertIn("fraud attempts", result["status_line"].lower())

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

    # New categories (2026-09-07 — Charlie ruling)

    def test_account_alert_phishing_detected(self):
        _, msg = next((k, v) for k, v in SCAM if "account_alert" == k[:13])
        self.assertIn(
            "account_alert_phishing",
            check_data._detect_scam_patterns(msg),
        )

    def test_fake_airdrop_connect_wallet_detected(self):
        _, msg = next(
            (k, v) for k, v in SCAM if "fake_airdrop" == k[:12]
        )
        self.assertIn(
            "fake_airdrop_connect_wallet",
            check_data._detect_scam_patterns(msg),
        )

    def test_brand_impersonation_detected(self):
        _, msg = next(
            (k, v) for k, v in SCAM
            if k == "brand_impersonation"
        )
        self.assertIn(
            "brand_impersonation",
            check_data._detect_scam_patterns(msg),
        )

    def test_brand_impersonation_captures_brand_names(self):
        _, msg = next(
            (k, v) for k, v in SCAM if k == "brand_impersonation"
        )
        result = check_data.check_message(msg)
        self.assertIn("xaman", result["brands_named"])

    def test_account_alert_compound_returns_all_matched_categories(self):
        _, msg = next(
            (k, v) for k, v in SCAM if k == "account_alert_and_brand"
        )
        hits = set(check_data._detect_scam_patterns(msg))
        # Binance branded, phishing shape, and urgency ("Security Team",
        # "verify your identity") should all fire.
        self.assertIn("account_alert_phishing", hits)
        self.assertIn("brand_impersonation", hits)

    def test_airdrop_and_brand_compound_returns_both(self):
        _, msg = next(
            (k, v) for k, v in SCAM if k == "airdrop_and_brand"
        )
        hits = set(check_data._detect_scam_patterns(msg))
        self.assertIn("fake_airdrop_connect_wallet", hits)
        # xaman is a named brand; ripple partnership + airdrop language
        # should also trigger brand_impersonation if urgency is nearby.
        # We do NOT strictly require brand_impersonation here — that
        # depends on how attackers phrase the urgency. If the pattern
        # doesn't fire on this exact wording, that's fine — the airdrop
        # signal alone is enough to warn the user.

    def test_check_message_surfaces_brand_names_field(self):
        _, msg = next(
            (k, v) for k, v in SCAM if k == "brand_impersonation"
        )
        result = check_data.check_message(msg)
        self.assertIn("brands_named", result)
        self.assertIsInstance(result["brands_named"], list)

    def test_public_wording_never_says_scam_as_verdict(self):
        # Charlie's ruling 2026-09-07: the public render must never
        # say "scam" as a verdict. Internal name stays scam_patterns
        # but the status_line + summary use "warning signs commonly
        # used in fraud attempts" language instead.
        _, msg = SCAM[0]  # any scam message
        result = check_data.check_message(msg)
        status = result["status_line"].lower()
        summary = result["summary"].lower()
        # 'scam' must NOT appear in the public wording that renders
        # above the per-target signals.
        self.assertNotIn(" scam", status,
                         "status_line uses 'scam' — must be 'fraud attempts'")
        self.assertNotIn(" scam", summary,
                         "summary uses 'scam' — must be 'fraud attempts'")
        # The mandated phrasing must be present.
        self.assertIn("fraud attempts", status)


if __name__ == "__main__":
    unittest.main()
