"""Tests for aura_v054_exit_engine_experiments.py -- the EXPERIMENTAL
exit-rule variants used only for the giveback A/B study. Not production
code; these tests exist so the variants can be trusted before their
numbers are reported to Martin."""

from __future__ import annotations

import random

import aura_v054_exit_engine as BASE
import aura_v054_exit_engine_experiments as VAR


def _random_series(n: int, *, seed: int, start: float = 100.0):
    rng = random.Random(seed)
    closes = [start]
    for _ in range(n - 1):
        closes.append(closes[-1] * (1.0 + rng.uniform(-0.02, 0.025)))
    highs = [c * (1.0 + rng.uniform(0.0, 0.01)) for c in closes]
    lows = [c * (1.0 - rng.uniform(0.0, 0.01)) for c in closes]
    atrs = [None] + [abs(highs[i] - lows[i]) * 1.5 + 0.1 for i in range(1, n)]
    return highs, lows, closes, atrs


def test_default_params_reproduce_frozen_engine_exactly_across_many_seeds():
    # NOTE (2026-10-04): aura_v054_exit_engine.py's OWN defaults for
    # early_trail_atr_mult/early_stage_r_threshold changed from None/None
    # to the approved candidate (0.75, 1.5) when the staged trail was
    # promoted to production -- see test_aura_v054_exit_engine.py for
    # that change's own regression tests. This test's job hasn't
    # changed (confirm this experiments module's own no-staging-args
    # call stays byte-identical to the ORIGINAL uniform-trail formula),
    # so it now passes early_trail_atr_mult=None/early_stage_r_threshold
    # =None to BASE explicitly rather than relying on BASE's defaults.
    for seed in range(60):
        n = 60
        highs, lows, closes, atrs = _random_series(n, seed=seed)
        entry_idx = 5
        entry_price = closes[entry_idx]

        base = BASE.simulate_atr_trailing_trade(
            highs=highs, lows=lows, closes=closes, atr_values=atrs,
            entry_idx=entry_idx, entry_price=entry_price, direction="LONG", cost_pct=0.001,
            early_trail_atr_mult=None, early_stage_r_threshold=None,
        )
        variant = VAR.simulate_variant_atr_trailing_trade(
            highs=highs, lows=lows, closes=closes, atr_values=atrs,
            entry_idx=entry_idx, entry_price=entry_price, direction="LONG", cost_pct=0.001,
        )
        assert variant.exit_reason == base.exit_reason, seed
        assert variant.bars_held == base.bars_held, seed
        assert variant.exit_price == base.exit_price, seed
        assert variant.exit_bar_index == base.exit_bar_index, seed
        assert variant.mfe_frac == base.mfe_frac, seed
        assert variant.mae_frac == base.mae_frac, seed
        assert variant.stop_trace == base.stop_trace, seed
        assert variant.initial_stop_distance == base.initial_stop_distance, seed


def test_breakeven_variant_never_lets_stop_sit_below_entry_once_threshold_cleared():
    # A trade that runs straight up for a while (so mfe_r comfortably
    # clears 1.0) and then chops -- the stop must never be found below
    # entry_price from the bar it first crosses the threshold onward.
    n = 30
    closes = [100.0]
    for i in range(1, n):
        closes.append(closes[-1] * 1.03 if i < 15 else closes[-1] * 0.995)
    highs = [c * 1.002 for c in closes]
    lows = [c * 0.998 for c in closes]
    atrs = [None] + [1.0] * (n - 1)  # ATR=1.0 => trail_atr_mult=2.0 => 1R = 2.0 price units
    entry_idx = 1
    entry_price = closes[entry_idx]

    result = VAR.simulate_variant_atr_trailing_trade(
        highs=highs, lows=lows, closes=closes, atr_values=atrs,
        entry_idx=entry_idx, entry_price=entry_price, direction="LONG", cost_pct=0.0,
        breakeven_r_threshold=1.0,
    )
    # Once mfe has reached 1R, every subsequent recorded stop level must be >= entry_price.
    r_unit = BASE.initial_stop_distance_long(entry_price, 1.0, trail_atr_mult=2.0)
    running_extreme = entry_price
    crossed = False
    for i, stop_level in enumerate(result.stop_trace):
        bar_idx = entry_idx + 1 + i
        running_extreme = max(running_extreme, highs[bar_idx])
        if (running_extreme - entry_price) / r_unit >= 1.0:
            crossed = True
        if crossed:
            assert stop_level >= entry_price - 1e-9, (i, stop_level, entry_price)
    assert crossed, "test setup should have cleared 1R at some point"


def test_staged_variant_uses_tight_multiplier_before_threshold_and_wide_after():
    # Construct a trade that advances to just past the staging threshold
    # then reverses hard. The EARLY tight trail (1.0x ATR) should still
    # be in effect for the bars before the threshold is cleared, and the
    # WIDE trail (2.0x ATR, same as the frozen engine) after.
    closes = [99.0, 100.0, 101.0, 102.0, 101.4, 100.6, 100.5, 95.0, 95.0, 95.0, 95.0, 95.0]
    n = len(closes)
    highs = [c + 0.3 for c in closes]
    lows = [c - 0.3 for c in closes]
    atrs = [None] + [1.0] * (n - 1)
    entry_idx = 1
    entry_price = closes[entry_idx]

    staged = VAR.simulate_variant_atr_trailing_trade(
        highs=highs, lows=lows, closes=closes, atr_values=atrs,
        entry_idx=entry_idx, entry_price=entry_price, direction="LONG", cost_pct=0.0,
        early_trail_atr_mult=1.0, early_stage_r_threshold=1.5,
    )
    wide = VAR.simulate_variant_atr_trailing_trade(
        highs=highs, lows=lows, closes=closes, atr_values=atrs,
        entry_idx=entry_idx, entry_price=entry_price, direction="LONG", cost_pct=0.0,
    )
    # The staged variant's tighter early trail must catch this pullback
    # sooner than the always-wide trail (reversal happens before 1.5R is
    # reached, so the tight multiplier is still in effect) -- and lock
    # in MORE of the run-up, not less, which is the whole point of
    # staging the trail rather than just tightening it everywhere.
    assert staged.exit_bar_index < wide.exit_bar_index
    assert staged.exit_reason == wide.exit_reason == "STOP"
    assert staged.net_return_frac > wide.net_return_frac


def test_direction_guard_matches_frozen_engine():
    import pytest
    with pytest.raises(VAR.ExitEngineExperimentError):
        VAR.simulate_variant_atr_trailing_trade(
            highs=[1, 2], lows=[1, 2], closes=[1, 2], atr_values=[None, 1.0],
            entry_idx=0, entry_price=1.0, direction="SHORT", cost_pct=0.0,
        )


def test_inconsistent_staging_args_rejected():
    import pytest
    with pytest.raises(VAR.ExitEngineExperimentError):
        VAR.simulate_variant_atr_trailing_trade(
            highs=[1, 2], lows=[1, 2], closes=[1, 2], atr_values=[None, 1.0],
            entry_idx=0, entry_price=1.0, direction="LONG", cost_pct=0.0,
            early_trail_atr_mult=1.0,  # threshold missing
        )
