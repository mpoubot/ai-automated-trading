#!/usr/bin/env python3
"""
AURA v0.5.4 -- Real-vs-random-timing permutation test for Track B
(Alpaca equities) SignalSources.

Generalizes `mexc_bot/permutation_test.py`'s real-vs-random-timing idea
onto Track B's REAL, already-working pipeline
(`aura_v054_backtest.run_backtest`, `aura_v054_signal_source.SignalSource`,
`aura_v054_data_interface.BarsProvider`) -- the same interface this
session already drove successfully in the 2026-10-03 sector-rotation-
weight sweep. No new simulation, exit, sizing, or risk-gating logic is
written here: every control run goes through `run_backtest` completely
unmodified, exactly like the real signal source does. This module adds
exactly one new thing -- a way to generate a "what if entries had been
timed randomly instead" SignalSource and compare its performance to the
real one, many times, for an empirical p-value.

WHY THIS IS A 1-D TEST (TIMING ONLY), NOT MEXC's 2x2 (TIMING x DIRECTION)
--------------------------------------------------------------------------
Confirmed by reading the real, current `aura_v054_backtest.py` AND
`aura_v054_exit_engine.py` (2026-10-08): `run_backtest`'s Pass 1 hardcodes
`direction="LONG"` on every call to
`EXIT.simulate_atr_trailing_trade(...)`, and that function itself raises
`ExitEngineError` if `direction != "LONG"` -- SHORT is explicitly
documented, not implemented, "since .54's scope stays STOCK/ETF long-only
for now." There is no direction axis to randomize against in this
pipeline. If a short-side path is later wired into a Track B backtest
harness (e.g. via `aura_v05352_stock_etf_short_side_signal.py`), a second,
direction-aware control could be added the same way this one was built --
nothing here assumes LONG-only as a permanent constraint, only as the
current, confirmed shape of `run_backtest`.

THE RANDOMIZATION DESIGN -- matched trade COUNT, random entry bars
--------------------------------------------------------------------
For each symbol, the control signal source draws the SAME NUMBER of
random candidate entry bars as the real signal source actually produced
TRADES for that symbol (i.e. `len([t for t in real_report.trades if
t.symbol == symbol])` -- trades that survived Pass 2's portfolio-risk
gating, not raw Pass-1 signal count, since `run_backtest` does not
expose a per-symbol raw-signal count and this module deliberately does
not modify `run_backtest` to add one). Candidate bars are drawn uniformly
at random (without replacement) from `[warmup_bars, n_bars)`.

This is a disclosed simplification, not a hidden one: because
`run_backtest`'s own Pass-1 forward-skip logic still applies to control
runs (an entry bar that lands inside a still-open simulated position's
lifetime is simply never reached), a control run's REALIZED trade count
can end up slightly LOWER than the real run's, never higher. That biases
the comparison conservatively against finding a false "no edge" result --
it never inflates the real signal's apparent significance by starving
the controls, since fewer control trades on a volatile instrument do not
systematically produce better metrics than more.

WHAT METRIC IS COMPARED
-------------------------
The real and control runs are compared on ONE performance metric,
computed over ALL trades (RESEARCH + HOLDOUT combined) -- intentionally
not split, since this permutation test answers "does this signal's
ENTRY TIMING contain information", a different question from "does
performance generalize out of sample" (already the job of
`run_backtest`'s own research/holdout split). Supported metrics:
`total_return_frac`, `avg_trade_return_frac`, `profit_factor`,
`sharpe_trade_level`. Each is recomputed locally from the public
`BacktestReport.trades`/`initial_equity`/`final_equity` fields rather
than importing `aura_v054_backtest.py`'s underscore-prefixed
`_segment_metrics` -- see `_metric_from_trades`'s docstring for the
cross-check that proves `total_return_frac` matches
`(final_equity - initial_equity) / initial_equity` exactly.

RESEARCH-ONLY: nothing here touches live trading, orders, or account
state. Every control run uses whatever `bars_provider` the caller
passes in -- it never knows or cares whether that data is real or
synthetic; see `aura_v054_data_interface.py`'s own real/synthetic
labeling for how to keep that distinction visible in a final report.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Sequence

import numpy as np
import pandas as pd

import aura_v054_backtest as BT
import aura_v054_data_interface as DATA
import aura_v054_multiple_testing as STATS

VERSION = "AURA v0.5.4"

SUPPORTED_METRICS = ("total_return_frac", "avg_trade_return_frac", "profit_factor", "sharpe_trade_level")
DEFAULT_METRIC = "total_return_frac"
DEFAULT_N_ITERATIONS = 200
DEFAULT_SEED = 54202


class PermutationTestError(Exception):
    pass


@dataclass
class RandomTimingSignalSource:
    """The ONE new piece. Returns `DECIDE_LONG` at exactly the pre-drawn
    bar indices in `entries_by_symbol[symbol]`, `ABSTAIN` everywhere else
    -- satisfies `aura_v054_signal_source.SignalSource` exactly, so
    `aura_v054_backtest.run_backtest` runs it completely unmodified,
    with its own real exit/sizing/portfolio-risk mechanics applying
    exactly as they do to any real signal source.
    """

    entries_by_symbol: dict[str, frozenset[int]]
    SIGNAL_SOURCE_LABEL: str = "RANDOM_TIMING_CONTROL"

    def decide_for_symbol(self, symbol: str, bars_up_to_now: pd.DataFrame, *, now: datetime) -> dict[str, Any]:
        # bars_up_to_now == df.iloc[:i+1] in run_backtest's own Pass 1 --
        # its length minus one IS the current absolute bar index, exactly
        # (the original DataFrame's positional index is never reset).
        i = len(bars_up_to_now) - 1
        if i in self.entries_by_symbol.get(symbol, frozenset()):
            return {"outcome": "DECIDE_LONG", "direction": "LONG_LEANING"}
        return {"outcome": "ABSTAIN", "direction": "NO_DIRECTIONAL_EVIDENCE"}


def draw_random_entries(
    *,
    n_bars_by_symbol: dict[str, int],
    k_by_symbol: dict[str, int],
    warmup_bars: int,
    rng: np.random.Generator,
) -> dict[str, frozenset[int]]:
    """Draws, per symbol, `k_by_symbol[symbol]` distinct bar indices
    uniformly at random from `[warmup_bars, n_bars_by_symbol[symbol])`.
    If `k` exceeds the number of eligible bars (pathologically short
    series), it is capped to the eligible count rather than raising --
    this can only happen on a near-degenerate dataset, never on a
    realistic one, and capping is the conservative choice (fewer control
    entries, never more than physically possible).
    """
    out: dict[str, frozenset[int]] = {}
    for symbol, n_bars in n_bars_by_symbol.items():
        k = k_by_symbol.get(symbol, 0)
        eligible = np.arange(warmup_bars, n_bars)
        if k <= 0 or len(eligible) == 0:
            out[symbol] = frozenset()
            continue
        k = min(k, len(eligible))
        chosen = rng.choice(eligible, size=k, replace=False)
        out[symbol] = frozenset(int(x) for x in chosen)
    return out


def _metric_from_trades(trades: Sequence[BT.TradeRecord], initial_equity: float, metric_name: str) -> float | None:
    """Recomputes one performance metric directly from a `BacktestReport`'s
    public `trades` list, without importing `aura_v054_backtest.py`'s
    underscore-prefixed `_segment_metrics` (an internal of a module this
    code does not own and should not couple to by name).

    Cross-checked (see tests/test_aura_v054_permutation_test.py::
    test_total_return_frac_matches_final_equity) against the ONE public
    invariant `run_backtest` guarantees regardless of its internals:
    `final_equity == initial_equity + sum(realized_pnl_dollars over all
    closed trades)` (true because `run_backtest`'s own `equity` variable
    is updated unconditionally, for every trade, regardless of
    RESEARCH/HOLDOUT period -- see its Pass 2 loop). So
    `total_return_frac` computed here is proven to equal
    `(report.final_equity - report.initial_equity) / report.initial_equity`
    exactly, for any real `BacktestReport` -- not just for this module's
    own synthetic test fixtures.
    """
    closed = [t for t in trades if t.net_return_frac is not None]
    if not closed:
        return None
    returns = [t.net_return_frac for t in closed]

    if metric_name == "total_return_frac":
        return sum((t.realized_pnl_dollars or 0.0) for t in closed) / initial_equity
    if metric_name == "avg_trade_return_frac":
        return sum(returns) / len(returns)
    if metric_name == "profit_factor":
        gross_profit = sum(r for r in returns if r > 0)
        gross_loss = -sum(r for r in returns if r <= 0)
        if gross_loss > 0:
            return gross_profit / gross_loss
        return float("inf") if gross_profit > 0 else None
    if metric_name == "sharpe_trade_level":
        if len(returns) < 2:
            return None
        mean = sum(returns) / len(returns)
        var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
        std = var ** 0.5
        return mean / std if std > 0 else None
    raise PermutationTestError(f"UNSUPPORTED_METRIC:{metric_name!r}:must be one of {SUPPORTED_METRICS}")


@dataclass(frozen=True)
class PermutationTestResult:
    metric_name: str
    real_value: float
    control_values: tuple[float, ...]
    n_iterations: int
    exceedances: int
    p_value: float
    real_report: BT.BacktestReport
    k_by_symbol: dict[str, int]
    notes: tuple[str, ...]


def run_permutation_test(
    *,
    universe: tuple[str, ...],
    bars_provider: DATA.BarsProvider,
    real_signal_source: Any,
    config: BT.BacktestConfig = BT.BacktestConfig(),
    metric_name: str = DEFAULT_METRIC,
    n_iterations: int = DEFAULT_N_ITERATIONS,
    seed: int = DEFAULT_SEED,
) -> PermutationTestResult:
    """Runs `real_signal_source` once (the real run), then
    `n_iterations` random-timing control runs matched to its per-symbol
    trade counts, all through the exact same, unmodified
    `aura_v054_backtest.run_backtest`. Returns an empirical p-value: the
    fraction of control runs whose metric is >= the real run's (using
    `aura_v054_multiple_testing.empirical_p`'s standard
    (exceedances + 1) / (iterations + 1) formula, reused rather than
    reimplemented).

    Raises `PermutationTestError` if the real run produced zero trades
    (metric_name undefined) or zero total bars-of-history across the
    universe -- a permutation test cannot be run on a signal that never
    fires.
    """
    if metric_name not in SUPPORTED_METRICS:
        raise PermutationTestError(f"UNSUPPORTED_METRIC:{metric_name!r}:must be one of {SUPPORTED_METRICS}")
    if n_iterations <= 0:
        raise PermutationTestError("INVALID_N_ITERATIONS:must be > 0")

    real_report = BT.run_backtest(
        universe=universe, bars_provider=bars_provider, signal_source=real_signal_source, config=config
    )
    real_value = _metric_from_trades(real_report.trades, real_report.initial_equity, metric_name)
    if real_value is None:
        raise PermutationTestError(
            f"REAL_RUN_PRODUCED_NO_TRADES: cannot run a permutation test -- "
            f"{real_signal_source!r} produced zero closed trades against this universe/config."
        )

    k_by_symbol: dict[str, int] = {s: 0 for s in universe}
    for t in real_report.trades:
        k_by_symbol[t.symbol] = k_by_symbol.get(t.symbol, 0) + 1

    n_bars_by_symbol: dict[str, int] = {}
    for symbol in universe:
        df = bars_provider.get_daily_bars(symbol)
        n_bars_by_symbol[symbol] = len(df)

    rng = np.random.default_rng(seed)
    control_values: list[float] = []
    exceedances = 0
    for _ in range(n_iterations):
        entries = draw_random_entries(
            n_bars_by_symbol=n_bars_by_symbol, k_by_symbol=k_by_symbol, warmup_bars=config.warmup_bars, rng=rng
        )
        control_source = RandomTimingSignalSource(entries_by_symbol=entries)
        control_report = BT.run_backtest(
            universe=universe, bars_provider=bars_provider, signal_source=control_source, config=config
        )
        control_value = _metric_from_trades(control_report.trades, control_report.initial_equity, metric_name)
        if control_value is None:
            # A control draw that produced zero trades (e.g. every drawn
            # bar got starved by Pass 1's own forward-skip) can never beat
            # a real signal that DID produce trades -- treated as a clean
            # non-exceedance, not an error.
            control_value = float("-inf")
        control_values.append(control_value)
        if control_value >= real_value:
            exceedances += 1

    p_value = STATS.empirical_p(exceedances, n_iterations)

    return PermutationTestResult(
        metric_name=metric_name,
        real_value=real_value,
        control_values=tuple(control_values),
        n_iterations=n_iterations,
        exceedances=exceedances,
        p_value=p_value,
        real_report=real_report,
        k_by_symbol=dict(k_by_symbol),
        notes=(
            "1-D permutation test: real entry timing vs. random entry timing, same LONG-only "
            "decision rule and exit/sizing/risk pipeline -- no direction axis, since "
            "aura_v054_backtest.py's run_backtest is hardcoded LONG-only (see module docstring).",
            "Metric computed over ALL trades (RESEARCH + HOLDOUT combined), not split -- this "
            "tests whether entry TIMING carries information, a different question from "
            "out-of-sample generalization (already run_backtest's own holdout split's job).",
            f"Control entry counts matched to the real run's per-symbol REALIZED trade count "
            f"(post-portfolio-risk-gating), not raw Pass-1 signal count -- "
            f"run_backtest exposes no per-symbol raw signal count without modification, and this "
            f"module deliberately makes no changes to run_backtest.",
        ),
    )
