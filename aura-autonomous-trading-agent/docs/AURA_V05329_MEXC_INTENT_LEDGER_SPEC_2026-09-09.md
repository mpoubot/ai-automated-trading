# `.29` MEXC Intent Ledger — Specification

Design/spec only, per your explicit sequencing. No code written. Grounded in: the locked `.29`/`.30` architecture from the prior design review, the Implementation Specification's original `ExecutionIntent` data model (§1.1–§1.3), `.27` and `.28` exactly as built and committed, and the existing `.12`–`.19` file conventions (canonical-JSON hash chaining, `guardrails` dicts, fail-closed on every ambiguity).

**Date:** 2026-09-09

---

## 1. What `.29` is and isn't

`.29` is the durable, `client_order_id`-keyed record of everything AURA believes happened to one execution intent, from creation through final reconciliation. It answers, for any intent, at any time including after a restart: *what did AURA believe happened, and what stage is it at.*

It does **not**: call MEXC (that's `.27`/`.28`); decide a reconciliation verdict (that's `.30` — `.29` only *records* the verdict `.30` hands it); perform replay protection itself (that remains `.27`'s own on-disk claim file — see §4's important precision on this); mutate Position State (`.15`, explicitly deferred per your instruction); fabricate any fact — every event `.29` records traces to something an upstream component (`.27`, `.28`, `.30`, or the not-yet-built orchestrator) explicitly told it.

---

## 2. Data model

One record per `client_order_id`, append-only event log plus a derived `current_state` convenience field:

```
{
  "schema_version": "1.0",
  "engine": "MEXC_INTENT_LEDGER",
  "agent_version": "AURA v0.5.3.29",
  "client_order_id": "...",
  "symbol": "...",
  "direction": "OPEN_LONG | OPEN_SHORT | CLOSE_LONG | CLOSE_SHORT",
  "spec_fingerprint": "sha256 of the execution spec this intent was built from",
  "created_at": "ISO8601 UTC",
  "updated_at": "ISO8601 UTC",
  "current_state": "<see §3>",
  "events": [
    { "event": "INTENT_CREATED", "at": "...", "fields": {...} },
    { "event": "CLAIMED", "at": "...", "fields": {...} },
    { "event": "SUBMISSION_ATTEMPTED", "at": "...", "fields": {} },
    { "event": "SUBMISSION_ACKNOWLEDGED", "at": "...", "fields": {"mexc_order_id": "...", "raw_response_ref": "..."} },
    { "event": "RECONCILIATION_ATTEMPT", "at": "...", "fields": {"observed_snapshot_hash": "...", "order_status": "UNRESOLVED"} },
    { "event": "RECONCILIATION_ATTEMPT", "at": "...", "fields": {"observed_snapshot_hash": "...", "order_status": "FILLED"} }
  ],
  "record_hash": "sha256 over canonical {engine, agent_version, client_order_id, symbol, spec_fingerprint, events} -- recomputed on every append"
}
```

The **event log is the source of truth**; `current_state` is derived from it and recomputed on every append, never set independently — the same "never let a summary field drift from its own evidence" discipline `.18`/`.28` already apply to their own hash fields.

**`direction` (locked 2026-09-09):** four explicit values — `OPEN_LONG` / `OPEN_SHORT` / `CLOSE_LONG` / `CLOSE_SHORT` — not just `side` (buy/sell), because `side` alone doesn't tell `.30` whether a fill should be expected to leave a position behind or remove one (a `sell` can open a short or close a long, depending on position mode). `direction` is set once at `create_intent()` and never changes. It exists **only** so `.30` can correctly interpret `position_exists` as supporting evidence — it does not itself prove anything, and `.29` still does not decide what a `direction` value implies; that reasoning lives entirely in `.30`. The anti-false-attribution rule from `.28`'s spec stays intact: an opening intent with a position present, or a closing intent with no position present, is corroborating evidence, never standalone proof that *this* intent is what produced that state.

---

## 3. State vocabulary and legal transitions

Using the renamed submission vocabulary from the prior design review, to keep "submitted" from ever meaning two different things at two layers:

| `current_state` | Entered by | Terminal? |
|---|---|---|
| `NEW` | `INTENT_CREATED` | no |
| `CLAIMED` | `CLAIMED` | no |
| `SUBMISSION_ATTEMPTED` | `SUBMISSION_ATTEMPTED` (transient — an orchestrator is about to call `.27`) | no |
| `AWAITING_RECONCILIATION` | `SUBMISSION_ACKNOWLEDGED` or `EXECUTION_UNCERTAIN` (both funnel here — see note below) | no |
| `RECONCILING` | first `RECONCILIATION_ATTEMPT` event, any `order_status` including `UNRESOLVED` | no |
| `RECONCILED_FILLED` | a `RECONCILIATION_ATTEMPT` with `order_status == FILLED` | **yes** |
| `RECONCILED_PARTIALLY_FILLED` | `order_status == PARTIALLY_FILLED` | **conditional — see the refined rule below (locked 2026-09-09, §10.1)** |
| `RECONCILED_CANCELED` | `order_status == CANCELED` | **yes** |
| `TERMINAL_SUBMISSION_REJECTED` | `SUBMISSION_REJECTED` (from `.27` — order never reached MEXC) | **yes** |
| `TERMINAL_DUPLICATE_CLAIM_REJECTED` | `DUPLICATE_CLAIM_REJECTED` — see §5's anomaly handling | **yes**, but see §5 |
| `ESCALATED_HUMAN_REVIEW` | `.30` decides a bounded reconciliation-attempt/lookback threshold has been exceeded with no resolution | **yes, but structurally blocking** — mirrors `.18`'s existing "an uncleared `HUMAN` record keeps that scope blocked" principle (Implementation Spec §5.3) |

**Refined rule for `RECONCILED_PARTIALLY_FILLED`'s terminality (locked 2026-09-09):** whether a `RECONCILED_PARTIALLY_FILLED` record is terminal depends on whether *the observed order itself* is terminal on MEXC's side at the moment of that `RECONCILIATION_ATTEMPT` — not on the state name alone:

```
order still OPEN on MEXC + partial fill  -> RECONCILED_PARTIALLY_FILLED, NON-terminal
                                             (more execution can still occur; .29 keeps
                                             accepting further RECONCILIATION_ATTEMPT events)

order CANCELED/CLOSED on MEXC + partial fill -> RECONCILED_PARTIALLY_FILLED, TERMINAL
                                             (nothing left to wait for; .29 refuses
                                             further RECONCILIATION_ATTEMPT events)
```

To make this checkable without inventing a second state name, every `RECONCILIATION_ATTEMPT` event that produces `order_status == PARTIALLY_FILLED` must carry an additional evidence field, `order_terminal_on_exchange: bool` (supplied by `.30`, which is the only component with access to the raw MEXC order state needed to know this — see `.30`'s spec §3/§4(d)). `.29`'s own transition-legality check for "is this specific `RECONCILED_PARTIALLY_FILLED` record still open to further reconciliation attempts" reads the most recent `RECONCILIATION_ATTEMPT` event's `order_terminal_on_exchange` value, not just the `current_state` name. This is a small refinement of the existing rule, not a new state.

**Note on `SUBMISSION_ACKNOWLEDGED → AWAITING_RECONCILIATION`, not straight to filled:** per your own point — *"A successful HTTP/API response from MEXC is not evidence that the position exists. `.28` remains the independent observer."* — `.29` never treats a successful submission as execution. Both `SUBMISSION_ACKNOWLEDGED` and `EXECUTION_UNCERTAIN` land in the same `AWAITING_RECONCILIATION`/`RECONCILING` path and are resolved identically by `.30`; `.29` does not give an acknowledged submission any evidentiary head start over an uncertain one.

**Legal-transition enforcement:** `.29` must refuse (not silently accept) any event that doesn't match the current state's allowed next events — e.g. `SUBMISSION_ATTEMPTED` can't be appended to a record still at `NEW` (no `CLAIMED` yet), a second `CLAIMED` can't be appended to a record already past `CLAIMED`. This is the same "no implicit assumption, fail closed" discipline as everywhere else in the chain, applied to the ledger's own write path rather than to MEXC data.

**Evidence-authority rule (locked 2026-09-09):** no `.29` state transition may imply an exchange fact that `.29` itself has not received as evidence. Concretely: `SUBMISSION_ACKNOWLEDGED` may only ever advance a record to `AWAITING_RECONCILIATION` (and from there, only a `RECONCILIATION_ATTEMPT` event advances it further):

```
SUBMISSION_ACKNOWLEDGED
        ↓
AWAITING_RECONCILIATION
        ↓
RECONCILING
```

Never a shortcut such as:

```
SUBMISSION_ACKNOWLEDGED
        ↓
RECONCILED_FILLED   ❌  -- .29 has no evidence this happened; only .30 + .28
                          observation evidence may produce a RECONCILED_*/
                          TERMINAL_* state.
```

This is enforced the same way as the rest of §3's transition table (`record_submission_outcome()` and `record_reconciliation_attempt()` are the *only* two functions that can move a record past `AWAITING_RECONCILIATION`, and only the latter can produce a `RECONCILED_*` state) — it is not a separate mechanism, just a explicit naming of the property the transition table already enforces, made explicit here because it is the core authority boundary of the whole `.27`/`.28`/`.29`/`.30` design: `.27` reports what happened to the submission call; `.28` reports what MEXC can be observed to show; `.30` decides whether the evidence reconciles; `.29` durably records the resulting intent lifecycle and enforces that no step is skipped.

`REJECTED`/`UNRESOLVED` semantics carried over faithfully from `.28`: `.28`'s `order_status` enum includes `REJECTED`, but `.28`'s own `classify_order_status()` never actually produces it today (nothing in its logic observes a pre-submission refusal that way — only `.27`'s `SUBMISSION_REJECTED` covers that case). `.29`'s `RECONCILIATION_ATTEMPT` event accepts `REJECTED` as a defined value for schema completeness, but no currently-built component would ever send one — worth knowing so its absence in practice isn't mistaken for a gap.

---

## 4. Replay protection — `.29` mirrors the fact, `.27` remains the mechanism

**Important precision, easy to get wrong:** `.29`'s `CLAIMED` event is an **audit-trail mirror**, not a **substitute source of truth**, for "has this `client_order_id` already been consumed." That safety property remains exactly where it is today — `.27`'s own `os.O_CREAT|O_EXCL` claim file, unchanged, already proven by test. Nothing in `.29`'s design weakens or duplicates that guarantee; `.29` just also *writes down* the same fact, for observability and for your restart question below, but an orchestrator checking "is it safe to invoke `.27` for this id" must still go through `.27` itself (which enforces it), never `.29` alone.

`.29`'s **own** write path needs the equivalent atomic discipline at its own layer, independently — `INTENT_CREATED` for a given `client_order_id` must be an atomic, `O_CREAT|O_EXCL`-style operation (create-the-file-if-and-only-if-it-doesn't-exist) so two racing callers can't create two records for the same id. This was your own §7 requirement from the prior review, carried into this spec as a hard requirement, not a suggestion.

---

## 5. Handling `DUPLICATE_CLAIM_REJECTED` as an anomaly, not a new intent

If `.27` ever reports `DUPLICATE_CLAIM_REJECTED` for a `client_order_id` `.29` already has a record for, that's evidence something (a bug, a misbehaving orchestrator, an operator error) tried to resubmit an already-claimed intent. `.29` must **not** create a second record — it appends `DUPLICATE_CLAIM_REJECTED` as a new event on the **existing** record and, per the Implementation Spec's §5.4 asymmetric-mismatch rule ("any case requiring an inference rather than a direct evidence match always produces a `HUMAN` record, never an automatic best-guess fix"), this specific anomaly should route straight to `ESCALATED_HUMAN_REVIEW` rather than being silently absorbed — a legitimate intent doesn't get resubmitted; something unexpected happened and a human should see it.

---

## 6. Restart recovery — your two questions, answered concretely

**"What did AURA believe happened to this intent before the process stopped?"** — `get_intent(client_order_id)` reads the per-intent file directly off disk and returns the full event log plus `current_state`. Durable by construction (one file per intent, on disk, `record_hash`-verified on read — a corrupted or tampered file is detected, not silently trusted).

**"Was this intent ever claimed for consumption?"** — answerable two ways, and the spec is explicit about which one is authoritative: `.29`'s own record shows whether a `CLAIMED` event exists (fast, convenient, good enough for reporting/dashboards); but per §4, the **authoritative** answer for anything safety-relevant is `.27`'s own claim file, which `.29` does not replace.

**For resuming after a restart**, `.29` exposes `list_unresolved_intents()` — every record whose `current_state` is non-terminal (`CLAIMED`, `SUBMISSION_ATTEMPTED`, `AWAITING_RECONCILIATION`, `RECONCILING`). Whatever orchestrator exists (still not built — the same open question the wiring survey raised) calls this on startup and resumes each one by re-invoking `.28`/`.30`, never by re-deriving state from `.27`'s claim files alone. This directly closes the gap named as open in the prior review's §6.

---

## 7. Interface (signatures only, no implementation)

```
create_intent(client_order_id, symbol, side, quantity, direction, spec_fingerprint) -> IntentRecord
    # Atomic create; refuses (does not overwrite) if client_order_id already has a record.
    # direction in {OPEN_LONG, OPEN_SHORT, CLOSE_LONG, CLOSE_SHORT} -- locked 2026-09-09,
    # see Sec.2. Immutable once set.

record_claimed(client_order_id, claimed_at) -> IntentRecord
    # Requires current_state == NEW. Mirrors .27's claim fact -- see §4.

record_submission_attempted(client_order_id) -> IntentRecord
    # Requires current_state == CLAIMED.

record_submission_outcome(client_order_id, outcome, mexc_order_id=None, raw_response_ref=None) -> IntentRecord
    # outcome in {SUBMISSION_ACKNOWLEDGED, SUBMISSION_REJECTED, EXECUTION_UNCERTAIN,
    #             DUPLICATE_CLAIM_REJECTED}
    # Requires current_state == SUBMISSION_ATTEMPTED, EXCEPT
    # DUPLICATE_CLAIM_REJECTED, which is legal from ANY non-terminal state
    # and routes to ESCALATED_HUMAN_REVIEW per §5.

record_reconciliation_attempt(client_order_id, observed_snapshot_hash, order_status,
                               order_terminal_on_exchange=None) -> IntentRecord
    # Called by .30. order_status is .28's six-value enum. UNRESOLVED keeps
    # current_state at RECONCILING (or moves it there from AWAITING_RECONCILIATION);
    # a terminal value transitions to the matching RECONCILED_*/TERMINAL_* state.
    # Requires current_state in {AWAITING_RECONCILIATION, RECONCILING}.
    #
    # order_terminal_on_exchange (locked 2026-09-09): required (non-None) whenever
    # order_status == PARTIALLY_FILLED; determines whether THIS RECONCILED_PARTIALLY_FILLED
    # record accepts further RECONCILIATION_ATTEMPT events (False) or is now terminal
    # (True) -- see the refined rule in Sec.3. Ignored/None for every other order_status.

escalate_to_human(client_order_id, reason) -> IntentRecord
    # Called by .30 under one of three distinct conditions (locked 2026-09-09,
    # calibration of the third still TBD -- see §10.2):
    #   1. Immediately unreconcilable / conflicting evidence -> escalate now.
    #   2. Temporarily insufficient evidence -> NOT escalated; record stays
    #      RECONCILING, .30 tries again later.
    #   3. Evidence stays insufficient beyond a configurable staleness policy
    #      -> escalate. The threshold itself is not fixed here; it is meant
    #      to be established from real MEXC live-observation behavior, not
    #      invented (e.g. no assumed "15 minutes = human review").
    # .29 does not decide which of the three applies -- that judgment belongs
    # to .30, which is why .30's spec must define the evidence matrix that
    # distinguishes case 1 from case 2 from case 3.

get_intent(client_order_id) -> IntentRecord | None
    # Reads and hash-verifies one record. See §6.

list_unresolved_intents() -> list[IntentRecord]
    # Every non-terminal record. See §6, the restart-recovery entry point.

verify_intent(record) -> (bool, list[str])
    # Recomputes record_hash and checks the event sequence is a legal path
    # through §3's table -- the same discipline .18's verify_ledger()/
    # verify_observed() already apply to their own inputs.
```

**Library-first, CLI optional (locked 2026-09-09):** `.29` is fundamentally a state/ledger component; the production orchestrator calls its functions directly — a CLI must never become a second execution pathway. A read-only reporting CLI is still worth having alongside the library interface, strictly for diagnostics/recovery — mirroring `.17`/`.18`'s existing `print_report()` convention — e.g. `--list-unresolved` and `--show <client_order_id>`. Read-only, no mutating flags.

---

## 8. Storage

**Configurable, not hardcoded (locked 2026-09-09):** the exact production path is not fixed here — it is a configuration value (e.g. `AURA_MEXC_INTENT_LEDGER_DIR`, matching the existing env-var convention), established when the execution runtime is actually integrated. The placeholder used through this spec — `regime_output\mexc_intent_ledger\intents\<client_order_id>.json` — mirrors `.27`'s own `claims-dir` pattern, and for the same reason: an unbounded, growing set of intents cannot be one giant JSON blob re-hashed as a whole the way `.12`–`.19`'s small, fixed symbol set can be (that pattern doesn't scale, and re-hashing everything on every single intent's state change would be both wasteful and a race hazard). Each file is independently hash-chained over its own fields (§2) — never a hash over the whole directory.

Every append: read the current file, verify its `record_hash`, append the new event, recompute `record_hash`, write to a temp file, then atomically rename over the original — so a crash mid-write leaves the last-known-good record intact rather than a truncated/corrupt one. `create_intent()`'s first write uses `O_CREAT|O_EXCL` per §4.

`client_order_id`'s existing safe-charset validation (`.27`'s `re.fullmatch(r"[A-Za-z0-9._-]+", ...)`, ≤48 chars) already makes it a safe filename — no new sanitization needed.

---

## 9. Guardrails (matching the existing file convention)

```
{
  "market_data_fetch": false,
  "signal_recalculation": false,
  "risk_recalculation": false,
  "position_sizing": false,
  "position_creation": false,
  "exchange_state_mutation": false,
  "orders_allowed": false,
  "reconciliation_decision_authority": false,   # .30 decides, .29 only records
  "replay_protection_authority": false,          # .27's claim file remains authoritative, see §4
  "fabricated_event": false,
  "fail_closed": true
}
```

---

## 10. Decisions (locked 2026-09-09)

All four items below were open questions in the prior draft; each is now decided.

1. **`RECONCILED_PARTIALLY_FILLED` terminality — refined 2026-09-09, superseding the original "always non-terminal" decision.** Terminal exactly when the observed order itself is terminal on MEXC's side (canceled/closed) with `0 < filled < requested`; non-terminal when the order is still open on MEXC with a partial fill so far (more execution can still occur). See the refined rule in §3 and the `order_terminal_on_exchange` field in §7's `record_reconciliation_attempt()`.
2. **Escalation threshold — not calibrated yet, but the decision structure is locked.** `.30` must distinguish three cases, not one flat timeout: (1) immediately unreconcilable/conflicting evidence → escalate now; (2) temporarily insufficient evidence → stay `RECONCILING`; (3) evidence stays insufficient beyond a configurable staleness policy → escalate. No timeout number (e.g. "15 minutes") is invented here — the actual threshold is established later from real MEXC live-observation behavior. `.30`'s spec must define the evidence matrix that distinguishes case 1 from case 2 from case 3 — see the new `.30` spec.
3. **Library-first, CLI optional.** `.29` is a state/ledger component; the production orchestrator calls its functions directly. A read-only diagnostic/recovery CLI may exist alongside it but must never become a second execution pathway.
4. **Storage path is configurable, not hardcoded.** A config value (placeholder name `AURA_MEXC_INTENT_LEDGER_DIR`) is defined at implementation time; the canonical path is established when the execution runtime is integrated, not fixed in this spec.

Plus one additional rule locked this pass, not from the original open-items list: the **evidence-authority rule** in §3 — no `.29` transition may imply an exchange fact `.29` has not itself received as evidence (e.g. `SUBMISSION_ACKNOWLEDGED` can never shortcut straight to a `RECONCILED_*` state; only `.30` + `.28` observation evidence may produce one).

---

## 11. Test requirements sketch (for when code begins — not built now)

Per-component before implementation is considered ready, mirroring the Implementation Spec's own §7 pattern: atomic `create_intent()` concurrency test (two racing creates, exactly one succeeds); every illegal transition in §3's table is refused, not silently accepted; `record_hash` tamper detection (`verify_intent()` catches a hand-edited event); the `SUBMISSION_DUPLICATE_CLAIM_REJECTED`-on-an-existing-record anomaly path (§5) routes to `ESCALATED_HUMAN_REVIEW`, never a second record; crash-mid-write recovery (a killed process during the temp-write/rename leaves the prior record intact); `list_unresolved_intents()` correctly excludes every terminal state and includes every non-terminal one across a mixed fixture.

---

**Status: DESIGN LOCKED 2026-09-09 (including the §10.1 refinement above).** No code written. Next: implementation planning for `.29` + `.30` together (files/modules, interfaces, storage layout, `.27`/`.28` integration points, error/crash behavior, test order, offline-vs-live-verification boundary) — still design/planning, not code, per explicit instruction. See `AURA_v05329_30_MEXC_IMPLEMENTATION_PLAN_2026-09-09.md`.

---

**Implementation note added at commit time (2026-09-09):** during test-driven implementation, `TERMINAL_DUPLICATE_CLAIM_REJECTED` as a distinct state name (referenced above in §3's table) was found to be vestigial — §5's own text, and the already-locked test matrix, both establish that a `DUPLICATE_CLAIM_REJECTED` event routes straight to `ESCALATED_HUMAN_REVIEW` from any state, never to a separate terminal state of its own. The implementation uses `ESCALATED_HUMAN_REVIEW` exclusively for this anomaly path, consistent with §5's already-locked text; `TERMINAL_DUPLICATE_CLAIM_REJECTED` was never implemented as a reachable state. This is a documentation/implementation consistency fix grounded in already-locked text, not a new design decision.
