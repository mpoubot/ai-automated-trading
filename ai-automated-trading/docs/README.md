# ai-automated-trading — Phase 5 Minimum Viable Research Core

Status: **Phase 5 complete.** One controlled pipeline-validation research run
has been executed and verified. Per the Phase 5 command's stop condition,
work stops here for review — no optimization, no live trading, no Alpaca,
no dashboard, no AI strategy discovery, and the old `mexc_bot` is untouched.

This folder is new and independent. It does not import from `mexc_bot`,
`AURA`, `DELTAX`, or `LABLAB` — indicator/risk/cost logic that already
existed and was verified there (ATR, position sizing, funding lookup,
trade-management mechanics) has been re-implemented from scratch here, not
imported, per the instruction that this core "must remain independent...
Reuse existing components only through clearly defined interfaces." Every
module that ports logic says so in its own docstring, with a pointer to the
original file it was verified against.

## What this is (and is not)

This is the **minimum research core** needed to run a strategy against real
MEXC data end-to-end: load a frozen, sha256-verified dataset → wrap a
canonical instrument → evaluate a pluggable strategy → run a backtest with
realistic costs, funding, and risk gating → allocate fixed, independent
capital per symbol → produce a reproducible, machine-readable result.

**It is not** a trading strategy evaluation. The one strategy currently
wired in, `PipelineValidationFixture`, is a synthetic, deterministic,
long-only fixture with no market view — it exists only to exercise every
stage of the pipeline. Every result it produces is labelled
`PIPELINE VALIDATION FIXTURE — NOT A TRADING STRATEGY` and must not be read
as evidence of trading edge. Porting a real strategy (e.g. AURA v0.4.x) is
explicit future work, out of Phase 5 scope.

## Layout

```
core/
  instrument.py       canonical Instrument (MEXC-only for now)
  dataset.py           manifest-driven, sha256-verified dataset loader
  strategy_base.py      Strategy interface (the engine depends only on this)
  signal.py              Signal / PositionIntent types
  indicators.py          ATR (Wilder) — minimal indicator set
  costs.py                 fees, slippage, funding lookup
  risk.py                    ACTIVE / RESTRICTED / HALTED risk model + position sizing
  portfolio.py                 fixed, non-shrinking per-symbol capital allocator
  backtest_engine.py             the backtest loop (strategy-agnostic)
  results.py                       ResultRecord + metrics + reproducibility hash
strategies/
  pipeline_fixture.py    PipelineValidationFixture — the pipeline-proof strategy
research/
  run_first_experiment.py    the one controlled run
tests/                        11 files, one per required test area (65 tests total)
results/<run_id>/             one JSON per symbol + summary.json
```

## Running the pipeline

Requires Python 3.11+, `pandas`, `numpy`, `pyarrow`, `pytest` (all already
used by `mexc_bot`, no new dependency introduced).

```bash
cd ai-automated-trading

# Run the test suite (65 tests)
python3 -m pytest tests/ -v

# Run the one controlled research experiment (all 11 symbols)
python3 research/run_first_experiment.py
```

By default the experiment looks for the frozen dataset at
`../mexc_bot/data/native_v2_full720day_20260917` (a sibling of this folder
under `AI automated trading/`, which is where it lives on this machine).
Override with the `AURA_DATASET_ROOT` environment variable if the dataset
is somewhere else.

Each run writes `results/<run_id>/<SYMBOL>.json` (one `ResultRecord` per
symbol — run ID, strategy ID/version, dataset ID/version, instrument,
timeframe, start/end, bar count, trades, wins, losses, gross P&L, fees,
funding, net P&L, return, drawdown, exposure, profit factor, expectancy,
validation status, and a `config_hash` that lets two runs be compared for
exact reproducibility) plus one `results/<run_id>/summary.json` aggregating
all symbols.

## What was verified (see the validation report for detail)

No look-ahead (proven by truncation invariance, not just asserted), no
duplicate candles, correct trade/position accounting, correct fee
application, correct funding treatment (including an explicit flag on every
result for symbols whose funding history does not cover the full dataset
window), a corrected drawdown calculation, exact reproducibility across
independent runs, and — the direct regression test for the bug that
originally motivated this work — no symbol's trade count or result changes
depending on which other symbols are in the run or what order they're
processed in.

## What's explicitly out of scope here

No parameter sweeps or optimization, no live trading or MEXC order
placement, no Alpaca, no options, no dashboard, no AI/LLM strategy
discovery, no changes to `mexc_bot`/`AURA`/`DELTAX`/`LABLAB`, and nothing
here has been committed to git — that is a separate, later decision.

## Known scope gap: no cross-symbol concurrent-position cap

(PHASE5_INDEPENDENT_AUDIT_2026-09-22.md finding #5.) `core/portfolio.py`'s
`PortfolioAllocator` gives each symbol a **fixed, independent capital
reservation** — that bounds any one symbol's maximum possible loss to its
own slice, and is what fixes the shared-equity-pool bug (see
`core/portfolio.py`'s module docstring). It does **not** model or enforce
any cap on how many symbols may hold an open position **at the same time**
across the whole portfolio. Each symbol's `core/backtest_engine.py` run
holds at most one position for *that symbol*, entirely independently of
what any other symbol is doing — there is currently no portfolio-level
concept of "at most N concurrent positions across all symbols" or "total
open notional across the book," and nothing here enforces one.

This is a deliberate omission, not an oversight: the Phase 5 command scoped
portfolio allocation as "can be simple, no sophisticated portfolio
optimization yet," and a concurrent-position cap is exactly that kind of
sophistication — adding one without a real strategy to size it against
would be premature complexity for a pipeline-validation-only phase. Per
the remediation command's explicit instruction, one is deliberately **not**
being added here.

**This must be resolved before real multi-symbol or live execution.**
Fixed independent capital per symbol prevents a *shared-pool* failure mode,
but it does NOT prevent all 11 symbols from entering simultaneously and
collectively exceeding whatever aggregate risk/exposure the operator
actually intends to carry at once — that is a portfolio-level risk control
that has to be designed deliberately (e.g. a max-concurrent-positions
limit, a max-aggregate-notional limit, or correlation-aware sizing), not
inherited for free from per-symbol capital fixing. Treat this as an open
item for the phase that introduces a real strategy and/or live execution,
not as something already handled by `PortfolioAllocator`.
