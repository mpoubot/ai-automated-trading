# AURA — News / Sentiment / Elliott Wave / Sector Rotation Wiring Build Report

**Date:** 2026-09-24
**Requested by:** Martin
**Scope:** Track B (`aura-autonomous-trading-agent/`) only. Track A (`mexc_bot/`) untouched.

## 1. What was requested

Following the System Inventory & Gap Analysis (2026-09-24) and the Options/ETF
Expansion Scoping doc (2026-09-24), Martin asked to wire the four
previously-disconnected evidence sources — news (`.46`), sentiment (`.47`),
Elliott Wave (`.48`), and sector rotation (previously unbuilt anywhere in
either AURA track) — into the decision path, **before** taking up ETF/options
expansion. Three clarifications shaped the build:

- **Sourcing:** Martin pointed to the DELTAX V1/V2 repositories and
  explicitly relaxed AURA's standing "DELTAX is a research reference only,
  never an implementation source" rule *for this build*, authorizing direct
  porting/adaptation of DELTAX code.
- **Weights / validation path:** no preference stated — resolved to (a) keep
  `technical_weight`/`short_technical_weight` frozen at `0.0` on the live
  signal source (untouched), and (b) keep all new evidence confined to the
  research track, validated there before any promotion decision, mirroring
  how `.51`/`.52` themselves were built and tested before ever reaching the
  live chain.
- **"Use anything better too":** Martin separately authorized adopting any
  DELTAX pattern found to be better than AURA's current approach, not just
  filling the four named gaps, scoped to this build.

## 2. What was built

Three artifacts, in dependency order:

### `aura_v05359_sector_rotation.py` (new)
A per-symbol sector-rotation regime engine — the first implementation of
sector rotation anywhere in either AURA track (confirmed unbuilt by both the
System Inventory doc and a fresh codebase grep). Adapted directly from
DELTAX V1's `deltax/rotation.py` (relative-strength ranking, the
benchmark-vs-safe-haven regime gate, the cyclical/defensive-ratio switch,
subsector amplification) under Martin's rule-relaxation, reimplemented in
AURA's own conventions (frozen dataclasses, all thresholds/lookbacks/universe
required and caller-supplied, never hardcoded). A lightweight two-feature
confirmation check, adapted in spirit from DELTAX_v2's
`helpers/etf_rotation_scanner.py`, is folded into the regime-gated design
rather than run as a separate system. Fail-closed throughout: missing data
resolves to `NO_DATA`/`NOT_APPLICABLE`, never a guessed regime. DELTAX V1's
own disclosed caveat — a short lookback is "a momentum tilt on a rotation
signal, not a rotation strategy" — is carried forward unchanged in the module
docstring.

### `aura_v05350_decision_engine.py` (edited, additive only)
Extended to accept `.359`'s output as a fifth, optional evidence dimension:
`CandidateEvidence` gained `sector_rotation_regime`/`sector_rotation_usable`;
`build_candidate_evidence`, `compute_base_rank_score`, `is_shortlist_eligible`,
`check_evidence_freshness`, `_render_evidence_summary`, and `decide` were all
extended to carry it through. Deliberately deviates from `.51`/`.52`'s
required-no-default convention: `sector_rotation_weight` defaults to `0.0`
(documented in a dedicated docstring addendum) so every pre-existing caller —
including the frozen live `AuraFrozenDecisionEngineSignalSource` and roughly
16 other existing call sites — gets a byte-identical result unless it
explicitly opts in. Verified, not just argued: all 64 pre-existing tests in
`test_aura_v05350_decision_engine.py` pass unmodified (see §4).

### `aura_v05360_research_full_evidence_builder.py` (new)
Wires `.46`(news)/`.47`(sentiment)/`.48`(Elliott Wave)/`.359`(sector
rotation) into one `.350` `CandidateEvidence` per symbol — the first place in
this codebase all four are actually combined for a real decision. Explicitly
scoped to the research track: does not import, get imported by, or modify
`.356`/`.357`/`AuraFrozenDecisionEngineSignalSource`. As a direct response to
a defect class the DELTAX-port research surfaced in DELTAX_v2's
`direction_router.py` (an AI output's `.direction`/`.confidence` reaching a
routing decision with no type/range check at that exact seam), this module
re-validates the shape of whatever `.47`/`.359` hand it immediately before
building `.350` evidence (`_validate_regime_shape`) — AURA's own
Decision-Seam Validation Rule, enforced at the seam even though `.46`–`.48`
are deterministic, non-AI modules where today's specific bug class doesn't
apply.

## 3. DELTAX provenance (disclosed, per the rule-relaxation)

| AURA file | DELTAX source | What was ported |
|---|---|---|
| `aura_v05359_sector_rotation.py` | DELTAX V1 `deltax/rotation.py` | Core algorithm: `roc()`, `rank_sectors()`, `regime()` → `market_regime()`, `defensive_switch()` → `defensive_switch_triggered()`, `best_subsector()`/`select()` → `select_rotation_leaders()`. Formulas and gating logic ported directly; code reimplemented in AURA's own style (frozen dataclasses, no hardcoded universe/thresholds). |
| `aura_v05359_sector_rotation.py` | DELTAX_v2 `helpers/etf_rotation_scanner.py` | Idea only (not code): confirming a rotation call with more than one price feature. Adapted as a lighter 2-feature confirmation folded into DELTAX V1's regime-gated design, not DELTAX_v2's standalone 5-feature vote. |

Every other Track B file in this build (`.350`, `.360`) is AURA's own design,
extending existing `.350` conventions established by `.51`/`.52` — no DELTAX
code in those two files.

## 4. Test coverage and verification

New/updated test files:

- `tests/test_aura_v05359_sector_rotation.py` — 37 tests: `roc`/`rank_sectors`/
  `market_regime`/`defensive_switch_triggered` unit tests, the full
  `analyze_sector_rotation` tier/score decision tree (NO_DATA,
  SAFE_HAVEN_FAVORED, SAFE_HAVEN_NOT_FAVORED, NOT_APPLICABLE, LAGGARD,
  LEADER, RANKED_NOT_TOP, NOT_FAVORED_RISK_OFF), required-parameter
  validation, score clipping, and `select_rotation_leaders`.
- `tests/test_aura_v05360_research_full_evidence_builder.py` — 18 tests: the
  Decision-Seam Validation Rule (`_validate_regime_shape`, including the
  bool-is-not-numeric edge case), end-to-end wiring with/without news, wave
  bars, and sector-rotation inputs, pass-through of `.51`/`.52` regimes,
  and confirmation the resulting evidence is directly consumable by `.350
  .decide()`.
- `tests/test_aura_v05350_decision_engine.py` — 14 new tests appended
  (section 15) covering the sector-rotation evidence integration: usable/
  not-usable construction, shortlist eligibility, weight validation, the
  additive score term (including that it's signed — can push either
  LONG or SHORT, unlike `.51`'s LONG-only technical term), and — the key
  backward-compatibility proof — that a caller never passing
  `sector_rotation_weight` gets a byte-identical score even when
  sector-rotation evidence happens to be present on the candidate.

**Backward compatibility:** all 64 pre-existing tests in
`test_aura_v05350_decision_engine.py` pass completely unmodified after the
edit (verified by running that file in isolation both before and after
appending the new section).

**Full suite regression check:** `python -m pytest tests/` — 1223 passed, 10
failed. All 10 failures were confirmed **pre-existing and unrelated** to this
build: they occur identically with `test_aura_v05350/59/60*.py` entirely
excluded from the run, and are concentrated in `.336`/`.338`/`.352`/`.355` —
files this build never touched. (`.355`'s failures in particular look like
date-drift against a hardcoded `NOW = 2026-09-23` fixture now that the
sandbox clock has advanced to 2026-09-24; `.352`'s single failure passes in
isolation but fails inside the full run, suggesting cross-file test-module
caching rather than a real defect. Flagging for Martin's awareness — not
caused by, and out of scope for, this build.)

## 5. DELTAX patterns evaluated but deliberately NOT adopted this pass

Per "use anything better too," these were reviewed and are genuinely
interesting, but adopting them would go beyond wiring the four named
evidence sources and risk changing more than this build's scope should:

- **`direction_router.py`'s routing pattern** (DELTAX_v2) — a single
  dispatch point mapping evidence combinations to actions. AURA's `.350`
  already centralizes this in `decide()`; re-architecting around a router
  is a bigger structural change than this build warrants, and would be the
  wrong place to introduce it given `direction_router.py` is also the exact
  file whose untyped-boundary bug motivated `.360`'s validation rule.
- **`decision_persistence.py`'s state-transition map** (DELTAX_v2) — explicit
  allowed-transition table for decision lifecycle states. Worth considering
  if/when `.350`'s `TradingDecision` outcomes grow more stateful.
- **`portfolio_risk_monitor.py`'s risk-event lifecycle** (DELTAX_v2) —
  structured risk-event open/acknowledge/close tracking, distinct from
  AURA's current point-in-time limit checks (`.344`).
- **`gates.py`'s pure-function/log-every-refusal pattern** (DELTAX V1) — every
  gate refusal is logged with its reason, not just the final allow/deny.
  AURA's `.344`/`.340` partially do this already; worth a closer diff.

None of these were ported. Flagged here as candidates for a future,
separately-scoped pass — not silently deferred.

## 6. Scope boundary — confirmed untouched

- `aura_v054_signal_source.py` — `FROZEN_DECIDE_KWARGS`,
  `AuraFrozenDecisionEngineSignalSource`: **not modified.** Still forces
  `technical_weight=0.0`/`short_technical_weight=0.0` and never supplies
  `sentiment_regime`/`wave_result`/`sector_rotation_regime` — the live chain
  still mathematically always `ABSTAIN`s, exactly as before this build.
- `aura_v05356_stage3_live_equity_cli.py`,
  `aura_v05357_stage3_scheduled_runner.py` — **not modified, not imported by
  `.360`.**
- Track A (`mexc_bot/`) — **not touched.**

## 7. Explicit reminder — not yet validated for promotion

This evidence has **not** been promoted through the Strategy Registry
(`.339`)'s evidence-gate requirements. `.359`'s own docstring carries DELTAX
V1's disclosed caveat forward: a short lookback is a momentum tilt, not a
validated rotation strategy. Nothing built in this pass should be read as
"ready for the live chain" — that is a separate, future decision requiring
`.339`'s evidence categories to clear, same as `.51`/`.52` went through
before ever reaching `.356`/`.357`.

## 8. New finding this session — Lablab/Alpaca hackathon competitor sources

While investigating "anything else that's better," Martin's own
`mpoubot/AURA-DELTAX-MEXC` repository's `sources/` folder was located,
containing 26 competitor codebases from the Alpaca hackathon (Vermilion,
Options Sniper, NewsFlow Trader, ThetaTrap, AlphaPilot AI, Hermes Trader,
BABIL, SentryTheta, Pin Desk, AlphaSwarm Sovereign, Strike Sentry, Odysseus,
AEGIS v3.1, optionwright, EdgeStack, SPY Sentinel AI, Aegis, Elite-Bot,
Machine Earning, ORION, Catalyst Surface Agent, Dark Wolf Sentinel,
TradePilot AI, Alpacaruns, plus a `competition/` notes folder). Martin
confirmed this session:

- **Timing:** review this as its **own next task**, after this build's
  verification was complete (i.e., after this report).
- **Reuse basis:** treat it the same as DELTAX — direct porting/adaptation
  with the same disclosed-adaptation documentation per module.

This is queued as the next piece of work, not started as part of this build.

## 9. Files changed/added this session

- `aura_v05359_sector_rotation.py` (new)
- `aura_v05350_decision_engine.py` (edited, additive)
- `aura_v05360_research_full_evidence_builder.py` (new)
- `tests/test_aura_v05359_sector_rotation.py` (new, 37 tests)
- `tests/test_aura_v05360_research_full_evidence_builder.py` (new, 18 tests)
- `tests/test_aura_v05350_decision_engine.py` (extended, +14 tests, 64
  pre-existing tests unmodified)

No git commit or push has been made — per standing instruction, that
requires Martin's explicit go-ahead.
