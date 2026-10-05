#!/usr/bin/env python3
"""
AURA v0.5.4 -- Equity/ETF trade diagnostics (MAE/MFE + the rest)

Generalizes `mexc_bot/trade_diagnostics.py`'s clean-room, read-only
analysis approach to `.054`'s equity/ETF backtest trades. Added
2026-10-04, Martin-approved, as part of "fix the remaining bugs and the
other things remaining" (Track B item: MAE/MFE for equities -- the
crypto side already had this, equities did not).

WHERE THE DATA COMES FROM
--------------------------
`aura_v054_exit_engine.simulate_atr_trailing_trade` already computes
`mfe_frac`/`mae_frac` (max favorable/adverse excursion, fractional
return from entry, walked bar-by-bar with the same no-look-ahead
discipline as every other `.054` exit decision) for every trade -- this
was previously computed and then DISCARDED at the point `TradeRecord`
was built in `aura_v054_backtest.py`. As of 2026-10-04, `TradeRecord`
carries `mfe_frac`/`mae_frac` through, and
`aura_v054_backtest.trade_records_to_dataframe()` serializes a trades
list (e.g. `BacktestReport.trades`) to a flat `pd.DataFrame`. This
script is the read-only analysis layer on top of that -- it does not
run a backtest or touch any strategy/risk/exit/sizing module.

DIFFERENCES FROM THE CRYPTO VERSION (disclosed, not hidden)
-------------------------------------------------------------
- No LONG/SHORT breakdown: `.054` is LONG-only (documented scope limit
  of the exit engine itself, which raises on `direction != "LONG"`).
- No "score" bucket breakdown: `.054`'s `TradeRecord` does not carry a
  composite score column the way the crypto trades CSV does. (`.350`'s
  `base_rank_score` exists upstream but is not currently plumbed through
  to `TradeRecord` -- a possible future addition, not done here.)
- R-multiples are derived here, not read from a pre-existing column:
  `realized_r = realized_pnl_dollars / planned_risk_dollars`,
  `mfe_r = mfe_frac * market_value / planned_risk_dollars`,
  `mae_r = mae_frac * market_value / planned_risk_dollars` -- consistent
  with how `realized_pnl_dollars` itself is computed in
  `aura_v054_backtest.py` (`market_value * net_return_frac`), and with
  `planned_risk_dollars` being the same per-trade risk budget that
  `aura_v054_position_sizing.size_position_by_atr_risk` sized the
  position against. `mae_frac` is a negative fractional return by
  convention (see `simulate_atr_trailing_trade`), so `mae_r` is negative
  too, matching the crypto tool's own sign convention (buckets take the
  absolute value for readability, same as the crypto script).
- `bars_held` instead of a calendar "candles_held" column -- same
  concept (`exit_bar_index - entry_bar_index`), already computed by
  `trade_records_to_dataframe`.

Input:
    A CSV produced by `aura_v054_backtest.trade_records_to_dataframe(
    report.trades).to_csv(path, index=False)` for some completed
    `BacktestReport` -- NOT a path this script fabricates or guesses.

Usage:
    python aura_v054_trade_diagnostics.py <trades_csv_path> [--out-dir DIR]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

VERSION = "AURA v0.5.4"


def find_col(df: pd.DataFrame, *names: str) -> str | None:
    """Return the first matching column, case-insensitively (same helper
    as the crypto tool, reused verbatim for consistency)."""
    lookup = {str(c).strip().lower(): c for c in df.columns}
    for name in names:
        if name.lower() in lookup:
            return lookup[name.lower()]
    return None


def numeric(df: pd.DataFrame, col: str | None) -> pd.Series:
    if col is None:
        return pd.Series(np.nan, index=df.index)
    return pd.to_numeric(df[col], errors="coerce")


def group_stats(df: pd.DataFrame, group_col: str) -> pd.DataFrame:
    rows = []
    for key, g in df.groupby(group_col, dropna=False, sort=True):
        pnl = g["_realized_r"].dropna()
        wins = pnl[pnl > 0]
        losses = pnl[pnl < 0]
        gross_profit = wins.sum()
        gross_loss = abs(losses.sum())
        pf = gross_profit / gross_loss if gross_loss > 0 else np.nan

        rows.append({
            str(group_col): key,
            "trades": len(g),
            "win_rate_pct": (pnl > 0).mean() * 100 if len(pnl) else np.nan,
            "profit_factor": pf,
            "expectancy_r": pnl.mean() if len(pnl) else np.nan,
            "total_r": pnl.sum() if len(pnl) else np.nan,
            "avg_mfe_r": g["_mfe_r"].mean(),
            "avg_mae_r": g["_mae_r"].mean(),
            "avg_bars_held": g["_bars_held"].mean(),
            "pct_reached_1R": (g["_mfe_r"] >= 1.0).mean() * 100,
            "pct_reached_1_5R": (g["_mfe_r"] >= 1.5).mean() * 100,
            "pct_reached_2R": (g["_mfe_r"] >= 2.0).mean() * 100,
        })
    return pd.DataFrame(rows)


def streak_summary(g: pd.DataFrame) -> dict:
    seq = (g["_realized_r"] > 0).tolist()
    streaks: list[int] = []
    cur = 0
    for win in seq:
        if not win:
            cur += 1
        else:
            if cur:
                streaks.append(cur)
            cur = 0
    if cur:
        streaks.append(cur)
    if not streaks:
        return {"trades": len(g), "max_loss_streak": 0, "avg_loss_streak": np.nan,
                "p95_loss_streak": np.nan, "p99_loss_streak": np.nan}
    return {
        "trades": len(g),
        "max_loss_streak": max(streaks),
        "avg_loss_streak": float(np.mean(streaks)),
        "p95_loss_streak": float(np.percentile(streaks, 95)),
        "p99_loss_streak": float(np.percentile(streaks, 99)),
    }


def print_table(title: str, df: pd.DataFrame, max_rows: int = 40) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)
    if df.empty:
        print("(no data)")
    else:
        with pd.option_context("display.max_rows", max_rows, "display.max_columns", 30, "display.width", 220):
            print(df.head(max_rows).to_string(index=False))


def run(input_csv: Path, out_dir: Path) -> None:
    if not input_csv.exists():
        print(f"ERROR: Input file not found:\n{input_csv}")
        print("\nProduce it from a BacktestReport with:")
        print("  import aura_v054_backtest as BT")
        print("  BT.trade_records_to_dataframe(report.trades).to_csv(path, index=False)")
        sys.exit(1)

    df = pd.read_csv(input_csv)

    print("=" * 78)
    print("AURA v0.5.4 EQUITY/ETF TRADE DIAGNOSTICS -- MAE/MFE AND THE REST")
    print("=" * 78)
    print(f"Input: {input_csv}")
    print(f"Rows loaded: {len(df)}")

    symbol_col = find_col(df, "symbol", "ticker")
    entry_time_col = find_col(df, "entry_timestamp", "entry_time", "timestamp")
    pnl_col = find_col(df, "realized_pnl_dollars")
    risk_col = find_col(df, "planned_risk_dollars")
    market_value_col = find_col(df, "market_value")
    mfe_col = find_col(df, "mfe_frac")
    mae_col = find_col(df, "mae_frac")
    bars_held_col = find_col(df, "bars_held")
    period_col = find_col(df, "period")
    exit_reason_col = find_col(df, "exit_reason")

    required = {
        "realized_pnl_dollars": pnl_col,
        "planned_risk_dollars": risk_col,
        "market_value": market_value_col,
        "mfe_frac": mfe_col,
        "mae_frac": mae_col,
        "entry_timestamp": entry_time_col,
    }
    missing = [k for k, v in required.items() if v is None]
    if missing:
        print("\nERROR: Required columns could not be found:")
        for m in missing:
            print(f"  - {m}")
        print("\nAvailable columns:")
        print(list(df.columns))
        sys.exit(2)

    df["_risk"] = numeric(df, risk_col)
    df["_market_value"] = numeric(df, market_value_col)
    df["_realized_r"] = numeric(df, pnl_col) / df["_risk"].replace(0, np.nan)
    df["_mfe_r"] = numeric(df, mfe_col) * df["_market_value"] / df["_risk"].replace(0, np.nan)
    df["_mae_r"] = numeric(df, mae_col) * df["_market_value"] / df["_risk"].replace(0, np.nan)
    df["_bars_held"] = numeric(df, bars_held_col)
    df["_entry_time"] = pd.to_datetime(df[entry_time_col], errors="coerce", utc=True)

    df = df[df["_realized_r"].notna()].copy()
    print(f"Usable (resolved) trades: {len(df)}")

    if period_col:
        print("\nPeriod breakdown:")
        print(df[period_col].value_counts(dropna=False).to_string())

    results: dict[str, pd.DataFrame] = {}

    # ---- Overall ----
    pnl = df["_realized_r"].dropna()
    wins = pnl[pnl > 0]
    losses = pnl[pnl < 0]
    gross_profit = wins.sum()
    gross_loss = abs(losses.sum())
    overall_pf = gross_profit / gross_loss if gross_loss > 0 else np.nan

    print("\n--- OVERALL ---")
    print(f"Trades:                  {len(df)}")
    print(f"Win rate:                {(pnl > 0).mean() * 100:.2f}%")
    print(f"Profit factor:           {overall_pf:.3f}")
    print(f"Expectancy:              {pnl.mean():.4f} R")
    print(f"Total realized R:        {pnl.sum():.2f} R")
    print(f"Average MFE:             {df['_mfe_r'].mean():.3f} R")
    print(f"Average MAE:             {df['_mae_r'].mean():.3f} R")
    print(f"Median MFE:              {df['_mfe_r'].median():.3f} R")
    print(f"Median MAE:              {df['_mae_r'].median():.3f} R")

    # ---- Symbol ----
    if symbol_col:
        s = df.copy()
        s["_symbol"] = s[symbol_col].astype(str)
        results["symbol"] = group_stats(s, "_symbol").sort_values(["trades", "expectancy_r"], ascending=[False, False])
        print_table("1. SYMBOL PERFORMANCE -- inspect only; do NOT select winners from this table",
                     results["symbol"].sort_values("expectancy_r", ascending=False), max_rows=50)

    # ---- Exit reason ----
    if exit_reason_col:
        e = df.copy()
        e["_exit_reason"] = e[exit_reason_col].astype(str)
        results["exit_reason"] = group_stats(e, "_exit_reason")
        print_table("2. EXIT REASON (STOP / TARGET / TIMEOUT)", results["exit_reason"])

    # ---- Hour / weekday ----
    if df["_entry_time"].notna().any():
        df["_hour"] = df["_entry_time"].dt.hour
        results["hour"] = group_stats(df, "_hour").sort_values("_hour")
        print_table("3. ENTRY HOUR (UTC)", results["hour"], max_rows=24)

        df["_weekday"] = df["_entry_time"].dt.day_name()
        weekday_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        results["weekday"] = group_stats(df, "_weekday")
        results["weekday"]["_order"] = results["weekday"]["_weekday"].map({x: i for i, x in enumerate(weekday_order)})
        results["weekday"] = results["weekday"].sort_values("_order").drop(columns="_order")
        print_table("4. DAY OF WEEK (UTC)", results["weekday"], max_rows=7)

    # ---- MFE monetization matrix ----
    thresholds = [0.5, 1.0, 1.5, 2.0, 3.0]
    mfe_rows = []
    for threshold in thresholds:
        reached = df[df["_mfe_r"] >= threshold]
        roundtrip = reached[reached["_realized_r"] <= 0]
        mfe_rows.append({
            "mfe_threshold_R": threshold,
            "trades_reached": len(reached),
            "pct_total": len(reached) / len(df) * 100 if len(df) else np.nan,
            "pct_roundtripped_to_loss_or_flat": (len(roundtrip) / len(reached) * 100) if len(reached) else np.nan,
            "median_bars_held": reached["_bars_held"].median() if len(reached) else np.nan,
        })
    results["mfe_thresholds"] = pd.DataFrame(mfe_rows)
    print_table("5. MFE MONETIZATION MATRIX", results["mfe_thresholds"], max_rows=10)

    # ---- MFE buckets ----
    bins = [-np.inf, 0.5, 1.0, 1.5, 2.0, 3.0, np.inf]
    labels = ["<0.5R", "0.5-<1R", "1-<1.5R", "1.5-<2R", "2-<3R", ">=3R"]
    df["_mfe_bucket"] = pd.cut(df["_mfe_r"], bins=bins, labels=labels, right=False)
    results["mfe_buckets"] = group_stats(df, "_mfe_bucket")
    print_table("6. MFE BUCKETS", results["mfe_buckets"], max_rows=10)

    # ---- MAE buckets (absolute adverse excursion) ----
    df["_abs_mae_r"] = df["_mae_r"].abs()
    mae_bins = [-np.inf, 0.25, 0.5, 0.75, 1.0, 1.5, np.inf]
    mae_labels = ["<0.25R", "0.25-<0.5R", "0.5-<0.75R", "0.75-<1R", "1-<1.5R", ">=1.5R"]
    df["_mae_bucket"] = pd.cut(df["_abs_mae_r"], bins=mae_bins, labels=mae_labels, right=False)
    results["mae_buckets"] = group_stats(df, "_mae_bucket")
    print_table("7. MAE BUCKETS", results["mae_buckets"], max_rows=10)

    # ---- MFE/MAE relationship ----
    valid = df[["_mfe_r", "_abs_mae_r", "_realized_r"]].dropna()
    print("\n" + "=" * 78)
    print("8. MFE vs MAE RELATIONSHIP")
    print("=" * 78)
    if len(valid) >= 3:
        print(f"Correlation MFE vs absolute MAE: {valid['_mfe_r'].corr(valid['_abs_mae_r']):.4f}")
        print(f"Correlation MFE vs realized R:    {valid['_mfe_r'].corr(valid['_realized_r']):.4f}")
        print(f"Correlation MAE vs realized R:    {valid['_abs_mae_r'].corr(valid['_realized_r']):.4f}")

    # ---- Research vs holdout ----
    if period_col:
        p = df.copy()
        p["_period"] = p[period_col].astype(str).str.upper()
        results["period"] = group_stats(p, "_period")
        print_table("9. RESEARCH vs HOLDOUT", results["period"], max_rows=10)

    # ---- Losing streaks ----
    print("\n" + "=" * 78)
    print("10. EMPIRICAL LOSING STREAKS")
    print("=" * 78)
    print(pd.DataFrame([streak_summary(df)]).to_string(index=False))
    if period_col:
        streak_rows = []
        for key, g in df.groupby(period_col, dropna=False):
            r = streak_summary(g)
            r["period"] = key
            streak_rows.append(r)
        results["streaks"] = pd.DataFrame(streak_rows)
        print(results["streaks"].to_string(index=False))

    # ---- Save CSVs ----
    out_dir.mkdir(parents=True, exist_ok=True)
    for key, value in results.items():
        if isinstance(value, pd.DataFrame):
            value.to_csv(out_dir / f"trade_diagnostics_{key}.csv", index=False)

    summary_rows = [{
        "trades": len(df),
        "win_rate_pct": (pnl > 0).mean() * 100,
        "profit_factor": overall_pf,
        "expectancy_r": pnl.mean(),
        "total_realized_r": pnl.sum(),
        "avg_mfe_r": df["_mfe_r"].mean(),
        "avg_mae_r": df["_mae_r"].mean(),
        "median_mfe_r": df["_mfe_r"].median(),
        "median_mae_r": df["_mae_r"].median(),
    }]
    pd.DataFrame(summary_rows).to_csv(out_dir / "trade_diagnostics_summary.csv", index=False)

    print("\n" + "=" * 78)
    print("DIAGNOSTIC COMPLETE")
    print("=" * 78)
    print(f"Results written to: {out_dir}")
    print("\nNo strategy, backtest, or decision-engine files were modified by this script.")


def main() -> None:
    parser = argparse.ArgumentParser(description="AURA v0.5.4 equity/ETF trade diagnostics (MAE/MFE and the rest).")
    parser.add_argument("input_csv", type=Path, help="Trades CSV produced by trade_records_to_dataframe(report.trades).to_csv(...)")
    parser.add_argument("--out-dir", type=Path, default=None, help="Output directory for the diagnostic CSVs (default: alongside the input file)")
    args = parser.parse_args()
    out_dir = args.out_dir or args.input_csv.parent
    run(args.input_csv, out_dir)


if __name__ == "__main__":
    main()
