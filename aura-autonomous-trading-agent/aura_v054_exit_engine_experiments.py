#!/usr/bin/env python3
"""
AURA v0.5.4 -- EXPERIMENTAL exit-rule variants (NOT wired into production)

Why this file exists
---------------------
`aura_v054_exit_engine.py::simulate_atr_trailing_trade` is the frozen,
Martin-approved .54 exit rule (TRAIL_ATR_MULT=2.0 applied uniformly for
both the initial stop distance and the ongoing per-bar trail, no take
profit, MAX_HOLD_BARS=20). MAE/MFE diagnostics run against real
production-equivalent data (2026-10-04) found that this constant trail
gives back ~1R of peak unrealized profit on essentially every STOP-exited
trade, regardless of how far the trade had run up -- and that 147 of 183
trades in that run netted -45.2R in aggregate, while just 36 trades (the
ones that got past ~2R, plus every TIMEOUT trade) contributed +64.2R. Any
exit-rule change that tightens up broadly risks clipping those 36 trades,
which fund the entire book.

This module is a sandbox for testing two specific, narrowly-targeted
variants against that same risk: nothing here is imported by
`aura_v054_backtest.py` or any other production module. It exists ONLY
to be monkeypatched into a throwaway copy of the backtest run for an A/B
comparison, per Martin's explicit go-ahead on 2026-10-04 to test this.
NOT a candidate for the frozen pipeline until its own holdout-validated
numbers are reviewed and separately approved.

Both variants below reuse `aura_v054_exit_engine.py`'s own documented
no-look-ahead discipline byte-for-byte (same bar-order, same
same-bar-stop-beats-target convention, same "trail only moves up") --
only the trail's SIZE changes, and only as a function of how much R the
trade has already banked. "R" here always means the trade's ORIGINAL
risk unit (`atr_at_entry * trail_atr_mult`), matching how position
sizing (and the rest of this codebase) defines R -- never recomputed
from the tightened multiplier, so a staged variant can't quietly change
how big a trade's assigned risk was.

Variant A -- `breakeven_r_threshold`:
    Once the running favorable extreme implies mfe_r >= threshold, the
    stop is clamped to be at least `entry_price` (never allowed to sit
    below breakeven again), on top of the normal 2.0x trail. Before that
    threshold, behavior is byte-identical to the frozen engine.

Variant B -- staged trail (`early_trail_atr_mult` / `early_stage_r_threshold`):
    Uses a TIGHTER multiplier (e.g. 1.0x ATR) for the trail while the
    trade is still below `early_stage_r_threshold` R of favorable
    excursion -- the zone where the diagnostics found the -45.2R of
    giveback actually lives -- then reverts to the frozen engine's own
    `trail_atr_mult` (2.0x) once the trade clears that threshold, so the
    wide trail that lets big trend trades run is never touched.

With every variant knob left at its default (no staging, no breakeven
threshold), `simulate_variant_atr_trailing_trade` must reproduce
`aura_v054_exit_engine.simulate_atr_trailing_trade` bar-for-bar --
verified in `test_aura_v054_exit_engine_experiments.py`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

import aura_v054_exit_engine as BASE

VERSION = "AURA v0.5.4 EXPERIMENTAL (not frozen, not production)"


class ExitEngineExperimentError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class VariantExitResult(BASE.ATRTrailingExitResult):
    # Same shape as the frozen engine's result (inherits every field) --
    # no new fields, so this is a drop-in replacement wherever
    # ATRTrailingExitResult is consumed (e.g. aura_v054_backtest.py's
    # `exit_result.initial_stop_distance`, `.exit_bar_index`, etc.)
    pass


def simulate_variant_atr_trailing_trade(
    *,
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    atr_values: Sequence[float],
    entry_idx: int,
    entry_price: float,
    direction: str,
    trail_atr_mult: float = BASE.TRAIL_ATR_MULT,
    take_profit_pct: float | None = BASE.TAKE_PROFIT_PCT,
    max_hold_bars: int = BASE.MAX_HOLD_BARS,
    cost_pct: float,
    early_trail_atr_mult: float | None = None,
    early_stage_r_threshold: float | None = None,
    breakeven_r_threshold: float | None = None,
) -> VariantExitResult:
    """Byte-identical to `aura_v054_exit_engine.simulate_atr_trailing_trade`
    when `early_trail_atr_mult`/`early_stage_r_threshold`/
    `breakeven_r_threshold` are all left at None. `direction` must be
    "LONG" (SHORT remains out of scope, same as the frozen engine).

    `early_trail_atr_mult` + `early_stage_r_threshold`: while the trade's
    running favorable excursion is below `early_stage_r_threshold` R
    (R defined by `trail_atr_mult`, NOT `early_trail_atr_mult` -- so
    position sizing and R-reporting stay anchored to the same risk unit
    as every other trade in this codebase), the trail uses
    `early_trail_atr_mult` instead of `trail_atr_mult`. Once the
    threshold is cleared, it reverts to `trail_atr_mult` for the rest of
    the trade's life -- including if price later pulls back below the
    threshold again (never re-tightens once it's earned the wider trail;
    doing otherwise would whipsaw a trade in and out of two different
    stop regimes on ordinary chop near the threshold).

    `breakeven_r_threshold`: once running favorable excursion reaches
    this many R, the stop is clamped to `max(stop, entry_price)` for the
    rest of the trade, on top of whatever the ordinary trail computes.
    """
    if direction != "LONG":
        raise ExitEngineExperimentError(
            f"UNSUPPORTED_DIRECTION:{direction!r}:only LONG is implemented (matches frozen .54 engine's own scoping)"
        )
    n = len(closes)
    if not (len(highs) == len(lows) == len(closes) == len(atr_values)):
        raise ExitEngineExperimentError("MISMATCHED_SERIES_LENGTH")
    if max_hold_bars <= 0:
        raise ExitEngineExperimentError("INVALID_MAX_HOLD_BARS:must be > 0")
    if cost_pct < 0:
        raise ExitEngineExperimentError("INVALID_COST_PCT:must be >= 0")
    if entry_idx < 0 or entry_idx >= n:
        raise ExitEngineExperimentError("INVALID_ENTRY_IDX")
    if (early_trail_atr_mult is None) != (early_stage_r_threshold is None):
        raise ExitEngineExperimentError(
            "early_trail_atr_mult and early_stage_r_threshold must both be set, or both left None"
        )

    atr_at_entry = atr_values[entry_idx]
    initial_distance = BASE.initial_stop_distance_long(entry_price, atr_at_entry, trail_atr_mult=trail_atr_mult)
    # R unit for staging/breakeven math -- ALWAYS the original (mature)
    # multiplier's distance, never the tightened early-stage one, so a
    # staged variant can't silently redefine how big this trade's risk
    # was for sizing/R-reporting purposes.
    r_unit_distance = initial_distance

    start = entry_idx + 1
    end = min(n, start + max_hold_bars)

    if start >= n:
        return VariantExitResult(
            exit_reason="NO_DATA_AFTER_ENTRY", bars_held=0, entry_price=entry_price, exit_price=None,
            gross_return_frac=None, net_return_frac=None, mfe_frac=None, mae_frac=None,
            initial_stop_distance=initial_distance, exit_bar_index=None, stop_trace=(),
        )

    running_extreme = entry_price
    worst_excursion = entry_price
    prev_stop = entry_price - initial_distance
    target_level = entry_price * (1.0 + take_profit_pct) if take_profit_pct is not None else None
    staged_promoted = False  # once True, early_trail_atr_mult is retired for the rest of the trade

    exit_reason: str | None = None
    exit_price: float | None = None
    exit_bar_index: int | None = None
    bars_held = 0
    stop_trace: list[float] = []

    for i in range(start, end):
        bars_held += 1
        bar_high, bar_low = highs[i], lows[i]

        stop_hit = bar_low <= prev_stop
        target_hit = target_level is not None and bar_high >= target_level

        if stop_hit:
            exit_reason, exit_price, exit_bar_index = "STOP", prev_stop, i
        elif target_hit:
            exit_reason, exit_price, exit_bar_index = "TARGET", target_level, i

        worst_excursion = min(worst_excursion, bar_low)

        if exit_reason is not None:
            stop_trace.append(prev_stop)
            break

        running_extreme = max(running_extreme, bar_high)
        current_mfe_r = (running_extreme - entry_price) / r_unit_distance

        if early_stage_r_threshold is not None and current_mfe_r >= early_stage_r_threshold:
            staged_promoted = True
        active_mult = trail_atr_mult if (early_stage_r_threshold is None or staged_promoted) else early_trail_atr_mult

        atr_i = atr_values[i]
        if atr_i is not None and not (isinstance(atr_i, float) and math.isnan(atr_i)) and atr_i > 0:
            candidate_stop = running_extreme - atr_i * active_mult
            prev_stop = max(prev_stop, candidate_stop)

        if breakeven_r_threshold is not None and current_mfe_r >= breakeven_r_threshold:
            prev_stop = max(prev_stop, entry_price)

        stop_trace.append(prev_stop)

    if exit_reason is None:
        exit_reason = "TIMEOUT"
        exit_bar_index = end - 1
        exit_price = closes[exit_bar_index]

    gross_return_frac = (exit_price / entry_price) - 1.0
    net_return_frac = gross_return_frac - cost_pct
    mfe_frac = (running_extreme / entry_price) - 1.0
    mae_frac = (worst_excursion / entry_price) - 1.0

    return VariantExitResult(
        exit_reason=exit_reason, bars_held=bars_held, entry_price=entry_price, exit_price=exit_price,
        gross_return_frac=gross_return_frac, net_return_frac=net_return_frac,
        mfe_frac=mfe_frac, mae_frac=mae_frac, initial_stop_distance=initial_distance,
        exit_bar_index=exit_bar_index, stop_trace=tuple(stop_trace),
    )
