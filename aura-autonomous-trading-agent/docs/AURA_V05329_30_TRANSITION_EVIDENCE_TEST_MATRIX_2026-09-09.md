# `.29` + `.30` Transition / Evidence Test Matrix — the implementation contract

Design artifact, no code. Per your instruction: this is the contract the eventual `.29`/`.30` implementation must satisfy — every row below is meant to become one automated test before either component ships. Grounded in the now-locked `.29` spec (`AURA_v05329_mexc_intent_ledger_spec_2026-09-09.md`) and `.30` spec (`AURA_v05330_mexc_intent_reconciliation_spec_2026-09-09.md`), both updated with your 2026-09-09 decisions.

**Date:** 2026-09-09

---

## 0. How to read this document

Four sections, increasing in scope:
- **§1** — `.29` alone: every legal state transition, and every illegal one that must be refused.
- **§2** — `.29` alone: structural integrity (atomicity, tamper detection, crash recovery, restart listing).
- **§3** — `.30` alone: one concrete test case per evidence-matrix row from `.30`'s spec §3, with example numbers.
- **§4** — `.29`+`.30` together: full lifecycle traces, end to end, including the anomaly and escalation paths.

Each test case has an ID (`T29-…`, `T30-…`, `TI-…` for integration) so it can be referenced directly when implementation begins. This matrix intentionally does **not** resolve the items still open in either spec (§9 of `.30`'s spec) — those are carried forward here as explicitly marked gaps, not guessed at, per §6.

---

## 1. `.29` alone — legal transition matrix

From `.29`'s spec §3. `current_state` down the rows, incoming event across the columns. **ALLOWED → new state**, or **REFUSED** (the ledger must raise/reject, not silently accept or no-op).

| current_state ↓ / event → | `CLAIMED` | `SUBMISSION_ATTEMPTED` | `SUBMISSION_ACKNOWLEDGED` | `SUBMISSION_REJECTED` | `EXECUTION_UNCERTAIN` | `DUPLICATE_CLAIM_REJECTED` | `RECONCILIATION_ATTEMPT` (any `order_status`) | `escalate_to_human()` |
|---|---|---|---|---|---|---|---|---|
| `NEW` | → `CLAIMED` | REFUSED | REFUSED | REFUSED | REFUSED | → `TERMINAL_DUPLICATE_CLAIM_REJECTED` (anomaly path, §5) | REFUSED | REFUSED (nothing to escalate yet) |
| `CLAIMED` | REFUSED (already claimed) | → `SUBMISSION_ATTEMPTED` | REFUSED (must go through `SUBMISSION_ATTEMPTED` first) | REFUSED | REFUSED | → anomaly path (§5) | REFUSED | ALLOWED, any non-terminal state per `.30`'s escalate trigger |
| `SUBMISSION_ATTEMPTED` | REFUSED | REFUSED (already attempted) | → `AWAITING_RECONCILIATION` | → `TERMINAL_SUBMISSION_REJECTED` | → `AWAITING_RECONCILIATION` | → anomaly path (§5) | REFUSED (no acknowledgment yet) | ALLOWED |
| `AWAITING_RECONCILIATION` | REFUSED | REFUSED | REFUSED (already acknowledged/uncertain) | REFUSED | REFUSED | → anomaly path (§5) | ALLOWED — first attempt: `UNRESOLVED`→stays here as `RECONCILING`\*, terminal `order_status`→matching `RECONCILED_*`/etc. | ALLOWED |
| `RECONCILING` | REFUSED | REFUSED | REFUSED | REFUSED | REFUSED | → anomaly path (§5) | ALLOWED — repeated attempts; terminal `order_status` moves out, `UNRESOLVED` stays | ALLOWED |
| `RECONCILED_FILLED` (terminal) | REFUSED | REFUSED | REFUSED | REFUSED | REFUSED | → anomaly path (§5), even on a terminal record | REFUSED | REFUSED (already terminal) |
| `RECONCILED_PARTIALLY_FILLED`, `order_terminal_on_exchange == False` (still-open order, non-terminal per `.29` §10.1's refined rule) | REFUSED | REFUSED | REFUSED | REFUSED | REFUSED | → anomaly path (§5) | ALLOWED — another `RECONCILIATION_ATTEMPT` may move it to `RECONCILED_FILLED`/`RECONCILED_CANCELED`, or stay here with an updated `order_terminal_on_exchange` | ALLOWED |
| `RECONCILED_PARTIALLY_FILLED`, `order_terminal_on_exchange == True` (order itself already canceled/closed on MEXC — terminal per `.29` §10.1's refined rule) | REFUSED | REFUSED | REFUSED | REFUSED | REFUSED | → anomaly path (§5), even on this terminal record | REFUSED (already terminal) | REFUSED (already terminal) |
| `RECONCILED_CANCELED` (terminal) | REFUSED | REFUSED | REFUSED | REFUSED | REFUSED | → anomaly path (§5) | REFUSED | REFUSED |
| `TERMINAL_SUBMISSION_REJECTED` (terminal) | REFUSED | REFUSED | REFUSED | REFUSED | REFUSED | → anomaly path (§5) | REFUSED | REFUSED |
| `TERMINAL_DUPLICATE_CLAIM_REJECTED` (terminal) | REFUSED | REFUSED | REFUSED | REFUSED | REFUSED | REFUSED (already the anomaly state) | REFUSED | REFUSED |
| `ESCALATED_HUMAN_REVIEW` (terminal-but-blocking) | REFUSED | REFUSED | REFUSED | REFUSED | REFUSED | → anomaly path (§5) — a duplicate-claim signal is still meaningful even mid-escalation | REFUSED (a human, not `.30`, resolves this) | REFUSED (already escalated) |

\* Note on `AWAITING_RECONCILIATION` → `RECONCILIATION_ATTEMPT` with `order_status == UNRESOLVED`: the resulting `current_state` is `RECONCILING`, not a no-op — this is the very first reconciliation attempt, and `.29`'s spec §3 requires `RECONCILING` to be entered by "first `RECONCILIATION_ATTEMPT` event, any `order_status` including `UNRESOLVED`."

**§5's anomaly-path row, spelled out as its own test group (`T29-DUP-*`):** `DUPLICATE_CLAIM_REJECTED` reported against a record in **any** state except `NEW`'s "no record yet" case is legal and appends to the *same* record, transitioning it to `ESCALATED_HUMAN_REVIEW` regardless of what state it was in before (including already-terminal states) — this is the one event type that is legal from every state, by design (§5 of `.29`'s spec). Test cases:
- `T29-DUP-01`: `DUPLICATE_CLAIM_REJECTED` against a `NEW` record → escalate.
- `T29-DUP-02`: … against `CLAIMED` → escalate.
- `T29-DUP-03`: … against `RECONCILING` → escalate.
- `T29-DUP-04`: … against an already-`RECONCILED_FILLED` record → escalate (a terminal record can still receive this anomaly event — it is never truly closed to it).
- `T29-DUP-05`: … against an already-`ESCALATED_HUMAN_REVIEW` record → appends a second anomaly event to the same record, state remains `ESCALATED_HUMAN_REVIEW` (idempotent, not a new escalation).
- `T29-DUP-06`: confirm exactly one record exists after `T29-DUP-01`…`05` — never a second record for the same `client_order_id`.

**Illegal-transition test group (`T29-ILLEGAL-*`):** every REFUSED cell above is one test — enumerate all of them (there are 60+ cells; implementation should generate these programmatically from the table rather than hand-writing each one, but every cell must be covered).

---

## 2. `.29` alone — structural integrity

| ID | Scenario | Expected behavior |
|---|---|---|
| `T29-ATOMIC-01` | Two callers race `create_intent()` for the same `client_order_id` at the same instant | Exactly one succeeds; the other gets a "record already exists" refusal, never a silently overwritten or duplicated record (`O_CREAT\|O_EXCL` semantics) |
| `T29-HASH-01` | `verify_intent()` on an untampered record | Passes; `record_hash` matches recomputation |
| `T29-HASH-02` | `verify_intent()` on a record with one hand-edited event field (e.g. `order_status` changed after the fact) | Fails; hash mismatch detected, not silently trusted |
| `T29-HASH-03` | `verify_intent()` on a record whose `events` list is a legal-looking but structurally invalid sequence (e.g. two `CLAIMED` events with no anomaly marker) | Fails — sequence validation catches it even if `record_hash` happens to be internally consistent with the tampered content (i.e. hash-consistency alone is not sufficient; sequence-legality is checked too) |
| `T29-CRASH-01` | Simulated crash mid-write: process killed after the temp file is written but before the atomic rename completes | On next read, the prior (pre-crash) record is intact — never a truncated or half-written file |
| `T29-CRASH-02` | Simulated crash mid-write: process killed after the atomic rename completes | On next read, the new event is present and the record verifies — crash after rename is equivalent to a completed write |
| `T29-LIST-01` | Fixture with one record in each of `.29`'s 11 states | `list_unresolved_intents()` returns exactly the non-terminal ones: `NEW`, `CLAIMED`, `SUBMISSION_ATTEMPTED`, `AWAITING_RECONCILIATION`, `RECONCILING`, `RECONCILED_PARTIALLY_FILLED`, `ESCALATED_HUMAN_REVIEW` — and excludes `RECONCILED_FILLED`, `RECONCILED_CANCELED`, `TERMINAL_SUBMISSION_REJECTED`, `TERMINAL_DUPLICATE_CLAIM_REJECTED` |
| `T29-LIST-02` | Empty intents directory | `list_unresolved_intents()` returns `[]`, not an error |
| `T29-DIRECTION-01` | `create_intent()` called with each of the four `direction` values | All four accepted; `direction` is immutable after creation — an attempt to change it on any subsequent event is REFUSED (no event type in `.29`'s vocabulary targets `direction`, so this is really "no interface exists to mutate it," verified by inspecting that no mutator accepts a `direction` parameter) |

---

## 3. `.30` alone — evidence-matrix test cases

One test per row of `.30`'s spec §3 table, each phrased as concrete example evidence. All test cases assume a valid, hash-verified `.29` record in `AWAITING_RECONCILIATION` or `RECONCILING`, and a valid, hash-verified `.28` snapshot — verification-failure cases are listed separately at the end.

| ID | `.28` raw evidence (abbreviated) | `direction` | `position_exists` | Expected verdict |
|---|---|---|---|---|
| `T30-FILL-01` | raw state `3`, `dealVol == vol == 1.0`, `dealAvgPrice`/`updateTime` present | `OPEN_LONG` | `True` | `RECORD_ATTEMPT` → `RECONCILED_FILLED` |
| `T30-FILL-02` | same fill evidence as `T30-FILL-01` | `CLOSE_LONG` | `False` | `RECORD_ATTEMPT` → `RECONCILED_FILLED` — **order evidence alone is sufficient; a surviving position is not required** (locked decision §4(b)) |
| `T30-FILL-03` | same fill evidence as `T30-FILL-01`, but `direction == OPEN_LONG` and `position_exists == False` (inconsistent: opened but no position survives) | `OPEN_LONG` | `False` | `RECORD_ATTEMPT` → `RECONCILED_FILLED`, with the `direction`/`position_exists` inconsistency recorded in `evidence` for visibility — **not downgraded to a conflict**, per the "order evidence proves execution, position evidence explains account state" principle |
| `T30-PARTIAL-OPEN-01` | raw state `2` (open), `dealVol == 0.4`, `vol == 1.0` | `OPEN_SHORT` | `True` | `RECORD_ATTEMPT` → `RECONCILED_PARTIALLY_FILLED`, `order_terminal_on_exchange = False` → non-terminal, record stays `RECONCILING` |
| `T30-PARTIAL-TERM-02` | continuation of `T30-PARTIAL-TERM-01`: a further `.30` pass is attempted against the now-terminal record | — | — | REFUSED at `.29` — `record_reconciliation_attempt()` refuses a further attempt once `order_terminal_on_exchange == True` was recorded; confirms the refined rule is actually enforced, not just recorded |
| `T30-PARTIAL-TERM-01` | raw state `4` (canceled), `dealVol == 0.4`, `vol == 1.0` | `OPEN_SHORT` | `True` | `RECORD_ATTEMPT` → `RECONCILED_PARTIALLY_FILLED`, `order_terminal_on_exchange = True` → **TERMINAL** (refined rule, locked 2026-09-09) — no further `RECONCILIATION_ATTEMPT` accepted on this record |
| `T30-CANCEL-01` | raw state `4` (canceled), `dealVol == 0` | any | `False` | `RECORD_ATTEMPT` → `RECONCILED_CANCELED` |
| `T30-CANCEL-02` (regression for the resolved §4(a) gap) | `.28`'s own `order_status == "CANCELED"`, but raw `dealVol == 0.6`, `vol == 1.0` (i.e. `.28`'s label says canceled, raw evidence says partially filled) | any | any | `RECORD_ATTEMPT` → `RECONCILED_PARTIALLY_FILLED` (or the terminal-order variant per Q1) — **never** `RECONCILED_CANCELED`; this is the exact regression `.30`'s dealVol-first design exists to prevent, so it must be an explicit test, not incidental coverage |
| `T30-PENDING-01` | raw state `2`, `dealVol == 0` | any | `False` | `RECORD_ATTEMPT` → target `RECONCILING` (no state change if already there) |
| `T30-REJECTED-01` | `.28` `order_status == "REJECTED"` (synthetic — currently unreachable via real `.28` output per its own docstring, but the schema allows it and `.30` must handle it) | any | any | `ESCALATE` → `ESCALATED_HUMAN_REVIEW`, reason references the post-acknowledgment contradiction — **never** `TERMINAL_SUBMISSION_REJECTED` |
| `T30-MULTI-01` | `.28` `match_count == 2` (`order_status == UNRESOLVED`, reason `MULTIPLE_MATCHING_ORDERS_FOUND`) | any | any | `ESCALATE` immediately (case 1) |
| `T30-NOTFOUND-01` | `.28` `match_count == 0` (`order_status == UNRESOLVED`, reason `NO_MATCHING_ORDER_FOUND_IN_WINDOW`), first attempt | any | any | `RECORD_ATTEMPT` → stays `RECONCILING` (case 2) |
| `T30-NOTFOUND-02` | same as `T30-NOTFOUND-01`, but elapsed time since `AWAITING_RECONCILIATION` entry now exceeds the configured `reconciliation_staleness_minutes` | any | any | `ESCALATE` (case 2 → case 3) — **the mechanism is locked (clock starts at `AWAITING_RECONCILIATION` entry, parameter name `reconciliation_staleness_minutes`); the test fixture supplies an arbitrary calibration-required placeholder value for `reconciliation_staleness_minutes` (e.g. a small number for fast test execution) rather than a real production number, which is still TBD from live MEXC observation** |
| `T30-UNMAPPED-01` | raw state `"1"` (unmapped by ccxt) | any | any | `RECORD_ATTEMPT` → stays `RECONCILING` (case 2), same staleness escalation applies over repeated attempts as `T30-NOTFOUND-02` |
| `T30-INCONSISTENT-01` | raw state `3` (closed) but `dealAvgPrice`/`updateTime` missing (`.28` `reason == FILL_STATUS_WITHOUT_SUBSTANTIATING_PRICE_OR_TIMESTAMP`) | any | any | `RECORD_ATTEMPT` → stays `RECONCILING` (case 2), same staleness escalation as above |
| `T30-UNRECOGNIZED-01` | `.28`'s `reason == UNRECOGNIZED_ORDER_STATUS_VALUE` (its own defense-in-depth catch fired) | any | any | `ESCALATE` immediately (case 1) |
| `T30-VERIFY-01` | `.29` record's `record_hash` doesn't recompute cleanly (tampered/corrupt) | — | — | `BLOCKED`, no verdict reached, `.30` refuses to reason over unverified input |
| `T30-VERIFY-02` | `.28` snapshot's `snapshot_hash` doesn't recompute cleanly | — | — | `BLOCKED`, same as above |
| `T30-PRECONDITION-01` | `reconcile_intent()` called with a `.29` record whose `current_state` is not `AWAITING_RECONCILIATION`/`RECONCILING` (e.g. still `CLAIMED`) | — | — | Refused as a precondition violation, not silently processed |

---

## 4. `.29` + `.30` together — full lifecycle traces

Each trace is a literal event sequence an integration test replays end to end, checking `.29`'s `current_state` after every step.

**`TI-CLEAN-OPEN-01` — clean open, single fill:**
`create_intent(direction=OPEN_LONG)` → `NEW`
→ `record_claimed()` → `CLAIMED`
→ `record_submission_attempted()` → `SUBMISSION_ATTEMPTED`
→ `record_submission_outcome(SUBMISSION_ACKNOWLEDGED)` → `AWAITING_RECONCILIATION`
→ `.30.reconcile_intent()` against a `.28` snapshot matching `T30-FILL-01` → verdict `RECORD_ATTEMPT`/`RECONCILED_FILLED`
→ `record_reconciliation_attempt(...)` → `RECONCILED_FILLED` (terminal). **Confirms:** `.29`'s own event log alone (no external system) shows the complete, evidence-backed history of this intent.

**`TI-CLEAN-CLOSE-01` — clean close, no position afterward (regression for the resolved §4(b) concern):**
`create_intent(direction=CLOSE_LONG)` → … → `AWAITING_RECONCILIATION`
→ `.30` against a `.28` snapshot matching `T30-FILL-02` (`position_exists == False`) → `RECONCILED_FILLED`. **Confirms:** a closing intent's absence of a resulting position is not misclassified as a conflict.

**`TI-UNCERTAIN-RESOLVES-01` — `EXECUTION_UNCERTAIN` treated identically to `SUBMISSION_ACKNOWLEDGED`:**
Same as `TI-CLEAN-OPEN-01`, but `record_submission_outcome(EXECUTION_UNCERTAIN)` in place of `SUBMISSION_ACKNOWLEDGED`. **Confirms:** the resulting reconciliation path and final state are identical — no evidentiary head start for the acknowledged case (`.29`'s locked note, §3).

**`TI-PARTIAL-THEN-FILL-01` — partial fill on an open order, later completes:**
→ `AWAITING_RECONCILIATION` → first `.30` pass (`T30-PARTIAL-OPEN-01` evidence, `order_terminal_on_exchange = False`) → `RECONCILED_PARTIALLY_FILLED` (non-terminal, `.29` state stays reachable for another attempt)
→ second `.30` pass, now `dealVol == vol` → `RECORD_ATTEMPT`/`RECONCILED_FILLED` → `RECONCILED_FILLED` (terminal). **Confirms:** `.29`'s non-terminal `RECONCILED_PARTIALLY_FILLED` genuinely allows a later transition to `RECONCILED_FILLED`, not just to itself.

**`TI-PARTIAL-TERMINAL-01` — partial fill on an already-canceled order (the refined rule, end to end):**
→ `AWAITING_RECONCILIATION` → `.30` pass with `T30-PARTIAL-TERM-01` evidence (raw state `4`, `dealVol == 0.4 < vol == 1.0`) → verdict carries `order_terminal_on_exchange = True` → `record_reconciliation_attempt()` → `RECONCILED_PARTIALLY_FILLED`, terminal
→ a further `.30`/`.29` call against the same `client_order_id` is refused (`T30-PARTIAL-TERM-02`). **Confirms:** the refined rule actually closes the record rather than leaving it perpetually `RECONCILING` with nothing left to observe.

**`TI-CANCEL-CLEAN-01` — clean cancel, zero fill:**
→ `AWAITING_RECONCILIATION` → `.30` against `T30-CANCEL-01` evidence → `RECONCILED_CANCELED` (terminal).

**`TI-CANCEL-MISLABELED-01` — regression, `.28` mislabels a partial fill as canceled:**
→ `AWAITING_RECONCILIATION` → `.30` against `T30-CANCEL-02` evidence → **not** `RECONCILED_CANCELED`; resolves per the dealVol-first rule instead. **Confirms:** the specific bug class `.30`'s design exists to prevent doesn't silently slip through the full pipeline, not just `.30` in isolation.

**`TI-POST-ACK-REJECTED-01` — contradictory post-acknowledgment rejection:**
→ `AWAITING_RECONCILIATION` → `.30` against `T30-REJECTED-01` evidence → `ESCALATE` → `escalate_to_human()` → `ESCALATED_HUMAN_REVIEW`. **Confirms:** this never lands on `TERMINAL_SUBMISSION_REJECTED`, which is reachable only via the direct `.27`-rejection path below.

**`TI-PRE-SUBMISSION-REJECTED-01` — the *other*, unrelated rejection path, for contrast:**
`create_intent()` → `CLAIMED` → `SUBMISSION_ATTEMPTED` → `record_submission_outcome(SUBMISSION_REJECTED)` → `TERMINAL_SUBMISSION_REJECTED` directly — **`.30` is never invoked on this path at all.** Included specifically so the two rejection paths (`TI-POST-ACK-REJECTED-01` vs. this one) are tested side by side and can't be conflated.

**`TI-DUPLICATE-CLAIM-01` — anomaly path, mid-flight:**
`create_intent()` → `CLAIMED` → `SUBMISSION_ATTEMPTED` → `record_submission_outcome(DUPLICATE_CLAIM_REJECTED)` → `ESCALATED_HUMAN_REVIEW`, same record (no second record created for the `client_order_id`). **Confirms:** `.27`'s replay-protection signal reaching `.29` mid-lifecycle routes correctly even though it isn't one of the "expected" outcomes for that state.

**`TI-STALENESS-01` — case 2 escalating to case 3:**
→ `AWAITING_RECONCILIATION` → repeated `.30` passes, each returning `T30-NOTFOUND-01`-style "stays `RECONCILING`" verdicts, with each `RECONCILIATION_ATTEMPT` event's timestamp advanced past the (test-fixture-configured) staleness threshold → the next `.30` pass returns `ESCALATE` instead of another wait verdict → `ESCALATED_HUMAN_REVIEW`. **Confirms:** the clock starts at `AWAITING_RECONCILIATION` entry (per `.30`'s spec §5), not at the first `.30` invocation — test fixture should specifically construct a record whose `AWAITING_RECONCILIATION` entry is already old even though `.30` is only now being invoked for the first time, and confirm it escalates immediately rather than getting a fresh case-2 grace period.

**`TI-RESTART-01` — restart recovery:**
Build a fixture directory with one record in each of `.29`'s non-terminal states (mirroring `T29-LIST-01`) plus several terminal ones → simulate a fresh process calling `list_unresolved_intents()` with no other state → confirms it returns exactly the non-terminal set, and that each returned record's full event history round-trips through `get_intent()` unchanged. **Confirms:** the concrete answer to "what did AURA believe happened before the process stopped" (`.29`'s spec §6) actually works from cold storage, not just in-memory.

**`TI-VERIFY-CHAIN-01` — hash-chain integrity across both components together:**
Run `TI-CLEAN-OPEN-01` to completion → separately re-verify: `.29`'s final record via `verify_intent()`, and the `.28` snapshot it reconciled against via its own `snapshot_hash` recomputation → both pass, and the `RECONCILIATION_ATTEMPT` event's `observed_snapshot_hash` field matches the `.28` snapshot's own `snapshot_hash` exactly (i.e. `.29`'s record of "what evidence was used" is independently checkable against the evidence itself, not just internally self-consistent).

---

## 5. Cross-cutting invariants every test case (not just the ones listed above) must respect

- **No test may assert a `RECONCILED_*`/`TERMINAL_*` outcome that isn't traceable to a specific matrix row from `.30`'s spec §3.** If a new scenario doesn't match any row, the only valid expected outcomes are `RECONCILING` or `ESCALATED_HUMAN_REVIEW` (§5a's "never an inferred success" principle) — a test asserting anything else is itself a spec violation, not just an implementation bug.
- **`.30` never writes `.29`'s storage directly** — every integration test should assert this at the boundary (e.g. by checking `.30`'s pure functions take/return data and never touch a filesystem path), not just that the end state is correct.
- **`.27`'s claim file stays authoritative for replay protection throughout** — no test in this matrix should ever substitute a `.29` `CLAIMED` event check for an actual `.27` claim-file check when testing replay protection specifically (that protection is tested in `.27`'s own suite, already committed).
- **Every `RECONCILED_FILLED`/`RECONCILED_PARTIALLY_FILLED` verdict's test fixture must include `dealAvgPrice`/`updateTime` in the raw order evidence** — per `.28`'s own contract, a fill verdict without substantiating price/timestamp shouldn't be constructible as a test fixture in the first place (if it is, that's a fixture bug, not a valid input to `.30`).

---

## 6. Formerly-open items — all resolved 2026-09-09

1. **Partial fill on a terminal MEXC order** — resolved. `RECONCILED_PARTIALLY_FILLED` is terminal exactly when `order_terminal_on_exchange == True` (the observed order is itself canceled/closed on MEXC). `T30-PARTIAL-TERM-01`/`02` and `TI-PARTIAL-TERMINAL-01` reflect this directly; no longer a placeholder.
2. **Staleness threshold** — mechanism resolved (config parameter `reconciliation_staleness_minutes`, clock starts at `AWAITING_RECONCILIATION` entry, calibration-required), production value still deliberately deferred to real MEXC observation. `T30-NOTFOUND-02`/`T30-UNMAPPED-01`/`T30-INCONSISTENT-01`/`TI-STALENESS-01` all use a test-fixture placeholder value for this parameter, not a production number — that's expected and correct, not a gap.
3. **`.30`'s own persisted output** — resolved: none. `.30` is a stateless deterministic engine; `.29`'s event log is the sole authoritative record. No additional test section needed for a `.30`-owned artifact.

This matrix now has no unresolved dependencies against `.29`/`.30`'s specs. The only residual uncertainty is the staleness threshold's actual *value*, which is explicitly out of scope for design (it requires live MEXC observation) and does not block implementation planning or test-writing — the tests are written against the mechanism, with the real number supplied later as ordinary configuration.

---

No code written. Design phase complete. Next: implementation planning for `.29` + `.30` — see `AURA_v05329_30_MEXC_IMPLEMENTATION_PLAN_2026-09-09.md`.

---

**Implementation note added at commit time (2026-09-09):** every `T29-*`, `T30-*` and `TI-*` case above has a corresponding automated test, plus additional crash/restart and replay/concurrency coverage beyond this matrix's original scope. `TERMINAL_DUPLICATE_CLAIM_REJECTED` as a distinct state name (§1's table, `T29-DUP-*`) was found vestigial during implementation — see the note appended to the `.29` spec document; every `T29-DUP-*` case above resolves to `ESCALATED_HUMAN_REVIEW` in the implementation, consistent with §5's already-locked text describing this anomaly's routing.
