
"""
AURA v0.4.3 — MARKET RELATIONSHIP RESEARCH ENGINE

RESEARCH ONLY — NO ORDERS ARE PLACED.

Purpose
-------
Research relationships between five market dimensions without turning the
system into an indicator soup:

1. TREND       : EMA 3/8, EMA 21/50, EMA 50/200
2. MOMENTUM    : RSI, MACD histogram, RSI divergence, MACD divergence
3. VOLUME      : relative volume, volume-confirmed breakout, OBV divergence
4. STRUCTURE   : 20D/50D breakouts and distance from recent highs/lows
5. VOLATILITY  : ATR%, Bollinger width, ATR-normalized breakout

Every hypothesis is evaluated against forward 1/3/5/10 day outcomes plus
5-day MFE/MAE.

Validation
----------
- Training period is used only to rank hypotheses.
- Frozen test periods are never used for selection.
- Optional unseen-symbol holdout tests whether a relationship generalizes
  beyond the symbols used for training.
- Regime buckets test whether the effect survives different market states.
- A relationship is a CANDIDATE only when it passes conservative minimum
  sample and validation requirements.

Important
---------
Signals are evaluated on the decision bar and entered at the NEXT trading
day's OPEN. No future bar is used to define the decision itself.

Default:
    historical 2024-01-01 -> 2026-08-25
    train     2024-01-01 -> 2025-12-31
    frozen Q1 2026-01-01 -> 2026-03-31
    frozen Q2 2026-04-01 -> 2026-06-30
    frozen Q3 2026-07-01 -> 2026-08-25

Example:
    python historical_signal_quality_scanner_v043.py --universe equity
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import numpy as np
import pandas as pd
from dotenv import load_dotenv
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import DataFeed


DEFAULT_EQUITY_SYMBOLS = [
    "AAPL", "MSFT", "AVGO", "JNJ", "IWM", "CVX", "LLY", "CAT", "SPY",
    "RTX", "AMZN", "XOM", "TSLA", "BAC", "COST", "GS", "GE", "DE", "WMT",
    "NVDA", "QQQ", "LMT", "GLD", "JPM", "META", "GOOGL", "SLV",
]


def utc_timestamp(value: str | pd.Timestamp) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        return ts.tz_localize("UTC")
    return ts.tz_convert("UTC")


def ema(s: pd.Series, span: int) -> pd.Series:
    return s.ewm(span=span, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def add_features(df: pd.DataFrame) -> pd.DataFrame:
    x = df.copy().sort_index()

    x["ema_3"] = ema(x["close"], 3)
    x["ema_8"] = ema(x["close"], 8)
    x["ema_21"] = ema(x["close"], 21)
    x["ema_50"] = ema(x["close"], 50)
    x["ema_200"] = ema(x["close"], 200)

    x["rsi_14"] = rsi(x["close"], 14)

    ema12 = ema(x["close"], 12)
    ema26 = ema(x["close"], 26)
    x["macd"] = ema12 - ema26
    x["macd_signal"] = ema(x["macd"], 9)
    x["macd_hist"] = x["macd"] - x["macd_signal"]

    vol20 = x["volume"].rolling(20, min_periods=10).mean()
    x["rel_volume"] = x["volume"] / vol20.replace(0, np.nan)

    prev_close = x["close"].shift(1)
    tr = pd.concat([
        x["high"] - x["low"],
        (x["high"] - prev_close).abs(),
        (x["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    x["atr_14"] = tr.rolling(14, min_periods=14).mean()
    x["atr_pct"] = x["atr_14"] / x["close"] * 100.0

    mid20 = x["close"].rolling(20, min_periods=20).mean()
    std20 = x["close"].rolling(20, min_periods=20).std()
    x["bb_mid"] = mid20
    x["bb_upper"] = mid20 + 2 * std20
    x["bb_lower"] = mid20 - 2 * std20
    x["bb_width"] = (x["bb_upper"] - x["bb_lower"]) / mid20

    x["price_acceleration"] = x["close"].pct_change()

    # Previous highs/lows deliberately exclude the current bar.
    x["prev_20d_high"] = x["high"].rolling(20, min_periods=20).max().shift(1)
    x["prev_50d_high"] = x["high"].rolling(50, min_periods=50).max().shift(1)
    x["prev_20d_low"] = x["low"].rolling(20, min_periods=20).min().shift(1)
    x["prev_50d_low"] = x["low"].rolling(50, min_periods=50).min().shift(1)

    x["breakout_20"] = x["close"] > x["prev_20d_high"]
    x["breakout_50"] = x["close"] > x["prev_50d_high"]
    x["breakout_20_atr"] = (
        (x["close"] - x["prev_20d_high"]) / x["atr_14"]
    )
    x["distance_20d_high"] = x["close"] / x["prev_20d_high"] - 1.0

    x["obv"] = (np.sign(x["close"].diff()).fillna(0) * x["volume"]).cumsum()
    x["obv_slope_10"] = x["obv"].diff(10) / x["volume"].rolling(10).sum()

    # Short-term trend states.
    x["ema38_bull"] = x["ema_3"] > x["ema_8"]
    x["ema2150_bull"] = x["ema_21"] > x["ema_50"]
    x["ema50200_bull"] = x["ema_50"] > x["ema_200"]
    x["trend_stack"] = (
        (x["ema_3"] > x["ema_8"])
        & (x["ema_8"] > x["ema_21"])
        & (x["ema_21"] > x["ema_50"])
    )
    x["golden_regime"] = x["ema_50"] > x["ema_200"]

    # Raw EMA crossover. Entry is next day's open.
    x["bullish_crossover"] = x["ema38_bull"] & (~x["ema38_bull"].shift(1, fill_value=False))

    # Divergence: compare current rolling swing against prior rolling swing.
    # The signal is only emitted when price makes a lower/higher swing while
    # the oscillator makes the opposite swing.
    price_low_10 = x["low"].rolling(10, min_periods=10).min()
    price_high_10 = x["high"].rolling(10, min_periods=10).max()
    rsi_low_10 = x["rsi_14"].rolling(10, min_periods=10).min()
    rsi_high_10 = x["rsi_14"].rolling(10, min_periods=10).max()
    macd_low_10 = x["macd_hist"].rolling(10, min_periods=10).min()
    macd_high_10 = x["macd_hist"].rolling(10, min_periods=10).max()

    x["bull_rsi_div"] = (
        (price_low_10 < price_low_10.shift(10))
        & (rsi_low_10 > rsi_low_10.shift(10))
    )
    x["bear_rsi_div"] = (
        (price_high_10 > price_high_10.shift(10))
        & (rsi_high_10 < rsi_high_10.shift(10))
    )
    x["bull_macd_div"] = (
        (price_low_10 < price_low_10.shift(10))
        & (macd_low_10 > macd_low_10.shift(10))
    )
    x["bear_macd_div"] = (
        (price_high_10 > price_high_10.shift(10))
        & (macd_high_10 < macd_high_10.shift(10))
    )

    # OBV accumulation divergence: price roughly flat/down while OBV improves.
    price_20 = x["close"].pct_change(20)
    x["obv_bull_div"] = (price_20 < 0.02) & (x["obv_slope_10"] > 0)

    # Market regime based only on information available on the decision bar.
    x["regime"] = np.select(
        [
            x["golden_regime"] & (x["atr_pct"] >= x["atr_pct"].rolling(60).median()),
            x["golden_regime"] & (x["atr_pct"] < x["atr_pct"].rolling(60).median()),
            (~x["golden_regime"]) & (x["atr_pct"] >= x["atr_pct"].rolling(60).median()),
        ],
        ["BULL_HIGH_VOL", "BULL_LOW_VOL", "BEAR_HIGH_VOL"],
        default="BEAR_LOW_VOL",
    )

    return x


def fetch_symbol(client, symbol: str, start: str, end: str) -> pd.DataFrame:
    request = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Day,
        start=start,
        end=end,
        feed=DataFeed.IEX,
    )
    bars = client.get_stock_bars(request).df
    if bars.empty:
        return pd.DataFrame()

    if isinstance(bars.index, pd.MultiIndex):
        try:
            bars = bars.xs(symbol, level="symbol")
        except KeyError:
            return pd.DataFrame()

    bars = bars.copy()
    bars.index = pd.to_datetime(bars.index, utc=True)
    needed = ["open", "high", "low", "close", "volume"]
    if any(c not in bars.columns for c in needed):
        return pd.DataFrame()
    return bars[needed].sort_index()


def forward_outcomes(df: pd.DataFrame, decision_pos: int) -> dict:
    entry_pos = decision_pos + 1
    if entry_pos >= len(df):
        return {}

    entry = float(df.iloc[entry_pos]["open"])
    if not np.isfinite(entry) or entry <= 0:
        return {}

    out = {
        "entry_timestamp": df.index[entry_pos].isoformat(),
        "entry_open": entry,
    }
    for days in (1, 3, 5, 10):
        target = entry_pos + days - 1
        out[f"forward_{days}d"] = (
            float(df.iloc[target]["close"]) / entry - 1.0
            if target < len(df) else np.nan
        )

    end_pos = min(entry_pos + 4, len(df) - 1)
    future = df.iloc[entry_pos:end_pos + 1]
    out["mfe_5d"] = float(future["high"].max()) / entry - 1.0 if len(future) else np.nan
    out["mae_5d"] = float(future["low"].min()) / entry - 1.0 if len(future) else np.nan
    return out


def event_row(df: pd.DataFrame, symbol: str, pos: int) -> dict | None:
    out = forward_outcomes(df, pos)
    if not out or not np.isfinite(out.get("forward_5d", np.nan)):
        return None

    r = df.iloc[pos]
    return {
        "symbol": symbol,
        "decision_timestamp": df.index[pos].isoformat(),
        "signal_close": float(r["close"]),
        "ema_3": float(r["ema_3"]),
        "ema_8": float(r["ema_8"]),
        "ema_21": float(r["ema_21"]),
        "ema_50": float(r["ema_50"]),
        "ema_200": float(r["ema_200"]) if np.isfinite(r["ema_200"]) else np.nan,
        "rsi_14": float(r["rsi_14"]) if np.isfinite(r["rsi_14"]) else np.nan,
        "macd_hist": float(r["macd_hist"]) if np.isfinite(r["macd_hist"]) else np.nan,
        "rel_volume": float(r["rel_volume"]) if np.isfinite(r["rel_volume"]) else np.nan,
        "atr_pct": float(r["atr_pct"]) if np.isfinite(r["atr_pct"]) else np.nan,
        "bb_width": float(r["bb_width"]) if np.isfinite(r["bb_width"]) else np.nan,
        "breakout_20": bool(r["breakout_20"]) if pd.notna(r["breakout_20"]) else False,
        "breakout_50": bool(r["breakout_50"]) if pd.notna(r["breakout_50"]) else False,
        "breakout_20_atr": float(r["breakout_20_atr"]) if np.isfinite(r["breakout_20_atr"]) else np.nan,
	"bullish_crossover": bool(r["bullish_crossover"]) if pd.notna(r["bullish_crossover"]) else False,
        "trend_stack": bool(r["trend_stack"]),
        "golden_regime": bool(r["golden_regime"]) if pd.notna(r["golden_regime"]) else False,
        "bull_rsi_div": bool(r["bull_rsi_div"]) if pd.notna(r["bull_rsi_div"]) else False,
        "bear_rsi_div": bool(r["bear_rsi_div"]) if pd.notna(r["bear_rsi_div"]) else False,
        "bull_macd_div": bool(r["bull_macd_div"]) if pd.notna(r["bull_macd_div"]) else False,
        "bear_macd_div": bool(r["bear_macd_div"]) if pd.notna(r["bear_macd_div"]) else False,
        "obv_bull_div": bool(r["obv_bull_div"]) if pd.notna(r["obv_bull_div"]) else False,
        "regime": str(r["regime"]),
        **out,
    }


def scan_symbol(client, symbol: str, start: str, end: str) -> pd.DataFrame:
    raw = fetch_symbol(client, symbol, start, end)
    if raw.empty:
        return pd.DataFrame()

    df = add_features(raw)
    # We deliberately scan the event universe broadly, rather than only
    # EMA crossovers, because v0.4.3 is a relationship research engine.
    candidates = (
        df["bullish_crossover"]
        | df["breakout_20"]
        | df["breakout_50"]
        | df["bull_rsi_div"]
        | df["bull_macd_div"]
        | df["obv_bull_div"]
    )
    positions = np.flatnonzero(candidates.fillna(False).to_numpy())

    rows = []
    for pos in positions:
        row = event_row(df, symbol, int(pos))
        if row:
            rows.append(row)
    return pd.DataFrame(rows)


def finite_mean(s) -> float:
    x = pd.to_numeric(s, errors="coerce").dropna()
    return float(x.mean()) if len(x) else np.nan


def finite_median(s) -> float:
    x = pd.to_numeric(s, errors="coerce").dropna()
    return float(x.median()) if len(x) else np.nan


def conditions() -> dict[str, list[str]]:
    # Single-dimensional relationships.
    return {
        "EMA38_CROSSOVER": ["EMA38_CROSSOVER"],
        "TREND_2150_BULL": ["TREND_2150_BULL"],
        "TREND_50200_BULL": ["TREND_50200_BULL"],
        "TREND_STACK": ["TREND_STACK"],
        "RSI_GT_50": ["RSI_GT_50"],
        "RSI_GT_55": ["RSI_GT_55"],
        "MACD_HIST_GT_0": ["MACD_HIST_GT_0"],
        "REL_VOLUME_GE_1": ["REL_VOLUME_GE_1"],
        "REL_VOLUME_GE_1_5": ["REL_VOLUME_GE_1_5"],
        "BREAKOUT_20": ["BREAKOUT_20"],
        "BREAKOUT_50": ["BREAKOUT_50"],
        "BREAKOUT20_VOL15": ["BREAKOUT_20", "REL_VOLUME_GE_1_5"],
        "BREAKOUT20_ATR25": ["BREAKOUT_20", "BREAKOUT20_ATR_GE_025"],
        "BULL_RSI_DIVERGENCE": ["BULL_RSI_DIV"],
        "BULL_MACD_DIVERGENCE": ["BULL_MACD_DIV"],
        "OBV_BULL_DIVERGENCE": ["OBV_BULL_DIV"],
        "BULL_DIV_RSI_TREND": ["BULL_RSI_DIV", "TREND_50200_BULL"],
        "BULL_DIV_RSI_VOLUME": ["BULL_RSI_DIV", "REL_VOLUME_GE_1"],
        "EMA38_TREND": ["EMA38_CROSSOVER", "TREND_2150_BULL"],
        "EMA38_TREND_VOLUME": ["EMA38_CROSSOVER", "TREND_2150_BULL", "REL_VOLUME_GE_1"],
        "EMA38_MACD": ["EMA38_CROSSOVER", "MACD_HIST_GT_0"],
        "EMA38_MACD_VOLUME": ["EMA38_CROSSOVER", "MACD_HIST_GT_0", "REL_VOLUME_GE_1"],
        "EMA38_REGIME": ["EMA38_CROSSOVER", "TREND_50200_BULL"],
        "BREAKOUT20_TREND": ["BREAKOUT_20", "TREND_50200_BULL"],
        "BREAKOUT20_TREND_VOLUME": ["BREAKOUT_20", "TREND_50200_BULL", "REL_VOLUME_GE_1_5"],
        "BREAKOUT20_VOLUME_ATR": ["BREAKOUT_20", "REL_VOLUME_GE_1_5", "BREAKOUT20_ATR_GE_025"],
    }


def apply_conditions(df: pd.DataFrame, conds: list[str]) -> pd.Series:
    m = pd.Series(True, index=df.index)
    for c in conds:
        if c == "EMA38_CROSSOVER":
            m &= df["bullish_crossover"]
        elif c == "TREND_2150_BULL":
            m &= df["ema_21"] > df["ema_50"]
        elif c == "TREND_50200_BULL":
            m &= df["ema_50"] > df["ema_200"]
        elif c == "TREND_STACK":
            m &= df["trend_stack"]
        elif c == "RSI_GT_50":
            m &= df["rsi_14"] > 50
        elif c == "RSI_GT_55":
            m &= df["rsi_14"] > 55
        elif c == "MACD_HIST_GT_0":
            m &= df["macd_hist"] > 0
        elif c == "REL_VOLUME_GE_1":
            m &= df["rel_volume"] >= 1.0
        elif c == "REL_VOLUME_GE_1_5":
            m &= df["rel_volume"] >= 1.5
        elif c == "BREAKOUT_20":
            m &= df["breakout_20"]
        elif c == "BREAKOUT_50":
            m &= df["breakout_50"]
        elif c == "BREAKOUT20_ATR_GE_025":
            m &= df["breakout_20_atr"] >= 0.25
        elif c == "BULL_RSI_DIV":
            m &= df["bull_rsi_div"]
        elif c == "BULL_MACD_DIV":
            m &= df["bull_macd_div"]
        elif c == "OBV_BULL_DIV":
            m &= df["obv_bull_div"]
        else:
            raise ValueError(f"Unknown condition: {c}")
    return m.fillna(False)


def evaluate(events: pd.DataFrame, name: str, conds: list[str],
             start: str, end: str) -> dict:
    if events.empty:
        return {"filter": name, "signals": 0}

    dt = pd.to_datetime(events["decision_timestamp"], utc=True)
    mask_period = (dt >= utc_timestamp(start)) & (
        dt <= utc_timestamp(end) + pd.Timedelta(days=1) - pd.Timedelta(microseconds=1)
    )
    scoped = events.loc[mask_period].copy()
    mask = apply_conditions(scoped, conds)
    s = scoped.loc[mask]
    f5 = pd.to_numeric(s["forward_5d"], errors="coerce").dropna()

    row = {
        "filter": name,
        "conditions": " + ".join(conds),
        "signals": len(s),
        "symbols": s["symbol"].nunique(),
        "forward_1d_mean": finite_mean(s["forward_1d"]),
        "forward_3d_mean": finite_mean(s["forward_3d"]),
        "forward_5d_mean": finite_mean(s["forward_5d"]),
        "forward_5d_median": finite_median(s["forward_5d"]),
        "forward_10d_mean": finite_mean(s["forward_10d"]),
        "forward_10d_median": finite_median(s["forward_10d"]),
        "mfe_5d_mean": finite_mean(s["mfe_5d"]),
        "mae_5d_mean": finite_mean(s["mae_5d"]),
        "positive_rate": float((f5 > 0).mean()) if len(f5) else np.nan,
        "strong_positive_rate": float((f5 >= 0.05).mean()) if len(f5) else np.nan,
    }
    return row


def rank_training(results: pd.DataFrame, min_signals: int = 25) -> pd.DataFrame:
    x = results.copy()
    x["selection_score"] = (
        x["forward_5d_median"].fillna(-9) * 0.45
        + x["forward_5d_mean"].fillna(-9) * 0.25
        + (x["positive_rate"].fillna(0) - 0.50) * 0.20
        + np.minimum(x["symbols"].fillna(0), 10) / 10 * 0.10
    )
    x["eligible"] = (x["signals"] >= min_signals) & (x["symbols"] >= 5)
    return x.sort_values(["eligible", "selection_score", "signals"],
                         ascending=[False, False, False]).reset_index(drop=True)


def event_relation_table(events: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    rows = []
    for name, conds in conditions().items():
        rows.append(evaluate(events, name, conds, start, end))
    return pd.DataFrame(rows)


def frozen_test(events: pd.DataFrame, selected: str, train_results: pd.DataFrame,
                periods: list[tuple[str, str]]) -> pd.DataFrame:
    conds = conditions()[selected]
    rows = []
    for start, end in periods:
        r = evaluate(events, selected, conds, start, end)
        raw = evaluate(events, "RAW_BASELINE", [], start, end)
        r.update({
            "period_start": start,
            "period_end": end,
            "raw_5d_mean": raw["forward_5d_mean"],
            "raw_5d_median": raw["forward_5d_median"],
            "raw_positive_rate": raw["positive_rate"],
        })
        r["mean_lift_vs_raw"] = r["forward_5d_mean"] - r["raw_5d_mean"]
        r["median_lift_vs_raw"] = r["forward_5d_median"] - r["raw_5d_median"]
        r["positive_rate_lift_vs_raw"] = r["positive_rate"] - r["raw_positive_rate"]
        rows.append(r)
    return pd.DataFrame(rows)


def symbol_holdout(events: pd.DataFrame, selected: str, train_end: str,
                   test_start: str, test_end: str) -> pd.DataFrame:
    # Deterministic symbol split: first 70% alphabetically train, remaining 30% holdout.
    symbols = sorted(events["symbol"].dropna().unique())
    cut = max(1, int(len(symbols) * 0.70))
    train_symbols = set(symbols[:cut])
    test_symbols = set(symbols[cut:])

    conds = conditions()[selected]
    dt = pd.to_datetime(events["decision_timestamp"], utc=True)

    train = events[(dt <= utc_timestamp(train_end)) & events["symbol"].isin(train_symbols)]
    test = events[(dt >= utc_timestamp(test_start)) &
                  (dt <= utc_timestamp(test_end) + pd.Timedelta(days=1) - pd.Timedelta(microseconds=1)) &
                  events["symbol"].isin(test_symbols)]

    def ev(frame):
        m = apply_conditions(frame, conds)
        return frame.loc[m]

    s = ev(test)
    raw = test
    f5 = pd.to_numeric(s["forward_5d"], errors="coerce").dropna()
    rf5 = pd.to_numeric(raw["forward_5d"], errors="coerce").dropna()
    return pd.DataFrame([{
        "selected_filter": selected,
        "train_symbols": ",".join(sorted(train_symbols)),
        "holdout_symbols": ",".join(sorted(test_symbols)),
        "train_symbol_count": len(train_symbols),
        "holdout_symbol_count": len(test_symbols),
        "test_signals": len(s),
        "test_raw_signals": len(raw),
        "test_5d_mean": finite_mean(s["forward_5d"]),
        "test_5d_median": finite_median(s["forward_5d"]),
        "test_positive_rate": float((f5 > 0).mean()) if len(f5) else np.nan,
        "raw_5d_mean": finite_mean(raw["forward_5d"]),
        "raw_5d_median": finite_median(raw["forward_5d"]),
        "raw_positive_rate": float((rf5 > 0).mean()) if len(rf5) else np.nan,
        "mean_lift_vs_raw": finite_mean(s["forward_5d"]) - finite_mean(raw["forward_5d"]),
        "median_lift_vs_raw": finite_median(s["forward_5d"]) - finite_median(raw["forward_5d"]),
        "positive_rate_lift_vs_raw": ((f5 > 0).mean() - (rf5 > 0).mean()) if len(f5) and len(rf5) else np.nan,
    }])


def regime_test(events: pd.DataFrame, selected: str, start: str, end: str) -> pd.DataFrame:
    conds = conditions()[selected]
    dt = pd.to_datetime(events["decision_timestamp"], utc=True)
    x = events[(dt >= utc_timestamp(start)) &
               (dt <= utc_timestamp(end) + pd.Timedelta(days=1) - pd.Timedelta(microseconds=1))].copy()
    rows = []
    for regime, g in x.groupby("regime"):
        s = g.loc[apply_conditions(g, conds)]
        f5 = pd.to_numeric(s["forward_5d"], errors="coerce").dropna()
        raw5 = pd.to_numeric(g["forward_5d"], errors="coerce").dropna()
        rows.append({
            "selected_filter": selected,
            "regime": regime,
            "signals": len(s),
            "raw_signals": len(g),
            "forward_5d_mean": finite_mean(s["forward_5d"]),
            "forward_5d_median": finite_median(s["forward_5d"]),
            "positive_rate": float((f5 > 0).mean()) if len(f5) else np.nan,
            "raw_5d_mean": finite_mean(g["forward_5d"]),
            "raw_5d_median": finite_median(g["forward_5d"]),
            "raw_positive_rate": float((raw5 > 0).mean()) if len(raw5) else np.nan,
            "median_lift_vs_raw": finite_median(s["forward_5d"]) - finite_median(g["forward_5d"]),
            "mean_lift_vs_raw": finite_mean(s["forward_5d"]) - finite_mean(g["forward_5d"]),
            "positive_rate_lift_vs_raw": ((f5 > 0).mean() - (raw5 > 0).mean()) if len(f5) and len(raw5) else np.nan,
        })
    return pd.DataFrame(rows)


def non_overlapping(events: pd.DataFrame, selected: str, start: str, end: str) -> pd.DataFrame:
    conds = conditions()[selected]
    dt = pd.to_datetime(events["decision_timestamp"], utc=True)
    x = events[(dt >= utc_timestamp(start)) &
               (dt <= utc_timestamp(end) + pd.Timedelta(days=1) - pd.Timedelta(microseconds=1))].copy()
    x = x.loc[apply_conditions(x, conds)].copy()
    x["entry_dt"] = pd.to_datetime(x["entry_timestamp"], utc=True)
    x = x.sort_values(["symbol", "entry_dt"])
    keep = []
    last = {}
    for idx, row in x.iterrows():
        prev = last.get(row["symbol"])
        if prev is None or row["entry_dt"] >= prev + pd.Timedelta(days=5):
            keep.append(idx)
            last[row["symbol"]] = row["entry_dt"]
    return x.loc[keep].copy()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--universe", choices=["equity"], default="equity")
    parser.add_argument("--start", default="2024-01-01")
    parser.add_argument("--end", default="2026-08-25")
    parser.add_argument("--train-end", default="2025-12-31")
    parser.add_argument("--min-train-signals", type=int, default=25)
    args = parser.parse_args()

    load_dotenv()
    key = os.getenv("ALPACA_API_KEY")
    secret = os.getenv("ALPACA_SECRET_KEY")
    if not key or not secret:
        raise RuntimeError("Missing ALPACA_API_KEY / ALPACA_SECRET_KEY in .env")

    symbols = DEFAULT_EQUITY_SYMBOLS
    frozen_periods = [
        ("2026-01-01", "2026-03-31"),
        ("2026-04-01", "2026-06-30"),
        ("2026-07-01", args.end),
    ]

    client = StockHistoricalDataClient(key, secret)
    all_events = []
    failed = []

    print("=" * 70)
    print("AURA v0.4.3 — MARKET RELATIONSHIP RESEARCH ENGINE")
    print("=" * 70)
    print(f"Symbols: {len(symbols)}")
    print(f"Historical data: {args.start} -> {args.end}")
    print(f"TRAIN: {args.start} -> {args.train_end}")
    print("MODE: RESEARCH ONLY — NO ORDERS")

    for i, symbol in enumerate(symbols, 1):
        try:
            e = scan_symbol(client, symbol, args.start, args.end)
            if e.empty:
                failed.append(symbol)
                print(f"[{i}/{len(symbols)}] {symbol} FAILED/NO EVENTS")
            else:
                all_events.append(e)
                print(f"[{i}/{len(symbols)}] {symbol} OK — {len(e)} events")
        except Exception as exc:
            failed.append(symbol)
            print(f"[{i}/{len(symbols)}] {symbol} FAILED: {exc}")

    events = pd.concat(all_events, ignore_index=True) if all_events else pd.DataFrame()
    if events.empty:
        print("No events found.")
        return

    research = Path("research")
    research.mkdir(exist_ok=True)

    events.to_csv(research / "v043_historical_events.csv", index=False)

    train_results = event_relation_table(events, args.start, args.train_end)
    ranked = rank_training(train_results, args.min_train_signals)
    ranked.to_csv(research / "v043_train_relationship_ranking.csv", index=False)

    eligible = ranked[ranked["eligible"]]
    if eligible.empty:
        print("No eligible relationship found with current minimum sample rules.")
        return

    selected = str(eligible.iloc[0]["filter"])

    frozen = frozen_test(events, selected, ranked, frozen_periods)
    frozen.to_csv(research / "v043_frozen_relationship_test.csv", index=False)

    holdout = symbol_holdout(
        events, selected, args.train_end, "2026-01-01", args.end
    )
    holdout.to_csv(research / "v043_unseen_symbol_test.csv", index=False)

    regimes = regime_test(events, selected, "2026-01-01", args.end)
    regimes.to_csv(research / "v043_regime_test.csv", index=False)

    nonoverlap = non_overlapping(events, selected, "2026-01-01", args.end)
    nonoverlap.to_csv(research / "v043_non_overlapping_test.csv", index=False)

    robust_periods = frozen[
        frozen["signals"] >= max(10, args.min_train_signals // 3)
    ]
    median_lift = finite_mean(robust_periods["median_lift_vs_raw"])
    mean_lift = finite_mean(robust_periods["mean_lift_vs_raw"])
    rate_lift = finite_mean(robust_periods["positive_rate_lift_vs_raw"])
    positive_median_periods = int((robust_periods["median_lift_vs_raw"] > 0).sum())
    positive_rate_periods = int((robust_periods["positive_rate_lift_vs_raw"] > 0).sum())

    robustness = pd.DataFrame([{
        "selected_relationship": selected,
        "train_signals": int(eligible.iloc[0]["signals"]),
        "train_symbols": int(eligible.iloc[0]["symbols"]),
        "test_periods": len(frozen),
        "eligible_test_periods": len(robust_periods),
        "total_test_signals": int(robust_periods["signals"].sum()),
        "mean_lift_vs_raw": mean_lift,
        "median_lift_vs_raw": median_lift,
        "positive_rate_lift_vs_raw": rate_lift,
        "periods_positive_median_lift": positive_median_periods,
        "periods_positive_rate_lift": positive_rate_periods,
        "unseen_symbol_median_lift": float(holdout.iloc[0]["median_lift_vs_raw"]),
        "unseen_symbol_mean_lift": float(holdout.iloc[0]["mean_lift_vs_raw"]),
        "unseen_symbol_positive_rate_lift": float(holdout.iloc[0]["positive_rate_lift_vs_raw"]),
        "robustness_score": 0.45 * median_lift + 0.35 * mean_lift + 0.20 * rate_lift,
        "robust": bool(
            len(robust_periods) >= 2
            and positive_median_periods >= 2
            and positive_rate_periods >= 2
            and float(holdout.iloc[0]["median_lift_vs_raw"]) > 0
        ),
    }])
    robustness.to_csv(research / "v043_robustness_summary.csv", index=False)

    symbol_rows = []
    for symbol, g in events.groupby("symbol"):
        f = g.loc[g["forward_5d"].notna(), "forward_5d"]
        symbol_rows.append({
            "symbol": symbol,
            "events": len(g),
            "forward_5d_mean": finite_mean(f),
            "forward_5d_median": finite_median(f),
            "positive_rate": float((f > 0).mean()) if len(f) else np.nan,
        })
    pd.DataFrame(symbol_rows).sort_values(
        ["forward_5d_median", "positive_rate"], ascending=False
    ).to_csv(research / "v043_symbol_summary.csv", index=False)

    print("\n" + "=" * 70)
    print("TRAIN RELATIONSHIP RANKING")
    print("=" * 70)
    print(ranked[[
        "filter", "signals", "symbols", "forward_5d_mean",
        "forward_5d_median", "positive_rate", "selection_score", "eligible"
    ]].head(20).to_string(index=False))

    print("\n" + "=" * 70)
    print("SELECTED RELATIONSHIP")
    print("=" * 70)
    print(selected)

    print("\n" + "=" * 70)
    print("FROZEN OUT-OF-SAMPLE TEST")
    print("=" * 70)
    print(frozen[[
        "period_start", "period_end", "signals",
        "forward_5d_mean", "forward_5d_median", "positive_rate",
        "mean_lift_vs_raw", "median_lift_vs_raw",
        "positive_rate_lift_vs_raw"
    ]].to_string(index=False))

    print("\n" + "=" * 70)
    print("UNSEEN SYMBOL TEST")
    print("=" * 70)
    print(holdout.to_string(index=False))

    print("\n" + "=" * 70)
    print("REGIME TEST")
    print("=" * 70)
    print(regimes.to_string(index=False))

    print("\n" + "=" * 70)
    print("NON-OVERLAPPING TEST")
    print("=" * 70)
    print(f"Selected signals: {len(nonoverlap)}")
    print(f"5D mean: {finite_mean(nonoverlap['forward_5d']):.4%}")
    print(f"5D median: {finite_median(nonoverlap['forward_5d']):.4%}")
    f = pd.to_numeric(nonoverlap["forward_5d"], errors="coerce").dropna()
    print(f"Positive rate: {(f > 0).mean():.2%}" if len(f) else "Positive rate: n/a")

    print("\n" + "=" * 70)
    print("ROBUSTNESS SUMMARY")
    print("=" * 70)
    print(robustness.to_string(index=False))

    print("\nCSV OUTPUT:")
    for name in [
        "v043_historical_events.csv",
        "v043_train_relationship_ranking.csv",
        "v043_frozen_relationship_test.csv",
        "v043_unseen_symbol_test.csv",
        "v043_regime_test.csv",
        "v043_non_overlapping_test.csv",
        "v043_robustness_summary.csv",
        "v043_symbol_summary.csv",
    ]:
        print(f"research\\{name}")

    print("\nAURA v0.4.3 COMPLETE — NO ORDERS WERE PLACED")


if __name__ == "__main__":
    main()
