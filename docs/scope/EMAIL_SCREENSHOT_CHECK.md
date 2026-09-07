# Email-screenshot /check — scope doc

**Status:** scope only, per Charlie's ruling 2026-09-07. **Build after attorney review**, not before. This doc exists to sharpen the decision and lay out the privacy contract explicitly.

## What the surface would do

Extend `/check` (D4 message-triage) to accept an **image** input — a screenshot of an email — in addition to pasted text. The path:

1. User uploads image (drag+drop or file picker).
2. Server runs OCR to extract text.
3. The extracted text runs through the **same** message-triage pipeline that pasted text uses today: seed-phrase detector, scam-request patterns (seed_request, giveaway_double, fake_support_urgency, account_alert_phishing, fake_airdrop_connect_wallet, brand_impersonation), address / token / URL extraction + D1/D2/D3 per-target checks.
4. Result renders like the current message-triage output, plus an "OCR confidence" chip and the extracted-text preview (for reader debugging).

## Why now (motivation)

Two ergonomics gaps in today's paste-only surface:

1. **Phone screenshots.** A user seeing a suspicious email on their phone naturally reaches for the screenshot button. Copying text from a phone email into a paste field is friction. Screenshot upload is the native flow.
2. **Rendered-vs-source drift.** Some fraud emails render text as an image on purpose — they know common phishing filters do keyword matches on message body text. Rendering as an image bypasses those filters. OCR closes that loophole.

## What OCR CAN'T verify

Explicit list; render this on the /check page whenever a user submits an image:

- **Email headers.** The visible screenshot shows sender name + subject as the mail client rendered them; it does NOT show `From:` header source, `Return-Path`, `Received:` chain, SPF/DKIM/DMARC results, or the actual sending domain. A screenshot of "From: Coinbase Support" is not proof of a real Coinbase send.
- **Link destinations.** A screenshot may show link text like "click here to secure your account" — but the underlying `href` (the URL the user would actually navigate to) is not visible in the screenshot. Even if OCR extracts the visible text, we have no way to see where the link actually points.
- **Timestamp accuracy.** The visible timestamp is what the mail client rendered; message headers may show a different timestamp. Spoofable.
- **Attachments.** OCR sees the paperclip icon; it doesn't see the attachment content.
- **Client trust chrome.** A screenshot showing a "Verified" badge from a mail client is the mail client's judgment, not ours. Some fraud campaigns clone client-chrome UI in the message body.

The /check response should present the OCR result as **"here's what the visible text of the screenshot says"** and NEVER as **"here's what the email actually is."** Attorney's wording review.

## Threat model

- **Malicious upload.** A user could upload a file that isn't an image (crafted PDF, HTML, executable) trying to exploit our OCR pipeline. Mitigation: strict `Content-Type: image/*` gate + magic-byte check + max size 5 MB.
- **PII in screenshots.** A screenshot of an email typically contains the user's own email address (in the "To:" field), maybe their real name, and possibly account balances or transaction details. This is sensitive.
- **Deliberate malicious message content.** A screenshot may contain a real recovery phrase or private key (the user pasted their own into an email trying to save it; they screenshot it later). OCR extracts, then the seed-phrase detector should fire the same STOP warning as today's paste path.

## The never-touch-the-link rule

Any URL extracted from OCR text is treated as **display only** in the /check response. We NEVER:

- fetch the URL
- resolve DNS for the domain
- attempt any active verification of the link destination

Reason: an attacker who wants to know whether a target has read their phishing email can construct a URL that, when fetched, logs the target's IP or triggers a webhook. If we fetch links from user-submitted OCR text, we make ourselves a fetch-agent for the attacker. Never.

The response renders extracted URLs as `<code>` text with a red "do not click" chip. If the user wants to verify a link, they type the target domain into their address bar themselves — same guidance as the existing account_alert_phishing category.

## Process-in-memory privacy contract

This is the hardest sub-decision — get it right, name it explicitly, and enforce it at multiple layers.

**Contract (proposed, attorney to bless):**

- Uploaded image bytes exist in the Flask request lifetime and NOWHERE ELSE:
  - Never written to disk (no `save()` call, no tempfile).
  - Never logged (no logger line containing bytes or PII).
  - Never cached (no Redis, no PG write).
  - Never passed to a subprocess unless the subprocess itself is bound to the same in-memory-only discipline.

- OCR runs in-process (no shelling out to an external OCR service that would leak the image over the network). Options:
  - **`pytesseract`** — Python bindings for Tesseract. Image bytes are passed to the C library in-memory, no disk I/O. Preferred.
  - **`easyocr` / `paddleocr`** — heavier deps; may write model files to disk (models are pre-loaded, not per-request — separate from image data).
  - **NEVER a cloud OCR service** (Google Vision, AWS Textract, Azure Computer Vision). Image content would leave our infrastructure. Absolute bar.

- The extracted OCR text is subject to the **existing** `/check` D4 privacy contract: text is not persisted, not logged beyond a 120-char preview in the request-log line, not cached in any DB.

- The response renders the extracted text back to the user (so they can see what OCR pulled out) but the response itself is not written to any store — the current `/check` POST path already skips `_log_page_view` for message input, and this extension inherits that skip.

- Response `Cache-Control: no-store` to prevent CDN caching.

**Enforcement checkpoints:**
- unit test that verifies no `.save()` / `open(...)` write happens during a mock OCR run
- code review checklist: any new logger call or db.write_* call in the /check image path is a blocker
- annual audit line in `docs/CREDENTIALS.md` or `docs/PRIVACY.md`

## Attorney touchpoints (explicit gates)

None of the following ships until reviewed:

1. **The "we can't verify X" wording.** OCR extracts visible text; the response's disclaimer about headers, links, and trust chrome is legally consequential (misrepresentation could pull us into common-carrier or dispute-adjudication liability). Attorney reviews final wording.
2. **The response's "this looks like a scam" language.** Same wording rule as the current pattern list (2026-09-07 update): NEVER "scam" as a verdict, always "warning signs commonly used in fraud attempts."
3. **The privacy notice.** The /privacy §2b that already handles the message-triage POST needs an addition covering image upload. Attorney reviews.
4. **Retention & subpoena.** Even though the contract says "in-memory only," we should document the response to a subpoena request. Attorney rules on whether the request-log 120-char preview counts as retained user content.

## Data prerequisites

- Add `pytesseract` + `tesseract` system binary to the Render deploy (tesseract-ocr apt package). Render build size grows ~30 MB.
- No new tables. No new walkers.
- Add `PIL.Image` (already in requirements.txt as `Pillow`) for input validation.

## Rollout gates

1. **Attorney review of the wording + privacy contract.** No build starts before this.
2. Local prototype behind a feature flag (`FEATURE_EMAIL_SCREENSHOT_CHECK=1`).
3. Enforcement tests for the process-in-memory discipline.
4. Beta on a `?beta=1` query param for opt-in testers.
5. Public flip with a fresh disclosure line on `/check` page describing what OCR does and doesn't verify.

## What this scope doc IS NOT

- Not a design for the OCR pipeline code.
- Not the attorney's ruling.
- Not a promise of a delivery date.

Owner: Charlie · attorney review gates everything · JJ builds after both.
