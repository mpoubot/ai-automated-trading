"""
AURA v0.4.1 — Corrected Persistence + Train/Test Validation

RESEARCH ONLY. NO ORDERS ARE PLACED.

Changes from v0.4:
1. Persistence is measured AFTER the EMA 3/8 crossover:
   - persistence_1 = EMA3 remains > EMA8 on the next bar
   - persistence_2 = EMA3 remains > EMA8 for the next 2 bars
   - persistence_3 = EMA3 remains > EMA8 for the next 3 bars
2. Historical signals are split into:
   - TRAIN: 2026-01-01 through 2026-06-30
   - TEST:  2026-07-01 through 2026-08-25
3. Candidate filters are evaluated on TRAIN first.
4. The selected filter is then frozen and evaluated on TEST.
5. Results are written to:
   research/v041_historical_signals.csv
   research/v041_train_filter_performance.csv
   research/v041_test_filter_performance.csv
   research/v041_validation_summary.csv
   research/v041_symbol_performance.csv

Example:
    python historical_signal_quality_scanner_v041.py --universe equity
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

TRAIN_END = pd.Timestamp("2026-06-30", tz="UTC")
TEST_START = pd.Timestamp("2026-07-01", tz="UTC")


def ema(s, span):
    return s.ewm(span=span, adjust=False).mean()


def rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def add_features(df):
    df = df.copy().sort_index()

    df["ema_3"] = ema(df["close"], 3)
    df["ema_8"] = ema(df["close"], 8)
    df["ema_21"] = ema(df["close"], 21)
    df["ema_50"] = ema(df["close"], 50)

    df["rsi_14"] = rsi(df["close"], 14)

    ema12 = ema(df["close"], 12)
    ema26 = ema(df["close"], 26)
    df["macd"] = ema12 - ema26
    df["macd_signal"] = ema(df["macd"], 9)
    df["macd_hist"] = df["macd"] - df["macd_signal"]

    vol_mean = df["volume"].rolling(20, min_periods=5).mean()
    df["rel_volume"] = df["volume"] / vol_mean.replace(0, np.nan)

    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr = tr.rolling(14, min_periods=14).mean()
    df["atr_pct"] = atr / df["close"] * 100

    df["price_acceleration"] = df["close"].pct_change()

    # Raw bullish crossover.
    df["bullish_crossover"] = (
        (df["ema_3"] > df["ema_8"])
        & (df["ema_3"].shift(1) <= df["ema_8"].shift(1))
    )

    df["trend_stack"] = (
        (df["ema_3"] > df["ema_8"])
        & (df["ema_8"] > df["ema_21"])
        & (df["ema_21"] > df["ema_50"])
    )

    # IMPORTANT: post-cross persistence.
    # persistence_N is TRUE only when the next N bars all remain EMA3 > EMA8.
    above = (df["ema_3"] > df["ema_8"]).fillna(False)

    for n in (1, 2, 3):
        future_ok = pd.Series(True, index=df.index)
        for shift in range(1, n + 1):
            future_ok &= above.shift(-shift, fill_value=False)
        df[f"persistence_{n}"] = future_ok

    return df


def fetch_symbol(client, symbol, start, end):
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


def forward_outcomes(df, pos):
    n = len(df)
    if pos + 1 >= n:
        return {}

    entry = float(df.iloc[pos + 1]["open"])
    if not np.isfinite(entry) or entry == 0:
        return {}

    out = {}
    for days in (1, 3, 5, 10):
        idx = pos + days
        out[f"forward_{days}d"] = (
            float(df.iloc[idx]["close"]) / entry - 1
            if idx < n else np.nan
        )

    end = min(pos + 5, n - 1)
    future = df.iloc[pos + 1:end + 1]
    if future.empty:
        out["mfe_5d"] = np.nan
        out["mae_5d"] = np.nan
    else:
        out["mfe_5d"] = float(future["high"].max()) / entry - 1
        out["mae_5d"] = float(future["low"].min()) / entry - 1

    return out


def outcome_label(x):
    if not np.isfinite(x):
        return "UNAVAILABLE"
    if x >= 0.05:
        return "STRONG_POSITIVE"
    if x > 0.01:
        return "POSITIVE"
    if x >= -0.01:
        return "FLAT"
    if x <= -0.05:
        return "STRONG_NEGATIVE"
    return "NEGATIVE"


def scan_symbol(client, symbol, fetch_start, signal_start, end):
    raw = fetch_symbol(client, symbol, fetch_start, end)
    if raw.empty:
        return pd.DataFrame()

    df = add_features(raw)
    # Accept both naive and timezone-aware timestamps.
    # signal_start is already timezone-aware when supplied by main().
    signal_start = pd.Timestamp(signal_start)
    if signal_start.tzinfo is None:
        signal_start = signal_start.tz_localize("UTC")
    else:
        signal_start = signal_start.tz_convert("UTC")

    signal_end = pd.Timestamp(end)
    if signal_end.tzinfo is None:
        signal_end = signal_end.tz_localize("UTC")
    else:
        signal_end = signal_end.tz_convert("UTC")

    positions = np.flatnonzero(df["bullish_crossover"].fillna(False).to_numpy())
    records = []

    for pos in positions:
        ts = df.index[pos]
        if ts < signal_start or ts > signal_end:
            continue

        outcomes = forward_outcomes(df, pos)
        # 5D outcome is required for a comparable validation sample.
        if not np.isfinite(outcomes.get("forward_5d", np.nan)):
            continue

        row = df.iloc[pos]

        records.append({
            "symbol": symbol,
            "signal_timestamp": ts.isoformat(),
            "signal_close": float(row["close"]),
            "rsi_14": float(row["rsi_14"]),
            "rel_volume": float(row["rel_volume"]),
            "macd": float(row["macd"]),
            "macd_signal": float(row["macd_signal"]),
            "macd_hist": float(row["macd_hist"]),
            "atr_pct": float(row["atr_pct"]),
            "price_acceleration": float(row["price_acceleration"]),
            "trend_stack": bool(row["trend_stack"]),
            "persistence_1": bool(row["persistence_1"]),
            "persistence_2": bool(row["persistence_2"]),
            "persistence_3": bool(row["persistence_3"]),
            "ema_3": float(row["ema_3"]),
            "ema_8": float(row["ema_8"]),
            "ema_21": float(row["ema_21"]),
            "ema_50": float(row["ema_50"]),
            **outcomes,
        })

    return pd.DataFrame(records)


def stats_row(name, subset, universe_count):
    if subset.empty:
        return {
            "filter": name,
            "signals": 0,
            "coverage_pct": 0,
            "forward_1d_mean": np.nan,
            "forward_3d_mean": np.nan,
            "forward_5d_mean": np.nan,
            "forward_5d_median": np.nan,
            "forward_10d_mean": np.nan,
            "forward_10d_median": np.nan,
            "mfe_5d_mean": np.nan,
            "mae_5d_mean": np.nan,
            "positive_rate": np.nan,
            "strong_positive_rate": np.nan,
        }

    return {
        "filter": name,
        "signals": len(subset),
        "coverage_pct": len(subset) / universe_count * 100,
        "forward_1d_mean": subset["forward_1d"].mean(),
        "forward_3d_mean": subset["forward_3d"].mean(),
        "forward_5d_mean": subset["forward_5d"].mean(),
        "forward_5d_median": subset["forward_5d"].median(),
        "forward_10d_mean": subset["forward_10d"].mean(),
        "forward_10d_median": subset["forward_10d"].median(),
        "mfe_5d_mean": subset["mfe_5d"].mean(),
        "mae_5d_mean": subset["mae_5d"].mean(),
        "positive_rate": (subset["forward_5d"] > 0).mean(),
        "strong_positive_rate": (subset["forward_5d"] >= 0.05).mean(),
    }


def candidate_masks(df):
    return {
        "RAW_CROSSOVER": pd.Series(True, index=df.index),

        "MACD_HIST_GT_0": df["macd_hist"] > 0,
        "REL_VOLUME_GE_1": df["rel_volume"] >= 1.0,
        "REL_VOLUME_GE_1_2": df["rel_volume"] >= 1.2,
        "RSI_GT_50": df["rsi_14"] > 50,
        "RSI_GT_55": df["rsi_14"] > 55,
        "TREND_STACK": df["trend_stack"],
        "PRICE_ACCEL_GT_0": df["price_acceleration"] > 0,

        "PERSISTENCE_1": df["persistence_1"],
        "PERSISTENCE_2": df["persistence_2"],
        "PERSISTENCE_3": df["persistence_3"],

        "TREND_STACK + MACD_HIST_GT_0":
            df["trend_stack"] & (df["macd_hist"] > 0),

        "TREND_STACK + PERSISTENCE_2":
            df["trend_stack"] & df["persistence_2"],

        "MACD_HIST_GT_0 + PERSISTENCE_2":
            (df["macd_hist"] > 0) & df["persistence_2"],

        "TREND_STACK + MACD_HIST_GT_0 + PERSISTENCE_2":
            df["trend_stack"] & (df["macd_hist"] > 0) & df["persistence_2"],

        "TREND_STACK + MACD_HIST_GT_0 + PERSISTENCE_3":
            df["trend_stack"] & (df["macd_hist"] > 0) & df["persistence_3"],

        "TREND_STACK + MACD_HIST_GT_0 + PRICE_ACCEL_GT_0":
            df["trend_stack"] & (df["macd_hist"] > 0) &
            (df["price_acceleration"] > 0),

        "TREND_STACK + MACD_HIST_GT_0 + PERSISTENCE_2 + PRICE_ACCEL_GT_0":
            df["trend_stack"] & (df["macd_hist"] > 0) &
            df["persistence_2"] & (df["price_acceleration"] > 0),
    }


def evaluate_filters(df):
    masks = candidate_masks(df)
    rows = [
        stats_row(name, df.loc[mask], len(df))
        for name, mask in masks.items()
    ]

    result = pd.DataFrame(rows)

    # Training selection score:
    # median return is primary, positive rate secondary, but a filter must
    # retain at least 10 signals to avoid tiny-sample traps.
    result["eligible"] = result["signals"] >= 10
    result["selection_score"] = np.where(
        result["eligible"],
        result["forward_5d_median"].fillna(-999) * 0.7
        + result["positive_rate"].fillna(0) * 0.3,
        -9999,
    )

    return result.sort_values(
        ["selection_score", "signals"],
        ascending=[False, False],
    ).reset_index(drop=True)


def evaluate_named_filters(df, names):
    masks = candidate_masks(df)
    rows = []
    for name in names:
        if name in masks:
            rows.append(stats_row(name, df.loc[masks[name]], len(df)))
    return pd.DataFrame(rows)


def symbol_summary(df):
    rows = []
    for symbol, g in df.groupby("symbol"):
        rows.append({
            "symbol": symbol,
            "split": g["split"].iloc[0],
            "signals": len(g),
            "forward_5d_mean": g["forward_5d"].mean(),
            "forward_5d_median": g["forward_5d"].median(),
            "forward_10d_mean": g["forward_10d"].mean(),
            "forward_10d_median": g["forward_10d"].median(),
            "positive_rate": (g["forward_5d"] > 0).mean(),
            "mfe_5d_mean": g["mfe_5d"].mean(),
            "mae_5d_mean": g["mae_5d"].mean(),
        })
    return pd.DataFrame(rows).sort_values(
        ["split", "forward_5d_median"],
        ascending=[True, False],
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", nargs="+")
    parser.add_argument("--universe", choices=["equity"], default=None)
    parser.add_argument("--start", default="2026-01-01")
    parser.add_argument("--end", default="2026-08-25")
    parser.add_argument("--train-end", default="2026-06-30")
    args = parser.parse_args()

    load_dotenv()

    api_key = os.getenv("ALPACA_API_KEY")
    secret_key = os.getenv("ALPACA_SECRET_KEY")
    if not api_key or not secret_key:
        raise SystemExit(
            "Missing ALPACA_API_KEY / ALPACA_SECRET_KEY in .env"
        )

    symbols = args.symbols or DEFAULT_EQUITY_SYMBOLS

    signal_start = pd.Timestamp(args.start, tz="UTC")
    signal_end = pd.Timestamp(args.end, tz="UTC")
    train_end = pd.Timestamp(args.train_end, tz="UTC")

    # Fetch extra history so EMA50/RSI/ATR are properly warmed up.
    fetch_start = (signal_start - pd.Timedelta(days=100)).strftime("%Y-%m-%d")

    output = Path("research")
    output.mkdir(exist_ok=True)

    client = StockHistoricalDataClient(api_key, secret_key)

    print("=" * 72)
    print("AURA v0.4.1 — CORRECTED PERSISTENCE + TRAIN/TEST")
    print("=" * 72)
    print(f"Symbols:     {len(symbols)}")
    print(f"Signal data: {signal_start.date()} -> {signal_end.date()}")
    print(f"TRAIN:       -> {train_end.date()}")
    print(f"TEST:        {(train_end + pd.Timedelta(days=1)).date()} -> {signal_end.date()}")
    print("Mode:        RESEARCH ONLY — NO ORDERS")
    print()

    frames = []
    failures = []

    for i, symbol in enumerate(symbols, 1):
        print(f"[{i:>2}/{len(symbols)}] {symbol:<6}", end=" ")
        try:
            result = scan_symbol(
                client,
                symbol,
                fetch_start,
                signal_start,
                signal_end.strftime("%Y-%m-%d"),
            )
            if result.empty:
                print("no qualifying signals")
            else:
                print(f"{len(result)} signals")
                frames.append(result)
        except Exception as exc:
            print(f"FAILED: {exc}")
            failures.append((symbol, str(exc)))

    if not frames:
        raise SystemExit("No historical signals found.")

    signals = pd.concat(frames, ignore_index=True)
    signals["signal_timestamp_dt"] = pd.to_datetime(
        signals["signal_timestamp"], utc=True
    )
    signals["split"] = np.where(
        signals["signal_timestamp_dt"] <= train_end,
        "TRAIN",
        "TEST",
    )

    # A signal's forward outcomes are calculated using only future bars.
    # The split is based on signal date, so the TEST signals are never used
    # to select the filter.
    train = signals[signals["split"] == "TRAIN"].copy()
    test = signals[signals["split"] == "TEST"].copy()

    train_perf = evaluate_filters(train)

    # Select ONLY from TRAIN. Require >=10 signals.
    eligible = train_perf[train_perf["eligible"]]
    if eligible.empty:
        raise SystemExit("No training filter has >=10 signals.")

    selected = eligible.iloc[0]["filter"]

    # For validation, freeze the selected training filter and evaluate it
    # unchanged on TEST. Include baseline for direct comparison.
    validation_names = [
        "RAW_CROSSOVER",
        "MACD_HIST_GT_0",
        "TREND_STACK + MACD_HIST_GT_0",
        selected,
    ]
    # Preserve order while removing duplicates.
    validation_names = list(dict.fromkeys(validation_names))

    test_perf = evaluate_named_filters(test, validation_names)

    selected_train = train_perf[train_perf["filter"] == selected].iloc[0]
    selected_test = test_perf[test_perf["filter"] == selected]
    raw_train = train_perf[train_perf["filter"] == "RAW_CROSSOVER"].iloc[0]
    raw_test = test_perf[test_perf["filter"] == "RAW_CROSSOVER"].iloc[0]

    validation = pd.DataFrame([{
        "selected_filter": selected,
        "train_signals": selected_train["signals"],
        "train_5d_mean": selected_train["forward_5d_mean"],
        "train_5d_median": selected_train["forward_5d_median"],
        "train_positive_rate": selected_train["positive_rate"],
        "test_signals": (
            int(selected_test.iloc[0]["signals"])
            if not selected_test.empty else 0
        ),
        "test_5d_mean": (
            selected_test.iloc[0]["forward_5d_mean"]
            if not selected_test.empty else np.nan
        ),
        "test_5d_median": (
            selected_test.iloc[0]["forward_5d_median"]
            if not selected_test.empty else np.nan
        ),
        "test_positive_rate": (
            selected_test.iloc[0]["positive_rate"]
            if not selected_test.empty else np.nan
        ),
        "raw_train_5d_median": raw_train["forward_5d_median"],
        "raw_test_5d_median": raw_test["forward_5d_median"],
        "test_median_lift_vs_raw": (
            selected_test.iloc[0]["forward_5d_median"]
            - raw_test["forward_5d_median"]
            if not selected_test.empty else np.nan
        ),
        "test_positive_rate_lift_vs_raw": (
            selected_test.iloc[0]["positive_rate"]
            - raw_test["positive_rate"]
            if not selected_test.empty else np.nan
        ),
        "train_end": train_end.date().isoformat(),
        "test_start": (train_end + pd.Timedelta(days=1)).date().isoformat(),
    }])

    # Remove helper datetime before writing the main signal CSV.
    signals_out = signals.drop(columns=["signal_timestamp_dt"])
    signals_out.to_csv(output / "v041_historical_signals.csv", index=False)
    train_perf.to_csv(output / "v041_train_filter_performance.csv", index=False)
    test_perf.to_csv(output / "v041_test_filter_performance.csv", index=False)
    validation.to_csv(output / "v041_validation_summary.csv", index=False)
    symbol_summary(signals_out).to_csv(
        output / "v041_symbol_performance.csv", index=False
    )

    print()
    print("=" * 72)
    print("TRAINING FILTER RANKING")
    print("=" * 72)
    print(train_perf[[
        "filter", "signals", "forward_5d_mean",
        "forward_5d_median", "positive_rate",
        "selection_score", "eligible"
    ]].head(15).to_string(index=False))

    print()
    print("=" * 72)
    print("FROZEN OUT-OF-SAMPLE TEST")
    print("=" * 72)
    print(test_perf[[
        "filter", "signals", "forward_5d_mean",
        "forward_5d_median", "positive_rate"
    ]].to_string(index=False))

    print()
    print("=" * 72)
    print("VALIDATION SUMMARY")
    print("=" * 72)
    print(validation.to_string(index=False))

    print()
    print("CSV OUTPUT:")
    for name in (
        "v041_historical_signals.csv",
        "v041_train_filter_performance.csv",
        "v041_test_filter_performance.csv",
        "v041_validation_summary.csv",
        "v041_symbol_performance.csv",
    ):
        print(output / name)

    if failures:
        print()
        print("FAILED SYMBOLS:")
        for symbol, reason in failures:
            print(f"{symbol}: {reason}")

    print()
    print("=" * 72)
    print("AURA v0.4.1 COMPLETE — NO ORDERS WERE PLACED")
    print("=" * 72)


if __name__ == "__main__":
    main()
