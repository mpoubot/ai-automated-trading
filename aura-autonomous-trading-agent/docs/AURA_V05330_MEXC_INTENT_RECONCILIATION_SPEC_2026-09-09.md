# `.30` MEXC Intent Reconciliation — Specification

Design/spec only, per your explicit sequencing. No code written. Grounded in: `.29`'s now-locked spec (in particular §10.2's three-case escalation structure and the evidence-authority rule), `.18`'s actual reconciliation implementation (`reconcile_symbol()`, its per-symbol state machine, its fail-closed input verification), and `.28` exactly as built and committed (its real six-value `order_status` enum, its actual `reason` strings, and — importantly — the specific evidence gaps its current output has, which this spec has to work around rather than assume away).

**Date:** 2026-09-09

---

## 1. What `.30` is and isn't

`.30` is the component that looks at one MEXC intent's evidence and decides what `.29`'s `current_state` should become next. It is `.18`'s direct conceptual successor — same "reconcile a recorded intent against an independent observation, fail closed on ambiguity" responsibility — rewired for `client_order_id`-keyed, many-intents-over-time reconciliation instead of `.18`'s symbol-keyed, single-cycle reconciliation.

It does **not**: call MEXC directly (that's `.28`'s job; `.30` consumes an already-produced `.28` snapshot); decide what was submitted (that's `.27`, already recorded by `.29`); mutate `.27`'s claim file; fabricate any fact not present in its inputs; skip straight to a `RECONCILED_*`/`TERMINAL_*` state without the matching evidence (the evidence-authority rule locked into `.29`'s spec applies here with equal force — `.30` is the *only* thing allowed to produce that evidence-backed judgment, and it must be able to point at exactly which input fields justified it).

`.30`'s output is a verdict that drives exactly one of `.29`'s three post-`AWAITING_RECONCILIATION` mutators: `record_reconciliation_attempt(...)` (stay `RECONCILING`, or move to a `RECONCILED_*`/terminal state), or `escalate_to_human(...)` (per your three-case structure). `.30` never writes to `.29`'s storage directly — it always goes through `.29`'s own interface, same separation `.27`/`.28` already keep from each other.

---

## 2. Inputs (per invocation, one intent at a time)

1. **`.29`'s current record** for the `client_order_id` being reconciled — specifically: `current_state` (must be `AWAITING_RECONCILIATION` or `RECONCILING`; anything else is a precondition violation `.30` refuses to act on), and the most recent `SUBMISSION_ACKNOWLEDGED`/`EXECUTION_UNCERTAIN` event's fields (so `.30` knows the MEXC order id `.27` was told about, if any, for cross-checking against what `.28` found).
2. **A freshly-produced `.28` snapshot** for the same `client_order_id`/`symbol` — `.30` does not itself decide when to re-query `.28`; that's an orchestrator concern (out of scope here, same as `.29`'s open orchestrator question). `.30` takes the snapshot as given and reconciles it.

`.30` verifies both the same way `.18` verifies its own two inputs today: `.29`'s record via `verify_intent()` (defined in `.29`'s spec), `.28`'s snapshot via its own `snapshot_hash` recomputation. Either verification failing is fail-closed — `.30` refuses to produce a verdict and reports the input as untrusted, never reconciles against unverified data.

---

## 3. The evidence matrix

This is the part you asked for particular attention on. Organized by: prior submission outcome (from `.29`) → `.28`'s `order_status` → supporting evidence (`position_exists`, `observed_fills`, and — where `.28`'s curated fields aren't enough — the raw evidence `.28` preserved) → verdict.

**Prior submission outcome** only has two live branches feeding `.30` at all, by construction: `SUBMISSION_ACKNOWLEDGED` and `EXECUTION_UNCERTAIN` are the *only* two outcomes that put a record into `AWAITING_RECONCILIATION` (`.29`'s own transition table, §3). `SUBMISSION_REJECTED` and `DUPLICATE_CLAIM_REJECTED` never reach `.30` at all — they resolve without reconciliation. Per `.29`'s locked note, `.30` treats `SUBMISSION_ACKNOWLEDGED` and `EXECUTION_UNCERTAIN` **identically** — no evidentiary head start for the acknowledged case, exactly as you specified.

**Step 1 — `.30` derives its own fill classification from raw evidence first (locked 2026-09-09).** Per your decision on §4(a): `.30` never trusts `.28`'s top-level `order_status` label for a terminal-state order (raw MEXC `state` `3` closed or `4` canceled) without independently checking `raw_evidence["order"]`'s own `dealVol` against the order's requested `vol`. Raw MEXC evidence is authoritative; `.28`'s normalized status is an observation, not permission to discard contradictory detail — exactly the reason `.28` was built to preserve raw evidence verbatim in the first place. Whenever `.30` has a resolved order (`match_count == 1`) in a terminal MEXC state, it computes:

```
dealVol == 0                  -> CANCELED_CLEAN
0 < dealVol < vol              -> PARTIAL_ON_TERMINAL_ORDER
dealVol >= vol                 -> FILLED_CLEAN
```

independently of whatever `.28`'s own `order_status` said — `.28`'s label becomes a cross-check, not the decision. A mismatch between `.30`'s own dealVol-derived read and `.28`'s label (e.g. `.28` said `CANCELED` but `dealVol > 0`) is itself informative and is carried into the verdict's `reason`, never silently discarded.

**Step 2 — the full matrix, `.30`'s own classification as the primary key:**

| `.30`'s own classification (Step 1) | Supporting evidence (`direction` + `position_exists`) | Verdict | Escalation case (§5) |
|---|---|---|---|
| `FILLED_CLEAN` | any (see below) | `RECONCILED_FILLED` (terminal) — **order/fill evidence alone proves execution; a surviving position is not required.** Per your decision on §4(b)/(c): `.28`'s `FILLED` classification already requires substantiating `dealAvgPrice`/`updateTime` before it can be reported (`.28`'s own `read_observed_execution()` downgrades to `UNRESOLVED` otherwise) — so by the time `.30` sees a `FILLED_CLEAN` order, quantity/price/timestamp/`externalOid` are already consistent. `position_exists` and `direction` are recorded alongside the verdict as supporting context (e.g. `OPEN_* ` + position present is corroborating; `CLOSE_*` + position absent is corroborating) but neither can override a fill the order evidence itself substantiates, and neither is required to reach the verdict. | — |
| `PARTIAL_ON_TERMINAL_ORDER` (i.e. the order itself is already canceled/closed on MEXC's side, with `0 < dealVol < vol`) | any | `RECONCILED_PARTIALLY_FILLED`, **`order_terminal_on_exchange = True` → TERMINAL (locked 2026-09-09, refined `.29` rule §10.1).** No more fills are coming; there is nothing left to wait for. | — |
| `PARTIAL_ON_OPEN_ORDER` (raw state `2`, `0 < dealVol < vol` — i.e. `.28`'s own `PARTIALLY_FILLED` on a still-open order) | any | `RECONCILED_PARTIALLY_FILLED`, **`order_terminal_on_exchange = False` → non-terminal, stays `RECONCILING`** (more execution can still occur) | — |
| `CANCELED_CLEAN` | any | `RECONCILED_CANCELED` (terminal) | — |
| `.28` `order_status == PENDING` (raw state `2`, no deal volume yet) | any | Not yet resolved, not alarming — stays `RECONCILING`, retry later | 2 |
| `.28` `order_status == REJECTED` | — | **Contradicts an earlier `SUBMISSION_ACKNOWLEDGED`/`EXECUTION_UNCERTAIN` — locked 2026-09-09: never silently converted to `TERMINAL_SUBMISSION_REJECTED`.** `.29`'s `TERMINAL_SUBMISSION_REJECTED` is reached only directly from `.27`'s own `SUBMISSION_REJECTED` outcome, with no `.30` involvement at all — a *pre-submission* rejection. A `.28`-observed `REJECTED` *after* an acknowledgment is a different, inconsistent-evidence situation with no clean terminal state of its own, so it escalates instead of being mapped onto a state that means something narrower than what actually happened. | 1 |
| `UNRESOLVED`, reason `NO_MATCHING_ORDER_FOUND_IN_WINDOW` | match_count 0 | Not found yet — could still appear (propagation delay, lookback window edge) | 2 |
| `UNRESOLVED`, reason `MULTIPLE_MATCHING_ORDERS_FOUND` | match_count > 1 | Genuine anomaly — an `externalOid` should be unique; more than one match means something is wrong with matching itself, not with waiting longer | 1 |
| `UNRESOLVED`, raw MEXC state outside `{2,3,4}` (unmapped by ccxt) | — | Could still resolve if MEXC's own state later moves into a mapped value | 2 → 3 if it never does |
| `UNRESOLVED`, reason `FILL_STATUS_WITHOUT_SUBSTANTIATING_PRICE_OR_TIMESTAMP` | order closed (state `3`) but `dealAvgPrice`/`updateTime` missing | Likely a transient MEXC-side eventual-consistency gap — retrying may resolve it | 2 → 3 if it never does |
| `UNRESOLVED`, reason `UNRECOGNIZED_ORDER_STATUS_VALUE` | — | `.28`'s own defense-in-depth catch fired — this indicates something `.30` doesn't recognize either, and should never be silently retried into obscurity | 1 |

**Why `UNRESOLVED` isn't one row:** this is the core reason the evidence matrix needed its own section rather than a flat timeout. `.28`'s `UNRESOLVED` value collapses several evidentially very different situations (order simply hasn't appeared yet vs. matching itself is ambiguous vs. genuinely unmapped MEXC state) into one enum value. `.30` must look past `order_status` alone into `.28`'s `reason` string to tell those apart — that distinction is exactly what separates your case 1 (escalate now) from case 2 (wait) in practice. Note that the previous draft's "closed (state 3) but dealVol != vol" `UNRESOLVED` row no longer applies as a separate case: Step 1's dealVol-first classification now resolves that situation directly into `FILLED_CLEAN`/`PARTIAL_ON_TERMINAL_ORDER`/`CANCELED_CLEAN` rather than leaving it `UNRESOLVED` — `.28`'s own conservative "closed but not confirmed" fallback is superseded by `.30` actually doing the dealVol comparison itself.

**On `direction` and `position_exists` (locked 2026-09-09, per your decision on §4(b)):** `position_exists`, read alongside the intent's `direction` (now part of `.29`'s data model — `OPEN_LONG`/`OPEN_SHORT`/`CLOSE_LONG`/`CLOSE_SHORT`), is corroborating context recorded with every verdict above, never a precondition for reaching one and never sufficient by itself to produce one. Concretely: an `OPEN_*` intent with `position_exists == True` after a fill, or a `CLOSE_*` intent with `position_exists == False` after a fill, is expected and gets no special verdict of its own — it's simply consistent. An `OPEN_*` intent whose fill left no position, or a `CLOSE_*` intent whose fill left a position still standing, is *inconsistent* and — per the "deterministic, never inferred" principle below — is not something `.30` explains away; it is recorded as a discrepancy in the verdict's evidence but does not, by itself, downgrade a `FILLED_CLEAN`/order-evidence-substantiated verdict, since (per §4(b)) order evidence proves execution and position evidence only explains current account state, which can legitimately move for reasons outside this one intent (other fills on the same symbol, MEXC position netting, etc.).

---

## 4. Evidence gaps — resolved 2026-09-09 (kept here for the record; see §3 for the resulting logic)

**(a) `.28`'s `CANCELED` classification doesn't check for a partial fill before cancellation — RESOLVED.** `.30` does not trust `order_status == CANCELED` alone; it independently derives `CANCELED_CLEAN`/`PARTIAL_ON_TERMINAL_ORDER`/`FILLED_CLEAN` from `raw_evidence["order"]`'s own `dealVol` vs `vol`, per §3 Step 1. This is not a fix to `.28` itself (that module stays as committed) — it's `.30` correctly treating `.28`'s normalized label as an observation to cross-check, not a conclusion to inherit.

**(b) `position_exists` is only interpretable with the intent's direction — RESOLVED.** `.29`'s data model now carries an explicit `direction` field (`OPEN_LONG`/`OPEN_SHORT`/`CLOSE_LONG`/`CLOSE_SHORT`, locked into `.29`'s spec §2/§7). But the resolution turned out to matter less than expected for the specific `FILLED + position_exists == False` case: per your decision, order/fill evidence alone (substantiated quantity, price, timestamp, `externalOid`) is sufficient to reach `RECONCILED_FILLED` without requiring a surviving position at all — `direction` + `position_exists` are recorded as supporting context on every verdict, but they inform interpretation and future closing-intent handling rather than gating the fill verdict itself. The anti-false-attribution principle stays intact: position evidence still never stands in as proof on its own.

**(c) `REJECTED` has no home in `.29`'s current terminal-state vocabulary if it shows up post-acknowledgment — RESOLVED.** Locked: a `.28`-observed `REJECTED` after `SUBMISSION_ACKNOWLEDGED`/`EXECUTION_UNCERTAIN` is never silently converted into `TERMINAL_SUBMISSION_REJECTED` (that state is reached only directly from `.27`'s own pre-submission `SUBMISSION_REJECTED`, with no `.30` step at all). A post-acknowledgment `REJECTED` observation is contradictory evidence and routes to `ESCALATED_HUMAN_REVIEW` instead — preserving the asymmetric-mismatch rule rather than being absorbed into a state that means something narrower than what actually happened.

**(d) A partial fill on an already-MEXC-terminated order — RESOLVED 2026-09-09.** Not a new state name — a refinement of `.29`'s existing `RECONCILED_PARTIALLY_FILLED` terminality rule. `.30` passes `order_terminal_on_exchange` (derived directly from the raw order's MEXC state: `True` when the order itself is canceled/closed, `False` when still open) alongside `order_status == PARTIALLY_FILLED` on every `record_reconciliation_attempt()` call. `.29` treats that specific record as terminal or non-terminal accordingly — see `.29`'s spec §3/§7 for the mechanics.

---

## 5. Escalation-case classification (applying your locked three-way split)

Restating your structure precisely, with `.30`'s actual conditions filled in from §3/§4:

1. **Immediately unreconcilable / conflicting evidence → escalate now.** Any matrix row marked "1" in §3's table: multiple matching orders, a post-acknowledgment `REJECTED` observation, an unrecognized status value.
2. **Temporarily insufficient evidence → stays `RECONCILING`, `.30` is invoked again later.** Rows marked "2": order not yet found in the lookback window, an unmapped-but-not-yet-terminal MEXC state, a closed order missing substantiating price/timestamp (assuming this resolves with a retry — see below).
3. **Stale beyond a configurable policy threshold → escalate.** Not a separate condition `.30` checks directly — it's what happens to a case-2 verdict that keeps recurring past a threshold `.30` is configured with (the threshold value itself: your call later, per `.29`'s §10.2, from real MEXC observation behavior, not invented here). Mechanically: each `RECONCILIATION_ATTEMPT` event `.29` already records carries a timestamp: `.30` (or the orchestrator) computes elapsed time / attempt count since the record entered `AWAITING_RECONCILIATION` and compares against the policy threshold before allowing another case-2 verdict to stand; past the threshold, `.30` produces `ESCALATED_HUMAN_REVIEW` instead of another "keep waiting" verdict, even though the *evidence itself* looks like case 2.

This means `.30` needs two things from its config, not one: the staleness threshold itself (open, per `.29`'s §10.2), and where the "how long has this been waiting" clock starts — proposed: the record's `AWAITING_RECONCILIATION` entry time (i.e. the time of the `SUBMISSION_ACKNOWLEDGED`/`EXECUTION_UNCERTAIN` event), not the time of `.30`'s first invocation, so a delayed orchestrator doesn't reset the clock.

---

## 5a. Architectural principle: deterministic, evidence-driven, never inferred (locked 2026-09-09)

`.30` never asks "what probably happened?" It only asks "does the supplied evidence satisfy the exact rule for a reconciled state?" Every verdict in §3's table traces to a specific, named rule over specific input fields — never a probability, a heuristic, or a best guess. Anything the matrix doesn't explicitly cover becomes one of exactly two things:

- `RECONCILING` (stay, evidence is incomplete but not contradictory), or
- `ESCALATED_HUMAN_REVIEW` (evidence is contradictory, or `.30` has no defined rule for it).

Never an inferred success. This is the same fail-closed discipline every other component in the chain already applies, stated explicitly here because `.30` is the one component whose entire job is *making a judgment call* — the principle exists precisely to bound what "judgment" is allowed to mean in this specific component.

**Locked chain diagram:**

```
.27 Submission Result
          |
          v
      .29 Intent
      Ledger
          |
          v
      .28 MEXC
     Observation
          |
          v
      .30 Rules
     /          \
    /            \
RECONCILED    UNRESOLVED
    |              |
    v              v
   .29        RECONCILING /
   state       HUMAN REVIEW
```

`.17`, `.18`, and `.15` remain untouched throughout.

**Full lifecycle sequence (locked 2026-09-09, supersedes the evidence-flow-only diagram above for describing the whole system):**

```
              ExecutionIntent
                    |
                    v
             Replay Protection      (.27's own claim file -- authoritative,
                    |                see .29 Sec.4)
                    v
                 .29
            Intent Ledger           (NEW -> CLAIMED)
                    |
                    v
                 .27
             MEXC Submit            (SUBMISSION_ATTEMPTED -> outcome)
                    |
                    v
                 .28
          MEXC Observation          (read-only snapshot)
                    |
                    v
                 .30
          Deterministic Rules       (Sec.3's matrix; never an inferred success)
                    |
                    v
              verdict/event
                    |
                    v
                 .29
            Updated State           (RECONCILING / RECONCILED_* / TERMINAL_* /
                                      ESCALATED_HUMAN_REVIEW)
```

Authority boundaries, restated: `.27` is the submission mechanism, `.28` is the observation mechanism, `.30` is the reconciliation decision, `.29` is the durable intent lifecycle. `.15` remains outside this implementation phase. The critical invariant across all five: **no component infers execution merely because an order submission was acknowledged.**

---

## 6. Interface (signatures only)

```
classify_terminal_order(raw_order_info) -> "FILLED_CLEAN" | "PARTIAL_ON_TERMINAL_ORDER"
                                          | "CANCELED_CLEAN" | None
    # Sec.3 Step 1's dealVol-vs-vol classification. Takes the raw MEXC
    # order dict (raw_evidence["order"] from .28's snapshot) directly --
    # never .28's own order_status label. Returns None when the order's
    # raw state is not terminal (open / unmapped) -- caller falls through
    # to the .28-order_status-driven branches in reconcile_intent(). The
    # None-vs-not-None split IS order_terminal_on_exchange (locked
    # 2026-09-09) -- reconcile_intent() passes that boolean straight
    # through to .29's record_reconciliation_attempt() whenever the
    # verdict's order_status == PARTIALLY_FILLED. This is the piece of
    # logic most likely to need revision as real MEXC behavior is
    # observed; kept isolated from reconcile_intent()'s otherwise-stable
    # matrix so that revision stays small and auditable.

reconcile_intent(intent_record, observed_snapshot, staleness_policy) -> ReconciliationVerdict
    # Pure function -- no I/O, no MEXC calls, no writes to .29 itself.
    # Verifies both inputs (record_hash / snapshot_hash) before reasoning
    # about them; a verification failure produces a BLOCKED verdict, not
    # a best-effort guess. Calls classify_terminal_order() first when a
    # resolved order is present; falls back to .28's own order_status for
    # PENDING/UNRESOLVED/REJECTED. Reads intent_record.direction and
    # observed_snapshot.position_exists as supporting context recorded on
    # the verdict, never as a precondition for reaching one (Sec.3/Sec.4b).
    # Every branch corresponds to exactly one Sec.3 table row -- nothing
    # outside the table produces RECONCILED_*; unmatched evidence produces
    # RECONCILING or ESCALATE per Sec.5a, never an inferred success.
    #
    # Returns one of:
    #   {"action": "RECORD_ATTEMPT", "order_status": <.28's value>,
    #    "target_state": "RECONCILING" | "RECONCILED_FILLED" |
    #                     "RECONCILED_PARTIALLY_FILLED" | "RECONCILED_CANCELED",
    #    "order_terminal_on_exchange": bool | None,  # required (non-None)
    #        # whenever order_status == PARTIALLY_FILLED (Sec.3/.29 Sec.3
    #        # refined rule); None for every other order_status.
    #    "reason": "<matrix row matched>",
    #    "evidence": {"direction": ..., "position_exists": ..., "dealVol_check": ...}}
    #   {"action": "ESCALATE", "reason": "<matrix row / staleness matched>"}
    #   {"action": "BLOCKED", "reason": "<input verification failure>"}

check_staleness(intent_record, staleness_policy, now) -> bool
    # True if the record has been in AWAITING_RECONCILIATION/RECONCILING
    # long enough (per §5's clock-start rule) that a case-2 verdict should
    # be upgraded to case 3 (escalate) instead of standing as-is.

apply_verdict(client_order_id, verdict) -> IntentRecord
    # Thin wrapper that calls exactly one of .29's own mutators
    # (record_reconciliation_attempt / escalate_to_human) based on
    # verdict["action"] -- .30 never writes .29's storage directly.
```

---

## 7. Does `.30` have its own persisted output? — RESOLVED 2026-09-09: no

Locked: `.30` is a **deterministic reconciliation engine with no storage of its own** — `.29` intent + `.28` observation in, a verdict out. It never becomes a second, competing state store. `.29`'s event log (`RECONCILIATION_ATTEMPT` events, already storing `observed_snapshot_hash` + `order_status` + now `order_terminal_on_exchange`) is the **sole** authoritative record of every reconciliation `.30` has ever performed for a given intent. This is the specific failure mode being designed against: `.29` says `FILLED`, `.30` says `UNRESOLVED`, and a separate `.30` file says something else again — three sources of truth that can silently drift apart. There is exactly one authoritative intent lifecycle, and it lives in `.29`.

If an audit artifact is ever needed beyond what `.29`'s own event log already provides, it must take the form of a further append/update **to the `.29` record itself** (e.g. a richer `evidence` sub-object on the `RECONCILIATION_ATTEMPT` event, which `.30`'s verdict already carries — see §6's `reconcile_intent()` return shape), never a second file `.30` maintains independently. Any operator-facing report reads `.29`'s records directly (via `.29`'s proposed reporting CLI), same as before.

---

## 8. Guardrails (matching the existing file convention)

```
{
  "market_data_fetch": false,
  "signal_recalculation": false,
  "risk_recalculation": false,
  "position_sizing": false,
  "position_creation": false,
  "exchange_state_mutation": false,
  "orders_allowed": false,
  "reconciliation_decision_authority": true,   # this IS .30's job
  "reconciliation_output_authority": false,     # writes go through .29's own mutators only
  "fabricated_fill": false,
  "fabricated_position": false,
  "fail_closed": true
}
```

---

## 9. Staleness threshold — RESOLVED 2026-09-09 (mechanism locked, value deferred)

Not hardcoded. A configuration parameter, `reconciliation_staleness_minutes`, explicitly marked calibration-required in whatever config surface `.30` eventually reads it from — the number itself comes from controlled MEXC observation later, never invented here. What's locked now is that `.30`'s behavior is fully deterministic **once that parameter is supplied**: the clock starts at the record's `AWAITING_RECONCILIATION` entry time (per §5, unchanged), and a case-2 "stay `RECONCILING`" verdict is upgraded to case-3 "escalate" the moment elapsed time exceeds `reconciliation_staleness_minutes`, regardless of what that number turns out to be.

---

## Status

**All open items resolved 2026-09-09.** Decisions 1–4 from your first `.30` message, plus this message's three final resolutions (§3/§4(d)'s `order_terminal_on_exchange` refinement, §9's `reconciliation_staleness_minutes` config parameter, §7's "no separate output store"), are locked throughout this document. No code written. Design phase for `.29` + `.30` is complete; next is the implementation planning pass — see `AURA_v05329_30_MEXC_IMPLEMENTATION_PLAN_2026-09-09.md`.

---

**Implementation note added at commit time (2026-09-09):** implemented and test-verified exactly as specified above, including the dealVol-first classification (Step 1) and the `T30-CANCEL-02` mislabeling regression test. `.30`'s own contract suite and the full `.29`+`.30` integration suite (real `.27`/`.28` wiring, all `TI-*` traces, crash/restart, replay/concurrency) both pass; see the implementation plan document for the final test inventory.
