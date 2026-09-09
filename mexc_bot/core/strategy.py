"""
Volatility-Adjusted Momentum Scanner — signal generation.

Long setup:
  - EMA20 > EMA50 > EMA200 (uptrend)
  - Close breaks above prior 20-candle high
  - Volume on breakout candle > its 20-candle average
  - RSI between RSI_LONG_MIN and RSI_LONG_MAX

Short setup (mirror image, only used if ALLOW_SHORTS and market is futures):
  - EMA20 < EMA50 < EMA200 (downtrend)
  - Close breaks below prior 20-candle low
  - Volume confirmation
  - RSI between RSI_SHORT_MIN and RSI_SHORT_MAX
"""
import pandas as pd
import config as cfg
from core import indicators as ind


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Attach all indicator columns needed for signal evaluation. Expects
    columns: open, high, low, close, volume. Returns a copy."""
    df = df.copy()
    df["ema_fast"] = ind.ema(df["close"], cfg.EMA_FAST)
    df["ema_mid"] = ind.ema(df["close"], cfg.EMA_MID)
    df["ema_slow"] = ind.ema(df["close"], cfg.EMA_SLOW)
    df["rsi"] = ind.rsi(df["close"], cfg.RSI_PERIOD)
    df["atr"] = ind.atr(df, cfg.RSI_PERIOD)
    df["adx"] = ind.adx(df, cfg.ADX_PERIOD)
    macd_line, signal_line, hist = ind.macd(df["close"], cfg.MACD_FAST,
                                             cfg.MACD_SLOW, cfg.MACD_SIGNAL)
    df["macd"] = macd_line
    df["macd_signal"] = signal_line
    df["macd_hist"] = hist
    df["roll_high"] = ind.rolling_high(df["close"], cfg.BREAKOUT_LOOKBACK)
    df["roll_low"] = ind.rolling_low(df["close"], cfg.BREAKOUT_LOOKBACK)
    df["vol_avg"] = ind.volume_avg(df["volume"], cfg.VOLUME_LOOKBACK)
    df["atr_pct"] = df["atr"] / df["close"]
    return df


def passes_universe_filter(df: pd.DataFrame, last_24h_volume_usdt: float,
                            listing_age_days: float) -> bool:
    """Checks pair-level filters unrelated to the signal itself."""
    if last_24h_volume_usdt < cfg.MIN_24H_VOLUME_USDT:
        return False
    if listing_age_days < cfg.MIN_LISTING_AGE_DAYS:
        return False
    latest_atr_pct = df["atr_pct"].iloc[-1]
    if pd.isna(latest_atr_pct):
        return False
    if not (cfg.ATR_PCT_MIN <= latest_atr_pct <= cfg.ATR_PCT_MAX):
        return False
    return True


def _evaluate_macd(df: pd.DataFrame) -> dict:
    """
    MACD crossover entries.

    Long  = golden cross: MACD line crosses ABOVE its signal line this candle.
    Short = death cross:  MACD line crosses BELOW its signal line this candle.

    Deliberately kept pure — no EMA trend filter or breakout confirmation — so
    the crossover signal itself is what's being tested. Exits, position sizing
    and stops all come from the shared ATR risk layer, identical to the
    momentum mode, so any performance difference is attributable to the entry
    signal rather than to different risk handling.
    """
    row, prev = df.iloc[-1], df.iloc[-2]

    if pd.isna(row["macd"]) or pd.isna(row["macd_signal"]) or pd.isna(prev["macd"]):
        return {"signal": None, "reason": "MACD warming up", "atr": row["atr"]}

    was_below = prev["macd"] <= prev["macd_signal"]
    was_above = prev["macd"] >= prev["macd_signal"]
    now_above = row["macd"] > row["macd_signal"]
    now_below = row["macd"] < row["macd_signal"]

    if was_below and now_above:
        return {"signal": "long", "reason": "MACD golden cross", "atr": row["atr"]}
    if cfg.ALLOW_SHORTS and was_above and now_below:
        return {"signal": "short", "reason": "MACD death cross", "atr": row["atr"]}

    return {"signal": None, "reason": "no crossover", "atr": row["atr"]}


def _evaluate_momentum(df: pd.DataFrame) -> dict:
    """Original strategy: EMA trend alignment + N-candle breakout + volume + RSI."""
    row = df.iloc[-1]

    if pd.isna(row["ema_slow"]) or pd.isna(row["roll_high"]) or pd.isna(row["vol_avg"]):
        return {"signal": None, "reason": "indicators warming up", "atr": None}

    uptrend = row["ema_fast"] > row["ema_mid"] > row["ema_slow"]
    downtrend = row["ema_fast"] < row["ema_mid"] < row["ema_slow"]
    volume_confirmed = row["volume"] > row["vol_avg"] * cfg.VOLUME_CONFIRM_MULT

    # --- Long setup ---
    if uptrend:
        breakout = row["close"] > row["roll_high"]
        rsi_ok = cfg.RSI_LONG_MIN <= row["rsi"] <= cfg.RSI_LONG_MAX
        if breakout and volume_confirmed and rsi_ok:
            return {"signal": "long", "reason": "uptrend breakout + volume + RSI ok",
                    "atr": row["atr"]}

    # --- Short setup ---
    if cfg.ALLOW_SHORTS and downtrend:
        breakdown = row["close"] < row["roll_low"]
        rsi_ok = cfg.RSI_SHORT_MIN <= row["rsi"] <= cfg.RSI_SHORT_MAX
        if breakdown and volume_confirmed and rsi_ok:
            return {"signal": "short", "reason": "downtrend breakdown + volume + RSI ok",
                    "atr": row["atr"]}

    return {"signal": None, "reason": "no setup", "atr": row["atr"]}


def direction_rule(row) -> str:
    """
    The strategy's DIRECTIONAL BIAS at any candle, independent of whether an
    entry signal actually fired there.

    Used by the permutation test's 2x2 decomposition: applying this at random
    timestamps isolates whether the strategy's direction-choosing logic adds
    value even when its timing is destroyed.
    """
    if cfg.STRATEGY_MODE == "macd":
        m, s = row.get("macd"), row.get("macd_signal")
        if m is None or s is None or pd.isna(m) or pd.isna(s):
            return "long"
        bias = "long" if m > s else "short"
    else:
        f, mid = row.get("ema_fast"), row.get("ema_mid")
        if f is None or mid is None or pd.isna(f) or pd.isna(mid):
            return "long"
        bias = "long" if f > mid else "short"
    return bias if (bias == "long" or cfg.ALLOW_SHORTS) else "long"


def evaluate_signal(df: pd.DataFrame) -> dict:
    """
    Evaluate the most recently CLOSED candle for an entry signal.
    Dispatches on cfg.STRATEGY_MODE. Returns:
        {"signal": "long"|"short"|None, "reason": str, "atr": float}
    """
    min_history = max(cfg.EMA_SLOW, cfg.BREAKOUT_LOOKBACK, cfg.MACD_SLOW) + 5
    if len(df) < min_history:
        return {"signal": None, "reason": "insufficient history", "atr": None}

    row = df.iloc[-1]

    # --- Regime filter (ADX) — applies to BOTH strategy modes ---
    # Breakout and crossover entries alike fail most often in choppy,
    # directionless markets. ADX measures trend STRENGTH (not direction), so
    # requiring a minimum is a direct attempt to sit those periods out.
    # Toggleable so filter-on vs filter-off can be compared honestly.
    if cfg.USE_ADX_FILTER:
        adx_val = row.get("adx")
        if adx_val is None or pd.isna(adx_val):
            return {"signal": None, "reason": "ADX warming up", "atr": row["atr"]}
        if adx_val < cfg.ADX_MIN_THRESHOLD:
            return {"signal": None, "reason": f"chop filtered (ADX {adx_val:.1f})",
                    "atr": row["atr"]}

    if cfg.STRATEGY_MODE == "macd":
        return _evaluate_macd(df)
    return _evaluate_momentum(df)
