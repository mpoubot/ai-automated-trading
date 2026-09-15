# AURA v0.5.4 — Zero-Trade Root-Cause / Signal-Path Audit

**Date:** 2026-09-15
**Type:** Diagnostic only. No frozen parameter changed. No `.50`/`.51`/`.52`/`.53` file modified. No optimization. No commit, no push.
**Method:** Direct code execution of the real `.50`/`.51`/`.52`/`.49` functions, against deterministic, code-constructed evidence objects (built via `.47`'s and `.48`'s own real dataclasses, using the exact fixture pattern already established in `tests/test_aura_v05350_decision_engine.py`). No real market data. No MEXC/crypto data. No invented provider. The diagnostic script itself lives outside the repository (session scratchpad) — not part of the `.54` shipped implementation, not imported by any production module.

---

## 1. Exact point where trades become zero — CONFIRMED

**`aura_v05350_decision_engine.py::compute_base_rank_score`, lines 599-637.** With the frozen weights (`technical_weight=0.0`, `short_technical_weight=0.0`) and no `sentiment_regime`/`wave_result` ever passed into `build_candidate_evidence` by `.54`'s current wiring, every one of the four additive terms evaluates to `0.0`:

```
score = 0.0
+ sentiment_weight * promotable_score      -> 0.0  (sentiment_usable is False: no sentiment_regime supplied)
+ wave_weight (if UP) / -wave_weight (DOWN) -> 0.0  (wave_usable is False: no wave_result supplied)
+ technical_weight * (signal_score/100)     -> 0.0  (weight is frozen at 0.0, regardless of the technical score)
- short_technical_weight * (signal_score/100) -> 0.0 (weight is frozen at 0.0, regardless of the score)
```

`base_rank_score = 0.0` for every symbol, every bar, unconditionally → `base_score_direction` returns `"NO_DIRECTIONAL_EVIDENCE"` → `decide()`'s own logic sets `outcome = "ABSTAIN"` directly from `direction`, **before** the AI-proposal step even runs (`decide()` only calls `proposal_module` when `direction != "NO_DIRECTIONAL_EVIDENCE"`) and **before** `decision_threshold` is ever consulted.

**This is a single, precisely located, single-cause zero — not a combination of independent gates each separately blocking.** The AI proposal/critique step, the deterministic critic, and `decision_threshold` are never even reached in this scenario; they are downstream of a decision that has already been made by the base score alone.

---

## 2. Complete signal-flow trace

| Stage | Module | Input required | Input actually supplied by `.54` today | Output |
|---|---|---|---|---|
| Market data | `aura_v054_data_interface.BarsProvider` | Daily OHLCV bars | `SyntheticBarsProvider` (test) or `UnavailableRealEquityDataSource` (production default) | Bars DataFrame |
| `.51` (technical, LONG) | `build_technical_regime_from_bars` | Bars + `TechnicalScoringParams` + `now` | Bars from above; frozen `TEST_PARAMS`-derived params; `now` = evaluation timestamp | `TechnicalRegime(status, signal_score, ...)` — **CONFIRMED functioning correctly** (Section 6) |
| `.52` (technical, SHORT) | `build_short_technical_regime_from_bars` | Same as `.51`, mirrored | Same pattern | `ShortTechnicalRegime(...)` — mirrors `.51` |
| `.46`/`.47` (sentiment) | `ingest_and_classify_news` → `score_symbol_sentiment` | Classified news events (from Alpaca News API) + `decay_window_hours` + `min_source_count` | **NOT WIRED — `.54` never calls `.46` or `.47`** | N/A — `sentiment_regime=None` always |
| `.48` (wave) | `analyze_elliott_wave` | The SAME OHLCV bars `.51` already has + `reversal_pct` | **NOT WIRED — `.54` never calls `.48`** | N/A — `wave_result=None` always |
| `.50` evidence assembly | `build_candidate_evidence` | All four regime objects (each optional, default `None`) | `technical_regime`, `short_technical_regime` only; `sentiment_regime=None`, `wave_result=None` | `CandidateEvidence` with `sentiment_usable=False`, `wave_usable=False` |
| `.50` base score | `compute_base_rank_score` | `CandidateEvidence` + 4 weights | Frozen weights (`technical_weight=0.0`, `short_technical_weight=0.0`) | **`0.0`, always** (Section 1) |
| `.50` direction | `base_score_direction` | `base_rank_score` | `0.0` | `"NO_DIRECTIONAL_EVIDENCE"`, always |
| `.49` AI proposal/challenge | `generate_proposal_for_candidate` / `challenge_proposal` | `llm_client` | `.54`'s `NeutralDeterministicLLMClient` (per Martin's approved resolution) | **Never called** when direction is `NO_DIRECTIONAL_EVIDENCE` — not the cause |
| `.50` deterministic critic | `run_deterministic_critic` | `CandidateEvidence` | Same evidence | `passed=True, issues=()` in the current wiring (no sentiment/wave contradiction possible when both are absent) — **not the cause** |
| `.50` outcome | `decide()` | `direction` (+ `final_rank_score` vs `decision_threshold`, only if direction is directional) | `direction="NO_DIRECTIONAL_EVIDENCE"` | **`outcome = "ABSTAIN"`, always** — decided by `direction` alone, threshold never consulted |
| `.54` risk gate / sizing / execution (research/backtest path) | `aura_v054_portfolio_risk`, `aura_v054_position_sizing`, `aura_v054_exit_engine` | A `"DECIDE_LONG"` outcome to act on | Never receives one | **Never invoked — no candidate ever reaches this stage** |

Note on scope: this trace is `.54`'s own research/backtest pipeline (built this session). The pre-existing **live-trading** risk-gate/execution chain (`.14` risk gate, `.23` execution-specification builder, `.44` portfolio exposure enforcement) is a separate, not-yet-connected path — `.54`'s backtest runner does not call it and this audit does not need to, since the zero occurs upstream of any risk/sizing/execution stage in either path.

---

## 3. Controlled diagnostic results (Cases A–E)

Full instrumented output saved to `zero_trade_diagnostic_results.json` (session scratchpad). Summary:

| Case | Evidence supplied | `base_rank_score` | `direction` | `decision_threshold` reached? | `outcome` |
|---|---|---|---|---|---|
| **A** — technical-only | `.51`+`.52` regimes only | `0.0` | `NO_DIRECTIONAL_EVIDENCE` | No | `ABSTAIN` |
| **B** — sentiment-only | `promotable_score=0.6`, `SUFFICIENT` | `0.6` | `LONG_LEANING` | Yes (`0.6 ≥ 0.1`) | **`DECIDE_LONG`** |
| **C** — wave-only | Single valid `UP` candidate | `1.0` | `LONG_LEANING` | Yes | **`DECIDE_LONG`** |
| **D** — all four evidence types | Sentiment + wave + `.51` + `.52` | `1.6` | `LONG_LEANING` | Yes | **`DECIDE_LONG`** |
| **E** — missing sentiment + wave (= `.54`'s actual current production wiring) | `.51`+`.52` regimes only | `0.0` | `NO_DIRECTIONAL_EVIDENCE` | No | `ABSTAIN` |

**Case A and Case E are identical** — this is itself a finding: under the frozen weights, supplying `.51`/`.52` technical evidence changes nothing about the outcome, with or without it. **Cases B, C, and D prove conclusively that `.50`'s scoring/decision machinery correctly produces a real, non-zero, directional, threshold-passing `DECIDE_LONG` decision the moment ANY sentiment or wave evidence is present** — this is not a broken decision engine; it is a decision engine with two of its four evidence inputs permanently unwired in `.54`.

Deterministic critique passed cleanly (`issues=()`) in every case — the critic never contributed to any of these outcomes, since it currently has no cross-source data to find a conflict in, and `not evidence.sources_present` is never true (at minimum `.51`/`.52` are always present).

---

## 4. Missing inputs — CONFIRMED

1. **`sentiment_regime`** — never constructed. `.54` never calls `.46::ingest_and_classify_news` or `.47::score_symbol_sentiment`.
2. **`wave_result`** — never constructed. `.54` never calls `.48::analyze_elliott_wave`.

Both are optional, `None`-defaulting parameters on `.50::build_candidate_evidence` (by design, per `.51`'s and `.52`'s own "additive, never breaks pre-`.51` callers" convention) — so their absence causes no error, only a silent `usable=False`, which is exactly why this went undetected as a "crash" and instead surfaces only as a scoring outcome.

---

## 5. Root cause classification

| Candidate cause | Verdict | Basis |
|---|---|---|
| 1. Missing upstream data (no sentiment/wave evidence source wired) | **CONFIRMED — the actual and sole root cause** | Section 1, Section 3 (Case E ≡ Case A; Cases B/C/D all trade) |
| 2. Frozen weights (`technical_weight=0.0`, `short_technical_weight=0.0`) | **CONFIRMED contributing, but NOT independently sufficient to explain the zero** — Case A (technical-only, weight=0) and Case D (all evidence, technical still weight=0) show the SAME `technical_contribution=0.0` in both, yet Case D still trades (on sentiment+wave alone). The zero-technical-weight fact is real but not what is currently blocking every trade; missing sentiment/wave is. | Section 3 |
| 3. `decision_threshold` (0.1) | **CONFIRMED NOT the cause** | Threshold is never reached in Cases A/E — `outcome` is set from `direction` alone before threshold is consulted |
| 4. Deterministic critic | **CONFIRMED NOT the cause** | `passed=True, issues=()` in every case tested; critic never fired a penalty in any scenario |
| 5. Signal wiring (`.54`'s own `build_candidate_evidence` call site) | **CONFIRMED — this is where the missing inputs originate** | `aura_v054_signal_source.py::AuraFrozenDecisionEngineSignalSource.decide_for_symbol` passes only `technical_regime`/`short_technical_regime`; `sentiment_regime`/`wave_result` are not parameters of that method at all today |
| 6. Another downstream gate (AI proposal/challenge, risk gate, sizing, execution) | **CONFIRMED NOT the cause** | None of these stages is ever reached (Section 2) — the decision is already `ABSTAIN` before any of them run |

**In one sentence: the zero is caused entirely by `.54` never constructing a `sentiment_regime` or `wave_result` object to pass into `.50`'s evidence assembly — not by the frozen weights, not by the threshold, not by the critic, and not by any risk/sizing/execution gate.**

---

## 6. Technical path (`.51`/`.52`) health check — CONFIRMED functioning correctly

Diagnostic instrumentation confirms `.51`'s scoring engine is fully operational and capable of producing a genuine, non-degenerate `CONFIRMING`/`CONFIRMED` signal: scanning 340 evaluation points across one synthetic symbol with `now` correctly aligned to each bar's own timestamp (matching how `.54`'s backtest runner actually calls it) produced:

```
FAILED: 305   EARLY: 6   CONFIRMING: 29
```

with a real, itemized reasons list on a sampled `CONFIRMING` case (`EMA3 remains above EMA8`, `EMA21 > EMA50`, `MACD bullish`, `RSI constructive`, `Positive acceleration`, `EMA3/8 persisted 3 bars`, `signal_score=45.0`). **This confirms `.51`'s technical calculation logic itself is sound and produces varied, real outputs** — the FAILED/CONFIRMING split is exactly what a real scoring function should show against a fluctuating price series, not a stuck/degenerate constant.

(Note: the Section 3 diagnostic used a single fixed `NOW=2026-09-15` against bars generated from a 2024 start date, which produced `STALE_DATA` for `.51`/`.52` in that specific script — this is an artifact of that script's fixed clock, not a defect; it does not change Section 1–5's conclusions since `technical_usable=False` either way is irrelevant when `technical_weight=0.0`. The check in this section, using bar-aligned `now` exactly as `.54`'s real backtest runner does, is the correct test of `.51`'s health, and it passes.)

This is **diagnostic only** — `technical_weight` remains frozen at `0.0`, unchanged.

---

## 7. Data contract required for SENTIMENT — CONFIRMED (from existing `.46`/`.47` code, not invented)

To produce a real `SentimentRegime`, `.54` (or a future `.55`) would need to add a call chain:

```
.46::fetch_alpaca_news(client, symbols=..., start=..., end=...)  ->  list[NewsItem], NewsFetchStatus
       -> .46::append_news_ledger(...)  ->  ledger persisted
       -> .46::load_news_ledger(...)    ->  list[dict] event records
.47::score_symbol_sentiment(events, symbol, decay_window_hours=..., min_source_count=..., now=...)  ->  SentimentRegime
```

- **Required upstream access:** an Alpaca News API client (`client.get_news(...)`), which needs a configured `alpaca-py` client — the SAME credential family (`ALPACA_API_KEY`/`ALPACA_SECRET_KEY` or paper equivalents) already blocked in this sandbox (Section 6 of `AURA_v0.5.4_Baseline_Implementation_Report_2026-09-15.md`), but a **separate, distinct endpoint** (news, not bars) requiring its own client construction and its own ingestion/ledger step (`.46`) before `.47` can score anything.
- **Schema per news item (`.46::NewsItem`):** `source`, `external_id`, `origin_source`, `headline`, `summary`, `url`, `author`, `created_at`, `updated_at`, `symbols` (tuple — used to filter relevance), `categories`+`classification_evidence` (from `.46`'s own deterministic keyword classifier), `classification_method`, `content_hash`, `fetched_at`.
- **Freshness:** `score_symbol_sentiment` filters to events whose `created_at` falls within `decay_window_hours` of `now` (a required, no-default research parameter — frozen fixture value `48.0` used in this session's `.50` test fixtures, but **not itself a value Martin has explicitly frozen for `.54` production use** — flagged as UNKNOWN below).
- **Symbol association:** exact string match against `NewsItem.symbols`.
- **Direction/score representation:** `promotable_score` (a `float | None` in `[-1, +1]`), populated **only** when `corroboration_status == "SUFFICIENT"` (i.e. `source_count >= min_source_count`, itself a required, no-default parameter).
- **Missing-data behavior:** `promotable_score=None` and `corroboration_status` in `{"INSUFFICIENT", "NO_DATA"}` when there isn't enough corroborating source diversity — `.50`'s `sentiment_usable` check (`promotable_score is not None`) is what then correctly excludes it from scoring. This fail-closed behavior is already correct and required no change.

**UNKNOWN (Martin must decide, not inferred):** `decay_window_hours` and `min_source_count` for `.54`'s production use — the `48.0`/test-fixture values used throughout this session's `.50` tests are a **test fixture**, not a value Martin has explicitly frozen for `.54`, per the same "recurring test fixture ≠ frozen decision until Martin says so" discipline applied to every other `.54` parameter.

---

## 8. Data contract required for WAVE — CONFIRMED (from existing `.48` code, not invented)

```
.48::analyze_elliott_wave(bars, symbol, reversal_pct=..., ewo_fast_period=5, ewo_slow_period=34, now=...)  ->  ElliottWaveResearchResult
```

- **Required upstream access:** **none beyond what `.51` already needs** — `analyze_elliott_wave` is a pure function over the SAME OHLCV bars `.51` consumes (`bars: list[dict[str, Any]]`, the same schema). This is a materially different, and better, situation than sentiment: wave evidence has **no separate data-source dependency** once a real bars feed exists — only a wiring gap, not an additional access blocker.
- **Schema:** list of bar dicts (open/high/low/close/volume + implicit ordering), `symbol`, required `reversal_pct` (a zigzag-swing sensitivity parameter — no default; test fixtures used `1.0`, which is **not** a value Martin has frozen for `.54`), optional `ewo_fast_period=5`/`ewo_slow_period=34` (defaulted, not frozen by Martin either).
- **Timestamps:** carried through from the bars themselves; `as_of` is stamped from the caller's `now`.
- **Direction/score representation:** `ElliottWaveResearchResult.ambiguity_status` (`SINGLE_VALID_CANDIDATE` / `AMBIGUOUS_MULTIPLE_CANDIDATES` / `ALL_CANDIDATES_INVALIDATED` / `INSUFFICIENT_DATA`) plus `impulse_candidates` (each with `wave_direction`, `validity`). `.50`'s `wave_usable` requires exactly `SINGLE_VALID_CANDIDATE`; `_single_valid_wave_direction` then re-derives the direction defensively rather than trusting the status string alone.
- **Missing/insufficient-data behavior:** `< 6` swings → `INSUFFICIENT_DATA`; correctly excluded from scoring by `.50`'s own usability check.

**UNKNOWN (Martin must decide):** `reversal_pct`, `ewo_fast_period`, `ewo_slow_period` for `.54` production use — same "test fixture ≠ frozen decision" caveat as Section 7's sentiment parameters. `AURA_v0.5.4_Final_Evidence_Recommendation_Pass_2026-09-15.md`'s DELTAX findings were explicitly scoped as future-research-only, not `.53`/`.54` freeze evidence — this remains true and is not changed by this audit.

---

## 9. Can `.54` technically trade once those inputs exist? — CONFIRMED YES

Cases B, C, and D (Section 3) are a direct, executed proof: the real `.50` decision engine, the real neutral LLM stub, and `.54`'s exact frozen weight configuration **do** produce a genuine `DECIDE_LONG` outcome — with a real `base_rank_score`, a real `final_rank_score` that clears `decision_threshold`, a `PROPOSED` AI proposal, and a passed challenge — the instant a `sentiment_regime` or `wave_result` object with usable evidence is supplied. Nothing about `.50`'s scoring, `.49`'s proposal/challenge step, or `.54`'s own `AuraFrozenDecisionEngineSignalSource` wiring prevents this. **Problem A (is `.54` technically capable of trading given valid evidence) and Problem B (does `.54` currently have a production-capable evidence source) are confirmed to be separate, and only Problem B is currently unsolved.**

---

## 10. What Martin must decide before the first real-equity backtest

None of these require changing anything already frozen — they are the currently-missing pieces:

1. **Whether to wire `.46`/`.47` (sentiment) into `.54` at all for the baseline**, given it requires a separate Alpaca News API access path beyond bars, and (if yes) the production `decay_window_hours` / `min_source_count` values (currently only test-fixture values exist, `48.0` / not confirmed).
2. **Whether to wire `.48` (wave) into `.54` for the baseline**, given it needs no separate data source (only the bars already planned for `.51`), and (if yes) the production `reversal_pct` / `ewo_fast_period` / `ewo_slow_period` values (currently only test-fixture values exist).
3. **Whether to leave `technical_weight`/`short_technical_weight` at `0.0` for the first real baseline** (in which case the baseline will trade on sentiment+wave alone, if those are wired) **or revisit that freeze** — a strategy decision explicitly out of scope for this diagnostic to recommend.
4. If Martin chooses to run a real baseline with sentiment/wave still unwired (i.e., keep `.54` exactly as currently implemented), **the expected, confirmed result is zero trades regardless of whether real market data is supplied** — real data alone does not change Section 1's conclusion, since the missing inputs are evidence-source wiring, not data availability for `.51`/`.52`.

---

## 11. What this audit deliberately did NOT do

- Did not change `technical_weight`, `short_technical_weight`, `decision_threshold`, or any other frozen `.50` parameter.
- Did not add synthetic/arbitrary sentiment or wave values into any production code path — Cases B/C/D exist only inside the temporary diagnostic script, never inside `aura_v054_signal_source.py` or any shipped `.54` module.
- Did not modify `.50`/`.51`/`.52`/`.53`.
- Did not run against real market data.
- Did not optimize or sweep any parameter.
- Did not commit or push. `git status --short` after this audit shows only the pre-existing set of untracked files plus this report and the diagnostic's own results JSON (kept in the session scratchpad, outside the repository) — no tracked file touched.
