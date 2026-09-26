"""Option B relay feed filters — byte-for-byte ports of the browser-side
live-feed filters (Milestone 1, Charlie ruling 2026-09-25).

Source of truth for each filter is the CURRENT browser JS on the page it
serves. This module is a straight Python port so the relay produces the
EXACT same event set the page accepts today (the "one definition per public
number" rule from OPTION_B_RELAY_PROPOSAL.md §2/§3). Any drift here is a bug.

These are PURE functions over a parsed tx envelope + a small set of reference
data (pool accounts, top-100 tokens, whale threshold, per-subscriber address
set). No I/O, no transport — Milestone 1 is filters only; relay wiring on
Lenovo is Milestone 2 (separate go).

Envelope shape (from our own node's `transactions` stream, matches what the
browser receives and what PG events.raw_json stores):
    {
      "type": "transaction",
      "validated": true,
      "meta": {"TransactionResult": "tesSUCCESS", "AffectedNodes": [...]},
      "tx_json": {"TransactionType": "Payment", "Account": "...", ...},
      ...
    }
The browser reads `data.transaction || data.tx_json` for the tx and
`data.meta` for metadata. We mirror that exactly in `_tx()` / `_meta()`.
"""
from __future__ import annotations

from typing import Iterable


# ── envelope accessors (mirror the browser's data.transaction || data.tx_json) ──

def _tx(data: dict) -> dict:
    if not isinstance(data, dict):
        return {}
    return data.get("transaction") or data.get("tx_json") or {}


def _meta(data: dict) -> dict:
    m = data.get("meta") if isinstance(data, dict) else None
    return m if isinstance(m, dict) else {}


def is_validated_success(data: dict) -> bool:
    """Browser gate present on the token/whale/pool handlers:
      data.type === 'transaction' && data.validated === true
      && meta.TransactionResult === 'tesSUCCESS'
    (pools.html/tokens.html/whales.html all apply this before filtering.)"""
    if not isinstance(data, dict):
        return False
    if data.get("type") != "transaction":
        return False
    if data.get("validated") is not True:
        return False
    return _meta(data).get("TransactionResult") == "tesSUCCESS"


# ── §3a amm_transactions (port of templates/pools.html:1149-1178) ──

def matches_amm(data: dict, pool_accounts: set[str]) -> bool:
    """Include if the tx touches a tracked AMM account, either directly
    (Account/Destination) or via any AffectedNode's
    {ModifiedNode,CreatedNode,DeletedNode}.{FinalFields|NewFields|PreviousFields}
    .Account/.Destination. Verbatim walk order from pools.html."""
    tx = _tx(data)
    meta = _meta(data)

    def is_pool(a):
        return bool(a) and a in pool_accounts

    if is_pool(tx.get("Account")):
        return True
    if is_pool(tx.get("Destination")):
        return True
    nodes = meta.get("AffectedNodes")
    if isinstance(nodes, list):
        for n in nodes:
            if not isinstance(n, dict):
                continue
            node = n.get("ModifiedNode") or n.get("CreatedNode") or n.get("DeletedNode")
            if not isinstance(node, dict):
                continue
            f = (node.get("FinalFields")
                 or node.get("NewFields")
                 or node.get("PreviousFields")
                 or {})
            if is_pool(f.get("Account")):
                return True
            if is_pool(f.get("Destination")):
                return True
    return False


# ── §3b token_top100_transactions (port of templates/tokens.html isTokenTrade) ──

def matches_token_top100(data: dict, top100: set[tuple[str, str]]) -> bool:
    """Include if Payment whose delivered amount is an issued currency
    (object, not a drops string) with (currency, issuer) in the top-100 set.
    Browser reads DeliverMax then legacy Amount.

    top100 is a set of (currency, issuer) tuples exactly as stored in
    tokens_top100_24h_volume (currency may be the 40-hex form or the
    3-char code — we match on the raw value the row carries, no
    normalization, to stay byte-for-byte with the page's own set)."""
    tx = _tx(data)
    if tx.get("TransactionType") != "Payment":
        return False
    amt = tx.get("DeliverMax")
    if amt is None:
        amt = tx.get("Amount")
    # XRP is a drops string; issued currency is an object {currency,issuer,value}
    if not isinstance(amt, dict):
        return False
    key = (amt.get("currency"), amt.get("issuer"))
    return key in top100


# ── §3c whale_transactions (port of templates/whales.html:1030-1033) ──

def matches_whale(data: dict, tier_drops: int) -> bool:
    """Include if Payment delivering XRP (Amount is a drops string) whose
    drops >= tier_drops. Verbatim from whales.html:
        var drops = parseInt(tx.Amount, 10);
        if (!isFinite(drops) || drops < tierDrops) return;

    NOTE: the browser reads tx.Amount (the delivered/legacy field) as a
    drops string for XRP-only payments. Token payments have an object
    Amount and are excluded by parseInt→NaN. We mirror parseInt semantics
    (leading-numeric, base 10)."""
    tx = _tx(data)
    if tx.get("TransactionType") != "Payment":
        return False
    amount = tx.get("Amount")
    # Browser uses tx.Amount specifically (not DeliverMax) for the drops read.
    if isinstance(amount, dict):
        return False  # issued currency → parseInt would be NaN
    drops = _parse_int_js(amount)
    if drops is None:
        return False
    return drops >= tier_drops


def _parse_int_js(v) -> int | None:
    """Mirror JS parseInt(v, 10): take the leading integer prefix of the
    string; return None for NaN (no leading digits / None / object)."""
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    i = 0
    if s[0] in "+-":
        i = 1
    j = i
    while j < len(s) and s[j].isdigit():
        j += 1
    if j == i:
        return None  # NaN
    try:
        return int(s[:j])
    except ValueError:
        return None


# ── §3d wallet_transactions (port of §3d / xrplcluster per-account behavior) ──

def matches_wallet(data: dict, addresses: set[str]) -> bool:
    """Include if the tx AFFECTS any address in the subscriber's set — the
    same set a rippled/xrplcluster per-account subscription yields today.

    rippled (TxMeta::getAffectedAccounts) fires the `accounts` stream for
    every AccountID-typed field in the tx and in every affected node's
    FinalFields / NewFields / PreviousFields: Account, Destination, Owner,
    RegularKey, trust-line HighLimit.issuer / LowLimit.issuer, amount
    issuers, … So an RLUSD OfferCreate by a third party that moves an
    issuer trust line fires for the ISSUER even though the issuer is
    neither tx.Account nor tx.Destination.

    Port: walk every string value in the tx and the affected nodes (all
    three field dicts, nested objects included) and match on set membership.
    A superset of rippled's typed walk only where a non-account string field
    happens to equal a watched address (e.g. a Domain/Memo hex) — negligible
    and never a false negative.

    Founding case 2026-09-26: the first --all-feeds canary run after the
    Milestone-2 restart returned FAIL_no_event for the RLUSD issuer while
    the own node showed six issuer-affecting OfferCreates in one ledger —
    the previous Account/Destination/FinalFields.Account-only match missed
    every one of them."""
    if not addresses:
        return False
    tx = _tx(data)
    if _walk_matches(tx, addresses):
        return True
    nodes = _meta(data).get("AffectedNodes")
    if isinstance(nodes, list):
        for n in nodes:
            if not isinstance(n, dict):
                continue
            node = n.get("ModifiedNode") or n.get("CreatedNode") or n.get("DeletedNode")
            if not isinstance(node, dict):
                continue
            for key in ("FinalFields", "NewFields", "PreviousFields"):
                f = node.get(key)
                if isinstance(f, dict) and _walk_matches(f, addresses):
                    return True
    return False


def _walk_matches(obj, addresses: set[str], _depth: int = 0) -> bool:
    """True if any string value inside obj (dict/list, nested ≤ 6 deep) is
    in addresses. Cheap: set-membership only, no base58 work per tx."""
    if _depth > 6:
        return False
    if isinstance(obj, dict):
        for v in obj.values():
            if isinstance(v, str):
                if v in addresses:
                    return True
            elif isinstance(v, (dict, list)) and _walk_matches(v, addresses, _depth + 1):
                return True
    elif isinstance(obj, list):
        for v in obj:
            if isinstance(v, str):
                if v in addresses:
                    return True
            elif isinstance(v, (dict, list)) and _walk_matches(v, addresses, _depth + 1):
                return True
    return False


# ── §11.1 address validation (Charlie 2026-09-25: validate before accepting sub) ──

_BASE58_XRPL = set("rpshnaf39wBUDNEGHJKLM4PQRST7VWXYZ2bcdeCg65jkm8oFqi1tuvAxyz")


def is_valid_classic_address(addr) -> bool:
    """Well-formed XRPL classic address gate for wallet_transactions subs.
    Checks: starts with 'r', length 25-35, all chars in XRPL base58 alphabet,
    and ripemd160+checksum validates. Rejects malformed before it ever reaches
    a watch set (Charlie §11.1)."""
    if not isinstance(addr, str) or not addr.startswith("r"):
        return False
    if not (25 <= len(addr) <= 35):
        return False
    if any(c not in _BASE58_XRPL for c in addr):
        return False
    try:
        import hashlib
        alphabet = "rpshnaf39wBUDNEGHJKLM4PQRST7VWXYZ2bcdeCg65jkm8oFqi1tuvAxyz"
        num = 0
        for ch in addr:
            num = num * 58 + alphabet.index(ch)
        raw = num.to_bytes(25, "big")
        payload, checksum = raw[:-4], raw[-4:]
        digest = hashlib.sha256(hashlib.sha256(payload).digest()).digest()
        return digest[:4] == checksum and payload[0] == 0x00
    except Exception:
        return False


def classify(data: dict, refs: dict) -> list[str]:
    """Router: return the list of feed names this tx matches.
    refs = {pool_accounts, top100, whale_tier_drops, wallet_subs:{sub_id:addr_set}}.
    ledger/supply_updates feeds are handled on ledgerClosed, not here."""
    if not is_validated_success(data):
        return []
    feeds = []
    if matches_amm(data, refs.get("pool_accounts", set())):
        feeds.append("amm_transactions")
    if matches_token_top100(data, refs.get("top100", set())):
        feeds.append("token_top100_transactions")
    if matches_whale(data, refs.get("whale_tier_drops", 0)):
        feeds.append("whale_transactions")
    return feeds
