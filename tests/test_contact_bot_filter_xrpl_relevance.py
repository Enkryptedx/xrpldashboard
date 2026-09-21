"""XRPL-relevance drop filter for /contact (Charlie ruling 2026-09-21,
Monday build item 6 — cheap second layer while Turnstile awaits its
site key).

Two assertions:

1. **Real XRPL-topic submissions pass** — every plausible on-topic
   message shape (XRPL, XRP variants, RLUSD, AMM, MPT, NFT, trustline,
   issuer, credential, /check, dashboard, ledger, ripple, wallet,
   stablecoin, anchor, amendment, signed snapshot) hits the filter's
   allow-path.

2. **Off-topic spam campaigns drop** — the four campaign shapes
   observed in contact_inquiries (SEO backlink pitch, "improve your
   rankings", "we build websites", multilingual "hi I want to know
   your price") return (True, "xrpl_irrelevant").

3. **False-positive guards** — terms that could confuse a substring
   match ("attempt", "prompt", "empty") do NOT trigger the filter's
   on-topic path. Bare "xrp" at message-end with no trailing punct
   DOES pass because the blob has a trailing space stitched in.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _run():
    import app as app_module
    fn = app_module._is_bot_contact_submission

    UA_REAL = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Safari/605.1.15"

    on_topic = [
        ("XRPL question", "I have a question about XRPL AMMs and RLUSD."),
        ("XRP standalone", "How do I check my XRP balance?"),
        ("XRP end-of-message", "just want info on xrp"),
        ("Ripple named", "Is this affiliated with Ripple?"),
        ("MPT topic", "How does MPT metadata surface on /check?"),
        ("NFT topic", "Any coverage of NFT taxons?"),
        ("Trustline", "Getting a trustline error, please help."),
        ("Issuer topic", "How do you decide canonical issuer?"),
        ("Token topic", "Curious about your token registry."),
        ("Credential", "Verifiable credential path on /check.json?"),
        ("Amendment", "Question about amendment tallies."),
        ("Signed snapshot", "Verifying signed snapshot cross-check."),
        ("Ledger", "Show me the ledger index freshness."),
        ("Dashboard", "Suggestion for the dashboard homepage layout."),
        ("Wallet", "My wallet has a currency-collision I want reported."),
        ("Anchor", "Question about on-ledger anchor commits."),
        ("Stablecoin", "Which stablecoin sources do you use?"),
    ]
    for label, message in on_topic:
        is_bot, sig = fn(UA_REAL, message)
        assert not is_bot, f"FALSE-POSITIVE ({label}): flagged as {sig!r} — {message!r}"
    print(f"OK  {len(on_topic)} on-topic messages passed")

    off_topic = [
        ("SEO backlink pitch",
         "Hello, I noticed your website could use more backlinks to improve rankings. "
         "We offer high-DA links starting at $50/month."),
        ("Design-agency pitch",
         "Hi, we build modern websites for your business. Interested?"),
        ("Price inquiry LT",
         "Sveiki, noreciau suzinoti jusu paslaugu kaina."),
        ("Generic hello",
         "Hi there, wanted to say hello and check in!"),
        ("SEO variation",
         "Improve your rankings and traffic with our proven SEO strategy."),
    ]
    for label, message in off_topic:
        is_bot, sig = fn(UA_REAL, message)
        assert is_bot and sig == "xrpl_irrelevant", (
            f"FAIL ({label}): expected xrpl_irrelevant drop, got is_bot={is_bot} sig={sig!r}"
        )
    print(f"OK  {len(off_topic)} off-topic messages dropped as xrpl_irrelevant")

    # False-positive guards — substrings that could look on-topic but shouldn't
    fp_guards = [
        ("attempt substring", "Just attempting to reach out about SEO."),
        ("prompt substring", "I want a prompt reply, please. -- backlink service"),
        ("empty substring", "Message is empty on purpose, just checking response."),
    ]
    for label, message in fp_guards:
        is_bot, sig = fn(UA_REAL, message)
        assert is_bot and sig == "xrpl_irrelevant", (
            f"FALSE-NEGATIVE ({label}): {message!r} passed as on-topic ({sig!r}). "
            f"'mpt' fragment must be space-anchored so it doesn't match "
            f"attempt/prompt/empty."
        )
    print(f"OK  {len(fp_guards)} false-positive guards held")

    # Email/name domain path — submitter emails from a ripple-adjacent
    # domain should pass even if message body is bare
    is_bot, sig = fn(UA_REAL, "hi", email="jane@ripple.com", name="Jane")
    assert not is_bot, (
        f"FALSE-POSITIVE: email domain @ripple.com should let a bare "
        f"'hi' through; got {sig!r}"
    )
    print("OK  email-domain hint (@ripple.com) lets bare-body submission through")

    # Prior existing signatures still fire (regression)
    is_bot, sig = fn(UA_REAL, "Hello Xrpldashboard Com Owner, ...")
    assert is_bot and sig == "seo_spam_owner", f"regression: {sig!r}"
    is_bot, sig = fn(
        "Mozilla/5.0 (Windows) AppleWebKit/537 (KHTML, like Gecko; Chrome/1)",
        "anything",
    )
    assert is_bot and sig == "unclosed_paren_ua", f"regression: {sig!r}"
    print("OK  prior signatures still fire (regression)")

    print("\nALL PASS")


def test_xrpl_relevance_filter():
    _run()


if __name__ == "__main__":
    _run()
