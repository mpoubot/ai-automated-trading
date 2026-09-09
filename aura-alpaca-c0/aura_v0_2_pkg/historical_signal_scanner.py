import argparse
import os
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

from technical_agent import build_features
from signal_validator import evaluate_signal


OUTPUT_DIR = Path("research")


def load_data(symbol: str, start: str, end: str) -> pd.DataFrame:
    load_dotenv()
    key = os.getenv("ALPACA_API_KEY")
    secret = os.getenv("ALPACA_SECRET_KEY")
    if not key or not secret:
        raise RuntimeError("Missing Alpaca credentials in .env")

    client = StockHistoricalDataClient(key, secret)
    req = StockBarsRequest(
        symbol_or_symbols=[symbol],
        timeframe=TimeFrame.Day,
        start=start,
        end=end,
        feed="iex",
    )
    df = client.get_stock_bars(req).df
    if df.empty:
        raise RuntimeError(f"No market data returned for {symbol}")
    if hasattr(df.index, "levels") and len(df.index.levels) > 1:
        df = df.xs(symbol, level="symbol")
    return df.sort_index()


def classify_signal(forward_1, forward_3, forward_5, forward_10, mae_5):
    """Simple research labels; these are deliberately not trading rules."""
    if pd.isna(forward_5):
        return "INCOMPLETE"
    if forward_5 >= 0.02 and (pd.isna(mae_5) or mae_5 > -0.02):
        return "STRONG_POSITIVE"
    if forward_5 >= 0.01:
        return "POSITIVE"
    if forward_5 <= -0.02:
        return "NEGATIVE"
    if forward_1 > 0 and forward_3 < 0 and forward_5 <= 0:
        return "WHIPSAW"
    return "FLAT"


def scan_signals(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    rows = []
    min_history = 55
    horizon = 10

    for i in range(min_history, len(df)):
        row = df.iloc[i]
        if not bool(row.get("cross_3_8", False)):
            continue

        prefix = df.iloc[: i + 1]
        result = evaluate_signal(prefix)

        # Signal is known at the close of bar i. Entry proxy is next day's open.
        entry_i = i + 1
        if entry_i >= len(df):
            continue
        entry_price = float(df.iloc[entry_i]["open"])

        def close_return(days):
            j = i + days
            if j >= len(df):
                return None
            return float(df.iloc[j]["close"]) / entry_price - 1

        f1 = close_return(1)
        f3 = close_return(3)
        f5 = close_return(5)
        f10 = close_return(10)

        def excursion(days, high=True):
            end_i = min(i + days, len(df) - 1)
            window = df.iloc[entry_i : end_i + 1]
            if window.empty:
                return None
            series = window["high"] if high else window["low"]
            return float(series.max() / entry_price - 1) if high else float(series.min() / entry_price - 1)

        mfe_5 = excursion(5, high=True)
        mae_5 = excursion(5, high=False)
        complete_10 = i + horizon < len(df)

        rows.append({
            "symbol": symbol,
            "signal_timestamp": df.index[i],
            "signal_close": float(row["close"]),
            "entry_next_open": entry_price,
            "ema_3": float(row["ema_3"]),
            "ema_8": float(row["ema_8"]),
            "ema_21": float(row["ema_21"]),
            "ema_50": float(row["ema_50"]),
            "rsi_14": float(row["rsi_14"]) if pd.notna(row["rsi_14"]) else None,
            "macd": float(row["macd"]) if pd.notna(row["macd"]) else None,
            "macd_signal": float(row["macd_signal"]) if pd.notna(row["macd_signal"]) else None,
            "rel_volume": float(row["rel_volume"]) if pd.notna(row["rel_volume"]) else None,
            "atr_pct": float(row["atr_pct"]) if pd.notna(row["atr_pct"]) else None,
            "price_acceleration": float(row["price_acceleration"]) if pd.notna(row["price_acceleration"]) else None,
            "persistence_bars": result.get("PERSISTENCE_BARS"),
            "signal_score": result.get("SIGNAL_SCORE"),
            "signal_status": result.get("STATUS"),
            "forward_1d": f1,
            "forward_3d": f3,
            "forward_5d": f5,
            "forward_10d": f10,
            "mfe_5d": mfe_5,
            "mae_5d": mae_5,
            "complete_10d": complete_10,
            "outcome": classify_signal(f1, f3, f5, f10, mae_5),
        })

    return pd.DataFrame(rows)


def print_summary(signals: pd.DataFrame):
    print("\nSIGNAL SUMMARY")
    print("-" * 70)
    if signals.empty:
        print("No bullish EMA 3/8 crossover signals found.")
        return

    complete = signals[signals["forward_5d"].notna()].copy()
    print(f"Total EMA 3/8 bullish crossovers: {len(signals)}")
    print(f"Signals with 5-day outcome:         {len(complete)}")
    print(f"Signals with 10-day outcome:        {signals['complete_10d'].sum()}")

    if complete.empty:
        return

    for col, label in [
        ("forward_1d", "Forward 1D"),
        ("forward_3d", "Forward 3D"),
        ("forward_5d", "Forward 5D"),
        ("forward_10d", "Forward 10D"),
        ("mfe_5d", "5D MFE"),
        ("mae_5d", "5D MAE"),
    ]:
        if col in complete:
            s = complete[col].dropna()
            if not s.empty:
                print(f"{label:20} mean={s.mean():7.2%}  median={s.median():7.2%}")

    print("\nOutcome counts")
    print(complete["outcome"].value_counts().to_string())

    print("\nSignals")
    display_cols = [
        "signal_timestamp", "signal_close", "signal_score", "signal_status",
        "persistence_bars", "rsi_14", "rel_volume", "macd", "forward_1d",
        "forward_3d", "forward_5d", "forward_10d", "mfe_5d", "mae_5d", "outcome"
    ]
    print(complete[display_cols].to_string(index=False))


def main():
    p = argparse.ArgumentParser(description="AURA historical EMA 3/8 signal scanner")
    p.add_argument("--symbol", default="AAPL")
    p.add_argument("--start", default="2026-01-01")
    p.add_argument("--end", default="2026-08-25")
    p.add_argument("--output", default=None)
    args = p.parse_args()

    print("=" * 70)
    print("AURA v0.2 — HISTORICAL SIGNAL SCANNER")
    print("=" * 70)
    print(f"Symbol: {args.symbol}")
    print(f"Period: {args.start} -> {args.end}")
    print("MODE: RESEARCH ONLY — NO ORDERS")
    print("Signal: bullish EMA 3/8 crossover")
    print("Entry proxy: next trading day's OPEN")
    print("Outcomes: forward close returns + 5D MFE/MAE")

    raw = load_data(args.symbol, args.start, args.end)
    df = build_features(raw)
    print(f"Bars received: {len(df)}")

    signals = scan_signals(df, args.symbol)
    OUTPUT_DIR.mkdir(exist_ok=True)
    output = Path(args.output) if args.output else OUTPUT_DIR / f"{args.symbol.lower()}_historical_signals.csv"
    signals.to_csv(output, index=False)

    print_summary(signals)
    print(f"\nCSV output: {output}")
    print("NO ORDERS WERE PLACED")


if __name__ == "__main__":
    main()
