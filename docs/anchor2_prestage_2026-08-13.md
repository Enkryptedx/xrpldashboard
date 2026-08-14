# Anchor #2 pre-stage — Thursday AM fetch

Weekly cadence anchor #2. First anchor was Fri 2026-08-07 (tx
`01D0BB9D230955F43DB35703E2EB7F5DFA43CEB69CCBBF57FBC8F17407E50DF8`,
ledger 106140698).

Per protocol: `chain_root_hex` and `<date>` come from a **live**
fetch of `chain.json` Thursday morning — never memory, never a prior
tx, never docs. If the fetch fails, ABORT — do not anchor a stale
root.

## Step 1 — Live fetch (paste this into a Mac terminal Thu AM)

```bash
curl -fsS https://xrpldashboard.com/.well-known/snapshots/chain.json \
  | python3 -c '
import json, sys
d = json.load(sys.stdin)
root = d.get("current_root") or d.get("root")
leaves = d.get("leaves") or []
date = leaves[-1].get("date") if leaves else None
if not root or not date:
    sys.exit(f"ABORT: chain.json shape unexpected (root={root!r}, date={date!r})")
print(f"date       = {date}")
print(f"chain_root = {root}")
print()
print("MemoData ready to paste into Xaman:")
print(f"xrpldashboard/anchor/v1|{date}|{root}")
'
```

## Step 2 — Xaman payload (ten taps)

1. Open Xaman on phone
2. Send → XRP
3. Destination: `rwrcJL3Exd1ZUYz11Wug6wvWC448CiTXfd` (ops account)
4. Amount: `0.000001` (1 drop)
5. Advanced → Add Memo → paste the MemoData line from Step 1
6. MemoType: leave blank (Type A shape)
7. MemoFormat: leave blank
8. Fee: default (10 drops)
9. Review — verify destination + amount + memo bytes on screen
10. Slide to sign → wait for `tesSUCCESS`

## Step 3 — Verify + record

```bash
# Replace <TXHASH> with hash from Xaman success screen
curl -s https://s1.ripple.com:51234/ -H 'Content-Type: application/json' \
  -d '{"method":"tx","params":[{"transaction":"<TXHASH>","binary":false}]}' \
  | python3 -m json.tool | head -60
```

Confirm: `meta.TransactionResult == "tesSUCCESS"`, `Destination == "rwrcJL3Exd1ZUYz11Wug6wvWC448CiTXfd"`, `Amount == "1"`, MemoData decoded matches Step 1's paste.

Record: ledger number, close time, tx hash. Append to `docs/anchor_history.md` (or equivalent — the running log).

## Guardrails

- **Payment-to-self BLOCKED** by Xaman — violates spec amendment #2.
- **Payment-to-any-other-address** = live-alarm per verifier rule #4.
- **More than 1 drop** = tamper signal — volume-through-anchor is a red flag.
- **MemoData whitespace** — do not append newlines at authoring time; Xaman may append them and verifiers strip via `.strip()`.

## If Thu is skipped

Reconcile explicitly in next anchor's Type B memo. Never backdate. A
gap in the anchor account's tx history is visible on-ledger and must
be acknowledged, not hidden.

## Genesis fixture (frozen ground truth)

Anchor #1 tx `01D0BB9D230955F43DB35703E2EB7F5DFA43CEB69CCBBF57FBC8F17407E50DF8`
at ledger 106140698 (2026-08-07 21:49:32 UTC) is the verifier fixture.
Any future verify tool MUST pass this fixture; anchor #2 does not
replace it.
