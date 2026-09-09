import numpy as np
import pandas as pd

def rsi(close, period=14):
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1/period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

def build_features(df):
    out = df.copy()
    close = out["close"]
    volume = out["volume"]

    out["ema_3"] = close.ewm(span=3, adjust=False).mean()
    out["ema_8"] = close.ewm(span=8, adjust=False).mean()
    out["ema_21"] = close.ewm(span=21, adjust=False).mean()
    out["ema_50"] = close.ewm(span=50, adjust=False).mean()

    out["rsi_14"] = rsi(close)

    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    out["macd"] = ema12 - ema26
    out["macd_signal"] = out["macd"].ewm(span=9, adjust=False).mean()

    mid = close.rolling(20).mean()
    std = close.rolling(20).std()
    out["bb_upper"] = mid + 2 * std
    out["bb_lower"] = mid - 2 * std
    width = (out["bb_upper"] - out["bb_lower"]).replace(0, np.nan)
    out["bb_position"] = (close - out["bb_lower"]) / width

    prev = close.shift(1)
    tr = pd.concat([
        out["high"] - out["low"],
        (out["high"] - prev).abs(),
        (out["low"] - prev).abs()
    ], axis=1).max(axis=1)
    out["atr_14"] = tr.rolling(14).mean()
    out["atr_pct"] = out["atr_14"] / close * 100

    out["rel_volume"] = volume / volume.rolling(20).mean().replace(0, np.nan)

    out["return_1"] = close.pct_change()
    out["price_acceleration"] = out["return_1"].diff()

    out["cross_3_8"] = (
        (out["ema_3"] > out["ema_8"]) &
        (out["ema_3"].shift(1) <= out["ema_8"].shift(1))
    )
    out["cross_8_21"] = (
        (out["ema_8"] > out["ema_21"]) &
        (out["ema_8"].shift(1) <= out["ema_21"].shift(1))
    )
    out["cross_21_50"] = (
        (out["ema_21"] > out["ema_50"]) &
        (out["ema_21"].shift(1) <= out["ema_50"].shift(1))
    )
    return out
