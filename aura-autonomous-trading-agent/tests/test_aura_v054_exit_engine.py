"""AURA v0.5.4 tests -- Categories B, C, D, E, F: ATR trailing-stop exit engine."""

from __future__ import annotations

import math

import pytest

import aura_v054_exit_engine as EXIT


def _flat_series(n, value):
    return [value] * n


def test_initial_stop_distance_long_is_atr_times_mult():
    # Category D: ATR x 2.0 multiplier (the frozen TRAIL_ATR_MULT).
    distance = EXIT.initial_stop_distance_long(100.0, 2.5, trail_atr_mult=2.0)
    assert distance == pytest.approx(5.0)
    assert EXIT.TRAIL_ATR_MULT == 2.0


def test_initial_stop_distance_long_fails_safe_on_invalid_atr():
    with pytest.raises(EXIT.ExitEngineError):
        EXIT.initial_stop_distance_long(100.0, 0.0)
    with pytest.raises(EXIT.ExitEngineError):
        EXIT.initial_stop_distance_long(100.0, -1.0)
    with pytest.raises(EXIT.ExitEngineError):
        EXIT.initial_stop_distance_long(100.0, float("nan"))
    with pytest.raises(EXIT.ExitEngineError):
        EXIT.initial_stop_distance_long(100.0, None)
    with pytest.raises(EXIT.ExitEngineError):
        # distance (atr*mult) >= entry_price -> degenerate, must raise
        EXIT.initial_stop_distance_long(10.0, 6.0, trail_atr_mult=2.0)


def test_category_b_atr_trailing_stop_exits_on_low_breach():
    # Price drifts up gently then drops hard enough to breach the trail.
    n = 10
    highs = [102, 103, 104, 105, 106, 107, 90, 90, 90, 90]
    lows = [98, 99, 100, 101, 102, 103, 80, 80, 80, 80]
    closes = [100, 101, 102, 103, 104, 105, 85, 85, 85, 85]
    atr = _flat_series(n, 2.0)  # constant ATR=2.0 for a predictable trail

    result = EXIT.simulate_atr_trailing_trade(
        highs=highs, lows=lows, closes=closes, atr_values=atr,
        entry_idx=0, entry_price=100.0, direction="LONG",
        trail_atr_mult=2.0, take_profit_pct=None, max_hold_bars=10, cost_pct=0.0,
    )
    assert result.exit_reason == "STOP"
    # The crash bar (index 6, low=80) must have breached the trail computed from bar 5's extreme.
    assert result.exit_bar_index == 6


def test_category_c_trail_only_moves_favorably_never_decreases():
    # Even if ATR spikes wildly on a later bar (which would imply a much
    # WIDER/lower stop if recomputed from scratch), the realized stop
    # trace must never decrease bar over bar.
    n = 8
    highs = [101, 103, 105, 107, 109, 111, 113, 115]
    lows = [99, 101, 103, 105, 107, 109, 111, 113]
    closes = [100, 102, 104, 106, 108, 110, 112, 114]
    atr = [1.0, 1.0, 1.0, 50.0, 1.0, 1.0, 1.0, 1.0]  # bar 3 ATR spike

    result = EXIT.simulate_atr_trailing_trade(
        highs=highs, lows=lows, closes=closes, atr_values=atr,
        entry_idx=0, entry_price=100.0, direction="LONG",
        trail_atr_mult=2.0, take_profit_pct=None, max_hold_bars=8, cost_pct=0.0,
    )
    trace = list(result.stop_trace)
    assert trace == sorted(trace) or all(trace[i] <= trace[i + 1] for i in range(len(trace) - 1)), trace
    for i in range(len(trace) - 1):
        assert trace[i + 1] >= trace[i], f"stop decreased at step {i}: {trace}"


def test_category_e_no_fixed_take_profit_never_exits_on_target():
    # A huge favorable surge must never trigger a TARGET exit when
    # take_profit_pct is None (the frozen baseline setting).
    n = 5
    highs = [200, 300, 400, 500, 600]
    lows = [99, 250, 350, 450, 550]
    closes = [150, 280, 380, 480, 580]
    atr = _flat_series(n, 1.0)

    result = EXIT.simulate_atr_trailing_trade(
        highs=highs, lows=lows, closes=closes, atr_values=atr,
        entry_idx=0, entry_price=100.0, direction="LONG",
        trail_atr_mult=2.0, take_profit_pct=None, max_hold_bars=5, cost_pct=0.0,
    )
    assert result.exit_reason != "TARGET"
    assert EXIT.TAKE_PROFIT_PCT is None


def test_category_f_max_hold_bars_timeout():
    n = 25
    highs = [1100 + i * 0.01 for i in range(n)]
    lows = [1099 + i * 0.01 for i in range(n)]
    closes = [1099.5 + i * 0.01 for i in range(n)]
    atr = _flat_series(n, 50.0)  # wide ATR relative to the small drift so the trail never gets hit

    result = EXIT.simulate_atr_trailing_trade(
        highs=highs, lows=lows, closes=closes, atr_values=atr,
        entry_idx=0, entry_price=1099.5, direction="LONG",
        trail_atr_mult=2.0, take_profit_pct=None, max_hold_bars=20, cost_pct=0.0,
    )
    assert result.exit_reason == "TIMEOUT"
    assert result.bars_held == 20
    assert EXIT.MAX_HOLD_BARS == 20


def test_cost_pct_is_a_fraction_subtracted_once():
    n = 5
    highs = [110, 110, 110, 110, 110]
    lows = [100, 100, 100, 100, 100]
    closes = [105, 105, 105, 105, 105]
    atr = _flat_series(n, 20.0)  # wide ATR -> no stop hit

    result = EXIT.simulate_atr_trailing_trade(
        highs=highs, lows=lows, closes=closes, atr_values=atr,
        entry_idx=0, entry_price=100.0, direction="LONG",
        trail_atr_mult=2.0, take_profit_pct=None, max_hold_bars=4, cost_pct=0.001,
    )
    assert result.net_return_frac == pytest.approx(result.gross_return_frac - 0.001)


def test_short_direction_is_documented_not_implemented():
    with pytest.raises(EXIT.ExitEngineError):
        EXIT.simulate_atr_trailing_trade(
            highs=[1, 2], lows=[1, 2], closes=[1, 2], atr_values=[1, 1],
            entry_idx=0, entry_price=1.0, direction="SHORT",
            cost_pct=0.0,
        )


def test_no_data_after_entry_handled_cleanly():
    result = EXIT.simulate_atr_trailing_trade(
        highs=[100.0], lows=[99.0], closes=[99.5], atr_values=[1.0],
        entry_idx=0, entry_price=99.5, direction="LONG",
        cost_pct=0.0,
    )
    assert result.exit_reason == "NO_DATA_AFTER_ENTRY"
    assert result.exit_bar_index is None
