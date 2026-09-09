"""
AURA v0.4 — Historical Signal Quality Scanner

Research only. NO ORDERS are placed.

Purpose:
- Rebuild the raw EMA 3/8 bullish crossover signal from historical daily bars.
- Calculate the signal-quality features used by AURA.
- Evaluate individual filters and combinations historically.
- Produce:
    research/v04_historical_signals.csv
    research/v04_filter_performance.csv
    research/v04_symbol_performance.csv

Designed to run inside the existing aura-autonomous-trading-agent project.

Example:
    python historical_signal_quality_scanner_v04.py ^
      --symbols AAPL MSFT JNJ IWM LLY AVGO ^
      --start 2026-01-01 --end 2026-08-25

Or use the same 27-symbol universe:
    python historical_signal_quality_scanner_v04.py --universe all
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from itertools import combinations

import numpy as np
import pandas as pd
from dotenv import load_dotenv

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import DataFeed


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_SYMBOLS = [
    "2Z", "A7A5", "AAVE", "ADA", "AERO", "AKE", "AKT", "ALGO", "APE",
    "APEPE", "APT", "APXUSD", "APYUSD", "AR", "ARB", "ASTER", "ATOM",
    "AUSD", "AVAX", "AXS", "B", "BCAP", "BCH", "BDX", "BFUSD", "BGB",
    "BNB", "BONK", "BORG", "BSV", "BTC", "BTT", "BTW", "BUILD", "CAKE",
    "CASHCAT", "CC", "CFX", "CHZ", "COCO", "COMP", "CRO", "CRV",
    "CRVUSD", "CVX", "DAI", "DASH", "DCR", "DOGE", "DOT", "DRV", "EIGEN",
    "ENA", "ENS", "ETC", "ETH", "ETHFI", "EURC", "EURCV", "EURSAFO",
    "EUTBL", "FARTCOIN", "FDUSD", "FET", "FF", "FIGR_HELOC", "FIL", "FLOKI",
    "FLR", "FRAX", "GHO", "GNO", "GRAM", "GRASS", "GRT", "GT", "GUSD",
    "H", "HASH", "HBAR", "HTX", "HYPE", "ICP", "INJ", "IOTA", "JAAA",
    "JASMY", "JST", "JTO", "JTRSY", "JUP", "KAG", "KAIA", "KAS", "KAU",
    "KCS", "KITE", "KMNO", "KOGE", "LDO", "LEO", "LINK", "LIT", "LTC",
    "LUNC", "M", "MANA", "META", "MNT", "MON", "MORPHO", "MX", "NEAR",
    "NEO", "NEXO", "NFT", "NIGHT", "OHM", "OKB", "ONDO", "ONYC", "OP",
    "OUSG", "PAXG", "PC0000031", "PC0000033", "PENDLE", "PENGU", "PEPE",
    "PI", "PIEVERSE", "POL", "PUMP", "PYTH", "PYUSD", "QNT", "RAIN", "RAY",
    "REAL", "RENDER", "REUSD", "RLUSD", "RUNE", "SAFO", "SEI", "SHIB",
    "SKY", "SOFID", "SOL", "SPX", "STABLE", "STRCX", "STRK", "STX", "SUI",
    "SUN", "SYRUP", "TAO", "TEL", "THETA", "TIA", "TIBBIR", "TRAC", "TRUMP",
    "TRX", "TUSD", "TWT", "U", "UB", "ULTIMA", "UNI", "USAT", "USD0",
    "USD1", "USDAI", "USDC", "USDD", "USDE", "USDF", "USDG", "USDGO",
    "USDS", "USDT", "USDTB", "USDY", "USTB", "USTBL", "USX", "USYC",
    "VELVET", "VET", "VIRTUAL", "VSN", "VVV",
]

# The equity symbols from the v0.3 result shown in the conversation.
DEFAULT_EQUITY_SYMBOLS = [
    "AAPL", "MSFT", "AVGO", "JNJ", "IWM", "CVX", "LLY", "CAT", "SPY",
    "RTX", "AMZN", "XOM", "TSLA", "BAC", "COST", "GS", "GE", "DE", "WMT",
    "NVDA", "QQQ", "LMT", "GLD", "JPM", "META", "GOOGL", "SLV",
]


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

    # True range / ATR percentage.
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = tr.rolling(14, min_periods=14).mean()
    df["atr_pct"] = atr / df["close"] * 100.0

    # Percentage change over one bar.
    df["price_acceleration"] = df["close"].pct_change()

    # Number of consecutive bars where EMA3 > EMA8.
    above = df["ema_3"] > df["ema_8"]
    persistence = []
    count = 0
    for value in above:
        count = count + 1 if value else 0
        persistence.append(count)
    df["persistence_bars"] = persistence

    # A raw bullish crossover occurs on the first bar EMA3 moves above EMA8.
    df["bullish_crossover"] = (
        (df["ema_3"] > df["ema_8"])
        & (df["ema_3"].shift(1) <= df["ema_8"].shift(1))
    )

    df["trend_stack"] = (
        (df["ema_3"] > df["ema_8"])
        & (df["ema_8"] > df["ema_21"])
        & (df["ema_21"] > df["ema_50"])
    )

    return df


def add_outcomes(df: pd.DataFrame, signal_index: int) -> dict:
    """Calculate forward close returns and 5-day MFE/MAE from next day's open."""
    n = len(df)
    row = df.iloc[signal_index]

    # Entry proxy = next trading day's open, matching the scanner's
    # "next trading day's OPEN" convention.
    if signal_index + 1 >= n:
        return {}

    entry = float(df.iloc[signal_index + 1]["open"])
    if not np.isfinite(entry) or entry == 0:
        return {}

    out = {}

    for days in (1, 3, 5, 10):
        target_idx = signal_index + days
        if target_idx < n:
            close = float(df.iloc[target_idx]["close"])
            out[f"forward_{days}d"] = close / entry - 1.0
        else:
            out[f"forward_{days}d"] = np.nan

    # MFE/MAE over the next 5 trading sessions, measured from next open.
    end = min(signal_index + 5, n - 1)
    future = df.iloc[signal_index + 1 : end + 1]

    if len(future):
        out["mfe_5d"] = future["high"].max() / entry - 1.0
        out["mae_5d"] = future["low"].min() / entry - 1.0
    else:
        out["mfe_5d"] = np.nan
        out["mae_5d"] = np.nan

    return out


def classify_outcome(ret5: float) -> str:
    if not np.isfinite(ret5):
        return "UNAVAILABLE"
    if ret5 >= 0.05:
        return "STRONG_POSITIVE"
    if ret5 > 0.01:
        return "POSITIVE"
    if ret5 >= -0.01:
        return "FLAT"
    if ret5 <= -0.05:
        return "STRONG_NEGATIVE"
    return "NEGATIVE"


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
    for col in needed:
        if col not in bars.columns:
            return pd.DataFrame()

    return bars[needed].sort_index()


def scan_symbol(client, symbol: str, start: str, end: str) -> pd.DataFrame:
    raw = fetch_symbol(client, symbol, start, end)
    if raw.empty:
        return pd.DataFrame()

    df = add_features(raw)

    records = []
    signal_positions = np.flatnonzero(df["bullish_crossover"].fillna(False).to_numpy())

    for pos in signal_positions:
        row = df.iloc[pos]
        outcomes = add_outcomes(df, pos)

        # We only keep signals for which at least a 5D outcome exists.
        if not np.isfinite(outcomes.get("forward_5d", np.nan)):
            continue

        record = {
            "symbol": symbol,
            "signal_timestamp": df.index[pos].isoformat(),
            "signal_close": float(row["close"]),
            "signal_score": np.nan,  # v0.4 researches the raw signal + filters.
            "signal_status": "RAW_CROSSOVER",
            "persistence_bars": int(row["persistence_bars"]),
            "rsi_14": float(row["rsi_14"]),
            "rel_volume": float(row["rel_volume"]),
            "macd": float(row["macd"]),
            "macd_signal": float(row["macd_signal"]),
            "macd_hist": float(row["macd_hist"]),
            "atr_pct": float(row["atr_pct"]),
            "price_acceleration": float(row["price_acceleration"]),
            "trend_stack": bool(row["trend_stack"]),
            "ema_3": float(row["ema_3"]),
            "ema_8": float(row["ema_8"]),
            "ema_21": float(row["ema_21"]),
            "ema_50": float(row["ema_50"]),
            **outcomes,
        }

        record["outcome"] = classify_outcome(record["forward_5d"])
        records.append(record)

    return pd.DataFrame(records)


def safe_mean(s):
    return float(s.mean()) if len(s) else np.nan


def safe_median(s):
    return float(s.median()) if len(s) else np.nan


def evaluate_filter(name: str, subset: pd.DataFrame, universe: pd.DataFrame) -> dict:
    if subset.empty:
        return {
            "filter": name,
            "signals": 0,
            "coverage_pct": 0.0,
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
        "coverage_pct": len(subset) / len(universe) * 100.0,
        "forward_1d_mean": safe_mean(subset["forward_1d"]),
        "forward_3d_mean": safe_mean(subset["forward_3d"]),
        "forward_5d_mean": safe_mean(subset["forward_5d"]),
        "forward_5d_median": safe_median(subset["forward_5d"]),
        "forward_10d_mean": safe_mean(subset["forward_10d"]),
        "forward_10d_median": safe_median(subset["forward_10d"]),
        "mfe_5d_mean": safe_mean(subset["mfe_5d"]),
        "mae_5d_mean": safe_mean(subset["mae_5d"]),
        "positive_rate": float((subset["forward_5d"] > 0).mean()),
        "strong_positive_rate": float((subset["forward_5d"] >= 0.05).mean()),
    }


def build_filter_tests(df: pd.DataFrame) -> pd.DataFrame:
    tests = {
        "RAW_CROSSOVER": df.index.to_series().map(lambda _: True).astype(bool),

        "TREND_STACK": df["trend_stack"],
        "RSI_GT_50": df["rsi_14"] > 50,
        "RSI_GT_55": df["rsi_14"] > 55,

        "REL_VOLUME_GE_1": df["rel_volume"] >= 1.0,
        "REL_VOLUME_GE_1_2": df["rel_volume"] >= 1.2,

        "MACD_HIST_GT_0": df["macd_hist"] > 0,
        "PRICE_ACCEL_GT_0": df["price_acceleration"] > 0,

        "PERSISTENCE_GE_2": df["persistence_bars"] >= 2,
        "PERSISTENCE_GE_3": df["persistence_bars"] >= 3,

        # These are deliberately broad volatility gates, not trade rules.
        "ATR_PCT_LE_4": df["atr_pct"] <= 4.0,
        "ATR_PCT_LE_3": df["atr_pct"] <= 3.0,
    }

    results = []
    for name, mask in tests.items():
        results.append(evaluate_filter(name, df.loc[mask], df))

    # Small, interpretable combinations. We deliberately avoid an enormous
    # combinatorial search that could overfit this relatively small sample.
    combo_groups = [
        ["TREND_STACK", "RSI_GT_50"],
        ["TREND_STACK", "MACD_HIST_GT_0"],
        ["TREND_STACK", "PRICE_ACCEL_GT_0"],
        ["TREND_STACK", "REL_VOLUME_GE_1"],
        ["TREND_STACK", "PERSISTENCE_GE_2"],
        ["RSI_GT_50", "MACD_HIST_GT_0"],
        ["RSI_GT_50", "REL_VOLUME_GE_1"],
        ["MACD_HIST_GT_0", "PRICE_ACCEL_GT_0"],
        ["PERSISTENCE_GE_2", "PRICE_ACCEL_GT_0"],
        ["TREND_STACK", "RSI_GT_50", "MACD_HIST_GT_0"],
        ["TREND_STACK", "RSI_GT_50", "REL_VOLUME_GE_1"],
        ["TREND_STACK", "RSI_GT_50", "PERSISTENCE_GE_2"],
        ["TREND_STACK", "MACD_HIST_GT_0", "PRICE_ACCEL_GT_0"],
        ["TREND_STACK", "RSI_GT_50", "MACD_HIST_GT_0", "PERSISTENCE_GE_2"],
        ["TREND_STACK", "RSI_GT_50", "MACD_HIST_GT_0", "PRICE_ACCEL_GT_0"],
        ["TREND_STACK", "RSI_GT_50", "REL_VOLUME_GE_1", "PERSISTENCE_GE_2"],
        ["TREND_STACK", "RSI_GT_50", "MACD_HIST_GT_0", "PRICE_ACCEL_GT_0", "PERSISTENCE_GE_2"],
    ]

    masks_by_name = {
        "TREND_STACK": tests["TREND_STACK"],
        "RSI_GT_50": tests["RSI_GT_50"],
        "MACD_HIST_GT_0": tests["MACD_HIST_GT_0"],
        "PRICE_ACCEL_GT_0": tests["PRICE_ACCEL_GT_0"],
        "REL_VOLUME_GE_1": tests["REL_VOLUME_GE_1"],
        "PERSISTENCE_GE_2": tests["PERSISTENCE_GE_2"],
    }

    for combo in combo_groups:
        mask = pd.Series(True, index=df.index)
        for part in combo:
            mask &= masks_by_name[part]
        name = " + ".join(combo)
        results.append(evaluate_filter(name, df.loc[mask], df))

    result = pd.DataFrame(results)

    # Rank primarily by median 5D return, then positive rate, while retaining
    # signal count so tiny samples are visible rather than hidden.
    result["quality_rank"] = (
        result["forward_5d_median"].fillna(-999) * 0.6
        + result["positive_rate"].fillna(0) * 0.4
    )
    result = result.sort_values(
        ["quality_rank", "signals"],
        ascending=[False, False],
    ).reset_index(drop=True)

    return result


def build_symbol_summary(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for symbol, g in df.groupby("symbol"):
        rows.append({
            "symbol": symbol,
            "signals": len(g),
            "forward_5d_mean": g["forward_5d"].mean(),
            "forward_5d_median": g["forward_5d"].median(),
            "forward_10d_mean": g["forward_10d"].mean(),
            "forward_10d_median": g["forward_10d"].median(),
            "mfe_5d_mean": g["mfe_5d"].mean(),
            "mae_5d_mean": g["mae_5d"].mean(),
            "positive_rate": (g["forward_5d"] > 0).mean(),
            "strong_positive_rate": (g["forward_5d"] >= 0.05).mean(),
        })

    return pd.DataFrame(rows).sort_values(
        ["forward_5d_median", "positive_rate"],
        ascending=[False, False],
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", nargs="+")
    parser.add_argument("--universe", choices=["equity"], default=None)
    parser.add_argument("--start", default="2026-01-01")
    parser.add_argument("--end", default="2026-08-25")
    args = parser.parse_args()

    load_dotenv()

    api_key = os.getenv("ALPACA_API_KEY")
    secret_key = os.getenv("ALPACA_SECRET_KEY")

    if not api_key or not secret_key:
        raise SystemExit(
            "Missing ALPACA_API_KEY / ALPACA_SECRET_KEY in .env"
        )

    if args.symbols:
        symbols = args.symbols
    elif args.universe == "equity":
        symbols = DEFAULT_EQUITY_SYMBOLS
    else:
        # Start with the equity universe because it is the cleanest
        # apples-to-apples validation set from v0.3.
        symbols = DEFAULT_EQUITY_SYMBOLS

    output_dir = Path("research")
    output_dir.mkdir(exist_ok=True)

    client = StockHistoricalDataClient(api_key, secret_key)

    print("=" * 72)
    print("AURA v0.4 — HISTORICAL SIGNAL QUALITY SCANNER")
    print("=" * 72)
    print(f"Symbols: {len(symbols)}")
    print(f"Period:  {args.start} -> {args.end}")
    print("Signal:  bullish EMA 3/8 crossover")
    print("Mode:    RESEARCH ONLY — NO ORDERS")
    print()

    all_frames = []
    failed = []

    for i, symbol in enumerate(symbols, start=1):
        print(f"[{i:>2}/{len(symbols)}] {symbol:<10}", end=" ")

        try:
            result = scan_symbol(client, symbol, args.start, args.end)
            if result.empty:
                print("no qualifying historical signals")
                continue

            print(f"{len(result)} signals")
            all_frames.append(result)
        except Exception as exc:
            print(f"FAILED: {exc}")
            failed.append((symbol, str(exc)))

    if not all_frames:
        raise SystemExit("No historical signals were found.")

    signals = pd.concat(all_frames, ignore_index=True)
    signals = signals.sort_values(["symbol", "signal_timestamp"]).reset_index(drop=True)

    filter_perf = build_filter_tests(signals)
    symbol_perf = build_symbol_summary(signals)

    signal_path = output_dir / "v04_historical_signals.csv"
    filter_path = output_dir / "v04_filter_performance.csv"
    symbol_path = output_dir / "v04_symbol_performance.csv"

    signals.to_csv(signal_path, index=False)
    filter_perf.to_csv(filter_path, index=False)
    symbol_perf.to_csv(symbol_path, index=False)

    print()
    print("=" * 72)
    print("AURA v0.4 — RESULTS")
    print("=" * 72)
    print(f"Symbols successfully scanned: {signals['symbol'].nunique()}")
    print(f"Symbols failed:               {len(failed)}")
    print(f"Historical signals:           {len(signals)}")
    print()
    print("TOP FILTERS BY QUALITY RANK")
    print("-" * 72)

    display_cols = [
        "filter", "signals", "coverage_pct",
        "forward_5d_mean", "forward_5d_median",
        "forward_10d_mean", "positive_rate",
        "mfe_5d_mean", "mae_5d_mean",
    ]
    print(filter_perf[display_cols].head(15).to_string(index=False))

    print()
    print("TOP SYMBOLS")
    print("-" * 72)
    print(symbol_perf.head(15).to_string(index=False))

    print()
    print("CSV OUTPUT")
    print(signal_path)
    print(filter_path)
    print(symbol_path)

    if failed:
        print()
        print("FAILED SYMBOLS")
        for symbol, reason in failed:
            print(f"{symbol}: {reason}")

    print()
    print("=" * 72)
    print("AURA v0.4 COMPLETE — NO ORDERS WERE PLACED")
    print("=" * 72)


if __name__ == "__main__":
    main()
