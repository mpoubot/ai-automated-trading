#!/usr/bin/env python3
"""
AURA v0.5.4 -- Wilder ATR (isolated component)

STATUS: EXPLICITLY_SELECTED (not recovered evidence, not optimized).

Martin's .54 specification (2026-09-15) explicitly selected WILDER ATR --
not .51's simple rolling-mean ATR (`aura_v05351_live_alpaca_equity_signal_
source.py`'s `tr.rolling(14).mean()`) -- as the ATR convention for .54's
equities/daily exit and sizing pipeline. This is a methodology decision,
not a discovered fact: prior read-only investigation this project
(`AURA_v0.53_ATR_Exit_Cost_Sizing_Decision_Support_2026-09-15.md`, Section
A) found BOTH conventions in use elsewhere in this repository --
.51's simple rolling mean (daily equities, computed but never consumed
downstream) and .12's Wilder-smoothed `wilder_atr` (1H crypto, actively
used as an entry-condition input on the out-of-scope crypto track) -- and
concluded the choice between them was UNKNOWN pending Martin's decision.

This module is a CLEAN, ISOLATED REIMPLEMENTATION of Wilder smoothing
against .51's own bar schema (not a wrapper around .12's function, which
is written against 1H crypto bars and imported alongside
`aura_regime_backtest_v2.py`'s crypto-only indicator stack) so that:
  (a) it can be swapped or compared later against .51's simple rolling
      variant without disturbing .51 itself, and
  (b) it carries no crypto-track dependency into the STOCK/ETF-only .54
      scope.

Do not optimize the smoothing period or formula. ATR_PERIOD = 14 is
Wilder's own original convention (matching .51's and .12's existing
period choice) -- kept for direct comparability, not re-derived.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

VERSION = "AURA v0.5.4"
ATR_METHOD = "WILDER"  # EXPLICITLY_SELECTED by Martin, 2026-09-15
ATR_PERIOD = 14


class WilderATRError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class ATRResult:
    """One symbol's full Wilder ATR series aligned to its input bars.

    `atr` is `NaN` for the first `period` bars (Wilder's own recurrence
    needs `period` true-range observations to seed the first average, then
    `period - 1` more smoothed steps before a bar's own value is a Wilder
    average rather than the seed) -- these are NOT usable ATR values and
    callers must never treat a `NaN` as zero or as "no distance."
    """

    atr: pd.Series
    true_range: pd.Series
    period: int
    method: str


def _true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            (high - low).abs(),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    # The very first bar has no prior close -- its "true range" collapses
    # to high-low only (no prior-close terms are computable), matching the
    # conventional Wilder/ATR treatment of the series' first observation.
    tr.iloc[0] = (high.iloc[0] - low.iloc[0]) if len(tr) else tr
    return tr


def wilder_atr(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    *,
    period: int = ATR_PERIOD,
) -> ATRResult:
    """Wilder's original smoothed ATR:

        TR[i]  = max(high[i]-low[i], |high[i]-close[i-1]|, |low[i]-close[i-1]|)
        ATR[period-1] = mean(TR[0 .. period-1])                     (seed)
        ATR[i] = (ATR[i-1] * (period - 1) + TR[i]) / period          for i >= period

    This is the standard recursive Wilder smoothing (an exponential
    moving average with alpha = 1/period), deliberately different from
    .51's plain `period`-bar rolling mean of TR, which has no memory
    beyond its own window and reacts differently to a single large-range
    bar.

    Indices before `period - 1` are `NaN` (insufficient history to seed
    the average) -- this is warm-up, not a computed zero.
    """
    if period <= 0:
        raise WilderATRError("INVALID_PERIOD:must be > 0")
    if not (len(high) == len(low) == len(close)):
        raise WilderATRError("MISMATCHED_SERIES_LENGTH")

    tr = _true_range(high, low, close)
    n = len(tr)
    atr = pd.Series(np.nan, index=tr.index, dtype=float)

    if n >= period:
        seed = float(tr.iloc[0:period].mean())
        atr.iloc[period - 1] = seed
        prev = seed
        for i in range(period, n):
            prev = (prev * (period - 1) + float(tr.iloc[i])) / period
            atr.iloc[i] = prev

    return ATRResult(atr=atr, true_range=tr, period=period, method=ATR_METHOD)


def wilder_atr_from_bars(bars_df: pd.DataFrame, *, period: int = ATR_PERIOD) -> ATRResult:
    """Convenience wrapper for a bars DataFrame with `high`/`low`/`close`
    columns (the same schema .51's `build_technical_features`/
    `_validate_bars_frame` expect). Pure function of its inputs; no I/O.
    """
    required = {"high", "low", "close"}
    missing = required - set(bars_df.columns)
    if missing:
        raise WilderATRError(f"MISSING_COLUMNS:{sorted(missing)}")
    return wilder_atr(bars_df["high"], bars_df["low"], bars_df["close"], period=period)
