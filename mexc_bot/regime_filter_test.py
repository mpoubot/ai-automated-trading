"""
AURA research-only experiment: LONG_ONLY + BTC market-regime filter.

Purpose
-------
Test one pre-declared market-regime hypothesis without changing the frozen
entry signal, exits, sizing, universe, or live bot.

Baseline:
    Existing strategy entry inventory -> LONG entries only -> CURRENT exits.

Candidate:
    Same LONG entry inventory -> allow only when BTC is bullish:
        BTC close > BTC EMA200
        AND
        BTC EMA50 > BTC EMA200

The regime is evaluated using the latest BTC candle at or before the entry
timestamp. No future BTC data is used.

Research/holdout:
    Default: 720 total days, 480 research, 240 untouched holdout.
    The candidate is NOT optimized or selected on the holdout. Both baseline
    LONG_ONLY and the filtered candidate are reported there.

This file is research-only. It does not modify config.py, live_bot.py, the
strategy, or any Git history.

Usage:
    python .\mexc_bot\regime_filter_test.py --days 720 --research-days 480

Optional:
    --symbols "BTC/USDT:USDT" "ETH/USDT:USDT" ...
    --windows 8
    --timeframe 1h
    --no-holdout
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
from permutation_test import build_windows
from run_broad_backtest import DEFAULT_SYMBOLS
from exit_sensitivity_test import (
    _make_entry_inventory,
    _simulate_current,
    summarize_trades,
    _warmup_bars,
)


BTC_SYMBOL = "BTC/USDT:USDT"
REGIME_NAME = "BTC_BULL_EMA50_GT_EMA200_AND_CLOSE_GT_EMA200"


def build_btc_regime(df: pd.DataFrame) -> pd.DataFrame:
    """Create the pre-declared BTC regime using only BTC candles up to t."""
    out = df[["timestamp", "close"]].copy().sort_values("timestamp").reset_index(drop=True)
    out["btc_ema50"] = out["close"].ewm(span=50, adjust=False, min_periods=50).mean()
    out["btc_ema200"] = out["close"].ewm(span=200, adjust=False, min_periods=200).mean()

    out["btc_bull_regime"] = (
        (out["close"] > out["btc_ema200"])
        & (out["btc_ema50"] > out["btc_ema200"])
    )
    return out


def regime_at(regime_df: pd.DataFrame, ts) -> dict:
    """Return the latest BTC regime at or before the entry timestamp."""
    ts = pd.Timestamp(ts)
    idx = regime_df["timestamp"].searchsorted(ts, side="right") - 1
    if idx < 0:
        return {
            "btc_regime": False,
            "btc_close": np.nan,
            "btc_ema50": np.nan,
            "btc_ema200": np.nan,
        }

    row = regime_df.iloc[int(idx)]
    return {
        "btc_regime": bool(row["btc_bull_regime"]),
        "btc_close": float(row["close"]) if pd.notna(row["close"]) else np.nan,
        "btc_ema50": float(row["btc_ema50"]) if pd.notna(row["btc_ema50"]) else np.nan,
        "btc_ema200": float(row["btc_ema200"]) if pd.notna(row["btc_ema200"]) else np.nan,
    }


def replay_entries(entries, data_cache, funding_cache, label):
    """Replay immutable entries through the exact CURRENT exit model."""
    rows = []
    for entry in entries:
        df = data_cache.get(entry.symbol)
        if df is None:
            continue

        seg = df[
            (df["timestamp"] >= min(df["timestamp"]))
            & (df["timestamp"] <= max(df["timestamp"]))
        ].reset_index(drop=True)

        idxs = np.where(seg["timestamp"].values == entry.entry_time.to_datetime64())[0]
        if len(idxs) == 0:
            continue

        result = _simulate_current(
            entry,
            seg,
            int(idxs[0]),
            funding_cache.get(entry.symbol),
        )
        result["symbol"] = entry.symbol
        result["entry_time"] = entry.entry_time
        result["side"] = entry.side
        result["period"] = label
        rows.append(result)

    return pd.DataFrame(rows)


def prepare_segments(data_cache, windows):
    """Create exact symbol/window segments used by the baseline inventory."""
    out = {}
    for symbol, df in data_cache.items():
        for w, (t0, t1) in enumerate(windows):
            seg = df[
                (df["timestamp"] >= t0) & (df["timestamp"] < t1)
            ].reset_index(drop=True)
            if len(seg):
                out[(symbol, w)] = seg
    return out


def replay_entries_by_window(entries, data_cache, funding_cache):
    """Replay entries using the same symbol/window segment they originated in."""
    seg_cache = {}
    for symbol, df in data_cache.items():
        # Windows are already encoded on Entry, so recover each entry's exact
        # segment by using the full frame. Entry replay itself does not need
        # future data outside the actual symbol history.
        seg_cache[symbol] = df.reset_index(drop=True)

    rows = []
    for entry in entries:
        seg = seg_cache.get(entry.symbol)
        if seg is None:
            continue

        idxs = np.where(
            seg["timestamp"].values == entry.entry_time.to_datetime64()
        )[0]
        if len(idxs) == 0:
            continue

        result = _simulate_current(
            entry,
            seg,
            int(idxs[0]),
            funding_cache.get(entry.symbol),
        )
        result["symbol"] = entry.symbol
        result["entry_time"] = entry.entry_time
        result["side"] = entry.side
        rows.append(result)

    return pd.DataFrame(rows)


def make_entry_inventory(data_cache, funding_cache, windows):
    """Build the immutable existing-strategy entry inventory."""
    entries, baseline_log = _make_entry_inventory(
        data_cache, funding_cache, windows
    )
    return entries, baseline_log


def annotate_long_entries(entries, btc_regime_df):
    """Annotate every LONG entry with the pre-declared BTC regime."""
    rows = []
    for entry in entries:
        if entry.side != "long":
            continue

        r = regime_at(btc_regime_df, entry.entry_time)
        rows.append({
            "entry": entry,
            **r,
        })
    return rows


def summarize(df):
    if df.empty:
        return {
            "num_trades": 0,
            "win_rate_pct": np.nan,
            "profit_factor": np.nan,
            "expectancy": np.nan,
            "total_pnl": 0.0,
            "avg_realized_r": np.nan,
            "avg_mfe_r": np.nan,
            "avg_mae_r": np.nan,
        }
    return summarize_trades(df)


def run_period(data_cache, funding_cache, windows, btc_regime_df, label):
    print(f"\n{'=' * 78}")
    print(f"{label} — IMMUTABLE ENTRY INVENTORY")
    print(f"{'=' * 78}")

    entries, _baseline = make_entry_inventory(
        data_cache, funding_cache, windows
    )

    long_rows = annotate_long_entries(entries, btc_regime_df)
    allowed = [x["entry"] for x in long_rows if x["btc_regime"]]
    rejected = [x["entry"] for x in long_rows if not x["btc_regime"]]

    print(f"Baseline entries:       {len(entries)}")
    print(f"LONG entries:            {len(long_rows)}")
    print(f"LONG allowed by regime: {len(allowed)}")
    print(f"LONG rejected:           {len(rejected)}")

    if long_rows:
        print(
            f"Regime acceptance:      "
            f"{len(allowed) / len(long_rows) * 100:.1f}%"
        )

    # Both are replayed with the exact same CURRENT exit architecture.
    baseline_df = replay_entries_by_window(
        [x["entry"] for x in long_rows],
        data_cache,
        funding_cache,
    )
    filtered_df = replay_entries_by_window(
        allowed,
        data_cache,
        funding_cache,
    )

    for df, name in [(baseline_df, "LONG_ONLY"), (filtered_df, "LONG_ONLY_BTC_REGIME")]:
        m = summarize(df)
        print(
            f"{name:<25} trades={m['num_trades']:<5} "
            f"win={m['win_rate_pct']:.2f}% "
            f"PF={m['profit_factor']:.3f} "
            f"expectancy={m['expectancy']:.4f} "
            f"total_R={m['avg_realized_r'] * m['num_trades'] if m['num_trades'] else 0:.2f}"
        )

    # Add explicit regime fields to the filtered trade records.
    regime_map = {
        pd.Timestamp(x["entry"].entry_time): x
        for x in long_rows
    }
    for df in (baseline_df, filtered_df):
        if df.empty:
            continue
        df["btc_regime"] = df["entry_time"].map(
            lambda ts: bool(regime_map[pd.Timestamp(ts)]["btc_regime"])
            if pd.Timestamp(ts) in regime_map else np.nan
        )
        df["btc_close"] = df["entry_time"].map(
            lambda ts: regime_map[pd.Timestamp(ts)]["btc_close"]
            if pd.Timestamp(ts) in regime_map else np.nan
        )
        df["btc_ema50"] = df["entry_time"].map(
            lambda ts: regime_map[pd.Timestamp(ts)]["btc_ema50"]
            if pd.Timestamp(ts) in regime_map else np.nan
        )
        df["btc_ema200"] = df["entry_time"].map(
            lambda ts: regime_map[pd.Timestamp(ts)]["btc_ema200"]
            if pd.Timestamp(ts) in regime_map else np.nan
        )

    summary_rows = []
    for name, df in [
        ("LONG_ONLY", baseline_df),
        ("LONG_ONLY_BTC_REGIME", filtered_df),
    ]:
        m = summarize(df)
        summary_rows.append({
            "period": label,
            "model": name,
            **m,
            "regime_rule": REGIME_NAME,
        })

    return pd.DataFrame(summary_rows), baseline_df, filtered_df, len(long_rows), len(allowed)


def main():
    parser = argparse.ArgumentParser(
        description="Research-only LONG_ONLY + BTC market regime filter test"
    )
    parser.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS)
    parser.add_argument("--days", type=int, default=720)
    parser.add_argument("--research-days", type=int, default=480)
    parser.add_argument("--windows", type=int, default=8)
    parser.add_argument("--timeframe", default=None)
    parser.add_argument("--no-holdout", action="store_true")
    args = parser.parse_args()

    timeframe = args.timeframe or cfg.TIMEFRAME

    if BTC_SYMBOL not in args.symbols:
        raise SystemExit(
            f"{BTC_SYMBOL} must be included in --symbols because the "
            "regime filter uses BTC."
        )

    print("=" * 78)
    print("AURA — LONG_ONLY + BTC MARKET REGIME FILTER")
    print("=" * 78)
    print(f"Timeframe: {timeframe}")
    print(f"Days: {args.days}")
    print(f"Research days: {args.research_days}")
    print(f"Regime rule: {REGIME_NAME}")
    print()
    print("ENTRY SIGNALS FROZEN.")
    print("EXITS FROZEN.")
    print("SIZING FROZEN.")
    print("NO PARAMETER OPTIMIZATION.")
    print("NO LIVE-BOT CHANGES.")
    print("NO HOLDOUT-BASED SELECTION.")

    data_cache = fetch_all_data(args.symbols, args.days, timeframe)
    if not data_cache:
        raise SystemExit("No usable historical data fetched.")

    if BTC_SYMBOL not in data_cache:
        raise SystemExit("BTC data was not fetched; cannot run regime test.")

    funding_cache = fetch_all_funding(list(data_cache.keys()), args.days)
    data_cache = {
        s: strat.add_indicators(df)
        for s, df in data_cache.items()
    }

    btc_regime_df = build_btc_regime(data_cache[BTC_SYMBOL])

    all_start = min(df["timestamp"].min() for df in data_cache.values())
    all_end = max(df["timestamp"].max() for df in data_cache.values())
    span_days = (all_end - all_start).total_seconds() / 86400.0

    if args.research_days <= 0 or args.research_days > args.days:
        raise SystemExit("--research-days must be > 0 and <= --days")

    split_ts = all_start + timedelta(
        days=span_days * (args.research_days / args.days)
    )

    if args.no_holdout:
        research_cache = data_cache
        holdout_cache = {}
        print("\nWARNING: --no-holdout means all results are in-sample.")
    else:
        research_cache = {
            s: df[df["timestamp"] < split_ts].reset_index(drop=True)
            for s, df in data_cache.items()
        }
        holdout_cache = {
            s: df[df["timestamp"] >= split_ts].reset_index(drop=True)
            for s, df in data_cache.items()
        }
        research_cache = {
            s: df for s, df in research_cache.items()
            if len(df) > _warmup_bars() + 60
        }
        holdout_cache = {
            s: df for s, df in holdout_cache.items()
            if len(df) > _warmup_bars() + 60
        }

    print("\n" + "=" * 78)
    print("CHRONOLOGICAL PROTOCOL")
    print("=" * 78)
    print(f"Full period : {all_start.date()} .. {all_end.date()}")
    print(f"Research    : {all_start.date()} .. {split_ts.date()}")
    if not args.no_holdout:
        print(f"Holdout     : {split_ts.date()} .. {all_end.date()}")
        print("Holdout is evaluated but NOT used to alter the rule.")

    research_windows = build_windows(research_cache, args.windows)
    research_summary, research_base, research_filtered, research_long, research_allowed = run_period(
        research_cache,
        funding_cache,
        research_windows,
        btc_regime_df,
        "RESEARCH",
    )

    all_summaries = [research_summary]
    all_trades = []

    if not research_base.empty:
        research_base["model"] = "LONG_ONLY"
        all_trades.append(research_base)
    if not research_filtered.empty:
        research_filtered["model"] = "LONG_ONLY_BTC_REGIME"
        all_trades.append(research_filtered)

    if not args.no_holdout:
        holdout_windows = build_windows(holdout_cache, max(2, args.windows // 2))

        holdout_summary, holdout_base, holdout_filtered, holdout_long, holdout_allowed = run_period(
            holdout_cache,
            funding_cache,
            holdout_windows,
            btc_regime_df,
            "HOLDOUT",
        )

        all_summaries.append(holdout_summary)

        if not holdout_base.empty:
            holdout_base["model"] = "LONG_ONLY"
            all_trades.append(holdout_base)
        if not holdout_filtered.empty:
            holdout_filtered["model"] = "LONG_ONLY_BTC_REGIME"
            all_trades.append(holdout_filtered)

        print("\n" + "=" * 78)
        print("RESEARCH vs HOLDOUT")
        print("=" * 78)
        print(
            pd.concat(all_summaries, ignore_index=True)[
                ["period", "model", "num_trades", "win_rate_pct",
                 "profit_factor", "expectancy", "total_pnl",
                 "avg_realized_r", "avg_mfe_r", "avg_mae_r"]
            ].round(4).to_string(index=False)
        )

        print("\nInterpretation:")
        print("  The holdout is evidence, not a tuning target.")
        print("  Do NOT change the regime rule based on the holdout result.")

    summary_df = pd.concat(all_summaries, ignore_index=True)
    trades_df = pd.concat(all_trades, ignore_index=True) if all_trades else pd.DataFrame()

    os.makedirs("backtest_results", exist_ok=True)
    stamp = int(time.time())
    summary_path = f"backtest_results/regime_filter_summary_{stamp}.csv"
    trades_path = f"backtest_results/regime_filter_trades_{stamp}.csv"

    summary_df.to_csv(summary_path, index=False)
    trades_df.to_csv(trades_path, index=False)

    print("\nSaved:")
    print(f"  {summary_path}")
    print(f"  {trades_path}")

    print("\nDECISION RULE:")
    print("  Do not promote the regime filter merely because it improves total PnL.")
    print("  Look for improvement in the untouched holdout, reasonable trade count,")
    print("  and improvement that is not concentrated in one symbol or one short period.")


if __name__ == "__main__":
    main()
