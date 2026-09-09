"""
Asset Selection & Executability Experiment V1
Analysis of research observations.

This script does not alter the trading strategy.
"""

from __future__ import annotations
import argparse
import csv
import json
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

import pandas as pd


def f(x):
    try:
        return float(x)
    except Exception:
        return None


def bucket_rank(x):
    x = f(x)
    if x is None or x <= 0: return "UNKNOWN"
    if x <= 50: return "TOP_50"
    if x <= 100: return "RANK_51_100"
    if x <= 200: return "RANK_101_200"
    if x <= 500: return "RANK_201_500"
    return "RANK_501_PLUS"


def bucket_volume(x):
    x = f(x)
    if x is None: return "UNKNOWN"
    if x >= 50_000_000: return "A_OVER_50M"
    if x >= 10_000_000: return "B_10M_50M"
    if x >= 1_000_000: return "C_1M_10M"
    return "D_UNDER_1M"


def bucket_spread(x):
    x = f(x)
    if x is None: return "UNKNOWN"
    if x < 5: return "EXCELLENT_LT_5"
    if x < 15: return "GOOD_5_15"
    if x < 30: return "MODERATE_15_30"
    if x < 100: return "POOR_30_100"
    return "DANGEROUS_GT_100"


def estimate_slippage_bps(spread_bps, depth_usd, risk_usd, buffer_bps):
    spread_bps = f(spread_bps)
    depth_usd = f(depth_usd)
    risk_usd = f(risk_usd)
    if spread_bps is None:
        return None
    # Conservative research estimate:
    # half-spread plus an impact component that grows as order size consumes depth.
    half_spread = spread_bps / 2
    if depth_usd and risk_usd and depth_usd > 0:
        impact = max(0.0, min(1000.0, (risk_usd / depth_usd) * 10000))
    else:
        impact = buffer_bps
    return half_spread + impact


def load_events(path):
    df = pd.read_csv(path)
    if df.empty:
        return df
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    for c in [
        "price","quantity","risk_usd","leverage","market_cap_rank",
        "market_cap_usd","volume_24h_usd","volume_mcap_pct","spread_bps",
        "depth_5bps_usd","depth_10bps_usd","depth_25bps_usd","depth_50bps_usd",
        "volatility_5m_pct","volatility_1h_pct","volatility_24h_pct",
        "estimated_slippage_bps"
    ]:
        if c in df:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", required=True)
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--outdir", default="research/asset_selection/results")
    args = ap.parse_args()

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    df = load_events(args.events)
    if df.empty:
        print("No research events found.")
        return

    # Use latest enrichment for each signal where available.
    enrich = df[df["event_type"].eq("enrichment")].copy()
    sig = df[df["event_type"].eq("signal")].copy()

    if sig.empty:
        print("No signal rows found. Candidate rows can still be inspected, but performance analysis needs signals.")
        return

    if not enrich.empty:
        enrich = enrich.sort_values("timestamp")
        keep = [
            "signal_id","market_cap_rank","market_cap_usd","volume_24h_usd",
            "volume_mcap_pct","spread_bps","depth_5bps_usd","depth_10bps_usd",
            "depth_25bps_usd","depth_50bps_usd","volatility_5m_pct",
            "volatility_1h_pct","volatility_24h_pct","listing_age_days",
            "asset_category","asset_category_source","mexc_api_eligible",
            "innovation_zone","estimated_slippage_bps"
        ]
        keep = [c for c in keep if c in enrich.columns]
        enrich = enrich[keep].drop_duplicates("signal_id", keep="last")
        sig = sig.merge(enrich, on="signal_id", how="left", suffixes=("", "_enrich"))

    sig["market_cap_bucket"] = sig.get("market_cap_rank", pd.Series(index=sig.index)).apply(bucket_rank)
    sig["volume_bucket"] = sig.get("volume_24h_usd", pd.Series(index=sig.index)).apply(bucket_volume)
    sig["spread_bucket"] = sig.get("spread_bps", pd.Series(index=sig.index)).apply(bucket_spread)

    # Repeated signal detection.
    sig = sig.sort_values(["symbol","timestamp"])
    sig["minutes_since_previous_symbol_signal"] = (
        sig.groupby("symbol")["timestamp"].diff().dt.total_seconds() / 60
    )
    sig["repeated_signal"] = sig["minutes_since_previous_symbol_signal"].notna()

    # Direction reversal detection.
    sig["previous_side"] = sig.groupby("symbol")["side"].shift(1)
    sig["direction_reversal"] = (
        sig["previous_side"].notna() &
        sig["side"].notna() &
        (sig["previous_side"].str.upper() != sig["side"].str.upper())
    )

    # Conservative execution estimate.
    if "estimated_slippage_bps" not in sig.columns:
        sig["estimated_slippage_bps"] = None
    missing = sig["estimated_slippage_bps"].isna()
    sig.loc[missing, "estimated_slippage_bps"] = sig.loc[missing].apply(
        lambda r: estimate_slippage_bps(
            r.get("spread_bps"),
            r.get("depth_10bps_usd"),
            r.get("risk_usd"),
            5.0
        ), axis=1
    )

    fee_pct = 0.08
    sig["estimated_total_execution_cost_pct"] = (
        sig["estimated_slippage_bps"] / 100
        + fee_pct * 2
    )

    # Research flags.
    sig["thin_orderbook_flag"] = (
        pd.to_numeric(sig.get("depth_10bps_usd"), errors="coerce") <
        pd.to_numeric(sig.get("risk_usd"), errors="coerce")
    )
    sig["wide_spread_flag"] = pd.to_numeric(sig.get("spread_bps"), errors="coerce") >= 30

    # Summary.
    summary = pd.DataFrame([{
        "signals": len(sig),
        "unique_symbols": sig["symbol"].nunique(),
        "repeated_signal_count": int(sig["repeated_signal"].sum()),
        "direction_reversal_count": int(sig["direction_reversal"].sum()),
        "median_market_cap_rank": sig["market_cap_rank"].median() if "market_cap_rank" in sig else None,
        "median_24h_volume_usd": sig["volume_24h_usd"].median() if "volume_24h_usd" in sig else None,
        "median_spread_bps": sig["spread_bps"].median() if "spread_bps" in sig else None,
        "median_estimated_slippage_bps": sig["estimated_slippage_bps"].median(),
        "wide_spread_signal_pct": sig["wide_spread_flag"].mean() * 100,
        "thin_orderbook_signal_pct": sig["thin_orderbook_flag"].mean() * 100,
    }])
    summary.to_csv(outdir / "asset_selection_summary.csv", index=False)

    # Bucket tables.
    group_cols = [
        "market_cap_bucket","volume_bucket","spread_bucket",
        "asset_category"
    ]
    results = []
    for col in group_cols:
        if col not in sig:
            continue
        g = sig.groupby(col, dropna=False).agg(
            signals=("signal_id","count"),
            unique_symbols=("symbol","nunique"),
            repeated_signals=("repeated_signal","sum"),
            reversals=("direction_reversal","sum"),
            median_rank=("market_cap_rank","median"),
            median_volume_usd=("volume_24h_usd","median"),
            median_spread_bps=("spread_bps","median"),
            median_slippage_bps=("estimated_slippage_bps","median"),
            wide_spread_pct=("wide_spread_flag","mean"),
            thin_book_pct=("thin_orderbook_flag","mean")
        ).reset_index()
        g["bucket_type"] = col
        g["wide_spread_pct"] *= 100
        g["thin_book_pct"] *= 100
        results.append(g.rename(columns={col:"bucket"}))

    if results:
        pd.concat(results, ignore_index=True).to_csv(
            outdir / "asset_selection_bucket_results.csv", index=False
        )

    symbol = sig.groupby("symbol").agg(
        signals=("signal_id","count"),
        repeated_signals=("repeated_signal","sum"),
        reversals=("direction_reversal","sum"),
        median_rank=("market_cap_rank","median"),
        median_volume_usd=("volume_24h_usd","median"),
        median_spread_bps=("spread_bps","median"),
        median_slippage_bps=("estimated_slippage_bps","median"),
        wide_spread_pct=("wide_spread_flag","mean")
    ).reset_index()
    symbol["wide_spread_pct"] *= 100
    symbol.sort_values(["signals","repeated_signals"], ascending=False).to_csv(
        outdir / "asset_selection_symbol_results.csv", index=False
    )

    sig.to_csv(outdir / "asset_selection_trade_enrichment.csv", index=False)

    print("\nASSET SELECTION & EXECUTABILITY — V1")
    print("=" * 70)
    print(f"Signals:                  {len(sig)}")
    print(f"Unique symbols:           {sig['symbol'].nunique()}")
    print(f"Repeated signals:         {int(sig['repeated_signal'].sum())}")
    print(f"Direction reversals:      {int(sig['direction_reversal'].sum())}")
    if "market_cap_rank" in sig:
        print(f"Median market-cap rank:   {sig['market_cap_rank'].median()}")
    if "volume_24h_usd" in sig:
        print(f"Median 24h volume:        ${sig['volume_24h_usd'].median():,.0f}")
    if "spread_bps" in sig:
        print(f"Median spread:             {sig['spread_bps'].median():.2f} bps")
    print(f"Median estimated slip:    {sig['estimated_slippage_bps'].median():.2f} bps")
    print(f"Wide spread signals:      {sig['wide_spread_flag'].mean()*100:.1f}%")
    print(f"Thin-book signals:        {sig['thin_orderbook_flag'].mean()*100:.1f}%")
    print("\nResults written to:", outdir.resolve())


if __name__ == "__main__":
    main()
