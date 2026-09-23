"""
MEXC-NATIVE FROZEN DIRECTIONAL DECOMPOSITION
=============================================

Research-only driver.

Purpose:
    Run the existing frozen strategy engine against the verified local
    MEXC-native 720-day Parquet dataset and compare:
      1) FULL CURRENT (long + short)
      2) LONG ONLY
      3) SHORT ONLY

Important:
    - Does NOT modify strategy.py, backtester.py, config.py, indicators.py,
      risk_manager.py, or trade_metrics.py.
    - Does NOT use CCXT or the MEXC API.
    - Uses the existing backtester.simulate_symbol() engine.
    - Direction filtering is implemented by a temporary wrapper around
      strategy.evaluate_signal(); the original function is restored after
      each run.
    - The production configuration file is never edited.

Run from:
    mexc_bot\\

Usage:
    python run_native_directional_decomposition.py

The script expects:
    data\\native_v2_full720day_20260917\\
or the equivalent extracted dataset directory.

It can also extract:
    data\\native_v2_full720day_20260917.zip
automatically if the directory does not exist.
"""

from __future__ import annotations

import json
import os
import shutil
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

import config as cfg
from core import strategy as strat
from core import trade_metrics
from core.risk_manager import CircuitBreaker
from backtester import simulate_symbol


ROOT = Path(__file__).resolve().parent

# Prefer the extracted dataset; otherwise extract the supplied archive.
DATA_DIR = ROOT / "data" / "native_v2_full720day_20260917"
ZIP_CANDIDATES = [
    ROOT / "data" / "native_v2_full720day_20260917.zip",
    ROOT / "data" / "mexc_native_v2_full720day_20260917.zip",
]

# Native filenames are BASE_USDT; backtester symbols are typically BASE/USDT:USDT.
NATIVE_SYMBOLS = [
    "BTC_USDT",
    "ETH_USDT",
    "BNB_USDT",
    "XRP_USDT",
    "ADA_USDT",
    "DOGE_USDT",
    "AVAX_USDT",
    "DOT_USDT",
    "SUI_USDT",
    "TIA_USDT",
    "COTI_USDT",
]

BACKTEST_SYMBOLS = {s: s.replace("_", "/") + ":USDT" for s in NATIVE_SYMBOLS}

OUT_DIR = ROOT / "directional_decomposition_results"


def ensure_dataset() -> Path:
    if DATA_DIR.exists():
        return DATA_DIR

    for z in ZIP_CANDIDATES:
        if z.exists():
            print(f"Extracting dataset: {z}")
            DATA_DIR.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(z, "r") as archive:
                archive.extractall(DATA_DIR.parent)

            # Some archives contain mexc_native_v2/ as their top-level folder.
            if not DATA_DIR.exists():
                alt = DATA_DIR.parent / "mexc_native_v2"
                if alt.exists():
                    alt.rename(DATA_DIR)
            break

    if not DATA_DIR.exists():
        raise FileNotFoundError(
            "Could not find the verified native dataset. Expected:\n"
            f"  {DATA_DIR}\n"
            f"or one of: {ZIP_CANDIDATES}"
        )
    return DATA_DIR


def load_symbol_data(native_symbol: str):
    ohlcv_path = DATA_DIR / "ohlcv" / f"{native_symbol}_1h.parquet"
    funding_path = DATA_DIR / "funding" / f"{native_symbol}_funding.parquet"

    if not ohlcv_path.exists():
        raise FileNotFoundError(f"Missing OHLCV: {ohlcv_path}")

    df = pd.read_parquet(ohlcv_path)
    funding = pd.read_parquet(funding_path) if funding_path.exists() else pd.DataFrame()

    # Do not silently alter market data. Only normalize column order if needed.
    required = ["timestamp", "open", "high", "low", "close", "volume"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"{native_symbol}: missing OHLCV columns: {missing}")

    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df = df.sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)

    if not funding.empty:
        if "timestamp" not in funding.columns or "funding_rate" not in funding.columns:
            raise ValueError(f"{native_symbol}: invalid funding schema")
        funding["timestamp"] = pd.to_datetime(funding["timestamp"])
        funding = funding.sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)

    return df, funding


def make_direction_wrapper(original, allowed_side: str | None):
    """
    Return a wrapper that preserves the existing signal generation completely,
    but rejects one direction.

    allowed_side=None -> full current strategy
    allowed_side='long' -> only long signals
    allowed_side='short' -> only short signals
    """
    if allowed_side is None:
        return original

    def wrapped(window):
        result = original(window)
        if result is None:
            return result

        # Existing strategy returns a signal dictionary.
        if isinstance(result, dict):
            signal = result.get("signal")
            if signal is not None and signal != allowed_side:
                result = dict(result)
                result["signal"] = None
                result["reason"] = f"directional diagnostic rejected {signal}"
        return result

    return wrapped


def run_mode(mode_name: str, allowed_side: str | None, datasets: dict):
    print("\n" + "=" * 78)
    print(mode_name)
    print("=" * 78)

    original_evaluate_signal = strat.evaluate_signal
    strat.evaluate_signal = make_direction_wrapper(original_evaluate_signal, allowed_side)

    all_events = []
    per_symbol = {}

    try:
        for native_symbol, (df, funding) in datasets.items():
            symbol = BACKTEST_SYMBOLS[native_symbol]

            # Match the existing per-symbol simulation convention:
            # independent starting equity and a fresh circuit breaker.
            equity_tracker = {
                "equity": float(cfg.BACKTEST_STARTING_EQUITY),
                "open_count": 0,
            }
            breaker = CircuitBreaker(cfg.BACKTEST_STARTING_EQUITY)
            trade_log = []

            # simulate_symbol itself calls add_indicators() if needed.
            simulate_symbol(
                df.copy(),
                symbol,
                equity_tracker,
                breaker,
                trade_log,
                funding_df=funding,
            )

            for event in trade_log:
                event = dict(event)
                event["directional_mode"] = mode_name
                all_events.append(event)

            event_df = pd.DataFrame(trade_log)
            per_symbol[native_symbol] = {
                "symbol": symbol,
                "candles": int(len(df)),
                "start": str(df["timestamp"].min()),
                "end": str(df["timestamp"].max()),
                "events": int(len(trade_log)),
                "ending_equity": float(equity_tracker["equity"]),
                "pnl": float(
                    event_df["pnl"].sum()
                    if not event_df.empty and "pnl" in event_df.columns
                    else 0.0
                ),
            }

            print(
                f"{symbol:<18} candles={len(df):>5} "
                f"events={len(trade_log):>4} "
                f"ending_equity=${equity_tracker['equity']:,.2f}"
            )
    finally:
        # Critical: restore the real strategy function.
        strat.evaluate_signal = original_evaluate_signal

    log_df = pd.DataFrame(all_events)

    if log_df.empty:
        metrics = {}
        trade_df = pd.DataFrame()
    else:
        metrics = trade_metrics.compute_metrics(
            log_df,
            cfg.BACKTEST_STARTING_EQUITY * len(datasets),
        )
        trade_df = trade_metrics.reconstruct_trades(log_df)

    if not trade_df.empty and "side" in trade_df.columns:
        long_count = int((trade_df["side"] == "long").sum())
        short_count = int((trade_df["side"] == "short").sum())
        win_count = int((pd.to_numeric(trade_df["total_pnl"], errors="coerce") > 0).sum())
    else:
        long_count = short_count = win_count = 0

    result = {
        "mode": mode_name,
        "allowed_side": allowed_side,
        "metrics": metrics,
        "trade_count": int(len(trade_df)),
        "long_trades": long_count,
        "short_trades": short_count,
        "winning_trades": win_count,
        "losing_or_flat_trades": int(len(trade_df) - win_count),
        "per_symbol": per_symbol,
    }

    return result, log_df, trade_df


def main():
    dataset = ensure_dataset()

    # Verify all 11 files exist before running anything.
    datasets = {}
    for native_symbol in NATIVE_SYMBOLS:
        datasets[native_symbol] = load_symbol_data(native_symbol)

    print("\nDATASET CHECK")
    print("=" * 78)
    print(f"Dataset: {dataset}")
    print(f"Symbols: {len(datasets)}")
    print(f"Timeframe: {cfg.TIMEFRAME}")

    for s, (df, funding) in datasets.items():
        print(
            f"{s:<10} OHLCV={len(df):>5} "
            f"{df['timestamp'].min()} -> {df['timestamp'].max()} "
            f"funding={len(funding):>5}"
        )

    modes = [
        ("FULL_CURRENT", None),
        ("LONG_ONLY", "long"),
        ("SHORT_ONLY", "short"),
    ]

    results = {}
    logs = {}
    trades = {}

    for mode_name, side in modes:
        result, log_df, trade_df = run_mode(mode_name, side, datasets)
        results[mode_name] = result
        logs[mode_name] = log_df
        trades[mode_name] = trade_df

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = int(time.time())

    for mode_name, log_df in logs.items():
        log_df.to_csv(
            OUT_DIR / f"{mode_name.lower()}_events_{stamp}.csv",
            index=False,
        )

    for mode_name, trade_df in trades.items():
        trade_df.to_csv(
            OUT_DIR / f"{mode_name.lower()}_trades_{stamp}.csv",
            index=False,
        )

    report = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "driver": Path(__file__).name,
        "dataset": str(dataset),
        "symbols": NATIVE_SYMBOLS,
        "timeframe": cfg.TIMEFRAME,
        "starting_equity_per_symbol": cfg.BACKTEST_STARTING_EQUITY,
        "results": results,
        "notes": [
            "Research-only directional decomposition.",
            "Existing strategy/backtester/risk/metrics source files were not modified.",
            "Direction filtering was applied through a temporary wrapper around strategy.evaluate_signal().",
            "No CCXT or exchange API was used.",
            "No parameter optimization was performed.",
            "The existing circuit-breaker behavior was preserved.",
            "Drawdown percentage from compute_metrics() must not be used for interpretation; use the previously established equity-based diagnostic instead.",
        ],
    }

    report_path = OUT_DIR / f"directional_decomposition_{stamp}.json"
    report_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    print("\n" + "=" * 78)
    print("FINAL COMPARISON")
    print("=" * 78)

    for mode_name in ["FULL_CURRENT", "LONG_ONLY", "SHORT_ONLY"]:
        r = results[mode_name]
        m = r["metrics"]
        print(
            f"{mode_name:<16} "
            f"trades={r['trade_count']:>4} "
            f"long={r['long_trades']:>4} "
            f"short={r['short_trades']:>4} "
            f"PnL={m.get('total_pnl', float('nan')):>10.2f} "
            f"PF={m.get('profit_factor', float('nan')):>7.3f}"
        )

    print(f"\nReport: {report_path}")
    print("STOP — no further experiments were run.")


if __name__ == "__main__":
    main()
