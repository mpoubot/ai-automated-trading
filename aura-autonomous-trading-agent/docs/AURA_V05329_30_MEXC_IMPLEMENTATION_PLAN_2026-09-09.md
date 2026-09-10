# `.29` + `.30` Implementation Planning — MEXC Intent Ledger + Reconciliation

Planning pass only — no code written. Per your instruction, this identifies exactly what the eventual implementation must do, using the now-locked specs (`AURA_v05329_mexc_intent_ledger_spec_2026-09-09.md`, `AURA_v05330_mexc_intent_reconciliation_spec_2026-09-09.md`) and the transition/evidence test matrix (`AURA_v05329_30_transition_evidence_test_matrix_2026-09-09.md`) as the acceptance contract. Structured around your nine points.

**Date:** 2026-09-09

---

## 1. Exact files/modules to create

Following the existing `aura_v053XX_<name>.py` convention (`.17`, `.18`, `.27`, `.28`):

| File | Role |
|---|---|
| `aura_v05329_mexc_intent_ledger.py` | `.29` — library of the nine functions from its spec §7, plus the proposed read-only reporting CLI (`--list-unresolved`, `--show <client_order_id>`) |
| `aura_v05330_mexc_intent_reconciliation.py` | `.30` — library only (`classify_terminal_order`, `reconcile_intent`, `check_staleness`, `apply_verdict`, from its spec §6). No CLI: `.30` has no standing input file to point one at (its inputs are a live `.29` record + a live `.28` snapshot, normally supplied by an orchestrator, not files a human would name on a command line) — an ad-hoc inspection need is served by `.29`'s reporting CLI instead, consistent with §7's "no separate output" decision. |
| `tests/test_aura_v05329_mexc_intent_ledger.py` | Matrix §1 + §2 (`T29-*`) |
| `tests/test_aura_v05330_mexc_intent_reconciliation.py` | Matrix §3 (`T30-*`) |
| `tests/test_aura_v05329_30_integration.py` | Matrix §4 (`TI-*`) — imports both modules together |
| `tests/fixtures_mexc_intent.py` | Shared, non-test helper module: builders for synthetic `.28` snapshots (raw order dicts with a given `state`/`dealVol`/`vol`, matching `.28`'s actual `raw_evidence["order"]` shape) and `.29` record fixtures, so `T30-*` and `TI-*` don't each hand-roll evidence dicts. Mirrors the role `.28`'s test file's `FakeExchange` already plays, but as a shared module since both `.30`'s unit tests and the integration tests need the same shapes. |
| `docs/AURA_V05329_30_MEXC_IMPLEMENTATION_PLAN_2026-09-09.md` | Copy of this document into the repo's existing `docs/` convention, alongside `.28`'s two docs, at commit time — not before. |

No changes to any existing file (`.17`, `.18`, `.27`, `.28`, `.15`) — confirmed untouched by everything in this plan.

---

## 2. Interfaces

Already fully specified — `.29`'s spec §7 (nine functions) and `.30`'s spec §6 (four functions), both updated with the `direction`/`order_terminal_on_exchange` fields locked this pass. Two implementation-level decisions this plan makes explicit, since the specs left them at the "signature, not code" level:

**Data shape:** plain `dict[str, Any]` in, `dict[str, Any]` out — not dataclasses or a custom class hierarchy. This matches every existing module (`.17`/`.18`/`.27`/`.28` are all dict-in/dict-out, JSON-serialized directly). `IntentRecord` and `ReconciliationVerdict` in the specs' pseudocode are shape descriptions, not literal types to define.

**Error convention:** a single `fail(message: str)` helper that raises `RuntimeError(message)`, exactly matching `.27`'s and `.28`'s existing pattern (both modules use this identical helper today). No new exception hierarchy. Concretely:
- `.29`'s illegal-transition refusals (matrix §1's REFUSED cells) raise via `fail("ILLEGAL_TRANSITION:<current_state>:<attempted_event>")`.
- `.29`'s `create_intent()` refusing an existing `client_order_id` raises via `fail("INTENT_ALREADY_EXISTS:<client_order_id>")`.
- `verify_intent()`/`.30`'s input verification return `(bool, list[str])` tuples rather than raising — matching `.18`'s `verify_ledger()`/`verify_observed()` pattern exactly, since these are meant to be checked and reported (e.g. in a `BLOCKED` verdict), not to abort a caller that wants to handle the failure gracefully. This mirrors the existing split in the codebase: structural/precondition violations raise (fail closed, loud), evidence-verification results are returned as data (so a caller can report *why* without a stack trace being the only signal).

---

## 3. Storage layout

`.29`: one JSON file per intent, matching `.27`'s claims-dir pattern exactly (per `.29` spec §8). Default path constant follows the existing literal-path convention used by every other module (`Path(r"regime_output\...\...")`, matching `.17`/`.18`/`.27`/`.28`'s own `DEFAULT_*` constants) — proposed: `AURA_MEXC_INTENT_LEDGER_DIR`, default `regime_output\mexc_intent_ledger\intents\`, overridable via an environment variable of the same name (matching `.27`/`.28`'s `AURA_MEXC_*_ENABLED` env-var convention for configuration, though this one is a path, not a boolean). Filename: `<client_order_id>.json`, safe by construction since `.27`'s existing `client_order_id` charset validation (`re.fullmatch(r"[A-Za-z0-9._-]+", ...)`, ≤48 chars) already makes it a safe filename — reused here, not reinvented.

`.30`: no storage (locked, spec §7). Nothing to lay out.

Write discipline for `.29` (spec §8, restated as an implementation checklist): `create_intent()` uses `os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)`, matching `.27`'s existing claim-file mechanism. Every subsequent append: read current file → `verify_intent()` it → append the new event → recompute `record_hash` → write to `<path>.tmp` → `os.replace(tmp, path)` (atomic on POSIX, which is what `.27`'s existing claim mechanism already relies on and this environment already runs on).

---

## 4. Integration points with `.27` and `.28`

No orchestrator exists yet (a standing gap already noted in the earlier wiring survey and both specs) — this plan defines the exact field contracts a future orchestrator must honor, not the orchestrator itself, which stays out of scope here.

**`.27` → `.29`:** the orchestrator reads `.27`'s own submission-result JSON (the file `.27`'s CLI already writes) and maps it onto `.29`'s mutators:

| `.27` output field | `.29` call |
|---|---|
| `status == "SUBMITTED"` (per `.27`'s existing output vocabulary) | `record_submission_outcome(client_order_id, "SUBMISSION_ACKNOWLEDGED", mexc_order_id=payload["mexc_order_id"], raw_response_ref=<path to .27's output file>)` |
| `status == "REJECTED"` | `record_submission_outcome(client_order_id, "SUBMISSION_REJECTED")` → `.29` goes straight to `TERMINAL_SUBMISSION_REJECTED`, `.30` never invoked |
| `status == "EXECUTION_UNCERTAIN"` | `record_submission_outcome(client_order_id, "EXECUTION_UNCERTAIN")` |
| `status == "DUPLICATE_CLAIM_REJECTED"` | `record_submission_outcome(client_order_id, "DUPLICATE_CLAIM_REJECTED")` → anomaly path, §5 of `.29`'s spec |
| `status` ∈ `{FAIL_CLOSED, BLOCKED, VALIDATED_NO_SUBMISSION}` | No `.29` call — nothing was actually attempted against MEXC; matches `.28`'s own existing `load_submission_result()` refusal for these same statuses |

Note: `.27`'s own output vocabulary is `SUBMITTED`/`REJECTED`/`EXECUTION_UNCERTAIN`/`DUPLICATE_CLAIM_REJECTED` (confirmed by direct reading during `.28`'s build) — `.29`'s `SUBMISSION_ACKNOWLEDGED` name is `.29`'s own renamed vocabulary (locked earlier specifically to avoid the `SUBMITTED` collision described in `.29`'s spec §3), so the orchestrator's mapping table above is a real translation step, not a passthrough — worth stating explicitly so it isn't implemented as a naive field copy.

**`.28` → `.30` → `.29`:** the orchestrator invokes `.28`'s `read_observed_execution()` (or its CLI) to get a snapshot, then:

```
verdict = reconcile_intent(intent_record, observed_snapshot, staleness_policy)
if verdict["action"] == "RECORD_ATTEMPT":
    .29.record_reconciliation_attempt(
        client_order_id,
        observed_snapshot["snapshot_hash"],
        verdict["order_status"],
        order_terminal_on_exchange=verdict["order_terminal_on_exchange"],
    )
elif verdict["action"] == "ESCALATE":
    .29.escalate_to_human(client_order_id, verdict["reason"])
elif verdict["action"] == "BLOCKED":
    # orchestrator's own concern -- retry, alert, or halt; .29/.30 make no
    # decision here since verification failed, not evidence
```

`staleness_policy` is supplied by the orchestrator (reads `reconciliation_staleness_minutes` from wherever the orchestrator's own config lives — not `.30`'s concern, per its spec §9).

---

## 5. Error/crash behavior

Already specified structurally (§2 above, §8 of `.29`'s spec, and matrix §2's `T29-CRASH-*`/`T29-HASH-*` cases). As an implementation checklist:

- A crash between the temp-file write and the atomic rename: on restart, the temp file is orphaned (ignorable/cleanable) and the prior record is intact — `T29-CRASH-01`.
- A crash after the atomic rename but before the caller observes success: idempotency matters here — if an orchestrator retries the same mutator call after a crash-recovery restart not knowing whether it completed, the *correct* behavior depends on which mutator: `create_intent()` retried is safe (second call sees "already exists" and the orchestrator treats that as success, not failure); `record_reconciliation_attempt()`/`record_submission_outcome()` retried with the *same* outcome value is not automatically idempotent under the current design (each call appends a new event) — **this needs an explicit idempotency rule before implementation**, not left implicit: proposed rule, for confirmation rather than silently assumed — a mutator call whose target event would be byte-identical to the record's own most recent event (same event type + same fields) is a no-op (returns the current record unchanged) rather than appending a duplicate; a call with *different* fields for the same event type at the same point is treated as the anomaly path (comparable to `.27`'s duplicate-claim handling) rather than blindly appended twice.
- Any unexpected exception during a multi-step mutator (e.g. mid-way through computing a new hash): the on-disk file must never be touched until the full new record is computed and validated in memory — matches `.18`'s/`.28`'s existing "compute everything, then write once" pattern; no partial in-place mutation of the JSON file at any point.
- `.30`'s `reconcile_intent()` raising on an unexpected exception (a genuinely unhandled evidence shape, not one of the defined `BLOCKED`/`ESCALATE` paths): per §5a's "never an inferred success" principle, an unhandled exception must propagate to the orchestrator rather than being caught and silently converted to any verdict — the orchestrator's own top-level handling (not `.30`'s) decides what "reconciliation itself crashed" means operationally. `.30` should not have a bare `except Exception` anywhere that could swallow a genuine bug into a false `RECONCILING`.

---

## 6. Test implementation order

1. `.29`'s atomic-create test (`T29-ATOMIC-01`) first — foundational, nothing else can be tested meaningfully if two intents can collide.
2. `.29`'s full legal/illegal transition table (`T29-ILLEGAL-*`, generated programmatically from matrix §1's table rather than hand-written per cell) — this is the core safety property and should be locked before anything layers on top of it.
3. `.29`'s hash/tamper/crash tests (`T29-HASH-*`, `T29-CRASH-*`).
4. `.29`'s restart-listing tests (`T29-LIST-*`) and the `direction` immutability test (`T29-DIRECTION-01`).
5. `.30`'s pure evidence-matrix tests (`T30-*`) — no dependency on `.29` actually running; fixtures supply synthetic `.29` records and `.28` snapshots directly. The dealVol-mislabeling regression (`T30-CANCEL-02`) should be written early in this group, not last, since it's the specific bug class the whole `classify_terminal_order()` design exists to catch.
6. `.30`'s verification-failure and precondition tests (`T30-VERIFY-*`, `T30-PRECONDITION-01`).
7. Integration traces (`TI-*`), roughly in the order listed in the matrix — clean paths first (`TI-CLEAN-OPEN-01`, `TI-CLEAN-CLOSE-01`), then the two rejection paths side by side (`TI-POST-ACK-REJECTED-01`/`TI-PRE-SUBMISSION-REJECTED-01`, deliberately adjacent so they can't be conflated), then the anomaly and staleness/restart traces last.

---

## 7. What can be tested completely offline

Everything in the matrix — all of §1/§2/§3/§4 (`T29-*`, `T30-*`, `TI-*`). Neither `.29` nor `.30` ever calls MEXC or ccxt; `.30` by design takes an already-produced `.28` snapshot as a plain input, and every test fixture in this plan constructs that snapshot synthetically (same technique `.28`'s own `FakeExchange`-based tests already use one layer down). No network, no credentials, no live account needed for any test in this plan.

---

## 8. What requires MEXC live verification

Not `.29`/`.30`'s correctness itself — their entire design is deterministic and evidence-driven, testable offline as above. What genuinely needs controlled live (or testnet) MEXC observation, tracked as a separate follow-up task, not a blocker on `.29`/`.30` shipping:

1. **`reconciliation_staleness_minutes`'s real value** — explicitly deferred per `.30`'s spec §9; needs observed MEXC propagation-delay behavior to calibrate sensibly rather than guessed.
2. **Whether MEXC ever actually produces a partial-fill-then-cancel** (the scenario `classify_terminal_order()`'s `PARTIAL_ON_TERMINAL_ORDER` branch exists for) — the capability verification that grounded `.28` didn't establish this one way or the other; the logic should be correct regardless, but confirming it's ever actually exercised (vs. dead code for this account's order types) is worth knowing.
3. **Whether raw MEXC state `1`/`5`** (unmapped by ccxt itself) **are ever actually observed** for this account's order flow, and what they mean if so — currently handled conservatively as `UNRESOLVED`/case-2-then-3, which is correct-by-construction regardless, but real observation would let that handling move from "conservative default" to "confirmed correct."
4. **Whether a post-acknowledgment `REJECTED` from `.28` is ever actually observed** — per `.28`'s own docstring, nothing in its current logic produces this today; `.30`'s handling for it (`T30-REJECTED-01`, currently a synthetic/unreachable-in-practice test) is defined and tested, but confirmed-unreachable-in-practice is a different claim than confirmed-correct-if-it-happens.

None of these block writing or shipping `.29`/`.30` — they're calibration and confirmation items for a config value and a small number of conservative-but-untested-live branches, tracked separately.

---

## 9. The exact boundary before anything is allowed to submit a live order

Unchanged by this entire body of work, restated precisely because it's the most safety-critical line in the whole design: **`.29` and `.30` never submit an order and never gain the capability to.** `.29` is a ledger (read/write its own JSON records only); `.30` is a pure function (no I/O at all). The only component in this entire chain that can place an order is `.27`, gated by `AURA_MEXC_LIVE_ORDERS_ENABLED`, checked only in `.27`'s own CLI `main()` — unchanged, untouched, not reachable from anything built or planned in this pass.

One boundary this plan surfaces as a **future** requirement, not one `.29`/`.30` need today: once an orchestrator exists that calls `.27` → `.29` → `.28` → `.30` → `.29` in sequence automatically (still not built, still explicitly out of scope), *that* orchestrator will need its own explicit enable gate before it's allowed to call `.27` at all — e.g. an `AURA_MEXC_ORCHESTRATOR_ENABLED`-style flag, checked by the orchestrator itself, independent of and in addition to `.27`'s own `AURA_MEXC_LIVE_ORDERS_ENABLED`. Flagging this now so it isn't forgotten when that orchestrator is eventually designed — it is not decided here and not needed for `.29`/`.30` as scoped.

**Zero live trading in this pass, confirmed**: nothing in this implementation plan, nor in `.29`/`.30` as specified, creates any new path to a live order. Research → specification → matrix → implementation plan is complete for `.29`+`.30`; code is the next step, still requiring your explicit go.

---

No code written. Ready for your review; on your go-ahead, implementation begins in the test order from §6, against the fixtures/contracts fixed in §1–§5, with §8's items tracked separately rather than blocking.

---

## Implementation report (added at commit time, 2026-09-09)

Code written and fully tested, per your explicit go-ahead and locked phased sequence (Phase 1 `.29`, Phase 2 `.30`, Phase 3 integration).

**Phase 1 — `.29` (`aura_v05329_mexc_intent_ledger.py`):** implemented per §1–§9 above. One deliberate deviation from §1's file list: `tests/fixtures_mexc_intent.py` was not extracted as a separate shared module — each test file builds its fixtures directly via the real mutators (`advance_to()` in `.29`'s test file, `make_intent()`/`snapshot_for()` in `.30`'s and the integration suite), which kept every fixture demonstrably built through the actual production code path rather than a hand-rolled shortcut, at the cost of some duplication across the three test files. 26 test functions, all passing (`AURA v0.5.3.29 CONTRACT: ALL PASS`), covering every `T29-*` case in the matrix plus additional crash-simulation and idempotency-conflict cases beyond the matrix's original scope.

**Phase 2 — `.30` (`aura_v05330_mexc_intent_reconciliation.py`):** implemented per §1–§9 above, including the one narrow exception to the codebase's file-boundary convention that both specs called out: `apply_verdict()` imports `.29`'s module directly (with a dynamic-load fallback) since it is explicitly specified to call `.29`'s own mutators. 21 test functions, all passing (`AURA v0.5.3.30 CONTRACT: ALL PASS`) — clean on first run; one test was subsequently strengthened to assert the `observed_snapshot_hash` value threads correctly end-to-end (not just that the resulting state was correct), closing a coverage gap that could have masked a regression of a bug caught and fixed during development (a missing `observed_snapshot_hash` key in `reconcile_intent()`'s `RECORD_ATTEMPT` verdicts).

**Phase 3 — integration (`tests/test_aura_v05329_30_integration.py`):** all thirteen items from §6's Phase-3 sequence:
- Item 9 (real `.27→.29→.28→.30→.29` wiring): exercises the actual `.27.submit()` and `.28.read_observed_execution()` functions (not hand-built dicts) against injected fake exchanges — clean open, clean close with no surviving position, pre-submission reject, `EXECUTION_UNCERTAIN`, and a real duplicate-claim replay through `.27`'s own claim file.
- Item 10 (end-to-end matrix): all thirteen `TI-*` traces from §4 of the test matrix, implemented verbatim.
- Item 11 (crash/restart): recovery from a crash between `.27` writing its result and `.29` recording the outcome; a leftover-temp-file crash simulation.
- Item 12 (replay/concurrency): identical-verdict replay through `apply_verdict()` (idempotent no-op); conflicting-verdict replay (escalates rather than silently applying or crashing); a purity check that `reconcile_intent()` alone never mutates `.29`'s on-disk record.
- Item 13 (no unintended file changes): `git diff --stat` against the repository is empty at commit time — the only changes are the new `.29`/`.30` code, test, and doc files listed in §1; `.27`'s and `.28`'s own test suites both still pass unchanged.

Test inventory at commit time: `.27` (34 assertions, pre-existing, unaffected), `.28` (51 assertions, pre-existing, unaffected), `.29` (26 tests), `.30` (21 tests, one strengthened per above), integration (56 assertions across 22 test functions). No orchestrator module was created — per §9's explicit scoping, the integration test file's pipeline helpers call `.27`/`.28`/`.29`/`.30`'s own already-committed public functions directly and are test-only wiring, not new production code.
