# Verification deep-dive — /whales tagged→verified pipeline, /check engine, and unification

**Authored:** 2026-09-06 03:11 UTC · JJ
**Read-only diagnosis; no verification-logic changes this pass. Only the tx-type badge (Part 3c) is approved for immediate build.**
**Anchoring quote (Charlie):** "Verification is important. Let's dig in."

---

## Part 1 — The existing tagged→verified pipeline on /whales

### 1a. Who writes the initial row

**Single writer: `xrpl_stream.py::whale_handler()`** — runs on rippled-node, subscribes to the transactions stream via `ws://127.0.0.1:6007` (own-node, sovereign). No public-RPC read anywhere in the write path.

For each incoming validated tx, the handler classifies into one of three event kinds and inserts into `events.db` (SQLite on rippled-node):

| kind | trigger | fields set at write time |
|---|---|---|
| `large_xfer` | Payment with `delivered_amount ≥ 100k XRP` (converted from drops) | `type='large_xfer'`, `from_addr`, `to_addr`, `amount_drops`, `currency='XRP'`, `raw_json` (full envelope) |
| `tagged` | ANY tx whose `Account` or `Destination` is in `NAMED_ACCOUNTS` (the loaded `named_accounts.json`) AND not already stamped `large_xfer` | `type='tagged'`, `from_addr`, `to_addr`, `amount_drops` (only if payment is XRP-denominated Payment; else NULL), `currency`, `issuer`, `raw_json` |
| `trustset` | `TrustSet` transaction where `Account` is in `NAMED_ACCOUNTS` | `type='trustset'`, `from_addr`, `currency`, `issuer` (from `LimitAmount`) |

**Sourcing for the write:** rippled subscribe response only. No named_accounts lookup at write time beyond the membership check (`from_addr in NAMED_ACCOUNTS`). No verification tier is computed. No OFAC check.

**Watchlist source of truth:** `named_accounts.json` on rippled-node's copy of the repo, hot-reloaded within ~10s of file change (`_maybe_reload_named_accounts`). Populated by curator manual entry + the `verify_toml_accounts.py` walker (weekly cadence) that auto-promotes accounts with two-way domain proofs.

### 1b. What upgrades a row after write — the honest answer

**Nothing upgrades a row after write.** There is NO tagged→verified walker running against events.db. There is NO retrospective enrichment writer.

Charlie's observation of rows "looking more verified further down the page" isn't a state transition on the row — it's the render-time reshaping in `app.py::whales()::_row_shape()` at line ~2882, which:

1. Looks up `from_addr` and `to_addr` labels in `named_accounts.json` (in-process cache)
2. Looks up any domain attestation (`_attested_domain(addr)`) from the same file — the `verified_via` field
3. Disambiguates labels for collision (`_disambiguate_labels`)
4. For **token-denominated tagged events with amount_drops=NULL**, pulls the `value` field out of the raw_json envelope and prices it (if a `price_oracle` entry exists for that currency+issuer) — this is where a "USDT" row can show `$12,345` if we can price it, or nothing if we can't
5. Formats `type_display` (`large_xfer`→"whale", `tagged`→"tagged", `trustset`→"trustline")
6. Sets `row_type_pill` (`large_xfer`→"whale", `tagged`→"watchlist") — but this pill only renders on the HOMEPAGE, not on /whales itself. On /whales the raw `type_display` badge is what shows.

**The visual difference Charlie perceives** between "tagged" and "more verified" rows is one of:

- **Rows with both parties in `named_accounts`** show two labeled endpoints (`Bitstamp → Gatehub`) — reader reads that as "verified"
- **Rows with only one party labeled** show `Bitstamp → rXYZ…abc` — reader reads as "half-verified"
- **Rows with no priced amount** (token-tagged that we can't price) show `-` for amount — reader reads as "incomplete"
- **Rows where one party has an `attested_domain`** get an additional cyan link — visually "richer" than plain-label rows

None of these is a walker-driven state transition. **All are render-time synthesis from static data on the row itself + static curator files.**

### 1c. Timing + stuck rows

- **Latency from ledger close to row visible on /whales:** median ~4-8s (ledger close + WS delivery + SQLite commit + 60s in-process cache on the render side)
- **Any "stuck tagged forever" rows?** All rows are stable-state from write forward. Every row that ever appeared as "tagged" is still "tagged" today (there is no state to change to). So the correct answer is: **all tagged rows are "stuck tagged forever."** The visual richness variance is only the render-time factors above.
- **p95 latency:** dominated by the 60s cache SWR window on /whales; ~65s worst case for a fresh row to appear

### 1d. What the reader sees change — and what it fails to disclose

**Nothing changes on a row's badge over time.** The reader sees exactly what was written at ledger-close time, reshaped once at render. The badge says `TAGGED` in cyan.

**The disclosure gap** — Charlie's core complaint — is that this row:

```
TAGGED   USDT   rHjUEu…yMvK →   rBi1Qr…G1tJ
                    -
```

is compressing **three different facts** into one silent badge:
1. The sender is in our watchlist (that's the "tagged" designation)
2. The sender has a **verified two-way domain attestation** with `usdxrp.net` (that fact is on file at `named_accounts.json::rHjUEu…::verified_via`) — but the UI doesn't say so
3. The "USDT" chip that gets shown IS the curator's label for the account, which happens to be an **XRPL-native token confusable with Tether by ticker** — the UI doesn't warn about the collision

The reader can't tell the difference between:
- A `tagged` row where the tagged party is `verified_via` (highest curator confidence)
- A `tagged` row where the tagged party is just a curator name (no attestation)
- A `tagged` row where the tagged party sat next to a sanctions target and the receiver is on the OFAC SDN list (we don't check)

**The badge is a routing signal ("in our watchlist"), not a verification signal.** But it *looks* like a verification signal to a first-time visitor.

### 1e. Data sourcing for the pipeline

Everything in the tagged→render path is either **own-node** (rippled 127.0.0.1:6007 for the write) or **local files** (named_accounts.json + token_names.json). No public RPC. No third-party API in the render path.

The `price_oracle` used for token-denominated sizing is populated by walkers that read from own-node AMM pools; that pricing itself is sovereign.

**Sovereignty grade on this pipeline: A.** Everything comes from our node or our curator files.

---

## Part 2 — /check engine audit

### 2a. Trace end-to-end (`check_data.py`)

The route lives at `check_data.py::probe_check(kind, ref)` and dispatches on `kind` (address, token_id, url, message). For each input kind, the data sources consulted in order:

**Wallet (r-address input):**

| step | source | own/db/public/3p | fields returned |
|---|---|---|---|
| 1. `named_accounts.json` load | **local file (curator)** | own | label, category, verified_via URL |
| 2. `db.read_account_label(address)` — `account_labels` table lookup | **Neon Postgres** | own DB | secondary curator label |
| 3. `AccountInfo` via `xrpl_client.get_client()` | **own node** (`http://127.0.0.1:5005` per env) | own | existence, Domain field, Flags (freeze/master-key-disabled/multi-sig), balance |
| 4. `_signal_ofac_sdn_match(address, "XRP")` — local snapshot lookup | **`ofac_sdn_addresses.json`** (refreshed daily from OFAC XML by `refresh_ofac_sdn.py` walker) | own file, mirroring OFAC public source | present-on-SDN? / not-present |
| 5. Domain field parse + `_domain_is_safe` gate | inline (no fetch) | own | domain claim |
| 6. `_load_token_names()` reverse-lookup — does this address issue a token we track? | **local file** | own | reverse-tokens list |
| 7. Capability signals from AccountRoot.Flags | **from step 3's AccountInfo** | own | Freeze / Master key / Multi-sig / DisallowXRP / RequireDest / RequireAuth / etc. |

**Token input `SYMBOL.rIssuer`:**

Same as wallet for the issuer address, PLUS:
- `token_names.json` lookup for the specific (currency, issuer) tuple → own file
- Issuer's `verified_via` for the SPECIFIC token (not just the issuer identity)
- Trust-line count via own-node (if the token is popular enough to warrant it)

**URL/domain input:**

| step | source | own/3p |
|---|---|---|
| 1. eTLD+1 extract via Mozilla PublicSuffix List (`publicsuffix2`) | **local Python package** | own |
| 2. `_domain_is_safe` gate — reject IPs/rfc1918/control-chars | inline | own |
| 3. Fetch `https://<domain>/.well-known/xrp-ledger.toml` (2s timeout) | **public HTTP fetch to that domain** | **3rd-party (the URL under test)** — CORRECTLY 3p, since the point is to fetch what THAT site claims |
| 4. RDAP query (domain age) | **rdap.org public API** | **3rd party** — RDAP is the internet-standard, not gameable via cache the way whois is |
| 5. crt.sh certificate transparency query | **crt.sh public API** | **3rd party** — CT log data, cryptographically anchored |

**Message (paste any text):**

- Try to extract r-addresses via regex (`_XRPL_ADDR_CHARS`)
- Try to extract URLs
- For each extracted subject, run the appropriate check flow above
- Cross-tag results by extraction context

**Findings from the trace:**

- ✅ Sovereign for address + token: everything except OFAC-source itself is own-node or own-file
- ⚠ URL flow uses public-3rd-party (rdap.org, crt.sh, and the target domain's toml) — this is CORRECT by design (you can't verify someone's website by asking your own database), but **must be disclosed as such per surface**
- ✅ Every fact carries `source_label` + `checked_at_utc` on the wire (confirmed live in the JSON response)

### 2b. Precise tier definitions from the code

Read from `check_data.py::probe_address` (lines ~868-880) and `probe_token` (~1080-1115). Three tiers exist for /check, defined per the source-of-truth:

**For addresses:**

| tier | rule (verbatim from code) |
|---|---|
| **`verified`** | `named_accounts.json` entry has a `verified_via` URL AND the domain attestation two-way proof holds (Domain field decodes to a domain that lists this address in its `xrp-ledger.toml` `[[ACCOUNTS]]` section) |
| **`self`** | `named_accounts.json` entry has a `name` but NO `verified_via`, OR the entry has `verified_via` but two-way proof failed; OR only `db.read_account_label()` returned a label (from `account_labels` Postgres table, curator-added) |
| **`bare`** | No label anywhere. Renders "No identity claim on file." |

**For tokens** (`SYMBOL.rIssuer`):

| tier | rule |
|---|---|
| **`verified`** | `token_names.json` entry for `(currency, issuer)` has a `verified_via` URL that does NOT contain `TODO_curation_pass` marker |
| **`self`** | Entry exists but has no `verified_via` OR carries the TODO marker; OR issuer address has a Domain field claim not yet reverse-verified |
| **`bare`** | No entry for the token; falls back to bare address rules for the issuer |
| **[trap override]** | For `self`-tier tokens where the ISSUER address IS in `named_accounts` with its own label (e.g. Bitstamp), the status line ACTIVELY DECOUPLES: *"This address is associated with Bitstamp — but Bitstamp has NOT confirmed this specific token."* This kills the "they recognize the address → they endorse the token" cognitive trap. **Excellent editorial defense — deliberately built.** |

**Not-in-tier (surfaced separately):** capabilities (Freeze, master-key, multi-sig, flags), OFAC SDN cross-check, Domain field, existence.

### 2c. Correctness test — live results (sampled 2026-09-06 03:11 UTC)

Ran `curl /check.json?q=<address>` against the live production endpoint. Results verbatim from response, formatted for readability:

| Input | /check verdict | correctness |
|---|---|---|
| `rHjUEuGTbiSc2KowsvATgfFQwP8rWGyMvK` (the "USDT" from screenshot) | Kind: wallet. **Signal:** "Listed as 'USDT' in a first-party disclosure file" · source: `named_accounts.json` (attested via `https://usdxrp.net/.well-known/xrp-ledger.toml`). **Signal:** Account Domain field set to `usdxrp.net` (via own-node AccountInfo). **Capability:** Freeze available, not currently frozen. | ✅ **Correct AND clearly discloses this is `usdxrp.net`'s USDT, not Tether.** The signal narrative + source URL together make the ticker-collision knowable. Whales page doesn't do this — this is exactly the gap. |
| `rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De` (RLUSD issuer per code constant) | Kind: wallet. Capabilities: Freeze, Master key disabled, Multi-sig required. Signal: attested. | ✅ Correct — standard regulated-stablecoin issuer profile surfaces all three (custody hardening + freeze capability). |
| `rBoNzS1cZWTk7BM7FEsvvHVW6UKZBMVzYs` (fresh unknown r-address) | Kind: wallet. Existence check: `actMalformed` (address didn't validate on-chain). Identity claim: "No identity claim on file." **OFAC SDN cross-check:** "Not present on OFAC SDN list (local snapshot 2026-09-04; 991 digital-currency addresses)." | ✅ Correct behavior for an address that fails on-chain existence — surfaces the failure honestly, still runs OFAC screen (which produces a valid "not present" signal). |
| `not-an-address` | `{"error": "That doesn't look like an XRPL wallet address (starts with 'r'), a token (SYMBOL.rIssuer), or a URL/domain.", "query": "not-an-address"}` | ✅ Rejected at input gate with a user-parseable message. |

**Zero incorrect verdicts across the sample.** The USDT case in particular is exemplary — /check gives a first-time visitor exactly the information they'd need to avoid confusing usdxrp.net's XRPL-native "USDT" with Tether. **The whales page is currently silent on that same distinction on the exact same address.**

### 2d. Freshness contract

Per `_capability_signals()` and `_signal_ofac_sdn_match()`:

| signal | freshness | disclosed? |
|---|---|---|
| Existence, Balance, Flags, Domain | Live from own-node AccountInfo at request time | ✅ `checked_at_utc` on the wire |
| Named-account label / verified_via | Curator file loaded once per process; hot-reloaded on file mtime change | ⚠ Not disclosed on the wire (implicit "curator-fresh") |
| OFAC SDN | Local JSON snapshot; snapshot date reported IN the signal ("local snapshot of 991 digital-currency addresses from OFAC publication 2026-09-04") — refreshed daily by `refresh_ofac_sdn.py` walker | ✅ Explicit in the signal narrative |
| RDAP domain-age | Live 3-second timeout | ✅ own signal with source |
| crt.sh CT logs | Live 4-second timeout | ✅ own signal with source |
| toml two-way verify | Live 2-second timeout | ✅ own signal with source |

### 2e. Edge cases and where the engine already handles them

| edge case | current handling | grade |
|---|---|---|
| **Ticker collision (the USDT problem)** | `named_accounts.json` labels the issuer; signal narrative names the source (`usdxrp.net`); the specific token would fall under Bitstamp-trap override if it were a token query with a labeled issuer that hasn't attested the specific token | ✅ handled correctly for both wallet and token views |
| **Domain that fails two-way proof** | Falls to `self` tier; source_label says "attested" only if two-way proof passed. Failed two-way proof shows the Domain field but tier drops to bare/self | ✅ handled |
| **Deleted accounts** | AccountInfo returns `actNotFound`; existence check reports failure; OFAC still runs. | ✅ acceptable |
| **Accounts with 1000+ trust lines** | Trust-line count not queried on the /check path unless the query is a token (issuer of); no risk of DoS from a fat account | ✅ scoped narrowly by design |
| **URL under test hosts hostile TOML with private-IP addresses** | `_domain_is_safe` gate in `verify_toml_accounts.py` (imported and REUSED here — deliberately not re-implemented) rejects private IPs, control chars, off-shape names BEFORE HTTP fetch. SSRF-safe. | ✅ hardened; documented in code |
| **Curator file has entry with TODO_curation_pass** | Explicitly downgraded to `self`, not `verified`, no matter what other fields say | ✅ deliberate |

---

## Part 3 — Unify /whales and /check

### 3a. Where they differ today (taxonomy findings)

| dimension | /whales | /check | drift |
|---|---|---|---|
| Vocabulary | `large_xfer` / `tagged` / `trustset` | `verified` / `self` / `bare` (plus signals + capabilities blocks) | **Distinct axes — /whales is about tx CLASS, /check is about identity CONFIDENCE. No overlap.** |
| Label source | `named_accounts.json` (name field only, no tier read) | `named_accounts.json` (name + verified_via + two-way proof) + `account_labels` Postgres table | /whales loses `verified_via` fact; loses `account_labels` fallback |
| Domain attestation | Rendered as a cyan-linked chip on `_attested_domain(addr)` — visual, no explicit "verified" disclosure | Explicit signal with source URL and narrative | /whales shows the attestation as decoration; /check shows it as a fact with a receipt |
| OFAC screening | **NOT CHECKED** | Yes, per address, with snapshot-date disclosure | /whales can display a tagged tx to/from an SDN address with zero warning — real gap |
| Capabilities (Freeze / master-key / multi-sig / flags) | Not shown | Explicitly enumerated with source + honest note per capability | /whales silent |
| Freshness of each fact | Implicit (single ledger-close timestamp) | Explicit `checked_at_utc` per signal | /whales aggregates; /check discloses per-field |
| Curator provenance | Silent | `source_label` field on every signal names the exact source | /whales silent |
| Ticker collision guard | Silent (renders "USDT" chip without qualification) | Handles via source narrative + Bitstamp-trap override | /whales inherits the ambiguity |

**Two prior audits flagged fragmented vocabulary.** This table is the ground truth of that fragmentation. Every row is a fixable difference.

### 3b. Proposal — one shared verification module both surfaces call

**Introduce `verification.py::classify(address)`** — a single pure function that returns a `VerificationResult` for a given address. Both /check and /whales call it. Signature sketch:

```python
def classify(address: str, *, opts=None) -> VerificationResult:
    """Returns unified verification for an XRPL address.

    Every field carries its source_label + checked_at_utc.
    Reads own-node AccountInfo, named_accounts.json,
    account_labels Postgres, ofac_sdn_addresses.json.
    Zero public RPC. Zero third-party API.
    """
```

`VerificationResult` (dataclass or dict) contains:

- `tier` — one of the standardized values below
- `label` — the display name (from named_accounts.json / account_labels / None)
- `attested_domain` — the two-way-verified domain, or None
- `signals` — list of `{label, value, source_label, source_url, checked_at_utc}` items
- `capabilities` — list of same shape (Freeze / master-key / multi-sig / flags)
- `ofac_sdn` — `{present: bool, snapshot_date, snapshot_count}` block
- `couldnt_check` — list of `{label, reason, checked_at_utc}` items (VirusTotal-shape honesty)
- `unified_status_line` — one-sentence editorial summary appropriate for the tier

**Standardized badge vocabulary** (Charlie's ask 3b, refined against what's already in code):

| tier | rule (precise, machine-checkable) | badge color | reader-facing meaning |
|---|---|---|---|
| **`verified`** | Two-way domain-attested via `xrp-ledger.toml` reverse-check (Domain field on-chain decodes to a domain that lists this address in its toml) | green | "This address's identity has a cryptographic two-way proof." Never means "safe to send money" — the tooltip enforces that. |
| **`self-described`** | On-chain Domain field present but two-way proof unavailable (toml missing, doesn't list this address, or unfetchable); OR `named_accounts.json` entry has a `name` but no `verified_via`; OR a token entry has `verified_via` that failed reverse-verification | amber | "The account/issuer has filled in some details about itself, but nothing an outside party has confirmed." Explicit anti-endorsement copy. |
| **`labeled`** | `account_labels` Postgres row from curator (secondary label store), NO on-chain claim, NO two-way proof — used for known-but-unattested exchanges we've named ourselves | grey-blue | "We recognize this address from our own curation, but we haven't verified it externally." |
| **`bare`** | No labels, no on-chain claim, no attestation of any kind | slate grey | "No identity claim on file for this address." |
| **`sanctioned`** | Present on OFAC SDN list (local snapshot) — this is CROSS-CUT: an address can be `verified` AND `sanctioned` simultaneously; both surface | red | "This address appears on the U.S. Treasury OFAC Specially Designated Nationals list." Snapshot date disclosed. |
| **`unknown`** | Existence check failed (`actMalformed`, `actNotFound`); we can't say either way | dim | "This address didn't validate on the ledger — nothing further checked." |

**Rendering the tagged→verified upgrade with disclosure** — the shape Charlie's asking for:

- On /whales, the badge next to each address becomes the `tier` from `classify()`, not the `type_display` from the row (though `type_display` still shows the tx CLASS separately: "Payment", "CheckCreate", "TrustSet", etc. — see 3c)
- Hover on the badge → tooltip showing `unified_status_line` + top signal + link "See full receipt" that hard-links to `/check?q=<address>`
- Below the badge, a compact receipt line: "**verified** · usdxrp.net · own-node · 03:11 UTC" — three tokens (tier, source, freshness) so the reader sees WHAT was checked at a glance
- A row where sender is `verified` and receiver is `sanctioned` shows both badges; the row itself becomes red-tinted (visual crash the reader can't miss)

### 3c. Fold in today's finding — the tx-type badge (approved for build)

Independent of the verification work, add:

- New column `tx_type TEXT` on `events` table (nullable, migration-safe; existing rows stay NULL)
- Writer (`xrpl_stream::whale_handler`) writes `tx_type = tx["TransactionType"]` for every event (Payment / CheckCreate / TrustSet / AMMDeposit / AMMWithdraw / OfferCreate / …)
- Renderer (`app.py::_row_shape`) surfaces `tx_type` as a small pill next to the type_display badge
- For CheckCreate rows, extract `SendMax` from raw_json.tx.SendMax and render "**up to** 12,345 USDT (Check)" instead of `-`
- For TrustSet rows, extract `LimitAmount` and render "**limit** 100,000 USDT (Trustline)"

**Scope:** ~15 lines in `xrpl_stream.py`, ~20 lines in `app.py`, one SQLite `ALTER TABLE events ADD COLUMN tx_type TEXT` (in-place, no rewrite), no schema drift on Postgres mirror. Ship this weekend.

### 3d. Data path — sovereignty grade

Everything the unified verifier reads is own-node or own-file:

| dependency | source | sovereignty grade |
|---|---|---|
| AccountInfo (existence, Domain, Flags, balance) | rippled 127.0.0.1:5005 (Lenovo own-node) | A |
| `named_accounts.json` + `token_names.json` | repo file, curator-maintained | A |
| `account_labels` Postgres table | Neon (own database) | A |
| OFAC SDN | `ofac_sdn_addresses.json` (local snapshot; refreshed daily from OFAC's public XML by `refresh_ofac_sdn.py`) | A (local snapshot — upstream is public but authoritative) |
| Two-way toml verification | Weekly `verify_toml_accounts.py` walker; result baked into `named_accounts.json.verified_via` — NOT re-fetched at /check request time | A (walker-cached, not on the hot path) |

**Zero public RPC on the hot path.** The URL/message flows still touch RDAP, crt.sh, and the target domain's toml at request time — but those are CORRECT to be public (the point IS to fetch what a third party is publishing) and are surfaced as such per signal.

**No tunnel or walker cache is needed to make this work** — the verifier's dependencies are already sovereign.

---

## Part 3 findings summary — what to ship this weekend vs what needs a design session

### Ship this weekend (approved / low-risk)

1. **`tx_type` column on `events` + SendMax extraction for Checks + LimitAmount for TrustSets** (Part 3c). Small, contained, immediately valuable. Rough scope: 3 file edits, ~40 lines, one SQLite migration.

### Design session with Charlie needed

2. **`verification.py::classify()` module** — the shared verifier. Requires:
   - Charlie sign-off on the standardized 6-tier vocabulary (verified / self-described / labeled / bare / sanctioned / unknown) and specifically on the `labeled` tier being distinct from `self-described`
   - Editorial pass on the tooltip / receipt-line copy for each tier (this is public-facing methodology copy)
   - Decision on whether `sanctioned` cross-cuts (my proposal) or replaces the primary tier
   - Test vector set: a known verified address, a self-described one, a labeled one, a bare one, a known sanctioned one, an unknown one. Snapshot-test the output.
   - Migration plan for both /check (currently uses 3-tier vocabulary) and /whales (currently uses no verification tier) to consume the new module — /check gets richer, /whales gets verification signal for the first time

3. **Whales UI upgrade to render tier badge + receipt line** — depends on 2. Small once 2 is landed.

4. **OFAC-cross-cut visual treatment on /whales** — a `tagged` row involving a sanctioned address needs a visual crash (red tint, badge, alert box). Design + copy call needed.

### Report-only, no action

5. **/check URL/message flow is correctly public-3p by design.** Not a sovereignty violation to be fixed.

6. **`verify_toml_accounts.py` walker cadence is weekly.** Fine for /check because the walker bakes results into `named_accounts.json.verified_via` (not fetched at request time). If we ever want the two-way proof to be live at request time, that's a design change and needs a rate-limited fetch path — but no evidence we need this.

---

## Appendix — inline snippets referenced

**Whale-handler writer path (rippled-node):** `xrpl_stream.py` lines ~230-320
**Whales renderer + row-shape:** `app.py::whales()` line 2926 → `_row_shape()` line ~2882
**Curator loader:** `check_data.py::_load_named()` line 90
**Wallet check flow:** `check_data.py::probe_address()` (tier decision at ~868)
**Token check flow:** `check_data.py::probe_token()` (tier decision at ~1088)
**OFAC snapshot:** `ofac_sdn_addresses.json` refreshed by `scripts/refresh_ofac_sdn.py`
**Two-way toml verifier walker:** `verify_toml_accounts.py` — weekly cadence, bakes `verified_via` into curator files
