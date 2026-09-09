import argparse
import os
from dotenv import load_dotenv
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from technical_agent import build_features
from signal_validator import evaluate_signal

def load_data(symbol, start, end):
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
        feed="iex"
    )
    df = client.get_stock_bars(req).df
    if df.empty:
        raise RuntimeError("No market data returned")
    if hasattr(df.index, "levels") and len(df.index.levels) > 1:
        df = df.xs(symbol, level="symbol")
    return df.sort_index()

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--symbol", default="AAPL")
    p.add_argument("--start", default="2026-07-01")
    p.add_argument("--end", default="2026-08-25")
    a = p.parse_args()

    print("=" * 70)
    print("AURA v0.1 — SIGNAL RESEARCH")
    print("=" * 70)
    print(f"Symbol: {a.symbol}")
    print(f"Period: {a.start} -> {a.end}")
    print("MODE: RESEARCH ONLY — NO ORDERS")
    print()

    df = build_features(load_data(a.symbol, a.start, a.end))
    result = evaluate_signal(df)

    print("LATEST SIGNAL")
    print("-" * 70)
    for k, v in result.items():
        print(f"{k:24} {v}")

    print()
    print("RECENT DATA")
    print(df[["close","ema_3","ema_8","ema_21","ema_50",
              "rsi_14","macd","macd_signal","rel_volume",
              "atr_pct","price_acceleration"]].tail(10).to_string())
    print()
    print("NO ORDERS WERE PLACED")

if __name__ == "__main__":
    main()
