# AURA v0.5.3.51 — Completion Report

**Date:** 2026-09-13
**Milestone:** Live Alpaca equities/ETFs technical signal source
**Module:** `aura_v05351_live_alpaca_equity_signal_source.py`
**Config:** `aura_v05351_equity_universe_v1.json`
**Tests:** `tests/test_aura_v05351_live_alpaca_equity_signal_source.py`
**`.50` extension (additive):** `aura_v05350_decision_engine.py` + `tests/test_aura_v05350_decision_engine.py`
**Status:** Complete. Stopping here per the standing procedure.

---

## 1. Why this milestone matters

Per every revision of the governing roadmap, `.51`'s one-line description has always been the same: *"the execution spine has been ready since `.38`; this finally feeds it."* This is the first module in AURA that computes a live, current-bar equity/ETF technical signal and delivers it as typed evidence into `.50`'s Decision Engine — using the venue (Alpaca) `.35`–`.38` already know how to execute against. Architecture, exactly as specified:

```
Pinned Equity/ETF Universe
         |
Existing Technical Features (technical_agent.py, adapted)
         |
.51 Live Technical Signal
         |
Typed Technical Evidence (TechnicalRegime)
         |
.50 Decision Engine (small additive extension)
         |
.44 Enforcement
```

`.51`'s own scope stops at producing a `TechnicalRegime` and feeding it into `.50`. It does not build a `CanonicalExecutionIntent` — that boundary belongs to `.50`, unchanged. `.39`, `.44`, and the overall roadmap sequencing are untouched.

## 2. Reuse-first audit (dedicated subagent pass, read-only, file:line cited, before any code was written)

| Source | Classification | Disposition |
|---|---|---|
| `technical_agent.py` (`build_features`, `rsi`) | **A** | Reused verbatim (copied, not imported, per this repo's convention). Pure, stateless, strictly backward-looking (`.diff()`/`.rolling()`/`.ewm()`/`.shift(1)` only) — confirmed no-look-ahead by construction and by a dedicated regression test. |
| `signal_validator.py` (`evaluate_signal`) | **B** | The "score the last row" (`df.iloc[-1]`) mechanism is a genuine, correct live-evaluation pattern — reused. Every scoring constant (point values, RSI band, rel-volume threshold, whipsaw thresholds, status cutoffs) was hand-picked with no derivation — conflicts with this project's "never invent numbers" discipline. Every one of those constants is now a required field on `TechnicalScoringParams`. |
| `historical_signal_scanner.py` (`load_data`) | **B** | The only genuine, working Alpaca equity market-data fetch code in the repo (`StockHistoricalDataClient`/`StockBarsRequest`). SDK call shape reused in `AlpacaHistoricalBarsClient`; adapted from an explicit historical `start`/`end` range to a live lookback window. |
| `historical_signal_quality_scanner_v04`–`v0471.py` series, `aura_regime_backtest_multi_symbol.py` | Read in full, **not adopted this milestone** | Explicitly self-labeled historical-backtest/research-only, no live/current-bar entry point (`discover_signals` is vectorized over an entire historical frame). Per Martin's explicit instruction, treated as research evidence only — `.51` does not optimize or tune its scoring basis against this series' results. |
| `multi_symbol_historical_scanner.py:28-68` | Verified, reused verbatim | The only pinned equity/ETF list anywhere in the codebase — 27 symbols, verified directly from the file (not reconstructed from memory), now `aura_v05351_equity_universe_v1.json` version `"v1"`. |
| AegisAlpha `strategy/screener.py` (`StockLatestBarRequest`/momentum-prescreen/shortlist pattern) | **B** | Closest external analog to `.51`'s fetch → deterministic-score → shortlist → Decision Engine shape. Not reused directly: its momentum metric is invented/hardcoded, and on fetch failure it silently falls back to a stale watchlist — the opposite of this project's fail-closed discipline. `.51` fails closed instead (`fetch_live_technical_regime` never silently substitutes stale data on a fetch failure — proven by a dedicated test). |
| Hermes Trader `pipeline/alpaca_feed/data.py` (`StockLatestQuoteRequest`) | Confirms a second viable SDK primitive, **not adopted** | Fetches only a bid/ask midpoint, not the OHLCV history the reused feature set needs. |
| Full competitor sweep (DELTAX/DELTAX-AURA/DELTAX V2, CAURA, BABIL, optionwright, and the rest) | — | No genuine live equity technical-signal-generation module found anywhere. `.51`'s evaluation core is greenfield, built from the reused pieces above. |

## 3. Martin's scoping decisions and how each was implemented

1. **`.51 → .50` integration: feed `.50` directly, same standard as `.46`/`.47`/`.48`.** No `.39` involvement — `.39` remains completely unchanged. See §4 for the exact additive `.50` extension.
2. **Technical signal basis: adapt `technical_agent.py` + `signal_validator.py`.** Features reused verbatim; the "score the last bar" mechanism reused; every scoring constant replaced by a required `TechnicalScoringParams` field (26 fields, all required, no defaults). `.51` provides a genuine deterministic current-bar evaluation function (`evaluate_current_bar`), not a historical scanner.
3. **Equity/ETF universe: the verified 27-symbol list, configuration-driven and versioned.** Stored in `aura_v05351_equity_universe_v1.json` (version `"v1"`, source cited in-file), loaded by `load_pinned_universe()`. This is a research starting universe, not a live-trading validation claim.
4. **Architecture: narrowly scoped.** `.39`, `.44`, and the roadmap sequence are unmodified. `.50`'s change is the one small additive extension below.

## 4. `.50` integration changes (the ONLY changes made to `aura_v05350_decision_engine.py`)

1. `CandidateEvidence` gains two new fields: `technical_regime: Any | None` (a `.51` `TechnicalRegime`, duck-typed exactly like `sentiment_regime`/`wave_result`) and `technical_usable: bool`.
2. `build_candidate_evidence()` gains one new optional parameter, `technical_regime: Any | None = None`, defaulting to `None` — every caller that predates `.51` is unaffected. A candidate is `technical_usable` only when `status in {"CONFIRMING", "CONFIRMED"}` — its own internal quality bar, mirroring `.47`/`.48`'s "usable means passed its own bar, not merely present" discipline.
3. `compute_base_rank_score()` gains one new REQUIRED (no default) parameter, `technical_weight: float`, and one new term: `score += technical_weight * (technical_regime.signal_score / 100.0)` — added only when usable, and always ADDED, never subtracted, because `.51` is scoped LONG-only this milestone. `.50`'s "direction is decided before penalties are applied" invariant is unchanged.
4. `decide()` gains the same new required `technical_weight` parameter, threaded straight through.
5. `.50`'s existing 51 tests were updated to pass `technical_weight=0.0` at each `decide()`/`compute_base_rank_score()` call site — an explicit "technical evidence not in use here" value, never a code-level default. **All 51 prior tests pass unmodified in behavior.**

No other `.50` function, dataclass, or docstring section was touched. `run_deterministic_critic`, `TradingDecision`, `source_kind` handling, and the `.49` AI-proposal/challenge integration are all completely unmodified. 13 new tests were added to `.50`'s own suite directly exercising this integration (technical-usable/not-usable classification, shortlist eligibility via technical alone, score additivity/normalization, the "technical never subtracts" LONG-only invariant, freshness via technical's own `as_of`, and the "absent-by-default is unaffected" guarantee).

## 5. Exact technical signal definition

**Features** (`build_technical_features`, reused verbatim from `technical_agent.py`): EMA 3/8/21/50, RSI-14, MACD (12/26) and its 9-period signal line, Bollinger-band position (20-period), ATR-14 and ATR%, relative volume (vs. its own trailing 20-bar average), price acceleration (second difference of returns), and EMA 3/8, 8/21, 21/50 crossover flags. Every computation reads only the current and prior rows.

**Evaluation** (`evaluate_current_bar`, adapted from `signal_validator.py`): reads only the last row (`df.iloc[-1]`) plus trailing `.tail(...)` windows for persistence and whipsaw checks. Score components, each driven by a required `TechnicalScoringParams` field (no defaults anywhere): EMA3-crosses-above-EMA8 (fresh crossover), EMA3>EMA8, EMA8>EMA21, EMA21>EMA50, MACD-bullish, RSI-in-constructive-band (with a separate RSI-extended penalty above a required threshold), relative-volume-above-threshold, positive-price-acceleration, and an EMA3/8 persistence bonus over a required lookback window; a momentum-reversal/whipsaw penalty over the last 4 closes. The combined score is clamped to a required `[score_floor, score_ceiling]` range, and classified into one of `EARLY`/`CONFIRMING`/`CONFIRMED`/`FAILED` using required score/persistence thresholds. `TechnicalRegime.status` additionally carries `INSUFFICIENT_DATA` (too few clean bars, or any NaN in a value actually read) and `STALE_DATA` (last bar older than a required `max_bar_age_seconds`, or future-dated). Only `CONFIRMING`/`CONFIRMED` are `technical_usable` by `.50`. `TechnicalRegime.signal_score` is an **unsigned bullishness magnitude in `[score_floor, score_ceiling]`** — `.51` is LONG-only this milestone; there is no bearish/short scoring path anywhere in the module (verified by a dedicated AST-based governance test, not a raw string search, to avoid false-positiving on the module's own docstring prose).

## 6. Pinned universe

`aura_v05351_equity_universe_v1.json`, version `"v1"`, 27 symbols verified directly from `multi_symbol_historical_scanner.py:28-68`: **Technology** (AAPL, MSFT, NVDA, AMZN, GOOGL, META, AVGO), **Consumer/EV** (TSLA, WMT, COST), **Financials** (JPM, BAC, GS), **Energy** (XOM, CVX), **Industrials/materials** (CAT, DE, GE), **Defense** (LMT, RTX), **Healthcare** (LLY, JNJ), **Market/commodity proxies** (SPY, QQQ, IWM, GLD, SLV). `load_pinned_universe()` fails closed (raises `LiveSignalSourceError`) on a missing file, malformed JSON, missing version, empty symbol list, or duplicate symbols. This is a research starting universe, not a claim any symbol here is validated for live trading.

## 7. All caller-supplied parameters (never invented by this module)

`TechnicalScoringParams` (26 required fields, no defaults): `min_bars_required`, `max_bar_age_seconds`, `ema3_cross_ema8_points`, `ema3_above_ema8_points`, `ema8_above_ema21_points`, `ema21_above_ema50_points`, `macd_bullish_points`, `rsi_constructive_low`, `rsi_constructive_high`, `rsi_constructive_points`, `rsi_extended_threshold`, `rsi_extended_penalty_points`, `rel_volume_threshold`, `rel_volume_points`, `price_acceleration_points`, `persistence_lookback_bars`, `persistence_min_bars_for_bonus`, `persistence_points`, `whipsaw_prior_move_threshold`, `whipsaw_now_move_threshold`, `whipsaw_penalty_points`, `score_floor`, `score_ceiling`, `early_status_max_persistence`, `confirmed_score_threshold`, `confirmed_min_persistence`, `confirming_score_threshold`. `__post_init__` validates internal consistency (bounds ordering, positivity) but never supplies a magnitude. On the `.50` side: `technical_weight` (required, no default). `fetch_recent_bars`'s `calendar_buffer_days` is the one exception — a purely mechanical weekend/holiday calendar-conversion margin, not a trading/strategy magnitude, kept as an explicit named parameter rather than invented silently inline.

## 8. Tests

**`.51`'s own suite** (`tests/test_aura_v05351_live_alpaca_equity_signal_source.py`) — **47 tests**, in two deliberate strategies: `evaluate_current_bar` tested against synthetic, hand-controlled feature rows for exact status/boundary control; `build_technical_features`/`build_technical_regime_from_bars` tested end-to-end against realistic, deterministically-generated (no randomness) price series for no-look-ahead, reproducibility, freshness, and `.50` integration. Categories: current-bar evaluation (CONFIRMED/EARLY/CONFIRMING/FAILED, RSI-extended penalty, whipsaw penalty, score clamping); deterministic repeatability; missing/invalid/incomplete OHLCV (missing columns raise, NaN in a required field → `INSUFFICIENT_DATA`, too few bars, empty frame, `None` input raises); stale data (too-old bar, future-dated bar, exact boundary is fresh, just-past-boundary is stale); boundary conditions (exactly `min_bars_required`, RSI exactly at band edges, rel-volume exactly at threshold, `TechnicalScoringParams` validation); no-look-ahead/no-repainting (appending future bars leaves a shared past row's every computed value bit-for-bit identical; re-evaluating the same historical cutoff twice is stable); evidence provenance (`universe_version`, `bars_used`, `last_bar_timestamp`, `scoring_params_hash` changes when params change, invalid status rejected); `.51 → .50` integration (a real `TechnicalRegime` flows into `build_candidate_evidence`/`is_shortlist_eligible`/`compute_base_rank_score`, and a `FAILED` regime correctly does **not** reach the shortlist); behavior when technical evidence is absent; pinned-universe loading (real file, 27 symbols, version `"v1"`, fail-closed on 5 malformed-config scenarios); the live fetch layer against a fake client (lookback trimming, MultiIndex handling, empty-response rejection, non-positive-lookback rejection, fetch-exception wrapping, and — matching the AegisAlpha anti-pattern explicitly rejected in the audit — proof a fetch failure never silently falls back to stale/cached data); and a governance check (AST-based, not a raw string search) that no short/bearish-named code path exists anywhere in the module.

**`.50`'s extended suite** — **64 tests** (51 prior, unmodified in behavior, + 13 new technical-integration tests).

**Full regression suite:** `python3 -m pytest tests/ -q` → **957 passed** (897 prior + 13 `.50`-side + 47 `.51`-side), run twice, clean both times (no flaky failures observed this pass).

**Lint (`pyflakes`):** clean on all four touched/new files (`aura_v05351_live_alpaca_equity_signal_source.py`, its test file, `aura_v05350_decision_engine.py`, its test file). Two test-only bugs were found and fixed during development (both in `.51`'s new test file, never in production code): (1) several tests constructed variant `TechnicalScoringParams` via `TEST_PARAMS.__dict__`, which doesn't exist on a `slots=True` frozen dataclass — fixed using `dataclasses.replace()`; (2) the initial governance test for "no short/bearish scoring path" used a raw substring search and false-positived on the module's own docstring prose (which legitimately discusses `.50`'s `SHORT_LEANING`/`DECIDE_SHORT` vocabulary to explain why `.51` never produces it) — fixed by switching to an AST-based scan of actual function/variable/attribute names, the same class of fix already applied once in `.49`'s suite for an analogous docstring-grep false positive.

## 9. Persistence

None. `build_technical_regime_from_bars` (the deterministic core) is a pure function of a caller-supplied bars DataFrame and required parameters. The live fetch layer (`fetch_recent_bars`/`AlpacaHistoricalBarsClient`/`fetch_live_technical_regime`) is isolated to three clearly-marked functions/classes — everything else in the module is network-free and directly testable, mirroring `.49`'s `AnthropicClient` isolation.

## 10. Known limitations (disclosed, not silently worked around)

- **No live smoke test against a real Alpaca market-data call** — no live credentials configured in this sandboxed environment. `AlpacaHistoricalBarsClient` is unit-tested against a fake client shaped like the real SDK's response object (`.df`, a `(symbol, timestamp)`-MultiIndex DataFrame), matching the exact disclosed-limitation pattern `.35`/`.49` already use.
- **`TechnicalScoringParams` has 26 required fields** — deliberately verbose rather than a small number of coarse weights, per Martin's explicit instruction to replace every undocumented constant with an explicit parameter rather than redesign the mechanism into something coarser. No default values are supplied for any of them; a real evaluation run requires a caller to supply genuine, derived research values.
- **`.51` is LONG-only this milestone** (Martin's explicit decision — `.52` remains separate for the short side). `TechnicalRegime` has no bearish/short scoring path, and `.50`'s new technical term is always additive.
- **The `v04`–`v0471` walk-forward-validated series is not what `.51` evaluates live** — per explicit instruction, `.51` did not optimize or tune its scoring basis using that series' historical results this milestone. Upgrading `.51`'s live basis to match that more rigorous series would be a deliberate, separate, explicitly-approved change.
- **No scheduled/orchestrated entry point** — mirrors `.43`'s own disclosed limitation. Wiring a periodic live evaluation run is left to a future orchestration milestone.
- **The pinned universe is unvalidated for live trading** — reused because it is the only vetted default anywhere in the codebase, explicitly framed as a research starting point, not a live-trading claim.

## 11. Governance summary

- `.51` computes evidence only; it never authorizes, sizes, modifies, or executes an order — no such field exists on `TechnicalRegime`, and the module contains no order/trade-submission function (checked by a dedicated test).
- `.50`'s existing boundary is unchanged: AI (`.49`) proposes/challenges, `.50` decides deterministically, `.33`–`.38` (unmodified) remain the only path to an actual order. `.51`'s technical evidence is just a third input into `.50`'s already-existing, already-tested decision mechanism.
- `.39` is completely untouched — `.51`'s evidence feeds `.50` directly, the same standard already applied to `.46`/`.47`/`.48`.
- A fetch failure in the live data layer propagates as `LiveSignalSourceError`; it is never silently swallowed or substituted with stale data (proven by a dedicated test, explicitly contrasting with the AegisAlpha anti-pattern found in the reuse-first audit).

## 12. Checkpoint

Local commit only, not pushed (per explicit instruction). Files to be committed: `aura_v05351_live_alpaca_equity_signal_source.py`, `aura_v05351_equity_universe_v1.json`, `tests/test_aura_v05351_live_alpaca_equity_signal_source.py`, `aura_v05350_decision_engine.py` (additive extension), `tests/test_aura_v05350_decision_engine.py` (extended), and this report. No other untracked files touched.

## 13. Next

Per the standing procedure, stopping here to report before continuing. Per the earlier agreed sequence: `.52` (Stock/ETF short-side signal — now able to revisit its own scope with `.35`'s short-execution readiness already confirmed), then `.53` (Full paper orchestration), then the post-`.53` crypto backtest validation campaign Martin has flagged as the next major gate. Not proceeding to `.52` until this report is reviewed and the next explicit GO is given.
