"""
AURA v0.4.4 — Relationship Survivability Engine

Research-only post-processor for the v0.4.3 historical event dataset.

Purpose:
- Take candidate relationships discovered in v0.4.3 TRAIN.
- Test them across frozen time periods, unseen symbols and market regimes.
- Score survivability using four pillars:
    1) OOS lift
    2) time/symbol consistency
    3) distribution stability (mean vs median + hit-rate stability)
    4) MFE/MAE efficiency
- Produce CSVs for research review.

IMPORTANT:
This script does NOT place orders.
The v0.4.3 symbol holdout is reused here, so this is validation/diagnostic
research, not a brand-new untouched final test.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import numpy as np
import pandas as pd


DEFAULT_RESEARCH = Path("research")
DEFAULT_EVENTS = DEFAULT_RESEARCH / "v043_historical_events.csv"
DEFAULT_RANKING = DEFAULT_RESEARCH / "v043_train_relationship_ranking.csv"

OOS_PERIODS = [
    ("Q1_2026", "2026-01-01", "2026-03-31"),
    ("Q2_2026", "2026-04-01", "2026-06-30"),
    ("Q3_2026", "2026-07-01", "2026-08-25"),
]

HOLDOUT_SYMBOLS = [
    "MSFT", "NVDA", "QQQ", "RTX", "SLV",
    "SPY", "TSLA", "WMT", "XOM",
]

# Candidate definitions are deliberately restricted to relationships
# already present in the v0.4.3 TRAIN ranking. No new optimization occurs here.
CONDITIONS = {
    "EMA38_CROSSOVER": lambda d: d["bullish_crossover"].fillna(False).astype(bool),
    "TREND_2150_BULL": lambda d: d["ema_21"] > d["ema_50"],
    "TREND_50200_BULL": lambda d: d["ema_50"] > d["ema_200"],
    "TREND_STACK": lambda d: (
        (d["ema_3"] > d["ema_8"])
        & (d["ema_8"] > d["ema_21"])
        & (d["ema_21"] > d["ema_50"])
    ),
    "RSI_GT_50": lambda d: d["rsi_14"] > 50,
    "RSI_GT_55": lambda d: d["rsi_14"] > 55,
    "MACD_HIST_GT_0": lambda d: d["macd_hist"] > 0,
    "REL_VOLUME_GE_1": lambda d: d["rel_volume"] >= 1.0,
    "REL_VOLUME_GE_1_5": lambda d: d["rel_volume"] >= 1.5,
    "BREAKOUT_20": lambda d: d["breakout_20"].fillna(False).astype(bool),
    "BREAKOUT_50": lambda d: d["breakout_50"].fillna(False).astype(bool),
    "BREAKOUT20_ATR_GE_025": lambda d: d["breakout_20_atr"] >= 0.25,
    "BULL_RSI_DIV": lambda d: d["bull_rsi_div"].fillna(False).astype(bool),
    "BULL_MACD_DIV": lambda d: d["bull_macd_div"].fillna(False).astype(bool),
    "OBV_BULL_DIV": lambda d: d["obv_bull_div"].fillna(False).astype(bool),
}


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--events", default=str(DEFAULT_EVENTS))
    p.add_argument("--ranking", default=str(DEFAULT_RANKING))
    p.add_argument("--outdir", default=str(DEFAULT_RESEARCH))
    p.add_argument("--min-oos-signals", type=int, default=30)
    return p.parse_args()


def finite_mean(s):
    x = pd.to_numeric(s, errors="coerce").dropna()
    return float(x.mean()) if len(x) else np.nan


def finite_median(s):
    x = pd.to_numeric(s, errors="coerce").dropna()
    return float(x.median()) if len(x) else np.nan


def positive_rate(s):
    x = pd.to_numeric(s, errors="coerce").dropna()
    return float((x > 0).mean()) if len(x) else np.nan


def apply_conditions(df: pd.DataFrame, condition_text: str) -> pd.Series:
    mask = pd.Series(True, index=df.index)
    for name in [x.strip() for x in condition_text.split("+")]:
        if name not in CONDITIONS:
            raise KeyError(f"Unknown condition: {name}")
        mask &= CONDITIONS[name](df)
    return mask


def evaluate(df: pd.DataFrame, condition_text: str) -> dict:
    mask = apply_conditions(df, condition_text)
    x = df.loc[mask].copy()

    return {
        "signals": int(len(x)),
        "symbols": int(x["symbol"].nunique()) if len(x) else 0,
        "forward_5d_mean": finite_mean(x["forward_5d"]),
        "forward_5d_median": finite_median(x["forward_5d"]),
        "positive_rate": positive_rate(x["forward_5d"]),
        "mfe_5d_mean": finite_mean(x["mfe_5d"]),
        "mae_5d_mean": finite_mean(x["mae_5d"]),
    }


def evaluate_raw(df: pd.DataFrame) -> dict:
    return {
        "signals": int(len(df)),
        "forward_5d_mean": finite_mean(df["forward_5d"]),
        "forward_5d_median": finite_median(df["forward_5d"]),
        "positive_rate": positive_rate(df["forward_5d"]),
    }


def period_table(events, candidates):
    rows = []

    for name, start, end in OOS_PERIODS:
        scoped = events[
            (events["decision_timestamp"] >= pd.Timestamp(start, tz="UTC"))
            & (events["decision_timestamp"] < pd.Timestamp(end, tz="UTC") + pd.Timedelta(days=1))
        ]
        raw = evaluate_raw(scoped)

        for _, cand in candidates.iterrows():
            result = evaluate(scoped, cand["conditions"])
            result.update({
                "filter": cand["filter"],
                "conditions": cand["conditions"],
                "period": name,
                "period_start": start,
                "period_end": end,
                "raw_signals": raw["signals"],
                "raw_5d_mean": raw["forward_5d_mean"],
                "raw_5d_median": raw["forward_5d_median"],
                "raw_positive_rate": raw["positive_rate"],
            })
            result["mean_lift_vs_raw"] = result["forward_5d_mean"] - raw["forward_5d_mean"]
            result["median_lift_vs_raw"] = result["forward_5d_median"] - raw["forward_5d_median"]
            result["positive_rate_lift_vs_raw"] = result["positive_rate"] - raw["positive_rate"]
            rows.append(result)

    return pd.DataFrame(rows)


def holdout_table(events, candidates):
    scoped = events[
        events["decision_timestamp"].between(
            pd.Timestamp("2026-01-01", tz="UTC"),
            pd.Timestamp("2026-08-25", tz="UTC") + pd.Timedelta(days=1),
            inclusive="left",
        )
        & events["symbol"].isin(HOLDOUT_SYMBOLS)
    ]
    raw = evaluate_raw(scoped)

    rows = []
    for _, cand in candidates.iterrows():
        result = evaluate(scoped, cand["conditions"])
        result.update({
            "filter": cand["filter"],
            "conditions": cand["conditions"],
            "holdout_symbols": ",".join(HOLDOUT_SYMBOLS),
            "raw_signals": raw["signals"],
            "raw_5d_mean": raw["forward_5d_mean"],
            "raw_5d_median": raw["forward_5d_median"],
            "raw_positive_rate": raw["positive_rate"],
        })
        result["mean_lift_vs_raw"] = result["forward_5d_mean"] - raw["forward_5d_mean"]
        result["median_lift_vs_raw"] = result["forward_5d_median"] - raw["forward_5d_median"]
        result["positive_rate_lift_vs_raw"] = result["positive_rate"] - raw["positive_rate"]
        rows.append(result)

    return pd.DataFrame(rows)


def regime_table(events, candidates):
    scoped = events[
        (events["decision_timestamp"] >= pd.Timestamp("2026-01-01", tz="UTC"))
        & (events["decision_timestamp"] < pd.Timestamp("2026-08-26", tz="UTC"))
    ]

    rows = []
    for regime in sorted(scoped["regime"].dropna().unique()):
        regime_df = scoped[scoped["regime"] == regime]
        raw = evaluate_raw(regime_df)

        for _, cand in candidates.iterrows():
            result = evaluate(regime_df, cand["conditions"])
            result.update({
                "filter": cand["filter"],
                "conditions": cand["conditions"],
                "regime": regime,
                "raw_signals": raw["signals"],
                "raw_5d_mean": raw["forward_5d_mean"],
                "raw_5d_median": raw["forward_5d_median"],
                "raw_positive_rate": raw["positive_rate"],
            })
            result["mean_lift_vs_raw"] = result["forward_5d_mean"] - raw["forward_5d_mean"]
            result["median_lift_vs_raw"] = result["forward_5d_median"] - raw["forward_5d_median"]
            result["positive_rate_lift_vs_raw"] = result["positive_rate"] - raw["positive_rate"]
            rows.append(result)

    return pd.DataFrame(rows)


def symbol_consistency(events, candidates):
    """Measure how broadly a relationship works across symbols in OOS."""
    scoped = events[
        (events["decision_timestamp"] >= pd.Timestamp("2026-01-01", tz="UTC"))
        & (events["decision_timestamp"] < pd.Timestamp("2026-08-26", tz="UTC"))
    ]

    rows = []
    for _, cand in candidates.iterrows():
        mask = apply_conditions(scoped, cand["conditions"])
        sig = scoped.loc[mask]

        per_symbol = []
        for symbol, g in scoped.groupby("symbol"):
            raw_mean = finite_mean(g["forward_5d"])
            sg = sig[sig["symbol"] == symbol]
            if len(sg) >= 2 and np.isfinite(raw_mean):
                sig_mean = finite_mean(sg["forward_5d"])
                if np.isfinite(sig_mean):
                    per_symbol.append({
                        "symbol": symbol,
                        "signals": len(sg),
                        "lift": sig_mean - raw_mean,
                    })

        ps = pd.DataFrame(per_symbol)
        if len(ps):
            rows.append({
                "filter": cand["filter"],
                "conditions": cand["conditions"],
                "symbols_tested": int(len(ps)),
                "symbols_positive_lift": int((ps["lift"] > 0).sum()),
                "symbol_positive_lift_rate": float((ps["lift"] > 0).mean()),
                "symbol_lift_mean": float(ps["lift"].mean()),
                "symbol_lift_median": float(ps["lift"].median()),
                "symbol_lift_std": float(ps["lift"].std(ddof=0)),
            })
        else:
            rows.append({
                "filter": cand["filter"],
                "conditions": cand["conditions"],
                "symbols_tested": 0,
                "symbols_positive_lift": 0,
                "symbol_positive_lift_rate": np.nan,
                "symbol_lift_mean": np.nan,
                "symbol_lift_median": np.nan,
                "symbol_lift_std": np.nan,
            })

    return pd.DataFrame(rows)


def build_survivability(periods, holdout, regimes, symbols, candidates, min_oos_signals):
    rows = []

    for _, cand in candidates.iterrows():
        name = cand["filter"]
        p = periods[periods["filter"] == name].copy()
        h = holdout[holdout["filter"] == name].iloc[0]
        r = regimes[regimes["filter"] == name].copy()
        s = symbols[symbols["filter"] == name].iloc[0]

        valid_p = p[p["signals"] > 0]
        total_oos = int(valid_p["signals"].sum())

        avg_mean_lift = finite_mean(valid_p["mean_lift_vs_raw"])
        avg_median_lift = finite_mean(valid_p["median_lift_vs_raw"])
        avg_pr_lift = finite_mean(valid_p["positive_rate_lift_vs_raw"])

        positive_mean_periods = int((valid_p["mean_lift_vs_raw"] > 0).sum())
        positive_median_periods = int((valid_p["median_lift_vs_raw"] > 0).sum())
        positive_pr_periods = int((valid_p["positive_rate_lift_vs_raw"] > 0).sum())

        # Four pillars, each mapped to 0..100.
        # 1) OOS lift: positive aggregate lift is required for high scores.
        oos_score = float(np.clip(50 + 5000 * avg_mean_lift, 0, 100)) if np.isfinite(avg_mean_lift) else 0

        # 2) Consistency: reward positive lift across periods and symbols.
        period_consistency = (
            0.5 * positive_mean_periods / max(len(valid_p), 1)
            + 0.5 * positive_median_periods / max(len(valid_p), 1)
        )
        symbol_consistency = s["symbol_positive_lift_rate"] if np.isfinite(s["symbol_positive_lift_rate"]) else 0
        consistency_score = float(100 * (0.7 * period_consistency + 0.3 * symbol_consistency))

        # 3) Stability: mean/median gap + hit-rate variability.
        gap = abs(avg_mean_lift - avg_median_lift) if np.isfinite(avg_mean_lift) and np.isfinite(avg_median_lift) else 1
        gap_penalty = min(gap / 0.01, 1.0)
        pr_std = float(valid_p["positive_rate"].std(ddof=0)) if len(valid_p) > 1 else 0.25
        pr_stability = max(0.0, 1.0 - pr_std / 0.20)
        stability_score = float(100 * (0.6 * (1 - gap_penalty) + 0.4 * pr_stability))

        # 4) MFE/MAE efficiency.
        mfe = finite_mean(valid_p["mfe_5d_mean"])
        mae = finite_mean(valid_p["mae_5d_mean"])
        efficiency_ratio = mfe / abs(mae) if np.isfinite(mfe) and np.isfinite(mae) and mae < 0 else 0
        efficiency_score = float(np.clip(50 * efficiency_ratio, 0, 100))

        # Regime breadth: require useful coverage, but do not let one tiny regime dominate.
        rr = r[r["signals"] >= 5]
        regime_positive_rate = float((rr["mean_lift_vs_raw"] > 0).mean()) if len(rr) else 0

        # Weighted final score.
        survivability = (
            0.35 * oos_score
            + 0.25 * consistency_score
            + 0.20 * stability_score
            + 0.20 * efficiency_score
        )

        # Strict research gate. This is intentionally harder than v0.4.3.
        robust = bool(
            total_oos >= min_oos_signals
            and np.isfinite(avg_mean_lift) and avg_mean_lift > 0
            and np.isfinite(avg_median_lift) and avg_median_lift > 0
            and positive_mean_periods >= 2
            and positive_median_periods >= 2
            and np.isfinite(h["median_lift_vs_raw"]) and h["median_lift_vs_raw"] > 0
            and np.isfinite(h["positive_rate_lift_vs_raw"]) and h["positive_rate_lift_vs_raw"] >= 0
            and regime_positive_rate >= 0.5
            and s["symbols_positive_lift"] >= max(3, int(np.ceil(s["symbols_tested"] * 0.5)))
        )

        rows.append({
            "filter": name,
            "conditions": cand["conditions"],
            "train_signals": int(cand["signals"]),
            "train_5d_mean": cand["forward_5d_mean"],
            "train_5d_median": cand["forward_5d_median"],
            "train_positive_rate": cand["positive_rate"],
            "oos_signals": total_oos,
            "oos_mean_lift": avg_mean_lift,
            "oos_median_lift": avg_median_lift,
            "oos_positive_rate_lift": avg_pr_lift,
            "positive_mean_periods": positive_mean_periods,
            "positive_median_periods": positive_median_periods,
            "positive_rate_periods": positive_pr_periods,
            "holdout_mean_lift": h["mean_lift_vs_raw"],
            "holdout_median_lift": h["median_lift_vs_raw"],
            "holdout_positive_rate_lift": h["positive_rate_lift_vs_raw"],
            "symbols_tested": s["symbols_tested"],
            "symbols_positive_lift": s["symbols_positive_lift"],
            "symbol_positive_lift_rate": s["symbol_positive_lift_rate"],
            "symbol_lift_median": s["symbol_lift_median"],
            "regime_positive_lift_rate": regime_positive_rate,
            "mfe_5d_mean": mfe,
            "mae_5d_mean": mae,
            "mfe_mae_ratio": efficiency_ratio,
            "oos_lift_score": oos_score,
            "consistency_score": consistency_score,
            "stability_score": stability_score,
            "efficiency_score": efficiency_score,
            "survivability_score": survivability,
            "robust": robust,
        })

    return pd.DataFrame(rows).sort_values(
        ["robust", "survivability_score", "oos_median_lift"],
        ascending=[False, False, False],
    ).reset_index(drop=True)


def main():
    args = parse_args()
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    events = pd.read_csv(args.events)
    ranking = pd.read_csv(args.ranking)

    events["decision_timestamp"] = pd.to_datetime(events["decision_timestamp"], utc=True)

    required = {
        "symbol", "decision_timestamp", "forward_5d", "mfe_5d", "mae_5d",
        "regime", "bullish_crossover", "ema_3", "ema_8", "ema_21",
        "ema_50", "ema_200", "rsi_14", "macd_hist", "rel_volume",
        "breakout_20", "breakout_50", "breakout_20_atr",
        "bull_rsi_div", "bull_macd_div", "obv_bull_div",
    }
    missing = sorted(required - set(events.columns))
    if missing:
        raise ValueError(f"Missing required event columns: {missing}")

    candidates = ranking[["filter", "conditions", "signals",
                          "forward_5d_mean", "forward_5d_median",
                          "positive_rate"]].copy()

    # Only candidates whose conditions can be reproduced from v0.4.3 fields.
    usable = []
    for _, row in candidates.iterrows():
        try:
            apply_conditions(events.head(1), row["conditions"])
            usable.append(row)
        except KeyError as exc:
            print(f"SKIP {row['filter']}: {exc}")

    candidates = pd.DataFrame(usable)

    print("=" * 72)
    print("AURA v0.4.4 — RELATIONSHIP SURVIVABILITY ENGINE")
    print("=" * 72)
    print(f"Historical events: {len(events):,}")
    print(f"Symbols:           {events['symbol'].nunique()}")
    print(f"Data range:        {events['decision_timestamp'].min()} -> {events['decision_timestamp'].max()}")
    print(f"Candidates:        {len(candidates)}")
    print("Mode:              RESEARCH ONLY — NO ORDERS")
    print()
    print("IMPORTANT: the v0.4.3 holdout symbols are reused for diagnostics.")
    print("A future production validation must reserve a NEW symbol holdout.")
    print()

    periods = period_table(events, candidates)
    holdout = holdout_table(events, candidates)
    regimes = regime_table(events, candidates)
    symbols = symbol_consistency(events, candidates)
    summary = build_survivability(
        periods, holdout, regimes, symbols, candidates, args.min_oos_signals
    )

    periods.to_csv(outdir / "v044_oos_period_results.csv", index=False)
    holdout.to_csv(outdir / "v044_unseen_symbol_results.csv", index=False)
    regimes.to_csv(outdir / "v044_regime_results.csv", index=False)
    symbols.to_csv(outdir / "v044_symbol_consistency.csv", index=False)
    summary.to_csv(outdir / "v044_survivability_summary.csv", index=False)

    print("SURVIVABILITY RANKING")
    print("-" * 72)
    cols = [
        "filter", "oos_signals", "oos_mean_lift", "oos_median_lift",
        "positive_mean_periods", "positive_median_periods",
        "holdout_median_lift", "symbol_positive_lift_rate",
        "mfe_mae_ratio", "survivability_score", "robust",
    ]
    print(summary[cols].to_string(index=False, float_format=lambda x: f"{x:.4f}"))

    print()
    print("ROBUST CANDIDATES")
    print("-" * 72)
    robust = summary[summary["robust"]]
    if robust.empty:
        print("NONE — this is a valid research result; do not force a winner.")
    else:
        print(robust[["filter", "conditions", "survivability_score"]].to_string(index=False))

    print()
    print("CSV OUTPUT")
    print("-" * 72)
    for fn in [
        "v044_oos_period_results.csv",
        "v044_unseen_symbol_results.csv",
        "v044_regime_results.csv",
        "v044_symbol_consistency.csv",
        "v044_survivability_summary.csv",
    ]:
        print(outdir / fn)

    print()
    print("=" * 72)
    print("AURA v0.4.4 COMPLETE — NO ORDERS WERE PLACED")
    print("=" * 72)


if __name__ == "__main__":
    main()
