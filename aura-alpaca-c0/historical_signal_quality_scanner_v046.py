
"""
AURA v0.4.6 — Risk & Exit Research Engine
-------------------------------------------

RESEARCH ONLY — NO ORDERS.

Purpose
-------
Evaluate risk/exit behavior for the current AURA research survivor:

    EMA38_CROSSOVER + MACD_HIST_GT_0 + REL_VOLUME_GE_1

This version does NOT optimize the entry signal and does NOT select a new
entry strategy. It studies how the survivor behaves after a next-day-open
entry, using empirical MFE/MAE and forward path information.

It compares candidate exit policies:
    - time exit: 1/3/5/10 trading days
    - fixed stop + fixed target
    - ATR stop + ATR target
    - trailing stop
    - stop/target with a time cap

Important methodology:
    - Signal is evaluated on decision bar.
    - Entry occurs at next trading day's OPEN.
    - Stop/target checks use intraday HIGH/LOW after entry.
    - If stop and target are both touched in the same daily bar and their
      order cannot be known from OHLC, the trade is marked AMBIGUOUS and uses
      a conservative stop-first assumption for the conservative report.
    - No exit parameters are selected from the final holdout.
    - Candidate policies are reported as research experiments.
    - Results include win rate, average/median return, profit factor,
      expectancy, max drawdown, MAE/MFE, stop-hit rate and ambiguous-bar rate.

The script expects:
    research/v043_historical_events.csv

It fetches fresh daily OHLC data from Alpaca for the symbols in the event file
so that path-level exit analysis has the complete post-entry daily bars.

Environment:
    ALPACA_API_KEY
    ALPACA_SECRET_KEY

Example:
    python historical_signal_quality_scanner_v046.py
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


DEFAULT_EVENTS = Path("research") / "v043_historical_events.csv"
DEFAULT_OUTDIR = Path("research")

# Current research survivor from v0.4.4.
SURVIVOR = "EMA38_MACD_VOLUME"
SURVIVOR_CONDITIONS = "EMA38_CROSSOVER + MACD_HIST_GT_0 + REL_VOLUME_GE_1"

HORIZONS = [1, 3, 5, 10]

# Deliberately broad but not absurdly granular. This is a research grid,
# not a parameter optimizer.
FIXED_POLICIES = [
    (0.010, 0.020),
    (0.010, 0.030),
    (0.015, 0.030),
    (0.015, 0.045),
    (0.020, 0.040),
    (0.020, 0.060),
    (0.030, 0.060),
]

ATR_POLICIES = [
    (1.0, 2.0),
    (1.0, 3.0),
    (1.5, 3.0),
    (1.5, 4.0),
    (2.0, 4.0),
]

TRAILING_POLICIES = [
    (0.010,),
    (0.015,),
    (0.020,),
    (0.030,),
]


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--events", default=str(DEFAULT_EVENTS))
    p.add_argument("--outdir", default=str(DEFAULT_OUTDIR))
    p.add_argument("--start", default="2024-01-01")
    p.add_argument("--end", default="2026-08-25")
    return p.parse_args()


def utc(v):
    x = pd.Timestamp(v)
    return x.tz_localize("UTC") if x.tzinfo is None else x.tz_convert("UTC")


def finite_mean(s):
    x = pd.to_numeric(s, errors="coerce").dropna()
    return float(x.mean()) if len(x) else np.nan


def finite_median(s):
    x = pd.to_numeric(s, errors="coerce").dropna()
    return float(x.median()) if len(x) else np.nan


def max_drawdown(returns):
    r = pd.Series(returns).dropna()
    if r.empty:
        return np.nan
    equity = (1.0 + r).cumprod()
    dd = equity / equity.cummax() - 1.0
    return float(dd.min())


def profit_factor(returns):
    r = pd.Series(returns).dropna()
    gains = r[r > 0].sum()
    losses = -r[r < 0].sum()
    if losses == 0:
        return np.inf if gains > 0 else np.nan
    return float(gains / losses)


def condition_mask(events):
    # Reconstruct the exact v0.4.4 survivor from stored event features.
    return (
        events["bullish_crossover"].fillna(False).astype(bool)
        & (events["macd_hist"] > 0)
        & (events["rel_volume"] >= 1.0)
    )


def load_signals(path):
    events = pd.read_csv(path)
    required = {
        "symbol", "decision_timestamp", "entry_timestamp", "entry_open",
        "forward_1d", "forward_3d", "forward_5d", "forward_10d",
        "mfe_5d", "mae_5d", "atr_pct", "bullish_crossover",
        "macd_hist", "rel_volume",
    }
    missing = sorted(required - set(events.columns))
    if missing:
        raise ValueError(f"Missing required event columns: {missing}")

    events["decision_timestamp"] = pd.to_datetime(events["decision_timestamp"], utc=True)
    events["entry_timestamp"] = pd.to_datetime(events["entry_timestamp"], utc=True)
    events["entry_open"] = pd.to_numeric(events["entry_open"], errors="coerce")

    signals = events.loc[condition_mask(events)].copy()
    signals = signals.dropna(subset=["entry_timestamp", "entry_open"])
    signals = signals.sort_values(["symbol", "entry_timestamp"]).reset_index(drop=True)

    # Keep only one entry per symbol when entries overlap the same 5-day
    # holding window. This avoids artificially multiplying nearly identical
    # trades in the exit research.
    kept = []
    last_entry = {}
    for idx, row in signals.iterrows():
        prev = last_entry.get(row["symbol"])
        if prev is None or row["entry_timestamp"] >= prev + pd.Timedelta(days=5):
            kept.append(idx)
            last_entry[row["symbol"]] = row["entry_timestamp"]

    return signals.loc[kept].reset_index(drop=True)


def fetch_symbol(client, symbol, start, end):
    req = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Day,
        start=start,
        end=end,
        feed=DataFeed.IEX,
    )
    df = client.get_stock_bars(req).df
    if df.empty:
        return pd.DataFrame()

    if isinstance(df.index, pd.MultiIndex):
        try:
            df = df.xs(symbol, level="symbol")
        except KeyError:
            return pd.DataFrame()

    df = df.copy()
    df.index = pd.to_datetime(df.index, utc=True)
    cols = ["open", "high", "low", "close", "volume"]
    return df[cols].sort_index()


def atr14(df):
    prev = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev).abs(),
        (df["low"] - prev).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(14, min_periods=14).mean()


def simulate_trade(bars, entry_time, entry_price, policy, max_days):
    """
    Return path-level exit statistics for one trade.

    policy:
      ("TIME", days)
      ("FIXED", stop_pct, target_pct)
      ("ATR", stop_atr, target_atr, entry_atr)
      ("TRAIL", trail_pct)

    All exits are evaluated from the daily bar after entry.
    """
    future = bars.loc[bars.index >= entry_time].copy()
    if future.empty:
        return None

    entry_idx = future.index[0]
    if future.loc[entry_idx, "open"] != entry_price:
        # We use the stored event entry price, but the fetched bar is the
        # authoritative path source.
        entry_price = float(future.loc[entry_idx, "open"])

    if policy[0] == "TIME":
        horizon = int(policy[1])
        segment = future.iloc[:horizon]
        if segment.empty:
            return None
        exit_price = float(segment.iloc[-1]["close"])
        ret = exit_price / entry_price - 1.0
        mfe = float(segment["high"].max()) / entry_price - 1.0
        mae = float(segment["low"].min()) / entry_price - 1.0
        return {
            "exit_type": f"TIME_{horizon}D",
            "exit_time": segment.index[-1],
            "return": ret,
            "mfe": mfe,
            "mae": mae,
            "stop_hit": False,
            "target_hit": False,
            "ambiguous": False,
            "holding_days": len(segment),
        }

    entry_bar = future.iloc[0]
    entry_atr = float(entry_bar.get("atr14", np.nan))
    if policy[0] == "ATR" and not np.isfinite(entry_atr):
        # Fall back to 1% ATR-equivalent only when ATR cannot be calculated.
        return None

    if policy[0] == "FIXED":
        stop = entry_price * (1.0 - policy[1])
        target = entry_price * (1.0 + policy[2])
    elif policy[0] == "ATR":
        stop = entry_price - policy[1] * entry_atr
        target = entry_price + policy[2] * entry_atr
    elif policy[0] == "TRAIL":
        trail_pct = policy[1]
        highest = entry_price
        stop = entry_price * (1.0 - trail_pct)
        target = np.inf
    else:
        raise ValueError(policy)

    segment = future.iloc[:max_days]
    highest = entry_price
    mfe = 0.0
    mae = 0.0

    for i, (ts, row) in enumerate(segment.iterrows(), start=1):
        high = float(row["high"])
        low = float(row["low"])
        close = float(row["close"])

        mfe = max(mfe, high / entry_price - 1.0)
        mae = min(mae, low / entry_price - 1.0)

        if policy[0] == "TRAIL":
            highest = max(highest, high)
            stop = max(stop, highest * (1.0 - trail_pct))

        hit_stop = low <= stop
        hit_target = high >= target

        if hit_stop and hit_target:
            # Daily OHLC cannot tell which occurred first.
            # Conservative report: stop first.
            return {
                "exit_type": "STOP_FIRST_AMBIGUOUS",
                "exit_time": ts,
                "return": stop / entry_price - 1.0,
                "mfe": mfe,
                "mae": mae,
                "stop_hit": True,
                "target_hit": True,
                "ambiguous": True,
                "holding_days": i,
            }

        if hit_stop:
            return {
                "exit_type": "STOP",
                "exit_time": ts,
                "return": stop / entry_price - 1.0,
                "mfe": mfe,
                "mae": mae,
                "stop_hit": True,
                "target_hit": False,
                "ambiguous": False,
                "holding_days": i,
            }

        if hit_target:
            return {
                "exit_type": "TARGET",
                "exit_time": ts,
                "return": target / entry_price - 1.0,
                "mfe": mfe,
                "mae": mae,
                "stop_hit": False,
                "target_hit": True,
                "ambiguous": False,
                "holding_days": i,
            }

    if segment.empty:
        return None

    last = segment.iloc[-1]
    return {
        "exit_type": "TIME_CAP",
        "exit_time": segment.index[-1],
        "return": float(last["close"]) / entry_price - 1.0,
        "mfe": mfe,
        "mae": mae,
        "stop_hit": False,
        "target_hit": False,
        "ambiguous": False,
        "holding_days": len(segment),
    }


def policy_name(policy):
    if policy[0] == "TIME":
        return f"TIME_{policy[1]}D"
    if policy[0] == "FIXED":
        return f"FIXED_SL{policy[1]*100:.1f}_TP{policy[2]*100:.1f}"
    if policy[0] == "ATR":
        return f"ATR_SL{policy[1]:.1f}_TP{policy[2]:.1f}"
    if policy[0] == "TRAIL":
        return f"TRAIL_{policy[1]*100:.1f}"
    return str(policy)


def build_policies():
    policies = [( "TIME", d) for d in HORIZONS]
    policies += [("FIXED", sl, tp) for sl, tp in FIXED_POLICIES]
    policies += [("ATR", sl, tp) for sl, tp in ATR_POLICIES]
    policies += [("TRAIL", trail) for trail, in TRAILING_POLICIES]
    return policies


def evaluate_policy(signals, bars_by_symbol, policy):
    rows = []

    for _, signal in signals.iterrows():
        symbol = signal["symbol"]
        bars = bars_by_symbol.get(symbol)
        if bars is None or bars.empty:
            continue

        result = simulate_trade(
            bars,
            signal["entry_timestamp"],
            float(signal["entry_open"]),
            policy,
            max_days=10,
        )
        if result is None:
            continue

        result.update({
            "symbol": symbol,
            "signal_time": signal["decision_timestamp"],
            "entry_time": signal["entry_timestamp"],
            "entry_price": float(signal["entry_open"]),
            "policy": policy_name(policy),
            "survivor": SURVIVOR,
        })
        rows.append(result)

    return pd.DataFrame(rows)


def summarize(trades):
    if trades.empty:
        return {
            "signals": 0,
            "mean_return": np.nan,
            "median_return": np.nan,
            "positive_rate": np.nan,
            "profit_factor": np.nan,
            "expectancy": np.nan,
            "mfe_mean": np.nan,
            "mfe_median": np.nan,
            "mae_mean": np.nan,
            "mae_median": np.nan,
            "mfe_mae_ratio": np.nan,
            "stop_hit_rate": np.nan,
            "target_hit_rate": np.nan,
            "ambiguous_rate": np.nan,
            "max_drawdown": np.nan,
            "p10_return": np.nan,
            "p25_return": np.nan,
            "p75_return": np.nan,
            "p90_return": np.nan,
        }

    r = pd.to_numeric(trades["return"], errors="coerce").dropna()
    mfe = pd.to_numeric(trades["mfe"], errors="coerce").dropna()
    mae = pd.to_numeric(trades["mae"], errors="coerce").dropna()

    mfe_mean = float(mfe.mean()) if len(mfe) else np.nan
    mae_mean = float(mae.mean()) if len(mae) else np.nan
    ratio = mfe_mean / abs(mae_mean) if np.isfinite(mfe_mean) and np.isfinite(mae_mean) and mae_mean < 0 else np.nan

    return {
        "signals": len(r),
        "mean_return": float(r.mean()),
        "median_return": float(r.median()),
        "positive_rate": float((r > 0).mean()),
        "profit_factor": profit_factor(r),
        "expectancy": float(r.mean()),
        "mfe_mean": mfe_mean,
        "mfe_median": float(mfe.median()) if len(mfe) else np.nan,
        "mae_mean": mae_mean,
        "mae_median": float(mae.median()) if len(mae) else np.nan,
        "mfe_mae_ratio": ratio,
        "stop_hit_rate": float(trades["stop_hit"].mean()),
        "target_hit_rate": float(trades["target_hit"].mean()),
        "ambiguous_rate": float(trades["ambiguous"].mean()),
        "max_drawdown": max_drawdown(r),
        "p10_return": float(r.quantile(0.10)),
        "p25_return": float(r.quantile(0.25)),
        "p75_return": float(r.quantile(0.75)),
        "p90_return": float(r.quantile(0.90)),
    }


def distribution_table(trades):
    if trades.empty:
        return pd.DataFrame()

    rows = []
    for policy, g in trades.groupby("policy"):
        r = pd.to_numeric(g["return"], errors="coerce").dropna()
        mfe = pd.to_numeric(g["mfe"], errors="coerce").dropna()
        mae = pd.to_numeric(g["mae"], errors="coerce").dropna()

        rows.append({
            "policy": policy,
            "signals": len(g),
            "return_p05": r.quantile(.05),
            "return_p10": r.quantile(.10),
            "return_p25": r.quantile(.25),
            "return_p50": r.quantile(.50),
            "return_p75": r.quantile(.75),
            "return_p90": r.quantile(.90),
            "return_p95": r.quantile(.95),
            "mfe_p10": mfe.quantile(.10),
            "mfe_p25": mfe.quantile(.25),
            "mfe_p50": mfe.quantile(.50),
            "mfe_p75": mfe.quantile(.75),
            "mfe_p90": mfe.quantile(.90),
            "mae_p10": mae.quantile(.10),
            "mae_p25": mae.quantile(.25),
            "mae_p50": mae.quantile(.50),
            "mae_p75": mae.quantile(.75),
            "mae_p90": mae.quantile(.90),
            "mae_le_minus_1pct": float((mae <= -.01).mean()),
            "mae_le_minus_2pct": float((mae <= -.02).mean()),
            "mae_le_minus_3pct": float((mae <= -.03).mean()),
            "mfe_ge_1pct": float((mfe >= .01).mean()),
            "mfe_ge_2pct": float((mfe >= .02).mean()),
            "mfe_ge_3pct": float((mfe >= .03).mean()),
            "mfe_ge_5pct": float((mfe >= .05).mean()),
        })

    return pd.DataFrame(rows)


def main():
    args = parse_args()
    load_dotenv()

    api_key = os.getenv("ALPACA_API_KEY")
    secret = os.getenv("ALPACA_SECRET_KEY")
    if not api_key or not secret:
        raise RuntimeError("Missing ALPACA_API_KEY / ALPACA_SECRET_KEY in .env")

    events = load_signals(args.events)
    if events.empty:
        raise RuntimeError("No signals matching the v0.4.4 survivor were found.")

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    print("=" * 72)
    print("AURA v0.4.6 — RISK & EXIT RESEARCH ENGINE")
    print("=" * 72)
    print(f"Survivor:       {SURVIVOR}")
    print(f"Conditions:     {SURVIVOR_CONDITIONS}")
    print(f"Non-overlap signals: {len(events)}")
    print(f"Symbols:        {events['symbol'].nunique()}")
    print("Mode:           RESEARCH ONLY — NO ORDERS")
    print()

    client = StockHistoricalDataClient(api_key, secret)

    start = min(events["entry_timestamp"].min(), utc(args.start))
    end = max(events["entry_timestamp"].max(), utc(args.end)) + pd.Timedelta(days=15)

    bars_by_symbol = {}
    for i, symbol in enumerate(sorted(events["symbol"].unique()), start=1):
        try:
            raw = fetch_symbol(client, symbol, start.isoformat(), end.isoformat())
            if raw.empty:
                print(f"[{i}] {symbol}: NO DATA")
                continue
            raw["atr14"] = atr14(raw)
            bars_by_symbol[symbol] = raw
            print(f"[{i}] {symbol}: {len(raw)} daily bars")
        except Exception as exc:
            print(f"[{i}] {symbol}: FAILED — {exc}")

    all_trades = []
    summaries = []

    for policy in build_policies():
        trades = evaluate_policy(events, bars_by_symbol, policy)
        if not trades.empty:
            all_trades.append(trades)

        s = summarize(trades)
        s["policy"] = policy_name(policy)
        summaries.append(s)

    trades = pd.concat(all_trades, ignore_index=True) if all_trades else pd.DataFrame()
    summary = pd.DataFrame(summaries)

    # This is a descriptive research ranking only. We deliberately do not
    # label a policy "optimal"; the output is intended to guide v0.4.7.
    summary = summary.sort_values(
        ["positive_rate", "median_return", "profit_factor"],
        ascending=[False, False, False],
    )

    summary.to_csv(outdir / "v046_exit_policy_summary.csv", index=False)
    trades.to_csv(outdir / "v046_trade_level_exit_results.csv", index=False)

    dist = distribution_table(trades)
    dist.to_csv(outdir / "v046_mfe_mae_distribution.csv", index=False)

    # Time-exit benchmark separately.
    time_summary = summary[summary["policy"].str.startswith("TIME_")].copy()
    time_summary.to_csv(outdir / "v046_time_exit_benchmark.csv", index=False)

    print()
    print("=" * 72)
    print("EXIT POLICY SUMMARY")
    print("=" * 72)
    display_cols = [
        "policy", "signals", "mean_return", "median_return",
        "positive_rate", "profit_factor", "max_drawdown",
        "mfe_mae_ratio", "stop_hit_rate", "target_hit_rate",
        "ambiguous_rate",
    ]
    print(summary[display_cols].to_string(index=False, float_format=lambda x: f"{x:.4f}"))

    print()
    print("=" * 72)
    print("MFE / MAE DISTRIBUTION")
    print("=" * 72)
    if dist.empty:
        print("No distribution data.")
    else:
        cols = [
            "policy", "signals",
            "mae_p10", "mae_p25", "mae_p50",
            "mae_le_minus_1pct", "mae_le_minus_2pct", "mae_le_minus_3pct",
            "mfe_p50", "mfe_p75", "mfe_p90",
            "mfe_ge_1pct", "mfe_ge_2pct", "mfe_ge_3pct", "mfe_ge_5pct",
        ]
        print(dist[cols].to_string(index=False, float_format=lambda x: f"{x:.4f}"))

    print()
    print("=" * 72)
    print("IMPORTANT INTERPRETATION")
    print("=" * 72)
    print("These results describe exit behavior; they do NOT prove an optimal stop/target.")
    print("Daily OHLC cannot determine intraday order when stop and target hit in one bar.")
    print("Those trades are marked ambiguous and reported conservatively as stop-first.")
    print("Do not deploy an exit policy from this report without a fresh holdout test.")

    print()
    print("CSV OUTPUT")
    for fn in [
        "v046_exit_policy_summary.csv",
        "v046_trade_level_exit_results.csv",
        "v046_mfe_mae_distribution.csv",
        "v046_time_exit_benchmark.csv",
    ]:
        print(outdir / fn)

    print()
    print("AURA v0.4.6 COMPLETE — NO ORDERS WERE PLACED")


if __name__ == "__main__":
    main()
