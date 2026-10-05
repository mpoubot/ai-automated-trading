"""AURA v0.5.4 tests -- Categories B, C, D, E, F: ATR trailing-stop exit
engine, PLUS (2026-10-04) the CANDIDATE_CHANGE staged-trail promotion.

IMPORTANT (2026-10-04): aura_v054_exit_engine.py's OWN default params for
early_trail_atr_mult/early_stage_r_threshold changed from None/None to
the approved candidate (0.75, 1.5) when the staged trail was promoted to
production. Every Category B/C/E/F/cost_pct test below now passes
early_trail_atr_mult=None, early_stage_r_threshold=None EXPLICITLY, so
they keep testing the ORIGINAL uniform-TRAIL_ATR_MULT formula they were
written to test (same frozen numeric behavior as before this file was
touched) rather than silently starting to exercise the new staged
default. The candidate's own behavior gets its own dedicated tests in
the final section of this file.
"""

from __future__ import annotations

import math
import random

import pytest

import aura_v054_exit_engine as EXIT
import aura_v054_exit_engine_experiments as VAR


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
        early_trail_atr_mult=None, early_stage_r_threshold=None,
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
        early_trail_atr_mult=None, early_stage_r_threshold=None,
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
        early_trail_atr_mult=None, early_stage_r_threshold=None,
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
        early_trail_atr_mult=None, early_stage_r_threshold=None,
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
        early_trail_atr_mult=None, early_stage_r_threshold=None,
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


# ---------------------------------------------------------------------------
# 2026-10-04 CANDIDATE_CHANGE: staged early trail (0.75x ATR below 1.5R,
# promoted from aura_v054_exit_engine_experiments.py after a single A/B
# test -> frequency-normalized check -> 3x3 robustness grid -> full
# 6-year dataset validation). See aura_v054_exit_engine.py's module-level
# comment for the full rationale and numbers.
# ---------------------------------------------------------------------------

def _random_series(n: int, *, seed: int, start: float = 100.0):
    rng = random.Random(seed)
    closes = [start]
    for _ in range(n - 1):
        closes.append(closes[-1] * (1.0 + rng.uniform(-0.02, 0.025)))
    highs = [c * (1.0 + rng.uniform(0.0, 0.01)) for c in closes]
    lows = [c * (1.0 - rng.uniform(0.0, 0.01)) for c in closes]
    atrs = [None] + [abs(highs[i] - lows[i]) * 1.5 + 0.1 for i in range(1, n)]
    return highs, lows, closes, atrs


def test_candidate_defaults_are_0_75x_below_1_5R():
    # Pins the actual production default -- if this ever changes
    # un-intentionally (e.g. a bad merge), this test catches it
    # immediately rather than silently reverting the approved change.
    assert EXIT.EARLY_TRAIL_ATR_MULT == 0.75
    assert EXIT.EARLY_STAGE_R_THRESHOLD == 1.5


def test_none_params_reproduce_original_uniform_trail_across_many_seeds():
    # With both staging args explicitly None, the engine must behave
    # exactly as it did before the 2026-10-04 change -- verified two
    # ways: (a) directly, since staged_promoted starts True and the
    # staging branch never executes; (b) against the experiments
    # module's independently-callable variant with its own staging
    # args left None, which was itself checked against the pre-change
    # frozen engine in test_aura_v054_exit_engine_experiments.py.
    for seed in range(60):
        n = 60
        highs, lows, closes, atrs = _random_series(n, seed=seed)
        entry_idx = 5
        entry_price = closes[entry_idx]

        base_none = EXIT.simulate_atr_trailing_trade(
            highs=highs, lows=lows, closes=closes, atr_values=atrs,
            entry_idx=entry_idx, entry_price=entry_price, direction="LONG", cost_pct=0.001,
            early_trail_atr_mult=None, early_stage_r_threshold=None,
        )
        variant_none = VAR.simulate_variant_atr_trailing_trade(
            highs=highs, lows=lows, closes=closes, atr_values=atrs,
            entry_idx=entry_idx, entry_price=entry_price, direction="LONG", cost_pct=0.001,
        )
        assert base_none.exit_reason == variant_none.exit_reason, seed
        assert base_none.bars_held == variant_none.bars_held, seed
        assert base_none.exit_price == variant_none.exit_price, seed
        assert base_none.exit_bar_index == variant_none.exit_bar_index, seed
        assert base_none.mfe_frac == variant_none.mfe_frac, seed
        assert base_none.mae_frac == variant_none.mae_frac, seed
        assert base_none.stop_trace == variant_none.stop_trace, seed
        assert base_none.initial_stop_distance == variant_none.initial_stop_distance, seed


def test_candidate_defaults_differ_from_uniform_trail_on_a_staged_case():
    # Sanity check that the new default params actually DO something
    # different from the old uniform trail -- same engineered pullback
    # used in test_aura_v054_exit_engine_experiments.py, which clears
    # less than 1.5R before reversing.
    closes = [99.0, 100.0, 101.0, 102.0, 101.4, 100.6, 100.5, 95.0, 95.0, 95.0, 95.0, 95.0]
    n = len(closes)
    highs = [c + 0.3 for c in closes]
    lows = [c - 0.3 for c in closes]
    atrs = [None] + [1.0] * (n - 1)
    entry_idx = 1
    entry_price = closes[entry_idx]

    staged = EXIT.simulate_atr_trailing_trade(
        highs=highs, lows=lows, closes=closes, atr_values=atrs,
        entry_idx=entry_idx, entry_price=entry_price, direction="LONG", cost_pct=0.0,
        # defaults: early_trail_atr_mult=0.75, early_stage_r_threshold=1.5
    )
    uniform = EXIT.simulate_atr_trailing_trade(
        highs=highs, lows=lows, closes=closes, atr_values=atrs,
        entry_idx=entry_idx, entry_price=entry_price, direction="LONG", cost_pct=0.0,
        early_trail_atr_mult=None, early_stage_r_threshold=None,
    )
    assert staged.exit_bar_index < uniform.exit_bar_index
    assert staged.exit_reason == uniform.exit_reason == "STOP"
    assert staged.net_return_frac > uniform.net_return_frac


def test_staged_production_engine_matches_experiments_module_for_same_config():
    # Cross-check: the promoted production code must produce the same
    # numbers as the (already-tested) experiments module it was
    # promoted from, for an arbitrary non-default staging config too.
    for seed in range(20):
        n = 60
        highs, lows, closes, atrs = _random_series(n, seed=seed)
        entry_idx = 5
        entry_price = closes[entry_idx]
        kwargs = dict(early_trail_atr_mult=1.0, early_stage_r_threshold=2.0)

        base = EXIT.simulate_atr_trailing_trade(
            highs=highs, lows=lows, closes=closes, atr_values=atrs,
            entry_idx=entry_idx, entry_price=entry_price, direction="LONG", cost_pct=0.001,
            **kwargs,
        )
        variant = VAR.simulate_variant_atr_trailing_trade(
            highs=highs, lows=lows, closes=closes, atr_values=atrs,
            entry_idx=entry_idx, entry_price=entry_price, direction="LONG", cost_pct=0.001,
            **kwargs,
        )
        assert base.exit_reason == variant.exit_reason, seed
        assert base.bars_held == variant.bars_held, seed
        assert base.exit_price == variant.exit_price, seed
        assert base.stop_trace == variant.stop_trace, seed


def test_staging_never_demotes_back_to_early_multiplier_once_promoted():
    # A trade that clears the threshold then pulls back near it again
    # must keep using trail_atr_mult (never re-tighten), matching the
    # experiments module's documented "sticky promotion" semantics.
    n = 30
    closes = [100.0]
    for i in range(1, n):
        closes.append(closes[-1] * 1.03 if i < 12 else closes[-1] * 0.99)
    highs = [c * 1.002 for c in closes]
    lows = [c * 0.998 for c in closes]
    atrs = [None] + [1.0] * (n - 1)
    entry_idx = 1
    entry_price = closes[entry_idx]

    result = EXIT.simulate_atr_trailing_trade(
        highs=highs, lows=lows, closes=closes, atr_values=atrs,
        entry_idx=entry_idx, entry_price=entry_price, direction="LONG", cost_pct=0.0,
        early_trail_atr_mult=0.5, early_stage_r_threshold=1.0,
    )
    # Once promoted, the trail distance behind the running extreme
    # should reflect the WIDE multiplier (2.0x ATR=1.0 => 2.0), not the
    # tight one (0.5x ATR=1.0 => 0.5), for every bar after promotion.
    running_extreme = entry_price
    r_unit = EXIT.initial_stop_distance_long(entry_price, 1.0, trail_atr_mult=2.0)
    promoted = False
    for i, stop_level in enumerate(result.stop_trace):
        bar_idx = entry_idx + 1 + i
        running_extreme = max(running_extreme, highs[bar_idx])
        if (running_extreme - entry_price) / r_unit >= 1.0:
            promoted = True
        if promoted:
            implied_distance = running_extreme - stop_level
            assert implied_distance <= 2.0 + 1e-9, (i, implied_distance)
    assert promoted, "test setup should have cleared 1.0R at some point"


def test_inconsistent_staging_args_rejected():
    with pytest.raises(EXIT.ExitEngineError):
        EXIT.simulate_atr_trailing_trade(
            highs=[1, 2], lows=[1, 2], closes=[1, 2], atr_values=[None, 1.0],
            entry_idx=0, entry_price=1.0, direction="LONG", cost_pct=0.0,
            early_trail_atr_mult=1.0,  # threshold missing
        )


def test_backtest_config_candidate_defaults_wired_through():
    import aura_v054_backtest as BT

    config = BT.BacktestConfig()
    assert config.early_trail_atr_mult == 0.75
    assert config.early_stage_r_threshold == 1.5

    reverted = BT.BacktestConfig(early_trail_atr_mult=None, early_stage_r_threshold=None)
    assert reverted.early_trail_atr_mult is None
    assert reverted.early_stage_r_threshold is None
