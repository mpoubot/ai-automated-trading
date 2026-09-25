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

## 6. Addendum — ATR risk-based position sizing (same date, follow-on)

Following "continue to position sizing," three more scoping questions
were confirmed via `AskUserQuestion` before any code was written:

- `LiveSymbolRequest.quantity` becomes **optional**: when omitted, size
  is auto-computed via ATR risk sizing; an explicit `quantity` in the
  requests JSON still always wins (no behavior change for existing
  configs).
- Short-side sizing reuses the **same stop-distance magnitude** as the
  existing long-only formula (`.054_exit_engine.initial_stop_distance_long`)
  — confirmed with Martin since no separate short-side formula exists
  anywhere in the repo, and the distance is a magnitude, not a signed
  price level.
- When a symbol can't be sized in a given cycle (no account equity, no
  usable ATR, or the risk budget floors to 0 shares), **that symbol
  alone is skipped** — recorded in the cycle output — and the rest of
  the cycle proceeds. Sizing never raises/blocks the whole cycle.

### What was built

- `aura_v05356_stage3_live_equity_cli.py` (edited, additive):
  - `SymbolEvidence` gained `atr_at_entry`, computed in
    `fetch_symbol_evidence()` from the **same already-fetched** `bars_df`
    via `.054_atr.wilder_atr_from_bars()` (last valid, non-NaN ATR value;
    never re-fetches bars).
  - New `resolve_quantity_for_symbol()`: explicit quantity always wins;
    otherwise computes `planned_stop_distance` via `.054_exit_engine`
    and quantity via `.054_position_sizing.size_position_by_atr_risk()`
    (0.5% equity risk per trade, Martin's existing `.54`-approved
    value). Returns `(None, <reason>)` on any precondition failure or a
    zero-share result — never raises.
  - `run_live_dry_run_cycle()` now sizes every otherwise-usable request,
    skips ones that can't be sized (recording `sizing_failures`), and
    only builds `SymbolRequest`s for the sizeable remainder.
  - New output keys: `position_sizing_note`, `sizing_failures`.

### Test coverage and verification

- `tests/test_aura_v05356_stage3_live_equity_cli.py` extended with 9 new
  tests: ATR population (with/without the module supplied, and with
  insufficient warm-up bars), explicit-quantity-wins, real ATR-risk
  auto-sizing (exact-value check), each of the three fail-open reasons,
  and two end-to-end cycle tests (auto-sizing end to end; an unsizeable
  symbol skipped without blocking the cycle).
- `.356` file alone: 44 passed. Combined with `.362`: 57 passed.
- **Full suite:** 1280 passed, 10 failed — the same 10 pre-existing
  `.336`/`.338`/`.352`/`.355` clock-drift failures as before. No new
  regressions.

## 7. Addendum — manual-trigger CLI for a capped real paper submission (same date)

Following "continue to task #137," scoping continued with two more
`AskUserQuestion` rounds before any code was written.

**Round 1 — architecture and gates.** `.356` has a standing, AST-test-proven
guarantee: it never calls `run_stage1b_paper_cycle` and never passes
`attempt_submission=True`. Rather than weaken that guarantee, the capped
real-submission path was built as its own new module,
`aura_v05363_stage1b_manual_trigger_cli.py` — `.356` is completely
unmodified by this addendum. Martin also confirmed: (a) an explicit,
hard-to-mistype `--i-confirm-this-submits-real-paper-orders` flag is
REQUIRED before `.363` will open `.36`'s auth gates or `.38`'s supervisor
kill switch — there is no way to pass it as "false," only to omit it and
have the CLI refuse to run; (b) scope (which symbols, how many orders) is
controlled entirely via `--requests-config` and
`--max-new-orders-per-cycle`, with no additional code-level symbol
allowlist or ceiling invented on top of that.

**A second gap found mid-build, also surfaced before continuing.** `.44`'s
daily_loss check BLOCKs unconditionally (`INSUFFICIENT_HISTORY`) whenever
there's no real same-day-prior equity snapshot — and nothing in this repo
persisted one anywhere before this addendum. An empty `equity_history`
(matching `.356`'s own harmless choice, safe there only because `.356`
never submits) would have meant `.363`'s real submissions BLOCK on
daily_loss alone, every single time, regardless of decision quality or
confirmation. Martin chose building a small persisted log over the two
lighter alternatives (self-seeding a same-run-only baseline, or shipping
`.363` permanently blocked pending a future fix).

### What was built

- **`aura_v05364_equity_history_log.py`** (new) — a small append-only
  JSON-Lines log for real ALPACA equity observations, deliberately
  mirroring `.361`'s enforcement-journal format/conventions rather than
  inventing a new one. Default path
  `regime_output/equity_history_log/alpaca_equity_history.jsonl`, following
  the same `regime_output/` root `.338` already uses. Every observation is
  a real, freshly-fetched `get_account().equity` value — this module never
  fabricates, estimates, or backfills a number.
- **`aura_v05363_stage1b_manual_trigger_cli.py`** (new) — reuses `.356`'s
  already-tested credential/client/evidence/sizing functions directly (no
  duplicated logic, only new sequencing), then calls `.55`'s EXISTING
  `run_stage1b_paper_cycle()`. Before calling it: appends this run's real
  equity to `.364`'s log, reads the full same-day history back, and passes
  that as `equity_history` — the first-ever observation in a day becomes
  its own zero-loss baseline (a real number, not fabricated); every later
  same-day run sees a genuine, strictly-earlier prior observation.
  `strategy_id`/`strategy_version` default to honestly-labeled values
  (`STAGE1B_MANUAL_TRIGGER_LIVE_PAPER` / `v1-manual-trigger`), not `.355`'s
  own `STAGE1_SYNTHETIC_SCENARIO` default (which would mislabel a real
  paper fill's audit trail). `limits=None` (→ `.355` defaults to
  `PortfolioLimits()`, unconfigured — Martin's standing choice).
- A bug caught and fixed before it shipped: my first draft omitted
  `technical_regime`/`short_technical_regime` from the `SymbolRequest` it
  builds — inconsistent with `.356`'s own construction and would have
  silently dropped real technical evidence from the decision despite
  `LIVE_EVIDENCE_DECIDE_KWARGS` weighting it at 1.0. Caught by re-reading
  `.356`'s equivalent code side by side before writing tests, fixed before
  any test ran.

### Test coverage and verification

- `tests/test_aura_v05364_equity_history_log.py` — new, 12 tests: append/
  read round-trip, append-only (never truncates), missing-file-is-not-an-
  error, corrupt-line-is-fail-closed, and the accumulate-across-calls
  behavior the daily-loss fix depends on.
- `tests/test_aura_v05363_stage1b_manual_trigger_cli.py` — new, 9 tests:
  the confirmation gate (direct call and CLI argparse), the exact
  supervision dict shape, a spy proving `run_stage1b_paper_cycle` (never
  `run_stage1a_dry_run`) is called only when confirmed, a genuine
  end-to-end test where a real order is actually submitted (a synthetic
  strongly-bullish technical regime is substituted for `.51`'s own
  indicator math, which is already covered by `.51`'s own test suite — this
  file's job is proving the WIRING, not re-proving `.51`'s scoring), a
  second-same-day-run test proving cumulative daily-loss history works,
  and two fail-open-per-symbol tests (a fetch failure, an unsizeable
  symbol) that skip only that symbol without blocking the cycle.
- One cross-file test-isolation bug was found and fixed while combining
  this file with `.356`'s: both test files reload the same canonical
  module names (e.g. `.355`) into `sys.modules`, and `.356`'s existing
  spy-based tests broke when my file's load ran after `.356`'s in the same
  pytest session, because production code re-resolves those names via
  `__import__` at call time. Fixed by making both new test files' loaders
  idempotent (reuse an already-registered module instead of overwriting
  it) — a test-hygiene fix confined to the two new test files, nothing
  produced by any prior session's test file was touched.
- **Full suite:** 1301 passed, 10 failed — the same pre-existing
  `.336`/`.338`/`.352`/`.355` clock-drift failures already documented,
  confirmed stable across two consecutive runs. One additional flaky
  concurrency test failed once, then passed both in isolation and on a
  full re-run — not a regression.

## 8. What this does NOT yet do

No git commit or push has been made for the manual-trigger CLI work — per
standing instruction, that requires Martin's explicit go-ahead. No real
(or paper) order has been submitted by anything built this session; `.363`
exists and is tested against fakes only. Per task #139, Martin's explicit
sign-off is still required before the very first real invocation of
`.363` against a live Alpaca paper account.
