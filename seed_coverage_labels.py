"""One-shot editorial seeder for coverage_labels — the human-readable
label + short_desc + linked_page for each XRPL vocabulary key.

Populated once before Coverage Register goes live so day-one doesn't
open with every seen type in the amber "unlabeled" state drowning the
genuinely interesting defined-but-unseen greys (Loan, LoanBroker, Vault,
Delegate, Batch et al. awaiting first-ever XRPL sighting).

Coverage: the 28 baseline types seen in Phase 1a's opening burst (from
artifacts/first_seen_baseline_2026-07-07.log) + editorial rows for the
defined-but-unseen types most likely to fire first (Loan, LoanBroker,
Vault, Delegate, Batch, XChain*, DIDSet, MPT* et al.) so their day-one
grey rows read as curated-with-expectation, not just curated-with-gap.

linked_page is nullable — omit when the type isn't yet surfaced on the
site, present when there's a real destination (e.g. AMM → /pools).

Idempotent: re-runnable via ON CONFLICT DO UPDATE.
"""

import db


# ── seen-in-baseline TRANSACTION_TYPES (fired 2026-07-07 21:58–22:04) ──
TX_LABELS = [
    ("Payment", "Payment",
     "The canonical value transfer between accounts. Pays XRP or issued "
     "tokens; supports cross-currency paths and partial payments.",
     "/tokens"),
    ("OfferCreate", "DEX Order",
     "Places a limit order on XRPL's native DEX order book. The oldest "
     "on-chain DEX (2012); still the top-throughput tx type today.",
     None),
    ("OfferCancel", "DEX Cancel",
     "Cancels a previously placed OfferCreate before it fills or expires.",
     None),
    ("AccountSet", "Account Config",
     "Sets or clears AccountRoot flags (DefaultRipple, RequireAuth, "
     "DisallowXRP, etc.) — the toggle surface for issuer behavior.",
     None),
    ("TrustSet", "Trust Line",
     "Creates, modifies, or removes a trust line to an issuer for a "
     "specific currency code. Prerequisite for holding any IOU or "
     "stablecoin on XRPL.",
     "/tokens"),
    ("NFTokenMint", "NFT Mint",
     "Creates a new NFToken under XLS-20 native NFTs.",
     "/nfts"),
    ("NFTokenCreateOffer", "NFT Offer",
     "Places a buy or sell offer for a specific NFToken.",
     "/nfts"),
    ("NFTokenAcceptOffer", "NFT Trade",
     "Accepts a matching NFT buy/sell offer, transferring ownership.",
     "/nfts"),
    ("NFTokenCancelOffer", "NFT Cancel",
     "Cancels one or more outstanding NFToken offers.",
     "/nfts"),
    ("TicketCreate", "Ticket Reserve",
     "Reserves sequence numbers for future out-of-order transaction "
     "submission — used by multisig setups and long-running signing "
     "workflows.",
     None),
    ("AMMDeposit", "AMM Deposit",
     "Adds liquidity to an AMM pool (XLS-30) in exchange for LP tokens.",
     "/pools"),
    ("AMMWithdraw", "AMM Withdraw",
     "Redeems LP tokens for a share of the pool's assets.",
     "/pools"),
    ("OracleSet", "Oracle Publish",
     "Publishes or updates a PriceOracle entry under XLS-47.",
     "/price-data"),
    ("AccountDelete", "Account Delete",
     "Deletes an AccountRoot and forwards the residual XRP to a "
     "destination (spec-limited: no active objects, minimum age).",
     None),
    ("EscrowCreate", "Escrow Create",
     "Locks XRP into a native escrow with a time-based or crypto-condition "
     "release. Baseline escrow long predates XLS-0085 TokenEscrow.",
     "/cold-storage"),
    ("CheckCash", "Check Cash",
     "Redeems a previously created Check for XRP or issued tokens.",
     None),
    ("CheckCreate", "Check Create",
     "Creates a Check — a deferred payment authorization the recipient "
     "can cash later.",
     None),
]

# ── seen-in-baseline LEDGER_ENTRY_TYPES ─────────────────────────────────
ENTRY_LABELS = [
    ("AccountRoot", "Account",
     "Root object for every XRPL account — carries balance, sequence, "
     "flags, and settings.",
     "/wallet"),
    ("RippleState", "Trust Line State",
     "The on-ledger state of a trust line (balance, limits, quality). "
     "One RippleState per (account, counterparty, currency) triple.",
     None),
    ("Offer", "DEX Offer",
     "An outstanding limit order on the XRPL DEX order book.",
     None),
    ("DirectoryNode", "Directory Node",
     "Owner directory and book directory entries — how rippled indexes "
     "objects owned by accounts and orders on the DEX order book.",
     None),
    ("NFTokenOffer", "NFT Offer State",
     "Outstanding NFT buy/sell offer object.",
     "/nfts"),
    ("NFTokenPage", "NFT Page",
     "A paginated owner's directory of NFTokens (holds up to 32 NFTs per "
     "page).",
     "/nfts"),
    ("Ticket", "Ticket",
     "A reserved sequence number available for out-of-order transaction "
     "submission.",
     None),
    ("AMM", "AMM Pool",
     "An automated-market-maker pool object under XLS-30 — one AMM object "
     "per (asset A, asset B) pair.",
     "/pools"),
    ("Oracle", "Price Oracle",
     "An on-ledger PriceOracle object under XLS-47 — publisher, provider, "
     "asset pairs, and last-published prices.",
     "/price-data"),
    ("Escrow", "Escrow",
     "A native XRP escrow object (time-locked or condition-locked).",
     "/cold-storage"),
    ("Check", "Check",
     "A Check ledger object awaiting cash-out.",
     None),
]

# ── EDITORIAL defined-but-unseen (day-one expectation notes) ───────────
# Types defined in server_definitions but not yet fired on-chain during
# Phase 1a's opening window. Labeled so their grey rows read as "expected
# and awaited" rather than "we don't know what this is."
DEFINED_BUT_UNSEEN_TX = [
    ("MPTokenIssuanceCreate", "MPT Issuance",
     "Creates a Multi-Purpose Token issuance under XLS-33 (activated "
     "2026 Q1). Distinct from issued-currency IOUs; MPTs are the "
     "spec-native path for tokenized instruments.",
     "/mpts"),
    ("MPTokenIssuanceDestroy", "MPT Destroy",
     "Destroys an MPT issuance and releases its object reserve.",
     "/mpts"),
    ("MPTokenAuthorize", "MPT Authorize",
     "Authorizes a holder to hold a specific MPT issuance (or "
     "unauthorizes them).",
     "/mpts"),
    ("MPTokenIssuanceSet", "MPT Config",
     "Updates configurable fields on an MPT issuance.",
     "/mpts"),
    ("CredentialCreate", "Credential Create",
     "Issues an XLS-70 on-chain credential from an issuer to a subject.",
     "/credentials"),
    ("CredentialAccept", "Credential Accept",
     "Subject-side acceptance of an issued credential.",
     "/credentials"),
    ("CredentialDelete", "Credential Delete",
     "Removes an outstanding credential.",
     "/credentials"),
    ("DIDSet", "DID Set",
     "Sets or updates a Decentralized Identifier document on an account.",
     "/credentials"),
    ("DIDDelete", "DID Delete",
     "Removes a DID document.",
     "/credentials"),
    ("PermissionedDomainSet", "Permissioned Domain Set",
     "Creates or updates a permissioned domain (XLS-80) — gates access "
     "to issued assets to credential-holders.",
     "/credentials"),
    ("PermissionedDomainDelete", "Permissioned Domain Delete",
     "Deletes a permissioned domain.",
     "/credentials"),
    ("XChainCreateBridge", "XChain Bridge Create",
     "Creates a native XLS-38 cross-chain bridge (door account + witness "
     "signature set). Sidechain-oriented; distinct from third-party "
     "bridges like Axelar.",
     "/sidechain"),
    ("XChainAddClaimAttestation", "XChain Claim Attestation",
     "Witness-signed attestation of a cross-chain claim under XLS-38.",
     "/sidechain"),
    ("Batch", "Batch",
     "Executes a group of transactions atomically under the Batch "
     "amendment (activated 2026). All-or-nothing composition of on-chain "
     "operations.",
     None),
    ("Delegate", "Delegate",
     "Delegate account-level permissions to another account under the "
     "Delegate amendment.",
     None),
    ("EscrowCreateFinish", "TokenEscrow Finish",
     "Placeholder — TokenEscrow under XLS-0085 introduces token-carrying "
     "escrow objects. Note: the actual tx-type name may differ once "
     "first-fired; this row will re-key on real observation.",
     "/cold-storage"),
]

DEFINED_BUT_UNSEEN_ENTRY = [
    ("MPTokenIssuance", "MPT Issuance State",
     "An XLS-33 Multi-Purpose Token issuance object.",
     "/mpts"),
    ("MPToken", "MPT Holding",
     "A holder's MPT balance object (distinct from MPTokenIssuance which "
     "is the issuer-side row).",
     "/mpts"),
    ("Credential", "Credential State",
     "An XLS-70 issued credential ledger object.",
     "/credentials"),
    ("DID", "DID State",
     "An XLS-40 Decentralized Identifier ledger object.",
     "/credentials"),
    ("PermissionedDomain", "Permissioned Domain State",
     "An XLS-80 permissioned domain object.",
     "/credentials"),
    ("Bridge", "XChain Bridge State",
     "A cross-chain bridge door object under XLS-38.",
     "/sidechain"),
    ("XChainOwnedClaimID", "XChain Claim ID",
     "An outstanding cross-chain claim ID under XLS-38.",
     "/sidechain"),
    ("XChainOwnedCreateAccountClaimID", "XChain Account Claim",
     "An outstanding cross-chain account-creation claim ID under XLS-38.",
     "/sidechain"),
    ("Loan", "Loan",
     "An on-chain loan object under the Lending amendment (not yet "
     "activated at time of writing). Grey until first-ever sighting.",
     "/lending"),
    ("LoanBroker", "Loan Broker",
     "An on-chain lending broker object under the Lending amendment.",
     "/lending"),
    ("Vault", "Vault",
     "A vault object under the Vaults amendment (not yet activated). "
     "Grey until first-ever sighting.",
     None),
    ("Delegate", "Delegate State",
     "A delegated-permissions object under the Delegate amendment.",
     None),
]


def main():
    if not db.pg_available():
        raise SystemExit("DATABASE_URL not set")
    n = 0
    for name, label, short_desc, linked_page in TX_LABELS + DEFINED_BUT_UNSEEN_TX:
        if db.upsert_coverage_label("tx", name, label, short_desc, linked_page):
            n += 1
    for name, label, short_desc, linked_page in ENTRY_LABELS + DEFINED_BUT_UNSEEN_ENTRY:
        if db.upsert_coverage_label("entry", name, label, short_desc, linked_page):
            n += 1
    total = (len(TX_LABELS) + len(DEFINED_BUT_UNSEEN_TX)
             + len(ENTRY_LABELS) + len(DEFINED_BUT_UNSEEN_ENTRY))
    print(f"Seeded {n}/{total} coverage_labels rows "
          f"(tx: {len(TX_LABELS)} baseline + {len(DEFINED_BUT_UNSEEN_TX)} "
          f"editorial; entry: {len(ENTRY_LABELS)} baseline + "
          f"{len(DEFINED_BUT_UNSEEN_ENTRY)} editorial).")


if __name__ == "__main__":
    main()
