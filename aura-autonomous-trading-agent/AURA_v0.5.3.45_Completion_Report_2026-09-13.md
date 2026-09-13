# AURA v0.5.3.45 Completion Report — Alpaca Execution Resolution Authority

**Date:** 2026-09-13
**Scope:** Milestone `.45` on the v0.5.5 Final Implementation Baseline — the Alpaca-side equivalent of MEXC's `.29`/`.30` reconciliation pipeline, resolving an ambiguous or acknowledged Alpaca order outcome to a definitive terminal state (or an explicit escalation), never auto-retrying. Built per Martin's three explicit scoping answers (AskUserQuestion, 2026-09-13).

Fourth milestone in the reordered build sequence (`.42`→`.43`→`.44`→`.45`→...→`.50`, then back to `.41`, then `.51`→`.53`).

---

## 1. Audit (per the standing 7-step milestone procedure)

A dedicated audit agent (plus direct verification of the installed `alpaca-py` and `ccxt` source) found:

- **`EXECUTION_UNCERTAIN` is an established, repo-wide status, not something to invent.** It originates in `.27`'s `classify_submission_exception()` (defaults any non-refusal exception to it, never guessed into `REJECTED`), flows through MEXC's `.29` ledger and `.38`'s Alpaca supervisor identically, and `.40`'s own docstring explicitly names it as a known, deliberately-deferred gap: *"An Alpaca `EXECUTION_UNCERTAIN` outcome recorded here has NO automated resolution path."* `.45` is exactly the module `.40` anticipated.
- **Alpaca order submission is real, not a stub.** `.35.submit()` (paper-only, network-capable) is called live by `.38`'s supervisor; `.38` wraps any exception around that call into `EXECUTION_UNCERTAIN` since `.35` itself has no basis to distinguish a broker refusal from a timeout. **`.38` is the last milestone that actually touches a real Alpaca order** — `.45` needed no new prerequisite submission milestone.
- **`observe_and_reconcile_alpaca_equity_execution()` in `.38` is an explicit, tested placeholder** — `{"status": "NOT_YET_IMPLEMENTED", "reason": "NO_ALPACA_EQUITY_OBSERVATION_RECONCILIATION_SPINE_EXISTS"}`, proven by its own regression test. `.45` is the real implementation this placeholder was always meant to be replaced by (wiring `.45` into that call site is left to a later step, consistent with `.44`'s own precedent of building the decision layer before wiring it into the execution spine).
- **MEXC's own vocabulary for "we don't know what happened"** (`.29`'s `UNRESOLVED`/`RECONCILING`, `.30`'s `reconcile_intent()`/`classify_terminal_order()`/`classify_unresolved()`/`check_staleness()`/`apply_verdict()`) is a mature, evidence-only, never-inferred pattern — reused conceptually for `.45` rather than reinvented (see §2).
- **`alpaca-py`'s `OrderStatus` enum has 17 values**, most of them "in-flight/ambiguous" (`NEW`, `PENDING_NEW`, `ACCEPTED`, `PENDING_REVIEW`, etc.) beyond the terminal `FILLED`/`CANCELED`/`REJECTED`/`EXPIRED` — verified by reading the installed package directly, not assumed. `alpaca-py` has no client-side request timeout and no built-in protection against a submission that timed out client-side but landed server-side — resolving that ambiguity is entirely on AURA's own side, which is precisely `.45`'s job.

---

## 2. Reuse scan (Martin's A/B/C/D framework)

| Component | Classification | Reasoning |
|---|---|---|
| `.30.reconcile_intent()` / `classify_terminal_order()` / `classify_unresolved()` / `check_staleness()` / `apply_verdict()` | (B) shape adapted, not imported | The pure-function/evidence-only/never-infer discipline, the verdict-then-separate-writer split, and the staleness-keyed-off-the-original-event-timestamp pattern are all reused directly in `.45`'s design. The function bodies are not reused because Alpaca's evidence (an `alpaca-py` `Order`, `status`/`filled_qty`/`qty`) is structurally different from MEXC's raw order dict (`dealVol`/`vol`/`state`) — copying the bodies would be copying the wrong shape, per Martin's explicit "do not import functionality merely because it exists elsewhere" instruction. |
| `.40`'s audit trail (`verify_record`, `derive_state`, atomic-write/lock, `record_outcome` conventions) | (A) directly reused and extended in place | Per Martin's explicit answer 2 ("extend `.40`'s audit trail"), no second Alpaca ledger was built. See §3 for the exact extension. |
| `alpaca-py`'s `OrderStatus` enum, `Order` model, `alpaca.common.exceptions.APIError` | Verified directly (A, as data, not as logic) | Field names (`status`, `filled_qty`, `qty`, `client_order_id`) and `APIError.status_code` read from the installed package source via `inspect.getsource()` — not assumed from memory or documentation. |
| CAURA / BABIL / DELTAX×3 / `mexc_bot` | (D) not applicable | Confirmed by both `.44`'s and this milestone's reuse scans: no sibling project has any Alpaca-specific or broker-order-resolution logic at all. |

---

## 3. What was newly implemented

### `.40` extension (minimal, additive, backward-compatible)

`aura_v05340_pre_submission_revalidation.py` gained exactly one new event type, `RESOLVED`, legal only immediately after any `OUTCOME:*` state (previously terminal with no legal follow-on), producing a new terminal state `RESOLVED:{resolution}`. A new mutator, `record_resolved(client_order_id, *, resolution, detail=None, proven_failed=None, base_dir=None)`:
- validates `resolution` against a closed vocabulary (`RESOLUTIONS`) — `RESOLVED_FILLED`, `RESOLVED_PARTIALLY_FILLED`, `RESOLVED_CANCELED`, `RESOLVED_REJECTED`, `ESCALATED_HUMAN_REVIEW`;
- **hard-refuses to run on a MEXC record** (`RESOLVED_EVENT_NOT_PERMITTED_FOR_VENUE`) — MEXC's `.29`/`.30` remain sole authority for MEXC truth; `.45` can never create a second, independently-derived opinion there;
- carries `proven_failed` as honest evidence-only metadata (never acted on by this module — see §1's third scoping answer).

**Backward compatibility verified**: this is additive only — `RESOLVED` did not exist as a legal event before this change, so no previously-recorded record's derived state can be affected. `.40`'s full pre-existing 23-test suite was re-run unchanged and passes (23/23).

### New file: `aura_v05345_alpaca_execution_resolution_authority.py` (~250 lines)

- **`fetch_alpaca_order_state(client, client_order_id)`** — injectable-client query wrapper (matching `.35`/`.40`'s convention). A confirmed 404 (`APIError.status_code == 404`) returns `{"found": False}`; any other failure raises `ResolutionQueryError`, never silently treated as "not found" or any other bucket.
- **`classify_alpaca_order_state(order_state)`** — the evidence matrix, mapping Alpaca's `status`/`filled_qty` onto one of: `FILLED`, `REJECTED`, `CANCELED` (zero fill), `PARTIALLY_FILLED_TERMINAL` (canceled/expired with a partial fill — no more fills coming), `PARTIALLY_FILLED_OPEN` (still live), `STILL_OPEN` (11 in-flight statuses), `NOT_FOUND`, `UNHANDLED` (`replaced` — AURA never replaces its own orders), or `UNRECOGNIZED` (any status value not in the mapping — never guessed past).
- **`check_staleness(audit_record, staleness_policy, now)`** — mirrors `.30.check_staleness()` exactly: off by default (`staleness_policy=None` → never auto-escalate on time alone, no invented threshold), keyed off the original `OUTCOME` event's own timestamp.
- **`resolve_alpaca_execution(audit_record, order_state, staleness_policy=None, now=None)`** — the pure decision function. Verifies the audit record first (`BLOCKED` on failure, never a best-effort guess); confirms it's an `ALPACA` record at `OUTCOME:*` (`BLOCKED` otherwise); classifies the evidence; returns `RESOLVE` (with a definitive `resolution` and a `proven_failed` flag), `STAY_PENDING` (nothing decided yet), or `ESCALATE`.
- **`apply_verdict(client_order_id, verdict, base_dir=None)`** — the sole writer: `RESOLVE`/`ESCALATE` call `.40.record_resolved()`; `STAY_PENDING`/`BLOCKED` write nothing.

### Martin's three scoping answers, as implemented

1. **"Both, mirror MEXC"** — `resolve_alpaca_execution()` takes any `.40` record whose `current_state` starts with `OUTCOME:` — this covers `OUTCOME:SUBMITTED` and `OUTCOME:EXECUTION_UNCERTAIN` identically, through the exact same evidence matrix, exactly mirroring `.30`'s identical treatment of `SUBMISSION_ACKNOWLEDGED` and `EXECUTION_UNCERTAIN`. Verified by a dedicated test (`test_resolve_clean_submitted_outcome_to_filled_same_pipeline`).
2. **"Extend `.40`'s audit trail"** — done exactly as described in §3; no second Alpaca ledger exists anywhere in this repo.
3. **"Resolve/escalate only, no auto-retry"** — `resolve_alpaca_execution()`/`apply_verdict()` have exactly two possible actions on a resolved outcome: `RESOLVE` (terminal state) or `ESCALATE` (terminal-for-automation, human-review state). Neither ever calls `.35.submit()`, `.27`, or any other submission path — verified by a static source-guard test in addition to the behavioral tests. The `proven_failed` evidence flag exists purely for transparency (a future, separately-approved capability could read it); this module takes no action on it.

### One deliberate simplification relative to MEXC (disclosed, not hidden)

MEXC's `.29`/`.30` append a ledger event on *every* reconciliation pass, including non-terminal "stay RECONCILING" ones. `.45`'s `STAY_PENDING` verdict writes nothing to `.40` — the record simply remains at `OUTCOME:*` until a later pass has something new to report. This keeps `.40`'s audit trail from accumulating an unbounded number of "nothing changed" events for an order still legitimately in flight; the queryable conclusion ("no `RESOLVED` event yet = still open") is identical either way. Flagged here explicitly so Martin can ask for per-attempt logging if it's wanted.

---

## 4. Tests

`tests/test_aura_v05345_alpaca_execution_resolution_authority.py` — **40 tests**, built on real `.40` audit records (created via `.40`'s own `record_decision`/`record_authorized`/`record_revalidated`/`record_consumed`/`record_submission_attempted`/`record_outcome` — not stand-ins) and an injectable `FakeAlpacaClient`/`FakeOrder` (no real network call, no real credentials). Covers:

- **The full evidence matrix**: every `OrderStatus` bucket (`filled`, `rejected`, `canceled` with/without partial fill, `expired`, `partially_filled`, all 11 still-open statuses, `replaced`, an unrecognized future status, and "not found").
- **The query layer**: a successful fetch, a confirmed 404 (`found: False`), a non-404 API error and a plain network error (both raise `ResolutionQueryError`, never silently "not found"), a missing-status response, and an enum-like status object read via `.value`.
- **Both scoped outcome types resolved through one pipeline**: `EXECUTION_UNCERTAIN` and a clean `SUBMITTED` outcome both resolve identically.
- **`proven_failed` correctness**: `True` for `REJECTED`/`CANCELED`-zero-fill, `False` for `FILLED`/partial-fill outcomes.
- **Staleness**: not-yet-stale stays pending; a `STILL_OPEN` or `NOT_FOUND` case past the configured threshold escalates — using the original `OUTCOME` event's own timestamp, not the resolution attempt's time.
- **`BLOCKED` verdicts**: a non-Alpaca (MEXC) record, a record not yet at `OUTCOME:*`, and a tampered/hash-mismatched record.
- **The `.40` write path**: `RESOLVE` and `ESCALATE` both produce a verified, hash-consistent `RESOLVED:*` terminal state; `STAY_PENDING`/`BLOCKED` write nothing (record unchanged); `record_resolved()` hard-refuses a MEXC record and an invalid resolution value; a second `RESOLVED` event after one is already recorded is rejected by `.40`'s state machine (a resolution is final).
- **Determinism**: identical inputs produce an identical verdict.
- **A static guard** confirming the module's executable code never references `submit_order`, `cancel_order`, `.submit(`, or `replace_order` — enforcement/resolution-only by construction.

Two test-authoring bugs were found and fixed during first-run verification (both test-fixture issues, not module bugs): (1) a staleness "not yet stale" test compared a fixed test clock (`NOW`) against an `OUTCOME` event timestamped with the real wall clock at test-run time, occasionally producing a spurious multi-hour "elapsed" gap — fixed by explicitly pinning the `OUTCOME` event's timestamp in that test, matching the already-correct pattern used in the "escalates when stale" tests; (2) the "resolution is final" test expected `.40`'s existing (unmodified) `AuditTrailError`, but `record_resolved()` — consistent with `.40`'s own pre-existing `_append_event()` convention — lets the state machine's `IllegalTransitionError` propagate directly rather than wrapping it; the test was corrected to expect either exception type.

## 5. Verification

- **New tests:** 40/40 passed.
- **`.40`'s pre-existing suite (backward compatibility):** 23/23 passed, unchanged.
- **Full regression suite:** 692/692 passed (was 652 prior + 40 new; +40, 0 removed, 0 modified elsewhere).
- **Lint (`ruff check`):** clean on all three touched/new files (0 errors).
- **Manual smoke test:** run directly against a real `.40` record before the automated suite was written — confirmed `OUTCOME:EXECUTION_UNCERTAIN` → `RESOLVED:RESOLVED_FILLED` end-to-end, with `.40.verify_record()` passing on the result.

## 6. Known limitations / what remains open

- **Not wired into `.38`'s `observe_and_reconcile_alpaca_equity_execution()` placeholder.** This milestone delivers the resolution-decision engine and the storage extension; connecting it to a real periodic/triggered resolution pass (calling `fetch_alpaca_order_state()` against a real Alpaca client, then `resolve_alpaca_execution()`/`apply_verdict()`) is a distinct orchestration step, mirroring how `.44`'s enforcement decision is not yet wired into `.31`/`.36`.
- **No automated retry**, per Martin's explicit answer — a `proven_failed` order (confirmed rejected or canceled with zero fill) is resolved and recorded, but resubmitting it (under a necessarily new `client_order_id`, since `.37`/`.38`'s claims never release) is left to a human or a future, separately-approved capability.
- **`STAY_PENDING` writes nothing to `.40`**, unlike MEXC's per-attempt logging — a deliberate, disclosed simplification (§3), not an oversight.
- **The 404-detection relies on `APIError.status_code`**, verified against the installed `alpaca-py` source; if a future SDK version changes how a "not found" response surfaces, `fetch_alpaca_order_state()`'s 404 check would need revisiting (would currently raise `ResolutionQueryError` instead of returning `{"found": False}` — a safe failure mode, not a silent misclassification, but worth flagging).

## 7. Commit

Checkpoint commit to be created locally after this report is saved (files: `aura_v05340_pre_submission_revalidation.py` (extended), `aura_v05345_alpaca_execution_resolution_authority.py` (new), `tests/test_aura_v05345_alpaca_execution_resolution_authority.py` (new), this report). **Not pushed to GitHub**, per the standing rule.

## 8. Next

Per the reordered build sequence: `.46` (News ingestion + classification), `.47` (Market sentiment scoring), `.48` (Elliott Wave), `.49` (AI proposal generation pipeline, amended scope), `.50` (Formal Decision Engine) — then back to `.41` (Ghost Trades, now that `.50` will exist), then `.51`→`.53`, then the full post-`.53` crypto validation campaign.
