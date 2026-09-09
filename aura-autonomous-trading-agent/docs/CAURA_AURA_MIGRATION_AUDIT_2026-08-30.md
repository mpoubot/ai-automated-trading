# CAURA → AURA Migration Audit

Date: 2026-08-30

## Scope

This audit compares the current GitHub `main` branch with the recovered CAURA/AURA project material in the File Library. It is an audit artifact only. It does not modify `main`, change the frozen strategy, enable paper execution, or enable live execution.

## GitHub baseline

Current `main` commit:

`dd21d12ec4e3818f6d6c0e99517e29be5b35463f`

Existing safety branch:

`backup/github-before-local-sync-2026-08-30`

Audit branch:

`audit/caura-v052-to-aura-v053`

The backup branch and `main` are divergent because the 2026-08-30 sync added the current research workspace to `main`. The backup remains available as a recovery point.

## Executive finding

The CAURA/AURA source material contains a deterministic v0.5.3 control architecture that is not present as source modules in the current GitHub `main` tree.

The recovered architecture is:

```text
Market State (.12)
    ↓
Signal Decision (.13)
    ↓
Risk Gate (.14)
    ↓
Position State (.15)
    ↓
Paper Execution (.16)
    ↓
Decision / Execution Ledger (.17)
    ↓
Kill Switch / Recovery (.18)
    ↓
Agent Supervisor (.19)
    ↓
Competition Dashboard (.20)
```

The deterministic execution boundary remains closed:

```text
ORDERS_ALLOWED      = FALSE
PAPER_EXECUTION     = FALSE
LIVE_EXECUTION      = FALSE
EXECUTION_PERMITTED = FALSE
```

## Classification

### CONFIRMED / RECOVERED SOURCE

- v0.5.3.12 Market State Engine exists in recovered source material.
- v0.5.3.13 Signal Decision Engine exists in recovered source material.
- v0.5.3.14 Risk Gate exists and has a locked fail-closed specification.
- v0.5.3.15 Position State Manager exists in recovered source material.
- v0.5.3.16 Paper Execution Adapter exists in recovered source material.
- v0.5.3.17 Execution & Decision Ledger exists in recovered source material.
- v0.5.3.18 Kill Switch / Recovery exists in recovered source material.
- v0.5.3.19 Agent Supervisor exists in recovered source material.
- v0.5.3.20 Competition Dashboard exists in recovered source material.
- A MEXC execution handover exists and defines the future adapter boundary.

### NOT YET VERIFIED IN GITHUB MAIN

The corresponding v0.5.3.12–v0.5.3.20 source modules have not been verified in the current GitHub `main` tree. Therefore they must not be described as GitHub-integrated merely because they exist in recovered project material.

### INCOMPLETE / NOT PROVEN

- Final clean `.13 → .14` contract/hash end-to-end proof.
- Authoritative exchange position reconciliation.
- Durable client/order identity and idempotency at the live venue boundary.
- Uncertain-submit recovery.
- Complete partial-fill lifecycle.
- Complete order-state monitoring and cancellation lifecycle.
- Native stop/take-profit lifecycle at the venue boundary.
- Kill-switch integration with a future live adapter.
- Complete venue ↔ AURA ledger integration.
- Live execution authorization.

## Component audit

| Version | Component | Recovered source | GitHub main | Classification | Migration action |
|---|---|---:|---:|---|---|
| .12 | Market State Engine | YES | NOT VERIFIED | CONFIRMED / RECOVER | Migrate exact source after contract review |
| .13 | Signal Decision Engine | YES | NOT VERIFIED | CONFIRMED / RECOVER | Migrate exact source after .12 contract review |
| .14 | Risk Gate | YES | NOT VERIFIED | BUILT / SPEC LOCKED | Migrate exact source; first prove contract |
| .15 | Position State Manager | YES | NOT VERIFIED | BUILT / RECOVERED | Migrate only after .14 acceptance |
| .16 | Paper Execution Adapter | YES | NOT VERIFIED | BUILT / RECOVERED | Migrate exact source; keep paper-only |
| .17 | Execution & Decision Ledger | YES | NOT VERIFIED | BUILT / RECOVERED | Migrate exact source; preserve schema lesson |
| .18 | Kill Switch / Recovery | YES | NOT VERIFIED | BUILT / RECOVERED | Migrate exact source; preserve hard barrier |
| .19 | Agent Supervisor | YES | NOT VERIFIED | BUILT / RECOVERED | Migrate exact source; observer only |
| .20 | Competition Dashboard | YES | NOT VERIFIED | BUILT / RECOVERED | Migrate corrected implementation; read-only |
| .21 | End-to-End Dry Run | NOT VERIFIED | NOT VERIFIED | NOT BUILT / NEXT | Build after contract alignment |

## Important schema lesson

The recovered v0.5.3.17 work documents an earlier source-engine interpretation error. `source_engine` in the upstream files is an AURA version string rather than the semantic engine name expected by the first ledger implementation.

Correct recovered chain:

```text
Position State source_engine  → AURA v0.5.3.14
Paper Execution source_engine → AURA v0.5.3.15
```

The corrected ledger run produced:

```text
STATUS           = RECORDED
OVERALL ACTION   = NO_ACTION
RECORDS APPENDED = 2
TOTAL RECORDS    = 2
```

This must be preserved as a schema-history item, not silently rewritten.

## .14 gate status

The Risk Gate must remain a security boundary, not a strategy optimizer.

Required semantics:

```text
SIGNAL_CANDIDATE + all checks pass → RISK_PASS
VALID + NO_SIGNAL                 → NO_SIGNAL
INVALID / incomplete / untrusted  → BLOCKED
```

`RISK_PASS` means authorization to continue to Position State. It does not authorize an order.

Required fail-closed tests:

1. VALID + NO_SIGNAL → NO_SIGNAL
2. VALID + SIGNAL_CANDIDATE → RISK_PASS with `execution_permitted=false`
3. Bad state hash → BLOCKED
4. Missing BTC/USD → BLOCKED
5. Missing ETH/USD → BLOCKED
6. Timestamp mismatch → BLOCKED
7. Candidate/reason/match inconsistency → BLOCKED
8. Malformed JSON → BLOCKED

Do not weaken hash verification to make a test pass.

## Research boundary

The frozen candidate remains:

`BEAR × LOW ATR × POSITIVE bar-2`

with:

- ATR threshold: `0.596%`
- trend: `4H EMA50`
- ATR: `1H ATR14`
- bar-2: `2H close-to-close`

Downstream components must consume the canonical `.12` state and must not reconstruct indicators or market state.

## What should migrate

### AURA Core

Migrate the deterministic control-plane source and its required fixtures/specifications:

- `.12` Market State
- `.13` Signal Decision
- `.14` Risk Gate
- `.15` Position State
- `.16` Paper Execution
- `.17` Ledger
- `.18` Kill Switch
- `.19` Supervisor
- `.20` Dashboard

### Research history

Keep existing research history intact. Do not rewrite older versions to fit the new architecture.

### Generated artifacts

Do not blindly migrate all generated CSV/HTML/log/output material into the application source tree. Outputs should be separated from authoritative source code and historical evidence.

### Old MEXC runtime

Treat existing MEXC/CCXT runtime as an execution-primitive source only. It must not become an alternate AURA authorization path.

## Recommended repository structure

```text
AURA/
├── research/
├── validation/
├── core/
├── market_state/
├── signal/
├── risk/
├── position/
├── execution/
├── ledger/
├── supervision/
├── evidence/
├── tests/
└── docs/
```

The existing historical research directories can remain where useful; the structure above is the target logical organization, not a justification for destructive reorganization.

## Migration sequence

### MUST DO NOW

1. Preserve the existing backup branch.
2. Keep `main` execution-disabled.
3. Diagnose and prove the `.12 → .13 → .14` contract/hash chain.
4. Migrate exact recovered `.12–.14` source into the audit/migration branch.
5. Add deterministic fixtures/tests for the `.14` acceptance matrix.
6. Only after `.14` is proven, integrate `.15`.
7. Then integrate `.16` and `.17` while preserving the documented schema lesson.
8. Integrate `.18–.20` as control/observation layers.
9. Build `.21` end-to-end dry run.

### LATER

- Independent validation of the frozen candidate.
- Paper competition agent.
- MEXC execution adapter around proven CCXT primitives.

### FUTURE LAB WORK

- Multi-asset research expansion.
- Top-down market intelligence / ranking.
- Options research.

## Non-negotiable safety rules

- No live trading now.
- No paper trading unless explicitly authorized by the future build.
- Never weaken a safety gate to make a test pass.
- Never tune after holdout evidence.
- Never modify the frozen candidate because current market conditions do not match it.
- No downstream reconstruction of canonical state.
- No SIGNAL → ORDER shortcut.
- `NO_SIGNAL` never becomes a trade.
- `BLOCKED` is a valid intentional outcome.
- `RISK_PASS` is authorization to continue, not order authorization.
- Position state must be reconciled before future execution.
- AI may research/analyze/propose; deterministic controls decide execution progression.

## Audit conclusion

The correct action is **controlled source recovery**, not a wholesale CAURA folder copy.

The recovered CAURA material should be treated as the source for the missing v0.5.3 deterministic control-plane implementation, while GitHub `main` remains the current baseline and the backup branch remains untouched.

The first technical promotion gate is the `.13 → .14` contract/hash proof. The migration must stop there if that proof fails. Once `.14` is proven, the remaining recovered control modules can be promoted in dependency order.
