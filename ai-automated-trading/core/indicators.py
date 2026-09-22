"""
core/indicators.py

Minimal indicator set for the Phase 5 research core -- currently just ATR,
which the backtest engine (stop distance, trailing stop) and risk model
(position sizing) both depend on.

This is a from-scratch re-implementation of the same Wilder ATR formula
already verified in mexc_bot/core/indicators.py::atr() -- reproduced here,
not imported, to keep this package independent of mexc_bot per the Phase 5
instruction. Only ATR is ported; Phase 5 does not need the rest of
mexc_bot's indicator set (EMA/RSI/MACD/ADX belong to the real strategy the
pipeline will validate in a later phase, not to the pipeline-proof fixture).
"""
from __future__ import annotations

import pandas as pd


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder's Average True Range. Expects columns: high, low, close.
    NaN for the first `period - 1` rows (warmup, via ewm min_periods=period);
    the row at index `period - 1` is the first with a valid value. Matches
    mexc_bot's behavior exactly (same formula, same min_periods)."""
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return true_range.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
