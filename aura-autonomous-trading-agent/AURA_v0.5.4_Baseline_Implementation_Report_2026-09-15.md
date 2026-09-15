# AURA v0.5.4 — Baseline Implementation Report

**Date:** 2026-09-15
**Status:** Implementation complete. Tests passing. **REAL_EQUITY_BACKTEST = NOT_RUN** (no real market data available in this environment — see Section 6).
**Scope authorization:** Martin's full 19-section `.54` implementation specification, this session, following approval of two pre-implementation clarifying questions (Section 2).

---

## 1. Summary

`.54` is a clean, isolated, frozen-parameter extension of the AURA codebase implementing:

1. A Wilder ATR component (`aura_v054_atr.py`) — EXPLICITLY_SELECTED methodology.
2. A dynamic per-bar ATR trailing-stop exit engine (`aura_v054_exit_engine.py`) — a new function, not an edit to the existing `aura_exit_policy_backtest.py::simulate_trade`.
3. ATR-risk-based position sizing (`aura_v054_position_sizing.py`) — replacing the old fixed-10%-of-equity model.
4. Portfolio risk controls and a planned-risk ledger (`aura_v054_portfolio_risk.py`) — fixing the confirmed `NOT_COMPUTABLE` per-trade-risk gap found in `.43`.
5. An isolated, swappable cost model (`aura_v054_cost_model.py`).
6. A neutral, deterministic LLM stub (`aura_v054_llm_stub.py`) resolving the conflict between `.50.decide()`'s AI-proposal step and the hard-determinism requirement.
7. A pluggable entry-signal-source interface (`aura_v054_signal_source.py`) with two implementations: the real, frozen `.50`/`.51`/`.52` decision-engine wiring, and a disclosed synthetic test-fixture rule used only to exercise the pipeline.
8. A pluggable data-ingestion interface (`aura_v054_data_interface.py`) and baseline backtest runner (`aura_v054_backtest.py`) that is ready to accept a real S&P 100 daily OHLCV dataset without any change to strategy/risk/exit/sizing logic.

`.53` is untouched (verified — Section 9). No optimization was performed. No commit, no push.

---

## 2. Pre-implementation clarifications (resolved before any code was written)

Per Martin's own spec, Section 19 ("STOP CONDITION... if the ambiguity affects a strategy parameter or research methodology"), two findings from the architecture investigation were surfaced to Martin *before* implementation began, and both were explicitly resolved by him:

| # | Finding | Martin's resolution |
|---|---|---|
| 1 | `.50.decide()` requires a real `llm_client` for its AI-proposal/adversarial-challenge step — not a pure deterministic function, in tension with the hard-determinism requirement. | **Neutral stub client** — a deterministic, always-no-concerns stub modeled on `.50`'s own `FakeLLMClient` test pattern. `ai_penalty_per_concern` structurally never fires; `run_deterministic_critic` remains fully active. |
| 2 | No real S&P 100 daily OHLCV data reachable in this sandbox (no network, no credentials, no pinned dataset). | **Proceed with full implementation and test suite now using deterministic synthetic fixtures; defer the live baseline backtest.** Explicitly reconfirmed in Martin's follow-up message: build a data-ingestion interface so a real dataset can be plugged in later without changing strategy/risk/exit/sizing logic, and report `REAL_EQUITY_BACKTEST = NOT_RUN` with an explicit reason — never fabricate real-market numbers. |

---

## 3. Frozen parameters (as implemented)

All values below are used **exactly as frozen** — none were re-derived, re-selected, or optimized in this pass.

| Parameter | Value | Status |
|---|---|---|
| Asset class / scope | STOCK + ETF, LONG only | CONFIRMED (frozen `.53` decision) |
| Universe | S&P 100, `UNIVERSE_VERSION = "S&P100_FROZEN_2026-08-26_CORRECTED"` (`aura_v0481_universe_integrity.py`) | CONFIRMED |
| Timeframe | Daily / 1D OHLCV | CONFIRMED |
| `.50` weights | `sentiment_weight=1.0, wave_weight=1.0, technical_weight=0.0, short_technical_weight=0.0, decision_threshold=0.1, ai_penalty_per_concern=0.2, critic_penalty_per_issue=0.15` | CONFIRMED (frozen recurring test fixture, `tests/test_aura_v05350_decision_engine.py::DECIDE_KWARGS`) |
| `.51`/`.52` scoring params | Verbatim from `tests/test_aura_v05351_...py::TEST_PARAMS` / `tests/test_aura_v05352_...py::TEST_PARAMS` | CONFIRMED (frozen recurring test fixture) |
| ATR method | **WILDER** (`aura_v054_atr.py`), period = 14 | EXPLICITLY_SELECTED (Martin, 2026-09-15) |
| Exit model | Dynamic per-bar ATR trailing stop | EXPLICITLY_SELECTED |
| `TRAIL_ATR_MULT` | 2.0 | EXPLICITLY_SELECTED / BASELINE_RESEARCH_HYPOTHESIS |
| `take_profit_pct` | `None` (no fixed target) | EXPLICITLY_SELECTED |
| `max_hold_bars` | 20 trading days | EXPLICITLY_SELECTED / BASELINE_RESEARCH_HYPOTHESIS |
| `risk_fraction_per_trade` | 0.005 (0.5% of equity) | EXPLICITLY_SELECTED / BASELINE_RESEARCH_HYPOTHESIS |
| `max_positions` | 10 | EXPLICITLY_SELECTED / BASELINE_RESEARCH_HYPOTHESIS |
| `max_gross_exposure` | 1.00 (no leverage) | EXPLICITLY_SELECTED / BASELINE_RESEARCH_HYPOTHESIS |
| `max_portfolio_risk` | 0.02 (2% of equity) | EXPLICITLY_SELECTED / BASELINE_RESEARCH_HYPOTHESIS |
| `cost_pct` | 0.001 (0.10%, round-trip) | EXPLICITLY_SELECTED (conservative equity-research assumption, not a recovered fee schedule) |
| Holdout | Trailing 20% of the *actual dataset used*, computed mechanically, chronological only | CONFIRMED methodology; boundary is computed at run time, never hardcoded |

---

## 4. Architecture

```
BarsProvider (pluggable)                SignalSource (pluggable)
  ├─ SyntheticBarsProvider (TEST ONLY)     ├─ AuraFrozenDecisionEngineSignalSource
  ├─ CSVBarsProvider (real-data-ready)     │    (.50 + .51 + .52 + neutral LLM stub)
  └─ UnavailableRealEquityDataSource       └─ SyntheticTestFixtureSignalSource (TEST ONLY)
       (honest default)                          │
            │                                     │
            ▼                                     ▼
      aura_v054_atr.py (Wilder ATR)   ─────►  aura_v054_backtest.py
                                                 │      (orchestrator)
      aura_v054_exit_engine.py  ◄───────────────┤
      aura_v054_position_sizing.py  ◄───────────┤
      aura_v054_portfolio_risk.py  ◄────────────┤
      aura_v054_cost_model.py  ◄────────────────┘
```

Every arrow into `aura_v054_backtest.py` is a protocol/interface, not a concrete import of a data or signal implementation — this is what lets a real dataset (via `CSVBarsProvider` or a future live-Alpaca-backed provider) and, separately, real signal generation be substituted without touching ATR/exit/sizing/portfolio-risk/cost code.

### 4.1 The critical, proven finding: the frozen decision engine currently never trades

`AuraFrozenDecisionEngineSignalSource` wires `.50`'s real `decide()`, `.51`'s real `build_technical_regime_from_bars`, and `.52`'s real `build_short_technical_regime_from_bars` together, using the frozen parameters above and the neutral LLM stub. Because `technical_weight=0.0` and `short_technical_weight=0.0` are frozen, and `.54` supplies no `sentiment_regime`/`wave_result` (the `.46`–`.49` sentiment/wave evidence pipeline is out of this pass's scope), `compute_base_rank_score` is **mathematically, provably always `0.0`** for every symbol and every bar. `direction` is always `"NO_DIRECTIONAL_EVIDENCE"`; `outcome` is always `"ABSTAIN"`.

This is **proven by test** (`tests/test_aura_v054_signal_source.py`, `tests/test_aura_v054_backtest.py::test_frozen_real_signal_source_produces_zero_trades_in_full_backtest`), not merely asserted — and independently confirmed by running the actual backtest runner against synthetic data (Section 7). This was already flagged, in weaker form, in `AURA_v0.53_Frozen_Candidate_Freeze_Record_2026-09-15.md`'s internal-consistency check ("`.51`/`.52` currently mathematically inert in the decision score"); this pass makes the full consequence — **zero trade decisions, ever, under the current frozen weights** — explicit and empirically confirmed.

This is not a defect in `.54`'s implementation. It is a faithful, correct reflection of the frozen `.50`/`.51`/`.52` parameter set. **It means the real decision-engine pipeline cannot produce a single trade until Martin either (a) reweights `technical_weight`/`short_technical_weight` away from `0.0`, or (b) wires a real `sentiment_regime`/`wave_result` evidence source, or (c) both** — a strategy decision, not an implementation one, and is surfaced here rather than worked around.

---

## 5. Module-by-module notes

- **`aura_v054_atr.py`** — Clean reimplementation of Wilder smoothing (not a wrapper around `.12`'s crypto-scoped `wilder_atr`), against `.51`'s daily-equity bar schema. `ATR_PERIOD=14`, matching both `.51`'s and `.12`'s existing period choice, kept for comparability, not re-derived.
- **`aura_v054_exit_engine.py`** — `simulate_atr_trailing_trade`, a genuinely new function (existing `aura_exit_policy_backtest.py::simulate_trade` is never imported or modified). Implements: no-look-ahead (stop checked against the PRIOR bar's level before this bar updates it), same-bar stop/target collision resolves to STOP (mirrors the existing harness), and a trail that can only move in the trade's favor (`stop_trace` monotonicity is directly tested). SHORT is documented, not implemented, per Martin's explicit scoping. Unit convention: **fractions** (0.01 = 1%) throughout — documented explicitly to avoid the percentage-point/fraction ambiguity present between `aura_exit_policy_backtest.py` and Martin's own `.54` spec text.
- **`aura_v054_position_sizing.py`** — `size_position_by_atr_risk`; fails safe (raises) on a missing/non-positive/NaN `planned_stop_distance`, equity, or price; returns `quantity=0` (not an error) when the risk budget can't afford even one share.
- **`aura_v054_portfolio_risk.py`** — `PortfolioRiskLimits` (10 positions / 100% gross / 2% portfolio risk, no-leverage enforced at construction) plus `PlannedRiskLedger`, which persists each open position's planned stop distance and risk dollars — the direct fix for `aura_v05343_portfolio_exposure_observability.py`'s confirmed `per_trade_risk NOT_COMPUTABLE` gap. Scoped to `.54` only; does not modify `.44`.
- **`aura_v054_cost_model.py`** — `FlatRoundTripCostModel(cost_pct=0.001)`, isolated behind a `CostModel` protocol so a future commission+spread+slippage model can be swapped in without touching any other `.54` module.
- **`aura_v054_llm_stub.py`** — `NeutralDeterministicLLMClient`; see Section 2, finding 1.
- **`aura_v054_signal_source.py`** — see Section 4.1.
- **`aura_v054_data_interface.py`** — see Section 6.
- **`aura_v054_backtest.py`** — orchestrator. Holdout boundary computed mechanically from the *shortest* symbol's actual bar count (`compute_holdout_boundary`), never hardcoded to any prior date estimate. Known, disclosed simplifications: (a) all symbols in one run are assumed to share an identical trading-day calendar — true of the synthetic fixture, not necessarily true of a real multi-symbol equities dataset (IPOs/delistings/halts) — flagged, not silently assumed for the real-data case; (b) equity marks to market only at each trade's close, not continuously daily, so `CAGR` is intentionally left `None` and Sharpe/Sortino are trade-level, not daily-return-level.

---

## 6. Data source status — REAL_EQUITY_BACKTEST = NOT_RUN

```
REAL_EQUITY_BACKTEST = NOT_RUN
REASON = "No real S&P 100 daily OHLCV dataset available in current environment."
```

Confirmed directly (not assumed) at implementation time:
- No general internet access: `curl` to both `data.alpaca.markets` and a generic external host failed identically (`curl: (56) CONNECT tunnel failed, response 403`).
- No real Alpaca credentials: only `.env.example` exists, itself scoped to CRYPTO keys.
- No pinned/cached continuous daily equities OHLCV dataset anywhere in this repository (only crypto `bars_1h.csv` files exist).
- `research/*_historical_signals.csv` files are sparse per-symbol signal-*event* snapshots from an older, differently-defined scanner — not continuous OHLC bar series, unusable as walk-forward input.

**When a real dataset becomes available**, the same frozen `.54` implementation runs against it with **no change to any strategy/risk/exit/sizing parameter**:

```python
provider = aura_v054_data_interface.CSVBarsProvider(directory="<path to per-symbol CSVs>")
report = aura_v054_backtest.run_baseline(
    universe=S_AND_P_100,  # aura_v0481_universe_integrity.S_AND_P_100
    bars_provider=provider,
    signal_source=aura_v054_signal_source.AuraFrozenDecisionEngineSignalSource(),
)
```

**Required data interface** for the real run: one CSV per symbol, named `<SYMBOL>.csv`, with header `timestamp,open,high,low,close,volume` — any pandas-parseable date string, ascending, no duplicate timestamps, no gaps invented (a missing trading day must be genuinely absent from the file, never forward-filled). This is validated automatically by `aura_v054_data_interface.validate_bars_frame` on every load. A future live-Alpaca-backed provider (adapting `.51`'s own `AlpacaHistoricalBarsClient`) can be substituted the same way, satisfying the same `BarsProvider` protocol.

No real-market performance numbers have been generated anywhere in this pass. Every number in Section 7 is explicitly and permanently labeled synthetic.

---

## 7. Synthetic-data validation results (NOT real performance — pipeline-wiring proof only)

Run against `SyntheticBarsProvider` (deterministic, formula-based, `DATA_SOURCE_LABEL="SYNTHETIC_TEST_FIXTURE"`), universe = 10 arbitrary large-cap tickers, 500 synthetic trading days, full results saved to `aura_v054_synthetic_validation_results.json`.

**7.1 — `SyntheticTestFixtureSignalSource` (NOT a strategy; a fixed 20-day-momentum rule used only to force trades so the exit/sizing/risk/cost machinery can be exercised end-to-end):**

- `real_equity_backtest_status`: `RUN` (synthetic), 103 trades, final equity $87,874.42 from a $100,000 start.
- This is **not evidence of edge, positive or negative** — the rule is arbitrary and the price series is a formula, not a market. It exists only to prove that entries open, ATR trailing stops move correctly, sizing respects the risk budget, portfolio limits gate correctly, and costs are applied.
- Holdout partition confirmed strictly chronological (all `RESEARCH` trades' entries precede the mechanically computed boundary; all `HOLDOUT` trades' entries are at or after it — directly tested).
- Determinism confirmed: two independent runs against fresh `SyntheticBarsProvider` instances produced byte-identical trade lists and final equity (directly tested).

**7.2 — `AuraFrozenDecisionEngineSignalSource` (the real `.50`/`.51`/`.52` pipeline) against the same synthetic data:**

- `real_equity_backtest_status`: `RUN`, **0 trades** — confirming Section 4.1's finding empirically within the actual backtest runner, not just at the unit level.

**7.3 — `UnavailableRealEquityDataSource`:**

- `real_equity_backtest_status`: `NOT_RUN`, reason as in Section 6.

---

## 8. Test results

| Suite | Result |
|---|---|
| New `.54` tests (`tests/test_aura_v054_*.py`, 9 files) | **59 passed, 0 failed** |
| Full pre-existing repository test suite (everything else under `tests/`) | **1024 passed, 0 failed** (no regressions) |
| Combined | **1083 passed, 0 failed** |

Coverage against Martin's 16 named categories:

| Cat. | Requirement | Covered by |
|---|---|---|
| A | Wilder ATR calc | `test_aura_v054_atr.py` |
| B | ATR trailing stop | `test_aura_v054_exit_engine.py::test_category_b_*` |
| C | Trail only moves favorably | `test_aura_v054_exit_engine.py::test_category_c_*` |
| D | ATR×2.0 multiplier | `test_aura_v054_exit_engine.py::test_initial_stop_distance_long_is_atr_times_mult` |
| E | No fixed take profit | `test_aura_v054_exit_engine.py::test_category_e_*` |
| F | 20-bar max hold | `test_aura_v054_exit_engine.py::test_category_f_*` |
| G | 0.5% risk-based sizing | `test_aura_v054_position_sizing.py::test_category_g_*` |
| H | 10-position max | `test_aura_v054_portfolio_risk.py::test_category_h_*` |
| I | 100% gross exposure max | `test_aura_v054_portfolio_risk.py::test_category_i_*` |
| J | 2% portfolio risk max | `test_aura_v054_portfolio_risk.py::test_category_j_*` |
| K | 0.10% round-trip cost | `test_aura_v054_cost_model.py` |
| L | 20% chronological holdout | `test_aura_v054_backtest.py::test_category_l_*` |
| M | No holdout leakage | `test_aura_v054_backtest.py::test_category_m_*` |
| N | Deterministic repeated execution | `test_aura_v054_backtest.py::test_category_n_*`, `test_aura_v054_signal_source.py::*is_deterministic` |
| O | Sizing fails safely | `test_aura_v054_position_sizing.py::test_category_o_*` |
| P | `.53` behavior untouched | Full pre-existing suite re-run unchanged (Section 8 above) + git diff (Section 9) |

---

## 9. Git / version control verification

- `git status --short` **before** this task: 16 untracked planning/report `.md` files (all authored in this project this session), zero modified tracked files.
- `git status --short` **after** this task: identical set of tracked files unmodified; only new **untracked** files added (all `.54` source, all `.54` tests, this report, the synthetic validation JSON).
- **Confirmed: no `.53` file, no test file, no config file was edited, renamed, or deleted.**
- No commit. No push.

**Files created (all new, all untracked):**
```
aura_v054_atr.py
aura_v054_data_interface.py
aura_v054_exit_engine.py
aura_v054_position_sizing.py
aura_v054_portfolio_risk.py
aura_v054_cost_model.py
aura_v054_llm_stub.py
aura_v054_signal_source.py
aura_v054_backtest.py
aura_v054_synthetic_validation_results.json
tests/test_aura_v054_atr.py
tests/test_aura_v054_exit_engine.py
tests/test_aura_v054_position_sizing.py
tests/test_aura_v054_portfolio_risk.py
tests/test_aura_v054_cost_model.py
tests/test_aura_v054_llm_stub.py
tests/test_aura_v054_signal_source.py
tests/test_aura_v054_data_interface.py
tests/test_aura_v054_backtest.py
AURA_v0.5.4_Baseline_Implementation_Report_2026-09-15.md   (this file)
```

**Files modified:** none.

**Files deliberately untouched:** every `.53` source/test/config file; `aura_exit_policy_backtest.py`; `aura_v05325_position_sizing.py`; `aura_v05344_portfolio_exposure_enforcement.py`; `aura_v05343_portfolio_exposure_observability.py`; every `.50`/`.51`/`.52`/`.49` source file (imported and called, never edited).

---

## 10. Deviations from the frozen specification (disclosed, not silent)

1. **LLM-determinism resolution** (Section 2, finding 1) — implemented exactly as Martin approved (neutral stub). Flagged here per his own instruction to document every interpretation of an ambiguity, even an approved one.
2. **Real baseline backtest deferred** (Section 2, finding 2 / Section 6) — implemented exactly as Martin approved. `REAL_EQUITY_BACKTEST=NOT_RUN` is a first-class, tested status, not a silent gap.
3. **Entry-signal-generation scope** — `.54`'s spec covers ATR/exit/sizing/portfolio-risk/cost/data (per Martin's own narrowing message this session: "Test ATR, exits, sizing, portfolio risk, costs, holdout calculation and determinism"). The real `.50`/`.51`/`.52` wiring was built and integration-tested (Section 4.1), but a full `.46`–`.49` sentiment/wave evidence-construction rebuild was judged out of scope for this pass and not attempted — `sentiment_regime`/`wave_result` are supplied as `None` to `build_candidate_evidence`, exactly as `.50`'s own API allows (both optional, default `None`). This is why the real pipeline currently abstains always (Section 4.1) — a scope boundary, disclosed here, not a hidden simplification.
4. **Multi-symbol calendar alignment** — disclosed as a known limitation in Section 5; not an issue for the synthetic fixture (identical calendar by construction) but will need reconciling for a real, ragged multi-symbol dataset.
5. **Equity curve granularity** — trade-level mark-to-market only (Section 5); `CAGR` intentionally omitted from segment metrics as a result; Sharpe/Sortino are trade-level statistics, not daily-return-based.

No strategy parameter was changed from what Martin froze. No value was invented. No backtest result — synthetic or otherwise — was used to adjust any parameter.

---

## 11. Remaining blockers for a real `.54` baseline result

1. **A real S&P 100 daily OHLCV dataset** must be supplied (CSV files via `CSVBarsProvider`, or Alpaca credentials + network access in a future session) before Section 7's numbers can be replaced with real ones.
2. **The zero-trade finding (Section 4.1)** is a strategy decision for Martin: the frozen `.50` weights currently make `.51`/`.52`'s technical signals inert, and no sentiment/wave source is wired. A real backtest, once real data exists, will also produce zero trades until this is addressed — supplying data alone will not produce a real result under the current frozen weights.
3. Multi-symbol ragged-calendar handling (Section 5/10.4) should be addressed before running against a real dataset where symbols have different listing histories.
