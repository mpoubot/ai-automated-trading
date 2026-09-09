"""
CCI30 30-DAY REGIME CONFIRMATION TEST
=====================================

Purpose
-------
Test one pre-declared hypothesis discovered in the CCI30 diagnostic:

    Existing strategy performance is better when CCI30's trailing
    30-day return is NEGATIVE than when it is POSITIVE.

This script does NOT change the strategy's entry/exit mechanics. It filters
the already-frozen CURRENT trade inventory by CCI30 regime.

IMPORTANT RESEARCH-HYGIENE NOTE
--------------------------------
The negative-30D observation was discovered while examining the full
1,458-trade dataset. Therefore this run is a CONFIRMATION / DIAGNOSTIC
experiment, not a pristine out-of-sample discovery test. The chronological
holdout is still reported and must not be used to tune the rule, but it is
not fully independent from the research process because the full dataset
was previously inspected.

Files expected
--------------
1) backtest_results/current_1458_trades.csv
2) cci30_OHLCV.csv in the project directory (or pass --cci30-csv)

The trade CSV must contain:
    entry_time
    realized_r
    side

The CCI30 CSV must contain:
    Date
    Close

Optional:
    Open High Low Volume

Run
---
python cci30_30d_regime_test.py
python cci30_30d_regime_test.py --days 720 --research-days 480
python cci30_30d_regime_test.py --trades-csv "backtest_results/current_1458_trades.csv" --cci30-csv ".\\cci30_OHLCV.csv"

The script uses the existing realized-R outcomes, so it isolates the regime
filter rather than re-simulating exits.
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path

import numpy as np
import pandas as pd


DEFAULT_TRADES = Path("backtest_results/current_1458_trades.csv")
DEFAULT_CCI30 = Path("cci30_OHLCV.csv")
DEFAULT_DAYS = 720
DEFAULT_RESEARCH_DAYS = 480


def find_col(df: pd.DataFrame, candidates: list[str], label: str) -> str:
    lower = {str(c).strip().lower(): c for c in df.columns}
    for candidate in candidates:
        if candidate.lower() in lower:
            return lower[candidate.lower()]
    raise ValueError(
        f"Could not find {label} column. Expected one of: {candidates}. "
        f"Available columns: {list(df.columns)}"
    )


def load_trades(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"Trade CSV not found: {path}")

    df = pd.read_csv(path)

    entry_col = find_col(
        df,
        ["entry_time", "entry_timestamp", "timestamp", "entry_date"],
        "entry time",
    )
    r_col = find_col(
        df,
        ["realized_r", "realized_pnl_r", "pnl_r", "r_multiple"],
        "realized-R",
    )
    side_col = find_col(df, ["side", "direction"], "direction")

    out = df.copy()
    out["_entry_time"] = pd.to_datetime(out[entry_col], utc=True, errors="coerce")
    out["_realized_r"] = pd.to_numeric(out[r_col], errors="coerce")
    out["_side"] = out[side_col].astype(str).str.upper().str.strip()

    bad = out["_entry_time"].isna() | out["_realized_r"].isna()
    if bad.any():
        raise ValueError(f"{int(bad.sum())} trade rows have invalid entry time or realized-R.")

    out = out.sort_values("_entry_time").reset_index(drop=True)
    return out


def load_cci30(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"CCI30 CSV not found: {path}")

    df = pd.read_csv(path)

    date_col = find_col(
        df,
        ["Date", "date", "timestamp", "datetime"],
        "CCI30 date",
    )
    close_col = find_col(
        df,
        ["Close", "close", "closing_price"],
        "CCI30 close",
    )

    out = pd.DataFrame()
    out["_cci_time"] = pd.to_datetime(df[date_col], utc=True, errors="coerce")
    out["_cci_close"] = pd.to_numeric(df[close_col], errors="coerce")

    out = out.dropna(subset=["_cci_time", "_cci_close"])
    out = out.sort_values("_cci_time").drop_duplicates("_cci_time")
    out = out.reset_index(drop=True)

    if len(out) < 40:
        raise ValueError("CCI30 CSV has too few valid rows.")

    # CCI30 file is daily. A 30-day return is defined as:
    # close[t] / close[t-30 calendar days] - 1.
    # Use time-based lookup so missing calendar days do not silently become
    # "30 observations".
    daily = out.set_index("_cci_time")["_cci_close"].sort_index()

    # Normalize to UTC midnight so merge_asof operates cleanly.
    daily.index = daily.index.normalize()
    daily = daily[~daily.index.duplicated(keep="last")].sort_index()

    # Exact 30-calendar-day prior close, with a small tolerance for weekends
    # / non-trading days. CCI30 is generally daily, so use the latest available
    # observation at or before target date.
    target = pd.DataFrame({"_cci_time": daily.index})
    target["_target_30d"] = target["_cci_time"] - pd.Timedelta(days=30)

    prior = pd.DataFrame({
        "_prior_time": daily.index,
        "_prior_close": daily.values,
    })

    target = pd.merge_asof(
        target.sort_values("_target_30d"),
        prior.sort_values("_prior_time"),
        left_on="_target_30d",
        right_on="_prior_time",
        direction="backward",
        tolerance=pd.Timedelta(days=5),
    )

    target["_cci30_30d_return"] = (
        target["_cci_close"] if "_cci_close" in target.columns else
        daily.reindex(target["_cci_time"]).to_numpy()
    ) / target["_prior_close"] - 1.0

    target = target[["_cci_time", "_cci30_30d_return"]].dropna()
    return target.sort_values("_cci_time").reset_index(drop=True)


def attach_regime(trades: pd.DataFrame, cci: pd.DataFrame) -> pd.DataFrame:
    left = trades.copy()
    left["_entry_day"] = left["_entry_time"].dt.normalize()

    c = cci.copy()
    c["_cci_time"] = c["_cci_time"].dt.normalize()

    enriched = pd.merge_asof(
        left.sort_values("_entry_day"),
        c.sort_values("_cci_time"),
        left_on="_entry_day",
        right_on="_cci_time",
        direction="backward",
        tolerance=pd.Timedelta(days=3),
    )

    enriched["cci30_regime"] = np.where(
        enriched["_cci30_30d_return"] < 0,
        "NEGATIVE_30D",
        np.where(
            enriched["_cci30_30d_return"] >= 0,
            "POSITIVE_30D",
            "UNMATCHED",
        ),
    )

    return enriched


def metrics(df: pd.DataFrame) -> dict:
    n = len(df)
    if n == 0:
        return {
            "trades": 0,
            "win_rate_pct": np.nan,
            "profit_factor": np.nan,
            "expectancy_R": np.nan,
            "total_R": 0.0,
            "avg_R": np.nan,
            "median_R": np.nan,
            "max_losing_streak": 0,
        }

    r = pd.to_numeric(df["_realized_r"], errors="coerce").dropna()
    wins = r[r > 0]
    losses = r[r < 0]

    gross_profit = float(wins.sum())
    gross_loss = float(-losses.sum())

    pf = (
        gross_profit / gross_loss
        if gross_loss > 0
        else (math.inf if gross_profit > 0 else np.nan)
    )

    outcomes = (r > 0).astype(int).to_numpy()
    max_streak = 0
    current = 0
    for x in outcomes:
        if x == 0:
            current += 1
            max_streak = max(max_streak, current)
        else:
            current = 0

    return {
        "trades": int(n),
        "win_rate_pct": float((r > 0).mean() * 100),
        "profit_factor": float(pf),
        "expectancy_R": float(r.mean()),
        "total_R": float(r.sum()),
        "avg_R": float(r.mean()),
        "median_R": float(r.median()),
        "max_losing_streak": int(max_streak),
    }


def print_metrics(label: str, df: pd.DataFrame) -> dict:
    m = metrics(df)
    pf_text = "inf" if math.isinf(m["profit_factor"]) else f'{m["profit_factor"]:.3f}'
    print(
        f"{label:<24} "
        f"trades {m['trades']:>4} | "
        f"win {m['win_rate_pct']:>6.2f}% | "
        f"PF {pf_text:>7} | "
        f"expectancy {m['expectancy_R']:>+8.4f}R | "
        f"total R {m['total_R']:>+9.2f} | "
        f"max loss streak {m['max_losing_streak']:>3}"
    )
    return m


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trades-csv", default=str(DEFAULT_TRADES))
    parser.add_argument("--cci30-csv", default=str(DEFAULT_CCI30))
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument("--research-days", type=int, default=DEFAULT_RESEARCH_DAYS)
    args = parser.parse_args()

    trades_path = Path(args.trades_csv)
    cci_path = Path(args.cci30_csv)

    print("=" * 78)
    print("CCI30 30-DAY REGIME CONFIRMATION TEST")
    print("=" * 78)
    print("PRE-DECLARED HYPOTHESIS:")
    print("Existing strategy performance is better when CCI30 trailing 30D")
    print("return is NEGATIVE than when it is POSITIVE.")
    print()
    print("ONLY CHANGE:")
    print("Reject an otherwise valid CURRENT trade when CCI30 30D return >= 0.")
    print("No new indicators. No exit changes. No direction changes.")
    print("No parameter tuning.")
    print()
    print("RESEARCH-HYGIENE WARNING:")
    print("The negative-30D observation was discovered from the full dataset.")
    print("Therefore this is confirmation/diagnostic evidence, not a pristine")
    print("out-of-sample discovery test. DO NOT tune the rule from the holdout.")
    print("=" * 78)

    trades = load_trades(trades_path)
    cci = load_cci30(cci_path)
    enriched = attach_regime(trades, cci)

    matched = enriched["cci30_regime"].ne("UNMATCHED").sum()
    unmatched = len(enriched) - matched

    print(f"\nTrades loaded:       {len(enriched)}")
    print(f"CCI30 rows:          {len(cci)}")
    print(f"Trades matched:      {matched}")
    print(f"Trades unmatched:    {unmatched}")

    if unmatched:
        print("\nWARNING: unmatched trades will be excluded from regime comparison.")

    valid = enriched[enriched["cci30_regime"] != "UNMATCHED"].copy()
    if valid.empty:
        raise RuntimeError("No trades could be matched to a CCI30 regime.")

    end_time = valid["_entry_time"].max()
    start_cut = end_time - pd.Timedelta(days=args.days)
    valid = valid[valid["_entry_time"] >= start_cut].copy()

    holdout_start = end_time - pd.Timedelta(days=args.research_days)
    research_start = end_time - pd.Timedelta(days=args.days)

    # Chronological split: oldest portion = research, newest portion = holdout.
    research = valid[
        (valid["_entry_time"] >= research_start)
        & (valid["_entry_time"] < holdout_start)
    ].copy()

    holdout = valid[valid["_entry_time"] >= holdout_start].copy()

    print("\nDATA PERIOD")
    print("-" * 78)
    print(f"Full matched period: {valid['_entry_time'].min()} -> {valid['_entry_time'].max()}")
    print(f"Research:            {research['_entry_time'].min()} -> {research['_entry_time'].max()}")
    print(f"Holdout:             {holdout['_entry_time'].min()} -> {holdout['_entry_time'].max()}")
    print(f"Research trades:     {len(research)}")
    print(f"Holdout trades:      {len(holdout)}")

    print("\nRESEARCH RESULTS")
    print("-" * 78)
    all_research = print_metrics("CURRENT", research)
    neg_research = print_metrics(
        "CCI30_NEGATIVE_30D",
        research[research["cci30_regime"] == "NEGATIVE_30D"],
    )

    pos_research = print_metrics(
        "CCI30_POSITIVE_30D",
        research[research["cci30_regime"] == "POSITIVE_30D"],
    )

    print("\nRESEARCH REGIME COUNTS")
    print(research["cci30_regime"].value_counts().to_string())

    print("\nRESEARCH DELTA (NEGATIVE_30D - CURRENT)")
    print("-" * 78)
    print(f"PF delta:          {neg_research['profit_factor'] - all_research['profit_factor']:+.3f}")
    print(f"Expectancy delta:  {neg_research['expectancy_R'] - all_research['expectancy_R']:+.4f}R")
    print(f"Win-rate delta:    {neg_research['win_rate_pct'] - all_research['win_rate_pct']:+.2f} pp")
    print(f"Trades removed:    {all_research['trades'] - neg_research['trades']}")

    print("\nHOLDOUT RESULTS — DECISIVE COMPARISON")
    print("-" * 78)
    all_holdout = print_metrics("CURRENT", holdout)
    neg_holdout = print_metrics(
        "CCI30_NEGATIVE_30D",
        holdout[holdout["cci30_regime"] == "NEGATIVE_30D"],
    )

    print("\nHOLDOUT REGIME COUNTS")
    print(holdout["cci30_regime"].value_counts().to_string())

    pf_delta = neg_holdout["profit_factor"] - all_holdout["profit_factor"]
    exp_delta = neg_holdout["expectancy_R"] - all_holdout["expectancy_R"]
    wr_delta = neg_holdout["win_rate_pct"] - all_holdout["win_rate_pct"]

    print("\nHOLDOUT DELTA (NEGATIVE_30D - CURRENT)")
    print("-" * 78)
    print(f"PF delta:          {pf_delta:+.3f}")
    print(f"Expectancy delta:  {exp_delta:+.4f}R")
    print(f"Win-rate delta:    {wr_delta:+.2f} pp")
    print(f"Trades removed:    {all_holdout['trades'] - neg_holdout['trades']}")

    print("\nFINAL INTERPRETATION")
    print("-" * 78)

    if (
        neg_holdout["trades"] >= 50
        and pf_delta > 0
        and exp_delta > 0
    ):
        print("RESULT: CCI30_NEGATIVE_30D improves BOTH holdout PF and expectancy.")
        print("This is evidence worth carrying to a fresh-data validation run.")
        print("DO NOT tune the threshold or combine it with other filters yet.")
    else:
        print("RESULT: CCI30_NEGATIVE_30D does NOT improve both holdout PF and expectancy.")
        print("Stop this branch; do not tune the CCI30 rule from these results.")

    print("\nImportant:")
    print("- Total R assumes the frozen trade inventory and is NOT an achievable")
    print("  compounded equity curve.")
    print("- The regime filter changes the number of trades, so PF/expectancy")
    print("  are the primary comparison; total R is secondary.")
    print("- This run does not prove causality.")
    print("- A successful result requires a fresh, untouched dataset before live use.")

    out_dir = Path("backtest_results")
    out_dir.mkdir(exist_ok=True)

    stamp = pd.Timestamp.now(tz="UTC").strftime("%Y%m%d_%H%M%S")

    save_cols = [
        c for c in enriched.columns
        if not c.startswith("_")
    ] + [
        "_entry_time",
        "_realized_r",
        "_side",
        "_cci30_30d_return",
        "cci30_regime",
    ]

    # Keep both original and diagnostic columns, avoiding duplicate column names.
    save_cols = list(dict.fromkeys([c for c in save_cols if c in enriched.columns]))
    enriched[save_cols].to_csv(
        out_dir / f"cci30_30d_enriched_trades_{stamp}.csv",
        index=False,
    )

    summary = pd.DataFrame([
        {"period": "RESEARCH", "model": "CURRENT", **all_research},
        {"period": "RESEARCH", "model": "CCI30_NEGATIVE_30D", **neg_research},
        {"period": "RESEARCH", "model": "CCI30_POSITIVE_30D", **pos_research},
        {"period": "HOLDOUT", "model": "CURRENT", **all_holdout},
        {"period": "HOLDOUT", "model": "CCI30_NEGATIVE_30D", **neg_holdout},
    ])
    summary.to_csv(
        out_dir / f"cci30_30d_summary_{stamp}.csv",
        index=False,
    )

    print(f"\nSaved enriched trades: {out_dir / f'cci30_30d_enriched_trades_{stamp}.csv'}")
    print(f"Saved summary:         {out_dir / f'cci30_30d_summary_{stamp}.csv'}")


if __name__ == "__main__":
    main()
