# AURA — Live Evidence Orchestrator Build Report

**Date:** 2026-09-25
**Requested by:** Martin
**Scope:** Track B (`aura-autonomous-trading-agent/`) only. Track A (`mexc_bot/`) untouched.

## 1. What was requested

Following "what needs to change so I can start seeing performance with
live paper-trading money," Martin chose the full evidence pipeline
(news + sentiment + wave + sector rotation), not technical-only, with a
"not rushed, build it properly" timeline. Scoping continued with
`AskUserQuestion`:

- Sector rotation: benchmark = SPY, safe havens = GLD + SLV, ranking
  universe = the existing 27-symbol list.
- Sector rotation influence on decisions: **compute + log only** this
  round (`sector_rotation_weight` stays at its safe default, `0.0`).
- Technical signals: **bring back to weight 1.0** (previously frozen at
  `0.0`).
- Research parameters with no prior approved production value
  (`reversal_pct`, `decay_window_hours`, `min_source_count`, sector-
  rotation `lookback_bars`/`top_n`/`score_scale`/`defensive_ma_period`):
  proposed with disclosed rationale, explicitly confirmed.
- Defensive-ratio secondary confirmation signal: skipped this round.

## 2. A correction surfaced mid-build

The original task plan assumed `aura_v054_signal_source.py`'s
`decide_for_symbol()` was the live decision call site. Investigation
before writing any code found this was wrong: `.054` is never called by
the live/paper path. The real chain is `.356` (CLI) → `.055`
(`SymbolRequest`) → `.53.run_cycle()` → `.50.decide()` directly. `.054`
is only ever imported by `.356` as a source of frozen constants.

This was surfaced to Martin before building, who confirmed retargeting
the work to the real path. One consequence: `.053`'s `SymbolCycleInput`
already had unused `sentiment_regime`/`wave_result` fields flowing
straight through to `.50.decide()` — so wiring those two in needed no
change to `.053`/`.055` at all, only to `.356` (which never populated
them). Only sector rotation genuinely had no field anywhere in that
chain.

## 3. What was built

### `aura_v05362_live_evidence_orchestrator.py` (new)

Computes real sentiment (`.47`), Elliott Wave (`.48`), and sector-rotation
(`.359`) evidence for a caller-supplied symbol universe, reusing `.360`'s
existing evidence-construction and Decision-Seam validation verbatim
(not reimplemented). Holds the confirmed research parameters and a new
`LIVE_EVIDENCE_DECIDE_KWARGS` — kept deliberately separate from `.054`'s
frozen constant so that module's own contract and test suite are
completely undisturbed.

Design decision made during the build, not pre-registered in the
original task plan: sector rotation is computed and exposed in `.356`'s
own CLI output (`sector_rotation_by_symbol`) for review, but is **not**
threaded into `.053`'s `SymbolCycleInput`/`.50`'s `CandidateEvidence` —
that dataclass has no `sector_rotation_regime` field today, and adding
one would mean modifying shared, already-tested infrastructure the
existing dry-run path also depends on, for a value that (at weight 0.0)
cannot affect any decision this round. This fully satisfies "compute +
log only" with strictly less blast radius than the originally-sketched
approach.

Fails open per symbol (a symbol with no/short bars just gets
`wave_result=None`) and fails open for the whole cycle's sector rotation
if benchmark/safe-haven bars are unavailable (recorded, not raised).

### `aura_v05356_stage3_live_equity_cli.py` (edited, additive)

- New `build_news_client()` — the first real construction of Alpaca's
  `NewsClient` anywhere in this repo (previously only ever exercised
  against a fake in `.46`'s own tests).
- `SymbolEvidence` gained `bars_as_dicts` so bars fetched once for
  `.51`/`.52` are reused for wave/sector-rotation — never fetched twice.
- `run_live_dry_run_cycle()` gained optional `news_client`/
  `news_state_dir`/`news_fetch_limit` params. When supplied: ingests real
  news for the cycle's symbols, builds live evidence via `.362`, and
  populates `.055`'s `sentiment_regime`/`wave_result` fields. When
  omitted (old call shape): behaves exactly as before — confirmed by
  test.
- `decide_kwargs` now sourced from `.362.LIVE_EVIDENCE_DECIDE_KWARGS`
  instead of `.054.FROZEN_DECIDE_KWARGS`.
- New CLI flags: `--news-state-dir` (opt-in; omitting it skips
  news/live-evidence entirely), `--news-fetch-limit` (defaults to `.46`'s
  own existing default of 50).
- **Still only ever calls `run_stage1a_dry_run`, never
  `run_stage1b_paper_cycle`; `attempt_submission` is never passed as
  `True` anywhere.** This guarantee is unchanged and still covered by its
  own AST-level test.

## 4. Test coverage and verification

- `tests/test_aura_v05362_live_evidence_orchestrator.py` — new, 13 tests:
  bars-shape conversion, news fetch-and-read-back, fail-open on a failed
  news fetch (prior ledger preserved), per-symbol sentiment/wave
  computation, sector-rotation computed only when the whole
  benchmark/safe-haven universe is present, and the exact confirmed
  configuration values.
- `tests/test_aura_v05356_stage3_live_equity_cli.py` — extended with 16
  new/changed tests: `bars_as_dicts` population, real `NewsClient`
  construction, decide-kwargs now sourced from `.362` not `.054` (with
  `.054`'s own frozen constant confirmed untouched), backward
  compatibility when news args are omitted, sentiment/wave populated end
  to end when they are supplied, and fail-open behavior on a broken news
  fetch.
- **Full suite:** 1268 passed, 10 failed. All 10 failures are in
  `.336`/`.338`/`.352`/`.355` — the same pre-existing, sandbox-clock-drift
  failures already flagged in the 2026-09-24 build report. None are in
  `.356` or `.362`.

## 5. Scope boundary — confirmed untouched

- `aura_v054_signal_source.py` — not modified. Its `FROZEN_DECIDE_KWARGS`
  and its own test suite (proving the frozen config always ABSTAINs) are
  exactly as before.
- `aura_v05353_full_paper_orchestration.py` / `aura_v05355_stage1_paper_trading_runner.py`
  — not modified. Both already supported everything this build needed.
- `run_stage1b_paper_cycle` — still never called from `.356`. No order
  has been, or can be, submitted by anything built this session.
- Track A (`mexc_bot/`) — not touched.

## 6. What this does NOT yet do

Position sizing (`.054_position_sizing` → `SymbolRequest.quantity`) is
still not wired in — `.356`'s `LiveSymbolRequest.quantity` is still
caller-supplied via the requests JSON file, not computed. A manual-
trigger path for `run_stage1b_paper_cycle` (an actual capped paper order)
does not exist yet and was not built or attempted this session. No git
commit or push has been made — per standing instruction, that requires
Martin's explicit go-ahead.
