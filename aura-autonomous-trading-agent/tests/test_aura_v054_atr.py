"""AURA v0.5.4 tests -- Category A: Wilder ATR calculation."""

from __future__ import annotations

import math

import pandas as pd
import pytest

import aura_v054_atr as ATR


def test_wilder_atr_matches_manual_recurrence_for_short_series():
    # Constant true range series: high-low = 2 every bar, close stays
    # between high/low so the |high-prev_close|/|low-prev_close| terms
    # never dominate -- true range should be exactly 2 every bar.
    high = pd.Series([10.0, 11.0, 12.0, 13.0, 14.0])
    low = pd.Series([8.0, 9.0, 10.0, 11.0, 12.0])
    close = pd.Series([9.0, 10.0, 11.0, 12.0, 13.0])
    result = ATR.wilder_atr(high, low, close, period=3)

    # bars 0,1 are NaN (insufficient history), bar 2 is the seed mean(TR[0:3]).
    assert math.isnan(result.atr.iloc[0])
    assert math.isnan(result.atr.iloc[1])
    assert result.true_range.tolist() == pytest.approx([2.0, 2.0, 2.0, 2.0, 2.0])
    assert result.atr.iloc[2] == pytest.approx(2.0)
    # Manual Wilder recurrence: ATR[3] = (ATR[2]*(3-1) + TR[3]) / 3
    expected_3 = (2.0 * 2 + 2.0) / 3
    assert result.atr.iloc[3] == pytest.approx(expected_3)
    expected_4 = (expected_3 * 2 + 2.0) / 3
    assert result.atr.iloc[4] == pytest.approx(expected_4)


def test_wilder_atr_differs_from_simple_rolling_mean_after_a_shock():
    # A single large-range bar should have a LONGER-lasting effect under
    # Wilder smoothing than it would under a plain rolling mean once it
    # exits the rolling window -- demonstrating this is genuinely Wilder
    # smoothing, not a relabeled rolling mean.
    n = 30
    high = pd.Series([100.0 + i * 0.1 for i in range(n)])
    low = pd.Series([99.0 + i * 0.1 for i in range(n)])
    close = pd.Series([99.5 + i * 0.1 for i in range(n)])
    # Inject one huge-range bar at index 5.
    high.iloc[5] = 130.0
    low.iloc[5] = 90.0

    wilder = ATR.wilder_atr(high, low, close, period=14).atr
    rolling_tr = ATR.wilder_atr(high, low, close, period=14).true_range.rolling(14).mean()

    # 20 bars after the shock (well outside a 14-bar rolling window but
    # still inside Wilder's infinite-memory exponential decay), Wilder's
    # ATR must still be measurably elevated above the plain rolling mean.
    idx = 25
    assert wilder.iloc[idx] > rolling_tr.iloc[idx]


def test_wilder_atr_from_bars_requires_ohlc_columns():
    df = pd.DataFrame({"close": [1.0, 2.0, 3.0]})
    with pytest.raises(ATR.WilderATRError):
        ATR.wilder_atr_from_bars(df)


def test_wilder_atr_invalid_period_raises():
    with pytest.raises(ATR.WilderATRError):
        ATR.wilder_atr(pd.Series([1.0]), pd.Series([1.0]), pd.Series([1.0]), period=0)


def test_wilder_atr_mismatched_lengths_raise():
    with pytest.raises(ATR.WilderATRError):
        ATR.wilder_atr(pd.Series([1.0, 2.0]), pd.Series([1.0]), pd.Series([1.0, 2.0]))


def test_wilder_atr_method_label_is_wilder():
    result = ATR.wilder_atr(pd.Series([1.0] * 20), pd.Series([0.5] * 20), pd.Series([0.75] * 20))
    assert result.method == "WILDER"
    assert ATR.ATR_METHOD == "WILDER"
