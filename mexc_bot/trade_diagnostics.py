"""
Trade Diagnostics — clean-room analysis of the 1,458 CURRENT trades.

Input:
    backtest_results/current_1458_trades.csv

This script is ANALYSIS ONLY.
It does not modify the strategy, backtester, parameters, or existing results.

Outputs:
    backtest_results/trade_diagnostics_summary.csv
    backtest_results/trade_diagnostics_symbol.csv
    backtest_results/trade_diagnostics_score.csv
    backtest_results/trade_diagnostics_hour.csv
    backtest_results/trade_diagnostics_weekday.csv
    backtest_results/trade_diagnostics_mfe_mae.csv
    backtest_results/trade_diagnostics_mfe_thresholds.csv

Usage:
    python trade_diagnostics.py
"""

from pathlib import Path
import sys
import numpy as np
import pandas as pd


BASE = Path(__file__).resolve().parent
INPUT = BASE / "backtest_results" / "current_1458_trades.csv"
OUT = BASE / "backtest_results"


def find_col(df, *names):
    """Return the first matching column, case-insensitively."""
    lookup = {str(c).strip().lower(): c for c in df.columns}
    for name in names:
        if name.lower() in lookup:
            return lookup[name.lower()]
    return None


def numeric(df, col):
    if col is None:
        return pd.Series(np.nan, index=df.index)
    return pd.to_numeric(df[col], errors="coerce")


def group_stats(df, group_col):
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
            "avg_candles_held": g["_candles_held"].mean(),
            "pct_reached_1R": (g["_mfe_r"] >= 1.0).mean() * 100,
            "pct_reached_1_5R": (g["_mfe_r"] >= 1.5).mean() * 100,
            "pct_reached_2R": (g["_mfe_r"] >= 2.0).mean() * 100,
        })

    return pd.DataFrame(rows)


def print_table(title, df, max_rows=40):
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)
    if df.empty:
        print("(no data)")
    else:
        with pd.option_context("display.max_rows", max_rows,
                               "display.max_columns", 30,
                               "display.width", 220):
            print(df.head(max_rows).to_string(index=False))


def main():
    if not INPUT.exists():
        print(f"ERROR: Input file not found:\n{INPUT}")
        print("\nCreate it first from exit_sensitivity_trades_*.csv with:")
        print("  python -c \"import pandas as pd; ...\"")
        sys.exit(1)

    df = pd.read_csv(INPUT)

    print("=" * 78)
    print("TRADE DIAGNOSTICS — CURRENT 1,458 TRADE CLEAN-ROOM ANALYSIS")
    print("=" * 78)
    print(f"Input: {INPUT}")
    print(f"Rows loaded: {len(df)}")

    # Flexible column mapping because column names may differ slightly between builds.
    direction_col = find_col(df, "direction", "side")
    symbol_col = find_col(df, "symbol", "ticker", "pair")
    score_col = find_col(df, "score", "composite_score", "compositeScore")
    entry_time_col = find_col(df, "entry_time", "entry_timestamp", "timestamp", "entry")
    realized_col = find_col(df, "realized_r", "realized_pnl_r", "realizedR", "avg_realized_r")
    mfe_col = find_col(df, "mfe_r", "mfeR", "avg_mfe_r")
    mae_col = find_col(df, "mae_r", "maeR", "avg_mae_r")
    candles_col = find_col(df, "candles_held", "bars_held", "holding_candles")
    period_col = find_col(df, "period")

    required = {
        "realized R": realized_col,
        "MFE R": mfe_col,
        "MAE R": mae_col,
        "entry time": entry_time_col,
    }
    missing = [k for k, v in required.items() if v is None]
    if missing:
        print("\nERROR: Required columns could not be found:")
        for m in missing:
            print(f"  - {m}")
        print("\nAvailable columns:")
        print(list(df.columns))
        sys.exit(2)

    # Standard analysis columns.
    df["_realized_r"] = numeric(df, realized_col)
    df["_mfe_r"] = numeric(df, mfe_col)
    df["_mae_r"] = numeric(df, mae_col)
    df["_candles_held"] = numeric(df, candles_col)
    df["_entry_time"] = pd.to_datetime(df[entry_time_col], errors="coerce", utc=True)

    # Remove rows with no usable trade outcome.
    df = df[df["_realized_r"].notna()].copy()

    print(f"Usable trades: {len(df)}")

    if period_col:
        print("\nPeriod breakdown:")
        print(df[period_col].value_counts(dropna=False).to_string())

    # ------------------------------------------------------------------
    # 1. Overall
    # ------------------------------------------------------------------
    pnl = df["_realized_r"].dropna()
    wins = pnl[pnl > 0]
    losses = pnl[pnl < 0]
    gross_profit = wins.sum()
    gross_loss = abs(losses.sum())
    overall_pf = gross_profit / gross_loss if gross_loss > 0 else np.nan

    max_losing_streak = 0
    current_streak = 0
    for value in (pnl > 0).tolist():
        if not value:
            current_streak += 1
            max_losing_streak = max(max_losing_streak, current_streak)
        else:
            current_streak = 0

    print("\n--- OVERALL ---")
    print(f"Trades:                 {len(df)}")
    print(f"Win rate:               {(pnl > 0).mean() * 100:.2f}%")
    print(f"Profit factor:          {overall_pf:.3f}")
    print(f"Expectancy:             {pnl.mean():.4f} R")
    print(f"Total realized R:       {pnl.sum():.2f} R")
    print(f"Average MFE:             {df['_mfe_r'].mean():.3f} R")
    print(f"Average MAE:             {df['_mae_r'].mean():.3f} R")
    print(f"Median MFE:              {df['_mfe_r'].median():.3f} R")
    print(f"Median MAE:              {df['_mae_r'].median():.3f} R")
    print(f"Max observed loss streak:{max_losing_streak}")

    # ------------------------------------------------------------------
    # 2. Direction
    # ------------------------------------------------------------------
    results = {}
    if direction_col:
        d = df.copy()
        d["_direction"] = d[direction_col].astype(str).str.upper()
        results["direction"] = group_stats(d, "_direction")
        print_table("1. LONG vs SHORT", results["direction"])

    # ------------------------------------------------------------------
    # 3. Symbol
    # ------------------------------------------------------------------
    if symbol_col:
        s = df.copy()
        s["_symbol"] = s[symbol_col].astype(str)
        results["symbol"] = group_stats(s, "_symbol").sort_values(
            ["trades", "expectancy_r"], ascending=[False, False]
        )
        print_table(
            "2. SYMBOL PERFORMANCE — inspect only; do NOT select winners from this table",
            results["symbol"].sort_values("expectancy_r", ascending=False),
            max_rows=50,
        )

    # ------------------------------------------------------------------
    # 4. Score buckets
    # ------------------------------------------------------------------
    if score_col:
        score = numeric(df, score_col)
        df["_score"] = score
        score_df = group_stats(df.dropna(subset=["_score"]), "_score")
        results["score"] = score_df.sort_values("_score")
        print_table("3. SCORE → EXPECTANCY / PF MONOTONICITY", results["score"])

        if len(score_df) >= 2:
            ex = score_df["expectancy_r"].dropna().values
            mono = all(ex[i] <= ex[i + 1] for i in range(len(ex) - 1))
            print(f"\nScore expectancy monotonic increasing: {mono}")

    # ------------------------------------------------------------------
    # 5. Hour of day
    # ------------------------------------------------------------------
    if df["_entry_time"].notna().any():
        df["_hour"] = df["_entry_time"].dt.hour
        results["hour"] = group_stats(df, "_hour").sort_values("_hour")
        print_table("4. ENTRY HOUR (UTC)", results["hour"], max_rows=24)

        df["_weekday"] = df["_entry_time"].dt.day_name()
        weekday_order = [
            "Monday", "Tuesday", "Wednesday", "Thursday",
            "Friday", "Saturday", "Sunday"
        ]
        results["weekday"] = group_stats(df, "_weekday")
        results["weekday"]["_order"] = results["weekday"]["_weekday"].map(
            {x: i for i, x in enumerate(weekday_order)}
        )
        results["weekday"] = results["weekday"].sort_values("_order").drop(columns="_order")
        print_table("5. DAY OF WEEK (UTC)", results["weekday"], max_rows=7)

    # ------------------------------------------------------------------
    # 6. MFE thresholds / round-trip analysis
    # ------------------------------------------------------------------
    thresholds = [0.5, 1.0, 1.5, 2.0, 3.0]
    mfe_rows = []

    for threshold in thresholds:
        reached = df[df["_mfe_r"] >= threshold]
        roundtrip = reached[reached["_realized_r"] <= 0]

        if len(reached):
            median_candles = reached["_candles_held"].median()
            median_hours = median_candles  # default fallback below
        else:
            median_candles = np.nan
            median_hours = np.nan

        # If the source has a direct time-to-threshold column, prefer it.
        candidates = [
            f"hit_{threshold:g}R_hours",
            f"hit_{threshold:g}r_hours",
            f"hit_{threshold:g}R",
        ]
        direct_col = find_col(df, *candidates)
        if direct_col:
            t = numeric(reached, direct_col)
            median_hours = t.dropna().median() if t.notna().any() else np.nan

        mfe_rows.append({
            "mfe_threshold_R": threshold,
            "trades_reached": len(reached),
            "pct_total": len(reached) / len(df) * 100,
            "pct_roundtripped_to_loss_or_flat": (
                len(roundtrip) / len(reached) * 100 if len(reached) else np.nan
            ),
            "median_candles_held": median_candles,
            "median_time_to_threshold_hours": median_hours,
        })

    results["mfe_thresholds"] = pd.DataFrame(mfe_rows)
    print_table("6. MFE MONETIZATION MATRIX", results["mfe_thresholds"], max_rows=10)

    # ------------------------------------------------------------------
    # 7. MFE buckets
    # ------------------------------------------------------------------
    bins = [-np.inf, 0.5, 1.0, 1.5, 2.0, 3.0, np.inf]
    labels = [
        "<0.5R",
        "0.5–<1R",
        "1–<1.5R",
        "1.5–<2R",
        "2–<3R",
        ">=3R",
    ]
    df["_mfe_bucket"] = pd.cut(df["_mfe_r"], bins=bins, labels=labels, right=False)
    results["mfe_buckets"] = group_stats(df, "_mfe_bucket")
    print_table("7. MFE BUCKETS", results["mfe_buckets"], max_rows=10)

    # ------------------------------------------------------------------
    # 8. MAE buckets
    # ------------------------------------------------------------------
    # MAE is represented as a negative excursion in the source.
    # Convert to absolute adverse excursion for readable buckets.
    df["_abs_mae_r"] = df["_mae_r"].abs()
    mae_bins = [-np.inf, 0.25, 0.5, 0.75, 1.0, 1.5, np.inf]
    mae_labels = [
        "<0.25R",
        "0.25–<0.5R",
        "0.5–<0.75R",
        "0.75–<1R",
        "1–<1.5R",
        ">=1.5R",
    ]
    df["_mae_bucket"] = pd.cut(
        df["_abs_mae_r"], bins=mae_bins, labels=mae_labels, right=False
    )
    results["mae_buckets"] = group_stats(df, "_mae_bucket")
    print_table("8. MAE BUCKETS", results["mae_buckets"], max_rows=10)

    # ------------------------------------------------------------------
    # 9. MFE / MAE relationship
    # ------------------------------------------------------------------
    valid = df[["_mfe_r", "_abs_mae_r", "_realized_r"]].dropna()
    print("\n" + "=" * 78)
    print("9. MFE vs MAE RELATIONSHIP")
    print("=" * 78)
    if len(valid) >= 3:
        print(f"Correlation MFE vs absolute MAE: {valid['_mfe_r'].corr(valid['_abs_mae_r']):.4f}")
        print(f"Correlation MFE vs realized R:    {valid['_mfe_r'].corr(valid['_realized_r']):.4f}")
        print(f"Correlation MAE vs realized R:    {valid['_abs_mae_r'].corr(valid['_realized_r']):.4f}")

    # ------------------------------------------------------------------
    # 10. Research vs holdout
    # ------------------------------------------------------------------
    if period_col:
        p = df.copy()
        p["_period"] = p[period_col].astype(str).str.upper()
        results["period"] = group_stats(p, "_period")
        print_table("10. RESEARCH vs HOLDOUT", results["period"], max_rows=10)

    # ------------------------------------------------------------------
    # 11. Losing streaks, separately by period where possible
    # ------------------------------------------------------------------
    def streak_summary(g):
        seq = (g["_realized_r"] > 0).tolist()
        streaks = []
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
            return {
                "trades": len(g),
                "max_loss_streak": 0,
                "avg_loss_streak": np.nan,
                "p95_loss_streak": np.nan,
                "p99_loss_streak": np.nan,
            }

        return {
            "trades": len(g),
            "max_loss_streak": max(streaks),
            "avg_loss_streak": np.mean(streaks),
            "p95_loss_streak": np.percentile(streaks, 95),
            "p99_loss_streak": np.percentile(streaks, 99),
        }

    print("\n" + "=" * 78)
    print("11. EMPIRICAL LOSING STREAKS")
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

    # ------------------------------------------------------------------
    # 12. Save CSVs
    # ------------------------------------------------------------------
    OUT.mkdir(exist_ok=True)

    for key, value in results.items():
        if isinstance(value, pd.DataFrame):
            path = OUT / f"trade_diagnostics_{key}.csv"
            value.to_csv(path, index=False)

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
        "max_loss_streak": max_losing_streak,
    }]
    pd.DataFrame(summary_rows).to_csv(
        OUT / "trade_diagnostics_summary.csv", index=False
    )

    print("\n" + "=" * 78)
    print("DIAGNOSTIC COMPLETE")
    print("=" * 78)
    print(f"Results written to: {OUT}")
    print("\nMain files:")
    print("  trade_diagnostics_summary.csv")
    print("  trade_diagnostics_direction.csv")
    print("  trade_diagnostics_symbol.csv")
    print("  trade_diagnostics_score.csv")
    print("  trade_diagnostics_hour.csv")
    print("  trade_diagnostics_weekday.csv")
    print("  trade_diagnostics_mfe_thresholds.csv")
    print("  trade_diagnostics_mfe_buckets.csv")
    print("  trade_diagnostics_mae_buckets.csv")
    print("  trade_diagnostics_period.csv")
    print("  trade_diagnostics_streaks.csv")
    print("\nNo strategy or backtest files were modified.")


if __name__ == "__main__":
    main()
