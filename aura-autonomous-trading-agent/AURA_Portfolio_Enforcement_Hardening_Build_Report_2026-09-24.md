# AURA — Portfolio Enforcement Hardening Build Report

**Date:** 2026-09-24
**Requested by:** Martin
**Scope:** Track B (`aura-autonomous-trading-agent/`) only. Track A (`mexc_bot/`) untouched.

## 1. What was requested

Following the 26-repo Lablab hackathon competitor audit
(`AURA_Lablab_Hackathon_26Repo_Competitor_Audit_2026-09-24.md`), Martin
asked what to add to the current codebase based on the findings. Through
a scoping conversation (per Martin's standing "ask before building"
instruction), the work was narrowed in three steps:

1. First choice among the audit's ranked priorities: **Phase 1 —
   options assignment/exercise handling + risk-gate shape hardening.**
2. Investigation showed assignment/exercise detection has nothing to
   attach to before options execution plumbing (O1/O4) exists, so it was
   folded into the Options/ETF Expansion Scoping document as a new phase
   (O4B) rather than built now — see that document's 2026-09-24 update.
   Investigation also showed `.344` (portfolio exposure enforcement) is a
   full-evidence dimensional evaluator, not a sequential shrink-only gate
   pipeline like optionwright's, so the literal "ordered, incident-cited,
   `min()`-only" pattern doesn't transfer as-is.
3. Asked to broaden the scope: build **two** audit-derived hardening
   items on `.344` in one pass, both of which fit its existing
   conventions exactly:
   - **Correlation-group concentration** (audit item 9 — optionwright,
     Alpacaruns).
   - **A persisted enforcement-decision journal** (audit item 12 —
     EdgeStack).

## 2. What was built

### `aura_v05344_portfolio_exposure_enforcement.py` (edited, additive only)

Two additions, both optional/off-by-default, both following `.44`'s own
"never invent a limit" convention:

- **`correlated_groups` / `max_correlation_group_concentration_ratio`**
  on `PortfolioLimits` (both default to empty/`None`), and a new
  `correlation_group_concentration` dimension. The existing
  `asset_concentration` dimension caps any single symbol's share of the
  book; it has no way to see that several symbols held together (e.g.
  SPY+QQQ+IWM) are effectively one directional bet — exactly the gap two
  independent hackathon teams (optionwright, Alpacaruns) built solutions
  for. Reuses `.43`'s already-computed per-symbol shares (does not
  re-derive exposure); one `DimensionVerdict` per configured group; a
  failed/incomplete venue blocks the whole dimension as one data-quality
  verdict, matching `asset_concentration`'s own aggregate-block pattern.
  Wired into both `evaluate_portfolio_enforcement` and
  `evaluate_hypothetical_trade` (current-state-only in the latter, same
  as `asset_concentration`).
- Module docstring extended with a dated addendum disclosing both
  additions' provenance (concept-only adaptation, not code — same
  disclosure discipline as `.359`'s DELTAX table).

### `aura_v05361_portfolio_enforcement_journal.py` (new)

A small, deliberately separate module that persists every
`EnforcementDecision` `.44` produces — ALLOW and BLOCK alike, including
flat/no-trade cycles — as an append-only JSON-Lines record. Adapted
(concept only) from EdgeStack's `agent/journal.py`: "every session,
including no-trade days, records the full gate trail... not just
executed trades." Kept out of `.344` itself because every function in
that module is a pure function of its inputs (no I/O, no side effects) —
adding file writes there would break that property for every existing
caller, including its own 40+ tests. `build_entry()` (pure) and
`append_entry()` (the only function that touches disk) are separated so
entry shape is testable without a filesystem. Fails closed on both ends:
a failed write raises `JournalError` rather than silently dropping the
entry; a corrupt line on read raises rather than being silently skipped.
`read_journal()` on a file that doesn't exist yet returns `[]` — no
entries recorded is a normal pre-first-call state, not an error.

**Not wired into any scheduled/live runner.** Per the same "wiring is a
later step" discipline `.44` itself already uses for its own
authorization-chain integration — `record_decision()` exists and is
tested, but nothing calls it automatically yet. That's an explicit,
separate decision for Martin, not assumed here.

## 3. Lablab-audit provenance (disclosed, per the "same as DELTAX" rule)

| AURA file | Source | What was adapted |
|---|---|---|
| `aura_v05344_portfolio_exposure_enforcement.py` | optionwright `PolicyState.open_positions_group`; Alpacaruns `strategy/ensemble/riskbudget.go` | Concept only, not code: treat a caller-defined group of correlated symbols as one exposure bucket for concentration purposes, alongside (not replacing) the existing per-symbol check. |
| `aura_v05361_portfolio_enforcement_journal.py` | EdgeStack `agent/journal.py` | Concept only, not code: persist every enforcement cycle's full decision, including no-trade/ALLOW cycles, as a first-class retained record. |

## 4. Test coverage and verification

- `tests/test_aura_v05344_portfolio_exposure_enforcement.py` — extended
  with 15 new tests (Section 2): no-groups-configured placeholder,
  configured-without-ratio, a breach that no single-symbol check would
  catch, within-limit pass, a referenced-but-unheld member treated as
  zero share (not an error), multiple independent groups, failed-venue
  data-quality block, flat-book pass, unpriced-position block,
  hypothetical-trade current-state-only behavior, decision-hash
  participation, and an explicit backward-compatibility proof (a caller
  that never configures `correlated_groups` gets identical verdicts on
  every *other* dimension). All 41 tests in this file pass, including
  every one of the original pre-extension tests (two of which were
  updated, not weakened, to also configure the new field so their own
  "everything is clean/fully-configured" intent still holds — see git
  diff for the two one-line additions).
- `tests/test_aura_v05361_portfolio_enforcement_journal.py` — new, 14
  tests: pure entry-building, append-only writes, ALLOW/BLOCK/no-trade
  cycles recorded identically, non-destructive appends, fail-closed
  write failure, empty-journal-is-valid, `since`/`limit` filtering,
  fail-closed corrupt-read, blank-line tolerance, and a structural check
  that this module never imports or hard-depends on `.344`.

**Full suite regression check:** `python -m pytest tests/` — 1249
passed, 10 failed. Confirmed (by re-running with both new/changed test
files excluded) that the identical 10 failures reproduce with zero
involvement from this build — same pre-existing, unrelated failures
already flagged in the 2026-09-24 `.359`/`.360` build report
(`.336`/`.338`/`.352`/`.355`, consistent with continuing sandbox-clock
drift against hardcoded fixture dates).

## 5. Scope boundary — confirmed untouched

- `aura_v054_signal_source.py` (`FROZEN_DECIDE_KWARGS`,
  `AuraFrozenDecisionEngineSignalSource`) — not modified. The live chain
  still mathematically always `ABSTAIN`s, unaffected by anything in this
  build (portfolio enforcement is downstream of a decision that never
  reaches it in the current frozen configuration).
- `.31`/`.36` execution authorization chain — not modified. `.344`'s own
  docstring already states wiring its decision into that chain is a
  later step; this build didn't change that boundary.
- Track A (`mexc_bot/`) — not touched.

## 6. Companion document update

`AURA_Options_ETF_Expansion_Scoping_2026-09-24.md` was updated the same
day: the confirmed Options Level 3 entitlement is now logged (closing
that document's open decision #1), and a new Phase O4B (assignment/
exercise reconciliation) was added, sequenced after O1/O4 since it has
nothing to reconcile against before options execution plumbing exists.
Planning-only update — no options code was written.

## 7. Files changed/added this session

- `aura_v05344_portfolio_exposure_enforcement.py` (edited, additive)
- `aura_v05361_portfolio_enforcement_journal.py` (new)
- `tests/test_aura_v05344_portfolio_exposure_enforcement.py` (extended,
  +15 tests, all pre-existing tests preserved — 2 updated to configure
  the new field per their own "fully configured" intent)
- `tests/test_aura_v05361_portfolio_enforcement_journal.py` (new, 14
  tests)
- `AURA_Options_ETF_Expansion_Scoping_2026-09-24.md` (project doc,
  updated)

No git commit or push has been made — per standing instruction, that
requires Martin's explicit go-ahead.
