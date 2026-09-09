"""
Technical indicators used by the strategy.
Pure pandas/numpy implementations — no external TA library dependency.
"""
import pandas as pd
import numpy as np


def ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential moving average."""
    return series.ewm(span=period, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Relative Strength Index (Wilder's smoothing)."""
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi_val = 100 - (100 / (1 + rs))
    return rsi_val.fillna(50)  # neutral fill for warmup period


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """
    Average True Range. Expects df with columns: high, low, close.
    """
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)

    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    return true_range.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 8):
    """
    MACD. Returns (macd_line, signal_line, histogram).

    macd_line   = EMA(fast) - EMA(slow)
    signal_line = EMA(macd_line, signal)
    A "golden cross" is macd_line crossing ABOVE signal_line; a "death cross"
    is macd_line crossing BELOW it.
    """
    macd_line = ema(series, fast) - ema(series, slow)
    signal_line = ema(macd_line, signal)
    return macd_line, signal_line, macd_line - signal_line


def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """
    Average Directional Index — measures TREND STRENGTH regardless of direction.

    Low ADX (roughly < 20-25) = choppy / directionless market, which is the
    known failure mode for breakout strategies: false breakouts that
    immediately reverse. High ADX = a real directional move underway.

    Wilder's method: smoothed +DM/-DM over ATR gives +DI/-DI, then ADX is
    the smoothed absolute difference between them relative to their sum.
    Expects df with columns: high, low, close.
    """
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)

    up_move = high.diff()
    down_move = -low.diff()

    plus_dm = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0.0),
                        index=df.index)
    minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0.0),
                         index=df.index)

    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    alpha = 1 / period
    atr_smooth = true_range.ewm(alpha=alpha, adjust=False, min_periods=period).mean()
    plus_di = 100 * plus_dm.ewm(alpha=alpha, adjust=False, min_periods=period).mean() / atr_smooth
    minus_di = 100 * minus_dm.ewm(alpha=alpha, adjust=False, min_periods=period).mean() / atr_smooth

    di_sum = (plus_di + minus_di).replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / di_sum

    return dx.ewm(alpha=alpha, adjust=False, min_periods=period).mean()


def rolling_high(series: pd.Series, window: int) -> pd.Series:
    """Rolling high over the *prior* `window` candles (excludes current candle)."""
    return series.shift(1).rolling(window=window).max()


def rolling_low(series: pd.Series, window: int) -> pd.Series:
    """Rolling low over the *prior* `window` candles (excludes current candle)."""
    return series.shift(1).rolling(window=window).min()


def volume_avg(series: pd.Series, window: int = 20) -> pd.Series:
    return series.shift(1).rolling(window=window).mean()
