#!/usr/bin/env python3
"""
AURA v0.5.4 -- Dynamic per-bar ATR trailing-stop exit engine

Why a NEW function, not an edit to `aura_exit_policy_backtest.py`
-------------------------------------------------------------------
`aura_exit_policy_backtest.py::simulate_trade` (confirmed by direct read,
`:250-343`) takes exactly one static scalar `trailing_stop_pct` and
applies it identically every bar
(`stop_level = running_extreme * (1.0 - trailing_stop_pct/100.0)`,
line 300). It has no parameter through which a per-bar-varying ATR
series could flow. Martin's .54 specification is explicit: "Do NOT fake
dynamic ATR trailing by calculating one static percentage. Instead,
create the minimum clean extension required" -- so this module is that
extension: a new, separate function, `.53`/existing callers of
`simulate_trade` are completely untouched (see `.53` git-diff
verification in the final report), and `simulate_trade` itself is never
imported or monkeypatched here.

Unit convention (documented explicitly to avoid the exact ambiguity that
exists between `aura_exit_policy_backtest.py`'s "cost_pct" -- percentage
POINTS, e.g. 0.10 means 0.10% -- and this module's, which follows
Martin's own .54 spec text "cost_pct = 0.001 = 0.10% ROUND-TRIP", i.e. a
FRACTION):

    Every return value in this module is a FRACTION (0.01 == 1%), never
    a percentage-point number. `entry_price`, `atr`, and all price levels
    are in the bars' native price units (dollars). `cost_pct` is a
    fraction (0.001 == 0.10%), matching Martin's .54 spec exactly.

Exit priority / no-look-ahead discipline (mirrors `simulate_trade`'s own
documented discipline exactly, so the two remain comparable):
  - Each bar is checked against the stop level AS COMPUTED FROM THE
    PRIOR BAR'S CLOSE (i.e. before this bar's own high/low can move the
    trail) -- the trail is only recomputed AFTER a bar's exit check.
  - A same-bar stop+target collision resolves to the STOP (conservative),
    identical to `simulate_trade`.
  - The trailing stop can only move in the trade's favor -- for a LONG,
    `new_stop = max(prev_stop, running_extreme_high - atr_at_this_bar *
    TRAIL_ATR_MULT)`; it is NEVER allowed to decrease. This is the
    literal reading of Martin's spec line: "The actual stop must never
    move downward (only up, or stay the same)."
  - `current_price` in Martin's formula `new_trail = current_price -
    ATR * TRAIL_ATR_MULT` is implemented as the running favorable
    extreme (highest high reached since entry for a LONG) -- the same
    "running_extreme" concept `simulate_trade` already uses, not the
    single current bar's close. This is a documented, smallest-
    reasonable-interpretation reading of an otherwise underspecified
    term (a pure implementation detail: the spec does not distinguish
    "current bar's close" from "best price reached so far", and the
    running-favorable-extreme reading is both the standard chandelier-
    exit convention and the one already used by this repo's own
    existing exit harness).

SHORT direction: Martin's spec explicitly asks this be documented, not
implemented, since .54's scope stays STOCK/ETF long-only for now. The
symmetrical formula would be:
    new_trail = current_price + ATR * TRAIL_ATR_MULT   (never moves UP)
    stop_hit when bar.high >= stop_level
This module raises `ExitEngineError` if `direction != "LONG"` rather than
silently guessing a SHORT implementation.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

VERSION = "AURA v0.5.4"

# EXPLICITLY_SELECTED / BASELINE_RESEARCH_HYPOTHESIS -- Martin's .54 spec,
# 2026-09-15. Not optimized, not claimed best.
TRAIL_ATR_MULT = 2.0
TAKE_PROFIT_PCT: float | None = None
MAX_HOLD_BARS = 20


class ExitEngineError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class ATRTrailingExitResult:
    exit_reason: str  # "STOP" | "TARGET" | "TIMEOUT" | "NO_DATA_AFTER_ENTRY"
    bars_held: int
    entry_price: float
    exit_price: float | None
    gross_return_frac: float | None
    net_return_frac: float | None
    mfe_frac: float | None
    mae_frac: float | None
    initial_stop_distance: float | None
    exit_bar_index: int | None
    stop_trace: tuple[float, ...]  # the realized stop level after each bar processed (for tests/audit)


def initial_stop_distance_long(entry_price: float, atr_at_entry: float, *, trail_atr_mult: float = TRAIL_ATR_MULT) -> float:
    """The planned initial risk distance for a LONG, computed BEFORE any
    position is sized -- `atr_at_entry * trail_atr_mult`, which is
    exactly the trailing-stop distance at the moment of entry (since
    `running_extreme == entry_price` at bar 0). Position sizing consumes
    this SAME number, never an independently-invented distance, so the
    initial risk stop and the trailing-stop mechanics stay mathematically
    consistent from bar zero.

    Fails safe: raises rather than returning a non-positive/NaN distance,
    since a caller sizing a position on a bad distance would silently
    mis-size (or divide by zero/negative).
    """
    if atr_at_entry is None or (isinstance(atr_at_entry, float) and math.isnan(atr_at_entry)):
        raise ExitEngineError("MISSING_ATR_AT_ENTRY:cannot compute initial stop distance")
    if atr_at_entry <= 0:
        raise ExitEngineError(f"INVALID_ATR_AT_ENTRY:{atr_at_entry!r}:must be > 0")
    if trail_atr_mult <= 0:
        raise ExitEngineError(f"INVALID_TRAIL_ATR_MULT:{trail_atr_mult!r}:must be > 0")
    distance = atr_at_entry * trail_atr_mult
    if entry_price is not None and distance >= entry_price:
        raise ExitEngineError(
            f"DEGENERATE_STOP_DISTANCE:{distance!r}:>=entry_price {entry_price!r} "
            f"(initial stop would be at or below zero)"
        )
    return distance


def simulate_atr_trailing_trade(
    *,
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    atr_values: Sequence[float],
    entry_idx: int,
    entry_price: float,
    direction: str,
    trail_atr_mult: float = TRAIL_ATR_MULT,
    take_profit_pct: float | None = TAKE_PROFIT_PCT,
    max_hold_bars: int = MAX_HOLD_BARS,
    cost_pct: float,
) -> ATRTrailingExitResult:
    """Walk forward bar-by-bar from `entry_idx + 1`, maintaining a
    per-bar ATR-based trailing stop that never moves against the
    position. `highs`/`lows`/`closes`/`atr_values` must be the same
    length and positionally aligned (index i is bar i for all four).
    `atr_values[entry_idx]` must be a valid (non-NaN, positive) ATR --
    this is what seeds the initial stop distance.

    Returns fractional returns (see module docstring's unit convention).
    """
    if direction != "LONG":
        raise ExitEngineError(
            f"UNSUPPORTED_DIRECTION:{direction!r}:only LONG is implemented in .54 "
            f"(SHORT is documented, not implemented, per Martin's explicit scoping)"
        )
    n = len(closes)
    if not (len(highs) == len(lows) == len(closes) == len(atr_values)):
        raise ExitEngineError("MISMATCHED_SERIES_LENGTH")
    if max_hold_bars <= 0:
        raise ExitEngineError("INVALID_MAX_HOLD_BARS:must be > 0")
    if cost_pct < 0:
        raise ExitEngineError("INVALID_COST_PCT:must be >= 0")
    if entry_idx < 0 or entry_idx >= n:
        raise ExitEngineError("INVALID_ENTRY_IDX")

    atr_at_entry = atr_values[entry_idx]
    initial_distance = initial_stop_distance_long(entry_price, atr_at_entry, trail_atr_mult=trail_atr_mult)

    start = entry_idx + 1
    end = min(n, start + max_hold_bars)

    if start >= n:
        return ATRTrailingExitResult(
            exit_reason="NO_DATA_AFTER_ENTRY",
            bars_held=0,
            entry_price=entry_price,
            exit_price=None,
            gross_return_frac=None,
            net_return_frac=None,
            mfe_frac=None,
            mae_frac=None,
            initial_stop_distance=initial_distance,
            exit_bar_index=None,
            stop_trace=(),
        )

    running_extreme = entry_price
    worst_excursion = entry_price
    prev_stop = entry_price - initial_distance
    target_level = entry_price * (1.0 + take_profit_pct) if take_profit_pct is not None else None

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

        # No exit this bar: NOW update the running extreme and recompute
        # the trail from THIS bar's own ATR -- never letting it decrease.
        running_extreme = max(running_extreme, bar_high)
        atr_i = atr_values[i]
        if atr_i is not None and not (isinstance(atr_i, float) and math.isnan(atr_i)) and atr_i > 0:
            candidate_stop = running_extreme - atr_i * trail_atr_mult
            prev_stop = max(prev_stop, candidate_stop)
        # If ATR is unavailable/invalid for this bar, the trail simply
        # does not update this bar (stays at prev_stop) -- it never
        # decreases and never fabricates a distance from a missing ATR.
        stop_trace.append(prev_stop)

    if exit_reason is None:
        exit_reason = "TIMEOUT"
        exit_bar_index = end - 1
        exit_price = closes[exit_bar_index]

    gross_return_frac = (exit_price / entry_price) - 1.0
    net_return_frac = gross_return_frac - cost_pct
    mfe_frac = (running_extreme / entry_price) - 1.0
    mae_frac = (worst_excursion / entry_price) - 1.0

    return ATRTrailingExitResult(
        exit_reason=exit_reason,
        bars_held=bars_held,
        entry_price=entry_price,
        exit_price=exit_price,
        gross_return_frac=gross_return_frac,
        net_return_frac=net_return_frac,
        mfe_frac=mfe_frac,
        mae_frac=mae_frac,
        initial_stop_distance=initial_distance,
        exit_bar_index=exit_bar_index,
        stop_trace=tuple(stop_trace),
    )
