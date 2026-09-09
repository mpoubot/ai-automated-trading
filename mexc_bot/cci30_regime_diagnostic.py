"""
CCi30 regime diagnostic for the frozen CURRENT trade inventory.

Primary pre-declared hypothesis:
    CURRENT trades perform materially better when CCi30 is above its
    200-day moving average than when it is below it.

This is an EX-POST diagnostic only. It does not change entries, exits,
sizing, or strategy parameters and does not select a trading rule.

The script uses the official CCi30 daily OHLCV CSV. If automatic discovery
fails, download the CSV from https://cci30.com/ and pass --cci30-csv.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from urllib.parse import urljoin
from urllib.request import Request, urlopen

import numpy as np
import math
import pandas as pd

OFFICIAL_PAGE = "https://cci30.com/"
DEFAULT_TRADES = "backtest_results/current_1458_trades.csv"
OUT_DIR = Path("backtest_results")


def discover_official_csv() -> str:
    req = Request(
        OFFICIAL_PAGE,
        headers={"User-Agent": "Mozilla/5.0 (research diagnostic)"}
    )
    with urlopen(req, timeout=30) as r:
        html = r.read().decode("utf-8", errors="replace")

    patterns = [
        r"href=['\"]([^'\"]+\.csv(?:\?[^'\"]*)?)['\"]",
        r"(?:https?:)?//[^\"'\s<>]+\.csv(?:\?[^\"'\s<>]*)?",
    ]

    hrefs = []
    for pattern in patterns:
        hrefs.extend(re.findall(pattern, html, flags=re.I))

    if not hrefs:
        raise RuntimeError(
            "Could not discover the official CCi30 CSV URL. "
            "Download the daily OHLCV CSV from https://cci30.com/ "
            "and rerun with --cci30-csv <file>."
        )

    scored = []
    for h in hrefs:
        low = h.lower()
        score = sum(term in low for term in
                    ("ohlcv", "history", "histor", "daily", "cci30", "index"))
        scored.append((score, h))

    scored.sort(reverse=True)
    return urljoin(OFFICIAL_PAGE, scored[0][1])


def load_cci30(csv_path: str | None) -> pd.DataFrame:
    if csv_path:
        path = Path(csv_path)
        if not path.exists():
            raise FileNotFoundError(f"CCi30 CSV not found: {path}")
        df = pd.read_csv(path)
    else:
        url = discover_official_csv()
        print(f"Discovered official CCi30 CSV: {url}")
        df = pd.read_csv(url)

    df.columns = [str(c).strip().lower().replace(" ", "_") for c in df.columns]

    date_col = next(
        (c for c in df.columns if c in {"date", "datetime", "timestamp", "time"}),
        None
    )
    close_col = next(
        (c for c in df.columns if c in {"close", "closing_price", "index", "value"}),
        None
    )

    if date_col is None or close_col is None:
        raise ValueError(
            "Could not identify date/close columns in the CCi30 CSV. "
            f"Columns found: {list(df.columns)}"
        )

    out = df[[date_col, close_col]].copy()
    out.columns = ["cci_date", "cci_close"]
    out["cci_date"] = pd.to_datetime(out["cci_date"], utc=True, errors="coerce")
    out["cci_close"] = pd.to_numeric(out["cci_close"], errors="coerce")
    out = out.dropna(subset=["cci_date", "cci_close"])
    out = out.sort_values("cci_date").drop_duplicates("cci_date", keep="last")

    out["cci_1d_return"] = out["cci_close"].pct_change(1)
    out["cci_3d_return"] = out["cci_close"].pct_change(3)
    out["cci_7d_return"] = out["cci_close"].pct_change(7)
    out["cci_30d_return"] = out["cci_close"].pct_change(30)

    out["cci_50dma"] = out["cci_close"].rolling(50, min_periods=50).mean()
    out["cci_200dma"] = out["cci_close"].rolling(200, min_periods=200).mean()

    out["cci_above_50dma"] = np.where(
        out["cci_50dma"].notna(),
        out["cci_close"] > out["cci_50dma"],
        np.nan,
    )
    out["cci_above_200dma"] = np.where(
        out["cci_200dma"].notna(),
        out["cci_close"] > out["cci_200dma"],
        np.nan,
    )
    out["cci_distance_200dma_pct"] = (
        out["cci_close"] / out["cci_200dma"] - 1.0
    ) * 100.0

    return out


def find_trade_time_column(df: pd.DataFrame) -> str:
    for c in (
        "entry_time", "entry_timestamp", "entry_datetime",
        "timestamp", "entry_date", "date"
    ):
        if c in df.columns:
            return c
    raise ValueError(
        "Could not identify the entry timestamp column. "
        f"Available columns: {list(df.columns)}"
    )


def find_result_column(df: pd.DataFrame) -> str:
    for c in (
        "realized_r", "realized_pnl_r", "pnl_r",
        "profit_r", "r_multiple"
    ):
        if c in df.columns:
            return c
    raise ValueError(
        "Could not identify the realized-R column. "
        f"Available columns: {list(df.columns)}"
    )


def find_direction_column(df: pd.DataFrame) -> str | None:
    for c in ("direction", "side", "trade_direction", "position_side"):
        if c in df.columns:
            return c
    return None


def normalize_direction(s: pd.Series) -> pd.Series:
    return s.astype(str).str.upper().str.strip().replace({
        "BUY": "LONG",
        "SELL": "SHORT",
        "1": "LONG",
        "-1": "SHORT",
    })


def normal_p_value_from_t(t: float) -> float:
    return float(math.erfc(abs(t) / np.sqrt(2.0)))


def welch_t(a: np.ndarray, b: np.ndarray) -> tuple[float, float]:
    a = a[np.isfinite(a)]
    b = b[np.isfinite(b)]

    if len(a) < 2 or len(b) < 2:
        return np.nan, np.nan

    va = np.var(a, ddof=1)
    vb = np.var(b, ddof=1)
    se2 = va / len(a) + vb / len(b)

    if se2 <= 0:
        return np.nan, np.nan

    t = (np.mean(a) - np.mean(b)) / np.sqrt(se2)
    return float(t), normal_p_value_from_t(t)


def summarize_group(
    df: pd.DataFrame, group_col: str, result_col: str
) -> pd.DataFrame:
    rows = []

    for group, g in df.groupby(group_col, dropna=False):
        r = pd.to_numeric(g[result_col], errors="coerce").dropna()
        if len(r) == 0:
            continue

        losses = r[r < 0]
        wins = r[r > 0]
        pf = wins.sum() / abs(losses.sum()) if len(losses) else np.inf

        rows.append({
            "group": group,
            "trades": len(r),
            "win_rate_pct": (r > 0).mean() * 100,
            "profit_factor": pf,
            "expectancy_R": r.mean(),
            "total_R": r.sum(),
            "median_R": r.median(),
        })

    return pd.DataFrame(rows)


def compare_binary(
    df: pd.DataFrame, flag: str, result_col: str
) -> dict:
    x = pd.to_numeric(df[result_col], errors="coerce")
    valid = df[flag].isin([True, False]) & x.notna()

    above = x[valid & (df[flag] == True)].to_numpy()
    below = x[valid & (df[flag] == False)].to_numpy()

    t, p = welch_t(above, below)

    return {
        "indicator": flag,
        "above_trades": len(above),
        "below_trades": len(below),
        "above_expectancy_R": np.mean(above) if len(above) else np.nan,
        "below_expectancy_R": np.mean(below) if len(below) else np.nan,
        "expectancy_delta_R_above_minus_below":
            (np.mean(above) - np.mean(below))
            if len(above) and len(below) else np.nan,
        "welch_t_normal_approx": t,
        "two_sided_p_normal_approx": p,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Attach official CCi30 regime data to the frozen trade inventory."
    )
    parser.add_argument(
        "--trades",
        default=DEFAULT_TRADES,
        help=f"Trade CSV (default: {DEFAULT_TRADES})",
    )
    parser.add_argument(
        "--cci30-csv",
        default=None,
        help="Optional local official CCi30 daily OHLCV CSV.",
    )
    args = parser.parse_args()

    trade_path = Path(args.trades)
    if not trade_path.exists():
        raise FileNotFoundError(
            f"Trade file not found: {trade_path}\n"
            "Expected: backtest_results/current_1458_trades.csv"
        )

    trades = pd.read_csv(trade_path)
    time_col = find_trade_time_column(trades)
    result_col = find_result_column(trades)
    direction_col = find_direction_column(trades)

    trades["__entry_time"] = pd.to_datetime(
        trades[time_col], utc=True, errors="coerce"
    )
    trades["__realized_r"] = pd.to_numeric(
        trades[result_col], errors="coerce"
    )

    if trades["__entry_time"].isna().all():
        raise ValueError(f"No usable timestamps found in {time_col!r}.")

    if direction_col:
        trades["direction_normalized"] = normalize_direction(
            trades[direction_col]
        )

    cci = load_cci30(args.cci30_csv)

    # Backward as-of merge: only a CCi30 reference already published
    # at or before the trade entry is attached to that trade.
    enriched = pd.merge_asof(
        trades.sort_values("__entry_time"),
        cci.sort_values("cci_date"),
        left_on="__entry_time",
        right_on="cci_date",
        direction="backward",
        tolerance=pd.Timedelta(days=3),
    )

    enriched["cci_200dma_regime"] = np.where(
        enriched["cci_above_200dma"].eq(True),
        "ABOVE_200DMA",
        np.where(
            enriched["cci_above_200dma"].eq(False),
            "BELOW_200DMA",
            "UNKNOWN",
        ),
    )

    enriched["cci_50dma_regime"] = np.where(
        enriched["cci_above_50dma"].eq(True),
        "ABOVE_50DMA",
        np.where(
            enriched["cci_above_50dma"].eq(False),
            "BELOW_50DMA",
            "UNKNOWN",
        ),
    )

    enriched["cci_30d_regime"] = np.where(
        enriched["cci_30d_return"] > 0,
        "POSITIVE_30D",
        np.where(
            enriched["cci_30d_return"] < 0,
            "NEGATIVE_30D",
            "FLAT_30D",
        ),
    )

    enriched["cci_7d_regime"] = np.where(
        enriched["cci_7d_return"] > 0,
        "POSITIVE_7D",
        np.where(
            enriched["cci_7d_return"] < 0,
            "NEGATIVE_7D",
            "FLAT_7D",
        ),
    )

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = pd.Timestamp.utcnow().strftime("%Y%m%d_%H%M%S")

    enriched_path = OUT_DIR / f"cci30_regime_enriched_{stamp}.csv"
    summary_path = OUT_DIR / f"cci30_regime_summary_{stamp}.csv"

    enriched.drop(
        columns=["__entry_time", "__realized_r"],
        errors="ignore",
    ).to_csv(enriched_path, index=False)

    print("\n" + "=" * 72)
    print("CCi30 REGIME DIAGNOSTIC — FROZEN CURRENT TRADE INVENTORY")
    print("=" * 72)
    print(f"Trades loaded:       {len(trades)}")
    print(f"Entry time column:   {time_col}")
    print(f"Realized-R column:   {result_col}")
    print(f"Direction column:    {direction_col or 'not found'}")
    print(f"CCi30 rows:          {len(cci)}")
    print(f"CCi30 coverage:      {cci.cci_date.min()} -> {cci.cci_date.max()}")
    print(f"Trades matched:      {enriched['cci_close'].notna().sum()}")
    print(f"Trades unmatched:    {enriched['cci_close'].isna().sum()}")

    primary = compare_binary(
        enriched, "cci_above_200dma", "__realized_r"
    )

    print("\nPRIMARY PRE-DECLARED HYPOTHESIS")
    print("CCi30 ABOVE 200DMA vs BELOW 200DMA")
    print("-" * 72)
    print(
        f"Above 200DMA: {primary['above_trades']} trades | "
        f"expectancy {primary['above_expectancy_R']:+.4f}R"
    )
    print(
        f"Below 200DMA: {primary['below_trades']} trades | "
        f"expectancy {primary['below_expectancy_R']:+.4f}R"
    )
    print(
        "Delta (above - below): "
        f"{primary['expectancy_delta_R_above_minus_below']:+.4f}R"
    )
    print(
        "Welch t (normal approximation): "
        f"{primary['welch_t_normal_approx']:.3f}"
    )
    print(
        "Two-sided p (normal approximation): "
        f"{primary['two_sided_p_normal_approx']:.4f}"
    )

    print("\n200DMA REGIME SUMMARY")
    print(
        summarize_group(
            enriched, "cci_200dma_regime", "__realized_r"
        ).to_string(index=False)
    )

    print("\n50DMA REGIME SUMMARY")
    print(
        summarize_group(
            enriched, "cci_50dma_regime", "__realized_r"
        ).to_string(index=False)
    )

    print("\n30-DAY RETURN REGIME SUMMARY")
    print(
        summarize_group(
            enriched, "cci_30d_regime", "__realized_r"
        ).to_string(index=False)
    )

    print("\n7-DAY RETURN REGIME SUMMARY")
    print(
        summarize_group(
            enriched, "cci_7d_regime", "__realized_r"
        ).to_string(index=False)
    )

    pd.DataFrame([
        primary,
        compare_binary(enriched, "cci_above_50dma", "__realized_r"),
    ]).to_csv(summary_path, index=False)

    if direction_col:
        valid = enriched["direction_normalized"].isin(["LONG", "SHORT"])
        cross = (
            enriched.loc[valid]
            .groupby(
                ["cci_200dma_regime", "direction_normalized"]
            )["__realized_r"]
            .agg(["count", "mean", "median", "sum"])
            .reset_index()
            .rename(columns={
                "count": "trades",
                "mean": "expectancy_R",
                "median": "median_R",
                "sum": "total_R",
            })
        )

        cross_path = OUT_DIR / f"cci30_direction_x_regime_{stamp}.csv"
        cross.to_csv(cross_path, index=False)

        print("\nDIRECTION × 200DMA REGIME — DESCRIPTIVE ONLY")
        print(cross.to_string(index=False))
        print(f"Saved cross-tab: {cross_path}")

    print("\n" + "=" * 72)
    print("INTERPRETATION RULE")
    print("=" * 72)
    print("1. This run does NOT modify the strategy.")
    print("2. 200DMA is the one pre-declared hypothesis.")
    print("3. 50DMA and return buckets are descriptive only.")
    print("4. Do NOT choose a trading rule from the best-looking bucket.")
    print("5. Only if 200DMA is genuinely interesting should we declare")
    print("   ONE CCi30-filter hypothesis and test it on fresh data.")
    print(f"\nSaved enriched trades: {enriched_path}")
    print(f"Saved summary:         {summary_path}")
    print("=" * 72)


if __name__ == "__main__":
    main()
