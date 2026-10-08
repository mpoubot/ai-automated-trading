"""
Tests for aura_v054_permutation_test.py, run against the REAL, unmodified
aura_v054_backtest.py / aura_v054_data_interface.py / aura_v054_exit_engine
(base logic) / aura_v054_position_sizing.py / aura_v054_portfolio_risk.py
(verbatim copies in tests/../_test_backtest_env/, see its README.md for
exactly what is real vs. a test stand-in -- only aura_v054_atr.py,
aura_v054_cost_model.py, and the staged-trail extension of
aura_v054_exit_engine.py are stand-ins; everything else these tests
exercise is Martin's real, current source as read 2026-10-08).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd
import pytest

import aura_v054_backtest as BT
import aura_v054_data_interface as DATA
from aura_v054_permutation_test import (
    PermutationTestError,
    RandomTimingSignalSource,
    _metric_from_trades,
    draw_random_entries,
    run_permutation_test,
)


# ---------------------------------------------------------------------------
# Unit tests: RandomTimingSignalSource / draw_random_entries
# ---------------------------------------------------------------------------

def test_random_timing_signal_source_recovers_bar_index():
    df = pd.DataFrame({"close": [1.0, 2.0, 3.0, 4.0, 5.0]})
    source = RandomTimingSignalSource(entries_by_symbol={"TEST": frozenset({2, 4})})

    # bars_up_to_now = df.iloc[:i+1] -- length i+1 -- mirrors run_backtest's own slicing exactly.
    assert source.decide_for_symbol("TEST", df.iloc[:3], now=datetime(2024, 1, 1))["outcome"] == "DECIDE_LONG"
    assert source.decide_for_symbol("TEST", df.iloc[:2], now=datetime(2024, 1, 1))["outcome"] == "ABSTAIN"
    assert source.decide_for_symbol("TEST", df.iloc[:5], now=datetime(2024, 1, 1))["outcome"] == "DECIDE_LONG"
    # A symbol with no entry
    assert source.decide_for_symbol("OTHER", df.iloc[:3], now=datetime(2024, 1, 1))["outcome"] == "ABSTAIN"


def test_draw_random_entries_respects_warmup_and_count():
    rng = np.random.default_rng(1)
    entries = draw_random_entries(
        n_bars_by_symbol={"A": 100}, k_by_symbol={"A": 10}, warmup_bars=60, rng=rng
    )
    assert len(entries["A"]) == 10
    assert all(60 <= i < 100 for i in entries["A"])


def test_draw_random_entries_caps_k_when_eligible_range_too_small():
    rng = np.random.default_rng(1)
    # Only 5 eligible bars (60..64 would be needed, but n_bars=63 -> eligible = {60,61,62} = 3 bars)
    entries = draw_random_entries(
        n_bars_by_symbol={"A": 63}, k_by_symbol={"A": 10}, warmup_bars=60, rng=rng
    )
    assert len(entries["A"]) == 3  # capped, never raises, never exceeds what's physically possible


def test_draw_random_entries_zero_k_gives_empty_set():
    rng = np.random.default_rng(1)
    entries = draw_random_entries(n_bars_by_symbol={"A": 100}, k_by_symbol={"A": 0}, warmup_bars=60, rng=rng)
    assert entries["A"] == frozenset()


# ---------------------------------------------------------------------------
# Cross-check: _metric_from_trades's total_return_frac vs. the one public
# invariant run_backtest guarantees regardless of its internals.
# ---------------------------------------------------------------------------

@dataclass
class _MomentumFixtureSignalSource:
    """Replicates aura_v054_signal_source.py's REAL
    SyntheticTestFixtureSignalSource exactly (20-day-momentum rule) --
    copied here rather than imported, since the real module can't be
    imported standalone (it unconditionally imports aura_v05349/50/51/52,
    which weren't shared with me -- see _test_backtest_env/README.md).
    NOT A STRATEGY, matches the real fixture's own disclosure."""

    SIGNAL_SOURCE_LABEL: str = "SYNTHETIC_TEST_FIXTURE_RULE"
    lookback_bars: int = 20

    def decide_for_symbol(self, symbol: str, bars_up_to_now: pd.DataFrame, *, now: datetime) -> dict[str, Any]:
        if len(bars_up_to_now) <= self.lookback_bars:
            return {"outcome": "ABSTAIN", "direction": "NO_DIRECTIONAL_EVIDENCE"}
        closes = bars_up_to_now["close"].to_numpy()
        if closes[-1] > closes[-1 - self.lookback_bars]:
            return {"outcome": "DECIDE_LONG", "direction": "LONG_LEANING"}
        return {"outcome": "ABSTAIN", "direction": "NO_DIRECTIONAL_EVIDENCE"}


def test_total_return_frac_matches_final_equity():
    universe = ("AAA", "BBB")
    bars_provider = DATA.SyntheticBarsProvider(n_days=300)
    report = BT.run_backtest(
        universe=universe,
        bars_provider=bars_provider,
        signal_source=_MomentumFixtureSignalSource(),
        config=BT.BacktestConfig(),
    )
    assert report.final_equity is not None
    expected = (report.final_equity - report.initial_equity) / report.initial_equity
    actual = _metric_from_trades(report.trades, report.initial_equity, "total_return_frac")
    assert actual == pytest.approx(expected, abs=1e-9)


def test_metric_from_trades_unsupported_metric_raises():
    # Needs at least one CLOSED trade to reach the metric-name dispatch --
    # an empty/all-open trade list returns None before that check (an
    # intentional short-circuit: "no result" and "bad metric name" are
    # different failure modes and the zero-trades one is checked first).
    trade = BT.TradeRecord(
        symbol="X", entry_bar_index=0, entry_timestamp=pd.Timestamp("2024-01-01", tz="UTC"),
        entry_price=100.0, quantity=1, exit_bar_index=1, exit_timestamp=pd.Timestamp("2024-01-02", tz="UTC"),
        exit_reason="TIMEOUT", gross_return_frac=0.01, net_return_frac=0.009,
        realized_pnl_dollars=0.9, planned_risk_dollars=1.0, market_value=100.0, period="RESEARCH",
    )
    with pytest.raises(PermutationTestError):
        _metric_from_trades([trade], 100_000.0, "not_a_real_metric")


# ---------------------------------------------------------------------------
# Planted-edge integration test: a signal source that KNOWS about an
# engineered jump beats random timing decisively.
# ---------------------------------------------------------------------------

@dataclass
class JumpBarsProvider:
    """TEST FIXTURE ONLY -- not a Track B market-data source, not even in
    the SyntheticBarsProvider "pipeline validation" sense. Deterministic,
    near-flat daily bars with a few sharp, SUSTAINED upward jumps at known
    indices (price steps up and stays there -- not a one-bar spike that
    reverts), used only to plant a known, exploitable timing edge so this
    test file can prove run_permutation_test distinguishes real edge from
    random timing. No RNG -- exactly reproducible."""

    n_days: int = 300
    base_price: float = 100.0
    jump_indices: tuple[int, ...] = (100, 150, 200)
    jump_pct: float = 0.15
    DATA_SOURCE_LABEL: str = "PERMUTATION_TEST_JUMP_FIXTURE"
    IS_REAL_MARKET_DATA: bool = False

    def get_daily_bars(self, symbol: str) -> pd.DataFrame:
        dates = pd.bdate_range(start="2024-01-02", periods=self.n_days, tz="UTC")
        rows = []
        level = self.base_price
        for i, ts in enumerate(dates):
            if i in self.jump_indices:
                level = level * (1.0 + self.jump_pct)
            osc = 1.0 + 0.003 * math.sin(i / 3.0)  # deterministic wiggle, keeps ATR non-degenerate
            close_px = level * osc
            open_px = close_px * (1.0 + 0.001 * math.cos(i))
            # Daily range ~1.2% of price -- large enough that ATR-risk sizing
            # (0.5% equity / (2*ATR)) doesn't blow through the 100%-gross-
            # exposure portfolio limit, which a too-tight range does (confirmed
            # by direct debugging: a ~0.12%-range version sized positions at
            # ~2x equity and every candidate was rejected, zero trades).
            high_px = max(open_px, close_px) * 1.006
            low_px = min(open_px, close_px) * 0.994
            rows.append(
                {
                    "timestamp": ts,
                    "open": round(open_px, 6),
                    "high": round(high_px, 6),
                    "low": round(low_px, 6),
                    "close": round(close_px, 6),
                    "volume": 1_000_000,
                }
            )
        df = pd.DataFrame(rows, columns=list(DATA.BARS_SCHEMA))
        DATA.validate_bars_frame(df, symbol=symbol)
        return df


@dataclass
class CheatingSignalSource:
    """TEST FIXTURE ONLY -- enters LONG exactly one bar before each of
    JumpBarsProvider's engineered jumps, i.e. has foreknowledge no real
    (or random) signal source has. This is the "known timing edge" this
    test suite uses to prove the permutation engine actually detects
    real timing information rather than always reporting significance."""

    jump_indices: tuple[int, ...]
    SIGNAL_SOURCE_LABEL: str = "TEST_CHEATING_FIXTURE"

    def decide_for_symbol(self, symbol: str, bars_up_to_now: pd.DataFrame, *, now: datetime) -> dict[str, Any]:
        i = len(bars_up_to_now) - 1
        if (i + 1) in self.jump_indices:
            return {"outcome": "DECIDE_LONG", "direction": "LONG_LEANING"}
        return {"outcome": "ABSTAIN", "direction": "NO_DIRECTIONAL_EVIDENCE"}


JUMP_UNIVERSE = ("JJA", "JJB")
JUMP_CONFIG = BT.BacktestConfig()


def _jump_bars_provider() -> JumpBarsProvider:
    return JumpBarsProvider()


def test_permutation_test_flags_planted_timing_edge():
    result = run_permutation_test(
        universe=JUMP_UNIVERSE,
        bars_provider=_jump_bars_provider(),
        real_signal_source=CheatingSignalSource(jump_indices=JumpBarsProvider().jump_indices),
        config=JUMP_CONFIG,
        metric_name="total_return_frac",
        n_iterations=300,
        seed=1,
    )
    assert result.real_value > 0.05  # clearly, substantially profitable (jumps are 15% each)
    assert result.p_value < 0.02  # almost no random-timing draw should beat deliberately-placed entries
    assert result.exceedances <= 5  # out of 300 iterations
    assert all(k > 0 for k in result.k_by_symbol.values())


def test_permutation_test_random_vs_random_gives_large_pvalue():
    # A "real" signal source that is ITSELF just random timing (different
    # seed from the controls) should NOT be flagged significant -- this is
    # the false-positive-rate sanity check.
    bars_provider = DATA.SyntheticBarsProvider(n_days=400)
    universe = ("CCC", "DDD")
    rng = np.random.default_rng(999)
    n_bars_by_symbol = {s: len(bars_provider.get_daily_bars(s)) for s in universe}
    real_entries = draw_random_entries(
        n_bars_by_symbol=n_bars_by_symbol, k_by_symbol={"CCC": 8, "DDD": 8}, warmup_bars=60, rng=rng
    )
    real_source = RandomTimingSignalSource(entries_by_symbol=real_entries, SIGNAL_SOURCE_LABEL="TEST_RANDOM_REAL")

    result = run_permutation_test(
        universe=universe,
        bars_provider=bars_provider,
        real_signal_source=real_source,
        config=BT.BacktestConfig(),
        metric_name="total_return_frac",
        n_iterations=300,
        seed=2,
    )
    assert result.p_value > 0.05


def test_run_permutation_test_raises_on_zero_real_trades():
    @dataclass
    class AlwaysAbstainSignalSource:
        SIGNAL_SOURCE_LABEL: str = "TEST_ALWAYS_ABSTAIN"

        def decide_for_symbol(self, symbol, bars_up_to_now, *, now):
            return {"outcome": "ABSTAIN", "direction": "NO_DIRECTIONAL_EVIDENCE"}

    with pytest.raises(PermutationTestError):
        run_permutation_test(
            universe=("ZZZ",),
            bars_provider=DATA.SyntheticBarsProvider(n_days=200),
            real_signal_source=AlwaysAbstainSignalSource(),
            config=BT.BacktestConfig(),
        )


def test_run_permutation_test_unsupported_metric_raises_before_running():
    with pytest.raises(PermutationTestError):
        run_permutation_test(
            universe=("ZZZ",),
            bars_provider=DATA.SyntheticBarsProvider(n_days=200),
            real_signal_source=_MomentumFixtureSignalSource(),
            metric_name="not_a_real_metric",
        )


def test_run_permutation_test_is_deterministic_given_same_seed():
    kwargs = dict(
        universe=("EEE", "FFF"),
        bars_provider=DATA.SyntheticBarsProvider(n_days=300),
        real_signal_source=_MomentumFixtureSignalSource(),
        config=BT.BacktestConfig(),
        metric_name="total_return_frac",
        n_iterations=50,
        seed=42,
    )
    r1 = run_permutation_test(**kwargs)
    r2 = run_permutation_test(**kwargs)
    assert r1.p_value == r2.p_value
    assert r1.control_values == r2.control_values
    assert r1.real_value == r2.real_value


def test_permutation_test_against_real_shaped_synthetic_fixture_smoke():
    # Integration smoke test: the real .054 SyntheticBarsProvider formula
    # (copied verbatim into _test_backtest_env/aura_v054_data_interface.py)
    # paired with a signal source shaped exactly like the real .054
    # SyntheticTestFixtureSignalSource -- proves the whole pipeline wires
    # together against something resembling the actual deployment's own
    # validation fixture, not just this file's engineered jump fixture.
    result = run_permutation_test(
        universe=("AAPL", "MSFT", "GOOGL"),
        bars_provider=DATA.SyntheticBarsProvider(n_days=500),
        real_signal_source=_MomentumFixtureSignalSource(),
        config=BT.BacktestConfig(),
        metric_name="profit_factor",
        n_iterations=100,
        seed=7,
    )
    assert 0.0 <= result.p_value <= 1.0
    assert len(result.control_values) == 100
    assert result.real_report.signal_source_label == "SYNTHETIC_TEST_FIXTURE_RULE"
