"""
AURA research-only walk-forward experiment:
LONG_ONLY versus LONG_ONLY + BTC market-regime filter.

Pre-declared regime:
    BTC close > BTC EMA200
    AND BTC EMA50 > BTC EMA200

No parameter optimization is performed by this script.
The regime rule is fixed before evaluation.

This script is intentionally standalone and uses the same existing data/strategy
and CURRENT exit components used by the existing research tests. It reports
chronological out-of-sample folds so we can see whether the apparent aggregate
holdout improvement survives across multiple regimes.

Usage:
    python .\mexc_bot\regime_filter_walk_forward.py --days 720 --train-days 180 --test-days 60

The --train-days argument is retained to define chronological research windows;
the regime filter itself is never optimized on them.
"""

import argparse
import os
import time
from datetime import timedelta

import numpy as np
import pandas as pd

import config as cfg
from core import strategy as strat
from param_sweep import fetch_all_data, fetch_all_funding
from run_broad_backtest import DEFAULT_SYMBOLS
from exit_sensitivity_test import _make_entry_inventory, _simulate_current, summarize_trades


BTC_SYMBOL = "BTC/USDT:USDT"
EMA_FAST = 50
EMA_SLOW = 200
REGIME_NAME = "BTC_CLOSE_GT_EMA200_AND_EMA50_GT_EMA200"


def build_btc_regime(df):
    out = df[["timestamp", "close"]].copy().sort_values("timestamp").reset_index(drop=True)
    out["btc_ema50"] = out["close"].ewm(
        span=EMA_FAST, adjust=False, min_periods=EMA_FAST
    ).mean()
    out["btc_ema200"] = out["close"].ewm(
        span=EMA_SLOW, adjust=False, min_periods=EMA_SLOW
    ).mean()
    out["btc_bull_regime"] = (
        (out["close"] > out["btc_ema200"])
        & (out["btc_ema50"] > out["btc_ema200"])
    )
    return out


def regime_at(regime_df, ts):
    ts = pd.Timestamp(ts)
    idx = regime_df["timestamp"].searchsorted(ts, side="right") - 1
    if idx < 0:
        return False, np.nan, np.nan, np.nan

    row = regime_df.iloc[int(idx)]
    return (
        bool(row["btc_bull_regime"]),
        float(row["close"]),
        float(row["btc_ema50"]),
        float(row["btc_ema200"]),
    )


def replay(entries, data_cache, funding_cache):
    rows = []

    for entry in entries:
        df = data_cache.get(entry.symbol)
        if df is None or df.empty:
            continue

        idx = df["timestamp"].searchsorted(
            pd.Timestamp(entry.entry_time), side="left"
        )
        if idx >= len(df) or pd.Timestamp(df.iloc[idx]["timestamp"]) != pd.Timestamp(entry.entry_time):
            continue

        result = _simulate_current(
            entry,
            df.reset_index(drop=True),
            int(idx),
            funding_cache.get(entry.symbol),
        )

        result["symbol"] = entry.symbol
        result["entry_time"] = entry.entry_time
        result["side"] = entry.side
        rows.append(result)

    return pd.DataFrame(rows)


def metrics(df):
    if df is None or df.empty:
        return {
            "trades": 0,
            "win_rate_pct": np.nan,
            "profit_factor": np.nan,
            "expectancy": np.nan,
            "total_pnl": 0.0,
            "avg_r": np.nan,
            "mfe_r": np.nan,
            "mae_r": np.nan,
        }

    s = summarize_trades(df)

    # Existing summarize_trades output is used where possible.
    return {
        "trades": int(s.get("num_trades", len(df))),
        "win_rate_pct": float(s.get("win_rate_pct", np.nan)),
        "profit_factor": float(s.get("profit_factor", np.nan)),
        "expectancy": float(s.get("expectancy", np.nan)),
        "total_pnl": float(s.get("total_pnl", 0.0)),
        "avg_r": float(s.get("avg_realized_r", np.nan)),
        "mfe_r": float(s.get("avg_mfe_r", np.nan)),
        "mae_r": float(s.get("avg_mae_r", np.nan)),
    }


def run_fold(fold_no, train_start, test_start, test_end,
             data_cache, funding_cache, btc_regime):
    """
    Generate the entry inventory from the training+test history available
    through the fold, then evaluate only entries whose timestamps are in the
    OOS test interval.

    The BTC regime itself is fixed and uses only BTC candles <= entry time.
    """
    # Use data through the end of the OOS fold so the strategy can form signals
    # normally during the test interval.
    fold_cache = {}
    for symbol, df in data_cache.items():
        fold_cache[symbol] = df[
            df["timestamp"] < test_end
        ].reset_index(drop=True)

    # Build a single-window inventory over the chronological fold.
    # We deliberately do not optimize any parameter.
    windows = [(train_start, test_end)]
    entries, _ = _make_entry_inventory(
        fold_cache, funding_cache, windows
    )

    long_entries = [
        e for e in entries
        if e.side == "long"
        and pd.Timestamp(e.entry_time) >= pd.Timestamp(test_start)
        and pd.Timestamp(e.entry_time) < pd.Timestamp(test_end)
    ]

    allowed = []
    rejected = []

    for e in long_entries:
        ok, btc_close, ema50, ema200 = regime_at(btc_regime, e.entry_time)
        if ok:
            allowed.append(e)
        else:
            rejected.append(e)

    baseline_df = replay(long_entries, fold_cache, funding_cache)
    filtered_df = replay(allowed, fold_cache, funding_cache)

    bm = metrics(baseline_df)
    fm = metrics(filtered_df)

    return {
        "fold": fold_no,
        "train_start": train_start,
        "oos_start": test_start,
        "oos_end": test_end,
        "baseline_trades": len(long_entries),
        "filtered_trades": len(allowed),
        "rejected_trades": len(rejected),
        "rejection_pct": (
            len(rejected) / len(long_entries) * 100
            if long_entries else np.nan
        ),
        "baseline_pf": bm["profit_factor"],
        "filtered_pf": fm["profit_factor"],
        "baseline_expectancy": bm["expectancy"],
        "filtered_expectancy": fm["expectancy"],
        "baseline_total_pnl": bm["total_pnl"],
        "filtered_total_pnl": fm["total_pnl"],
        "baseline_avg_r": bm["avg_r"],
        "filtered_avg_r": fm["avg_r"],
        "baseline_win_rate_pct": bm["win_rate_pct"],
        "filtered_win_rate_pct": fm["win_rate_pct"],
        "baseline_mfe_r": bm["mfe_r"],
        "filtered_mfe_r": fm["mfe_r"],
        "baseline_mae_r": bm["mae_r"],
        "filtered_mae_r": fm["mae_r"],
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS)
    p.add_argument("--days", type=int, default=720)
    p.add_argument("--train-days", type=int, default=180)
    p.add_argument("--test-days", type=int, default=60)
    p.add_argument("--timeframe", default=None)
    args = p.parse_args()

    timeframe = args.timeframe or cfg.TIMEFRAME

    if BTC_SYMBOL not in args.symbols:
        raise SystemExit(
            f"{BTC_SYMBOL} must be included in --symbols for the BTC regime filter."
        )

    print("=" * 78)
    print("AURA — REGIME-FILTER WALK-FORWARD")
    print("=" * 78)
    print(f"Days:       {args.days}")
    print(f"Train:      {args.train_days} days")
    print(f"OOS test:   {args.test_days} days")
    print(f"Timeframe:  {timeframe}")
    print()
    print("FIXED REGIME:")
    print("  BTC close > EMA200")
    print("  BTC EMA50 > EMA200")
    print()
    print("NO PARAMETER OPTIMIZATION.")
    print("NO LIVE-BOT CHANGES.")

    data_cache = fetch_all_data(args.symbols, args.days, timeframe)
    if not data_cache:
        raise SystemExit("No historical data fetched.")

    funding_cache = fetch_all_funding(list(data_cache.keys()), args.days)

    data_cache = {
        s: strat.add_indicators(df)
        for s, df in data_cache.items()
    }

    btc_regime = build_btc_regime(data_cache[BTC_SYMBOL])

    start = max(df["timestamp"].min() for df in data_cache.values())
    end = min(df["timestamp"].max() for df in data_cache.values())

    print(f"\nCommon data range: {start} -> {end}")

    rows = []
    cursor = start + timedelta(days=args.train_days)
    fold = 1

    while cursor + timedelta(days=args.test_days) <= end:
        test_start = cursor
        test_end = cursor + timedelta(days=args.test_days)
        train_start = test_start - timedelta(days=args.train_days)

        print(
            f"\nFold {fold}: "
            f"train {train_start.date()} -> {test_start.date()} | "
            f"OOS {test_start.date()} -> {test_end.date()}"
        )

        try:
            row = run_fold(
                fold, train_start, test_start, test_end,
                data_cache, funding_cache, btc_regime
            )
            rows.append(row)

            print(
                f"  LONG_ONLY:      trades={row['baseline_trades']} "
                f"PF={row['baseline_pf']:.3f} "
                f"Exp={row['baseline_expectancy']:.4f} "
                f"PnL={row['baseline_total_pnl']:.2f}"
            )
            print(
                f"  BTC_REGIME:     trades={row['filtered_trades']} "
                f"PF={row['filtered_pf']:.3f} "
                f"Exp={row['filtered_expectancy']:.4f} "
                f"PnL={row['filtered_total_pnl']:.2f} "
                f"rejected={row['rejection_pct']:.1f}%"
            )
        except Exception as exc:
            print(f"  Fold failed: {type(exc).__name__}: {exc}")

        fold += 1
        cursor = test_end

    if not rows:
        raise SystemExit("No walk-forward folds were produced.")

    out = pd.DataFrame(rows)

    print("\n" + "=" * 78)
    print("WALK-FORWARD SUMMARY")
    print("=" * 78)
    cols = [
        "fold", "oos_start", "oos_end",
        "baseline_trades", "filtered_trades", "rejection_pct",
        "baseline_pf", "filtered_pf",
        "baseline_expectancy", "filtered_expectancy",
        "baseline_total_pnl", "filtered_total_pnl",
    ]
    print(out[cols].round(4).to_string(index=False))

    positive_base = (out["baseline_total_pnl"] > 0).sum()
    positive_filtered = (out["filtered_total_pnl"] > 0).sum()

    print("\nPositive OOS folds:")
    print(f"  LONG_ONLY:  {positive_base}/{len(out)}")
    print(f"  BTC_REGIME: {positive_filtered}/{len(out)}")

    print("\nAggregate OOS PnL:")
    print(f"  LONG_ONLY:  {out['baseline_total_pnl'].sum():.2f}")
    print(f"  BTC_REGIME: {out['filtered_total_pnl'].sum():.2f}")

    os.makedirs("backtest_results", exist_ok=True)
    stamp = int(time.time())
    path = f"backtest_results/regime_filter_walk_forward_{stamp}.csv"
    out.to_csv(path, index=False)
    print(f"\nSaved: {path}")

    print("\nDECISION:")
    print("Do not promote the filter based on aggregate PnL alone.")
    print("Look for improvement across multiple OOS folds, not one favorable regime.")


if __name__ == "__main__":
    main()
