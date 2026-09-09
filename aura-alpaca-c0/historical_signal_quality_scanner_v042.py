"""
AURA v0.4.2 — Multi-Period Robustness Scanner

RESEARCH ONLY — NO ORDERS ARE PLACED.

Purpose
-------
1. Scan a multi-year equity universe.
2. Detect bullish EMA 3/8 crossovers.
3. Test signal-quality filters using information available at the
   actual decision time.
4. Treat persistence correctly: persistence_2/3 are CONFIRMATION
   rules. A persistence-filtered trade is entered on the next trading
   day's OPEN after confirmation, so future bars are never used to
   score an earlier entry.
5. Select a filter using TRAIN data only.
6. Freeze that filter and test it across multiple unseen 2026 periods.
7. Also perform a simple expanding-window walk-forward validation.
8. Produce CSV results for later analysis.

Default periods
---------------
Historical data: 2024-01-01 -> 2026-08-25
Frozen training: 2024-01-01 -> 2025-12-31
Frozen tests:
    2026-01-01 -> 2026-03-31
    2026-04-01 -> 2026-06-30
    2026-07-01 -> 2026-08-25

Example
-------
python historical_signal_quality_scanner_v042.py --universe equity

All output is written under ./research/
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


DEFAULT_EQUITY_SYMBOLS = [
    "AAPL", "MSFT", "AVGO", "JNJ", "IWM", "CVX", "LLY", "CAT", "SPY",
    "RTX", "AMZN", "XOM", "TSLA", "BAC", "COST", "GS", "GE", "DE", "WMT",
    "NVDA", "QQQ", "LMT", "GLD", "JPM", "META", "GOOGL", "SLV",
]


def ema(s: pd.Series, span: int) -> pd.Series:
    return s.ewm(span=span, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def utc_timestamp(value: str | pd.Timestamp) -> pd.Timestamp:
    """Return a UTC-aware Timestamp without passing tz twice."""
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        return ts.tz_localize("UTC")
    return ts.tz_convert("UTC")


def add_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy().sort_index()

    df["ema_3"] = ema(df["close"], 3)
    df["ema_8"] = ema(df["close"], 8)
    df["ema_21"] = ema(df["close"], 21)
    df["ema_50"] = ema(df["close"], 50)

    df["rsi_14"] = rsi(df["close"], 14)

    ema12 = ema(df["close"], 12)
    ema26 = ema(df["close"], 26)
    df["macd"] = ema12 - ema26
    df["macd_signal"] = ema(df["macd"], 9)
    df["macd_hist"] = df["macd"] - df["macd_signal"]

    vol_mean = df["volume"].rolling(20, min_periods=5).mean()
    df["rel_volume"] = df["volume"] / vol_mean.replace(0, np.nan)

    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = tr.rolling(14, min_periods=14).mean()
    df["atr_pct"] = atr / df["close"] * 100.0

    df["price_acceleration"] = df["close"].pct_change()

    above = df["ema_3"] > df["ema_8"]
    df["bullish_crossover"] = above & (~above.shift(1, fill_value=False))

    df["trend_stack"] = (
        (df["ema_3"] > df["ema_8"])
        & (df["ema_8"] > df["ema_21"])
        & (df["ema_21"] > df["ema_50"])
    )

    return df


def fetch_symbol(
    client: StockHistoricalDataClient,
    symbol: str,
    start: str,
    end: str,
) -> pd.DataFrame:
    request = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Day,
        start=start,
        end=end,
        feed=DataFeed.IEX,
    )

    bars = client.get_stock_bars(request).df
    if bars.empty:
        return pd.DataFrame()

    if isinstance(bars.index, pd.MultiIndex):
        try:
            bars = bars.xs(symbol, level="symbol")
        except KeyError:
            return pd.DataFrame()

    bars = bars.copy()
    bars.index = pd.to_datetime(bars.index, utc=True)

    needed = ["open", "high", "low", "close", "volume"]
    if any(col not in bars.columns for col in needed):
        return pd.DataFrame()

    return bars[needed].sort_index()


def forward_outcomes(df: pd.DataFrame, decision_pos: int) -> dict:
    """
    Entry = next trading day's OPEN after the decision/confirmation bar.

    This is deliberately different from the old scanner's persistence
    handling: a persistence filter cannot use future bars while pretending
    the entry happened on the original crossover bar.
    """
    n = len(df)
    entry_pos = decision_pos + 1

    if entry_pos >= n:
        return {}

    entry = float(df.iloc[entry_pos]["open"])
    if not np.isfinite(entry) or entry <= 0:
        return {}

    out = {
        "entry_timestamp": df.index[entry_pos].isoformat(),
        "entry_open": entry,
    }

    for days in (1, 3, 5, 10):
        target_pos = entry_pos + days - 1
        if target_pos < n:
            close = float(df.iloc[target_pos]["close"])
            out[f"forward_{days}d"] = close / entry - 1.0
        else:
            out[f"forward_{days}d"] = np.nan

    end_pos = min(entry_pos + 4, n - 1)
    future = df.iloc[entry_pos : end_pos + 1]

    if len(future):
        out["mfe_5d"] = float(future["high"].max()) / entry - 1.0
        out["mae_5d"] = float(future["low"].min()) / entry - 1.0
    else:
        out["mfe_5d"] = np.nan
        out["mae_5d"] = np.nan

    return out


def confirmation_position(
    df: pd.DataFrame,
    crossover_pos: int,
    persistence: int,
) -> int | None:
    """
    Correct persistence confirmation.

    persistence=1 means the crossover bar itself is sufficient.
    persistence=2 requires the crossover bar AND the next bar to keep
    EMA3 > EMA8.
    persistence=3 requires three consecutive bars.

    The returned position is the ACTUAL decision bar. Entry occurs on
    the following trading day's open.
    """
    end_pos = crossover_pos + persistence - 1
    if end_pos >= len(df):
        return None

    for pos in range(crossover_pos, end_pos + 1):
        if not bool(df.iloc[pos]["ema_3"] > df.iloc[pos]["ema_8"]):
            return None

    return end_pos


def make_event_row(
    df: pd.DataFrame,
    symbol: str,
    crossover_pos: int,
    persistence: int,
) -> dict | None:
    decision_pos = confirmation_position(df, crossover_pos, persistence)
    if decision_pos is None:
        return None

    outcomes = forward_outcomes(df, decision_pos)
    if not outcomes or not np.isfinite(outcomes.get("forward_5d", np.nan)):
        return None

    row = df.iloc[decision_pos]

    return {
        "symbol": symbol,
        "crossover_timestamp": df.index[crossover_pos].isoformat(),
        "decision_timestamp": df.index[decision_pos].isoformat(),
        "persistence_level": persistence,
        "signal_close": float(row["close"]),
        "rsi_14": float(row["rsi_14"]),
        "rel_volume": float(row["rel_volume"]),
        "macd": float(row["macd"]),
        "macd_signal": float(row["macd_signal"]),
        "macd_hist": float(row["macd_hist"]),
        "atr_pct": float(row["atr_pct"]),
        "price_acceleration": float(row["price_acceleration"]),
        "trend_stack": bool(row["trend_stack"]),
        "ema_3": float(row["ema_3"]),
        "ema_8": float(row["ema_8"]),
        "ema_21": float(row["ema_21"]),
        "ema_50": float(row["ema_50"]),
        **outcomes,
    }


def scan_symbol(
    client: StockHistoricalDataClient,
    symbol: str,
    start: str,
    end: str,
) -> pd.DataFrame:
    raw = fetch_symbol(client, symbol, start, end)
    if raw.empty:
        return pd.DataFrame()

    df = add_features(raw)

    crossover_positions = np.flatnonzero(
        df["bullish_crossover"].fillna(False).to_numpy()
    )

    records = []
    for pos in crossover_positions:
        for persistence in (1, 2, 3):
            row = make_event_row(df, symbol, int(pos), persistence)
            if row is not None:
                records.append(row)

    return pd.DataFrame(records)


def finite_mean(s: pd.Series) -> float:
    x = pd.to_numeric(s, errors="coerce").dropna()
    return float(x.mean()) if len(x) else np.nan


def finite_median(s: pd.Series) -> float:
    x = pd.to_numeric(s, errors="coerce").dropna()
    return float(x.median()) if len(x) else np.nan


def evaluate_subset(
    name: str,
    subset: pd.DataFrame,
    universe: pd.DataFrame,
) -> dict:
    n = len(subset)

    if n == 0:
        return {
            "filter": name,
            "signals": 0,
            "coverage_pct": 0.0,
            "forward_1d_mean": np.nan,
            "forward_3d_mean": np.nan,
            "forward_5d_mean": np.nan,
            "forward_5d_median": np.nan,
            "forward_10d_mean": np.nan,
            "forward_10d_median": np.nan,
            "mfe_5d_mean": np.nan,
            "mae_5d_mean": np.nan,
            "positive_rate": np.nan,
            "strong_positive_rate": np.nan,
        }

    f5 = pd.to_numeric(subset["forward_5d"], errors="coerce")

    return {
        "filter": name,
        "signals": n,
        "coverage_pct": n / max(len(universe), 1) * 100.0,
        "forward_1d_mean": finite_mean(subset["forward_1d"]),
        "forward_3d_mean": finite_mean(subset["forward_3d"]),
        "forward_5d_mean": finite_mean(subset["forward_5d"]),
        "forward_5d_median": finite_median(subset["forward_5d"]),
        "forward_10d_mean": finite_mean(subset["forward_10d"]),
        "forward_10d_median": finite_median(subset["forward_10d"]),
        "mfe_5d_mean": finite_mean(subset["mfe_5d"]),
        "mae_5d_mean": finite_mean(subset["mae_5d"]),
        "positive_rate": float((f5 > 0).mean()),
        "strong_positive_rate": float((f5 >= 0.05).mean()),
    }


def candidate_filters() -> dict[str, dict]:
    """
    Each filter specifies the persistence level whose decision bar is used.
    This prevents persistence filters from leaking future information.
    """
    return {
        "RAW_CROSSOVER": {
            "persistence": 1,
            "conditions": [],
        },
        "TREND_STACK": {
            "persistence": 1,
            "conditions": ["TREND_STACK"],
        },
        "RSI_GT_50": {
            "persistence": 1,
            "conditions": ["RSI_GT_50"],
        },
        "MACD_HIST_GT_0": {
            "persistence": 1,
            "conditions": ["MACD_HIST_GT_0"],
        },
        "PRICE_ACCEL_GT_0": {
            "persistence": 1,
            "conditions": ["PRICE_ACCEL_GT_0"],
        },
        "REL_VOLUME_GE_1": {
            "persistence": 1,
            "conditions": ["REL_VOLUME_GE_1"],
        },
        "ATR_PCT_LE_4": {
            "persistence": 1,
            "conditions": ["ATR_PCT_LE_4"],
        },
        "PERSISTENCE_2": {
            "persistence": 2,
            "conditions": [],
        },
        "PERSISTENCE_3": {
            "persistence": 3,
            "conditions": [],
        },
        "TREND_STACK + MACD_HIST_GT_0": {
            "persistence": 1,
            "conditions": ["TREND_STACK", "MACD_HIST_GT_0"],
        },
        "TREND_STACK + PRICE_ACCEL_GT_0": {
            "persistence": 1,
            "conditions": ["TREND_STACK", "PRICE_ACCEL_GT_0"],
        },
        "TREND_STACK + REL_VOLUME_GE_1": {
            "persistence": 1,
            "conditions": ["TREND_STACK", "REL_VOLUME_GE_1"],
        },
        "TREND_STACK + PERSISTENCE_2": {
            "persistence": 2,
            "conditions": ["TREND_STACK"],
        },
        "MACD_HIST_GT_0 + PERSISTENCE_2": {
            "persistence": 2,
            "conditions": ["MACD_HIST_GT_0"],
        },
        "MACD_HIST_GT_0 + PERSISTENCE_3": {
            "persistence": 3,
            "conditions": ["MACD_HIST_GT_0"],
        },
        "TREND_STACK + MACD_HIST_GT_0 + PERSISTENCE_2": {
            "persistence": 2,
            "conditions": ["TREND_STACK", "MACD_HIST_GT_0"],
        },
        "TREND_STACK + MACD_HIST_GT_0 + PERSISTENCE_3": {
            "persistence": 3,
            "conditions": ["TREND_STACK", "MACD_HIST_GT_0"],
        },
        "TREND_STACK + MACD_HIST_GT_0 + PRICE_ACCEL_GT_0 + PERSISTENCE_2": {
            "persistence": 2,
            "conditions": [
                "TREND_STACK",
                "MACD_HIST_GT_0",
                "PRICE_ACCEL_GT_0",
            ],
        },
        "TREND_STACK + MACD_HIST_GT_0 + PRICE_ACCEL_GT_0 + PERSISTENCE_3": {
            "persistence": 3,
            "conditions": [
                "TREND_STACK",
                "MACD_HIST_GT_0",
                "PRICE_ACCEL_GT_0",
            ],
        },
    }


def apply_condition_mask(df: pd.DataFrame, conditions: list[str]) -> pd.Series:
    mask = pd.Series(True, index=df.index)

    for condition in conditions:
        if condition == "TREND_STACK":
            mask &= df["trend_stack"].astype(bool)
        elif condition == "RSI_GT_50":
            mask &= df["rsi_14"] > 50
        elif condition == "MACD_HIST_GT_0":
            mask &= df["macd_hist"] > 0
        elif condition == "PRICE_ACCEL_GT_0":
            mask &= df["price_acceleration"] > 0
        elif condition == "REL_VOLUME_GE_1":
            mask &= df["rel_volume"] >= 1.0
        elif condition == "ATR_PCT_LE_4":
            mask &= df["atr_pct"] <= 4.0
        else:
            raise ValueError(f"Unknown filter condition: {condition}")

    return mask


def evaluate_all_filters(
    events: pd.DataFrame,
    start: pd.Timestamp | None = None,
    end: pd.Timestamp | None = None,
) -> pd.DataFrame:
    if events.empty:
        return pd.DataFrame()

    scoped = events.copy()
    scoped["decision_dt"] = pd.to_datetime(
        scoped["decision_timestamp"], utc=True
    )

    if start is not None:
        scoped = scoped[scoped["decision_dt"] >= utc_timestamp(start)]
    if end is not None:
        # End is inclusive through the requested calendar date.
        end_ts = utc_timestamp(end) + pd.Timedelta(days=1) - pd.Timedelta(microseconds=1)
        scoped = scoped[scoped["decision_dt"] <= end_ts]

    results = []

    for name, spec in candidate_filters().items():
        level = spec["persistence"]
        frame = scoped[scoped["persistence_level"] == level].copy()
        mask = apply_condition_mask(frame, spec["conditions"])

        result = evaluate_subset(name, frame.loc[mask], frame)
        result["persistence_level"] = level
        result["conditions"] = " + ".join(spec["conditions"]) or "NONE"
        results.append(result)

    out = pd.DataFrame(results)

    # Training score is deliberately conservative and visible.
    # Returns are decimal fractions; positive_rate is 0..1.
    out["selection_score"] = (
        out["forward_5d_median"].fillna(-999) * 0.50
        + out["forward_5d_mean"].fillna(-999) * 0.30
        + (out["positive_rate"].fillna(0) - 0.50) * 0.20
    )

    return out.sort_values(
        ["selection_score", "signals"],
        ascending=[False, False],
    ).reset_index(drop=True)


def choose_training_filter(
    ranking: pd.DataFrame,
    min_signals: int,
) -> pd.Series | None:
    eligible = ranking[ranking["signals"] >= min_signals].copy()
    if eligible.empty:
        return None
    return eligible.iloc[0]


def evaluate_named_filter(
    events: pd.DataFrame,
    filter_name: str,
    start: str,
    end: str,
) -> dict:
    spec = candidate_filters()[filter_name]
    scoped = events.copy()
    scoped["decision_dt"] = pd.to_datetime(
        scoped["decision_timestamp"], utc=True
    )

    start_ts = utc_timestamp(start)
    end_ts = utc_timestamp(end) + pd.Timedelta(days=1) - pd.Timedelta(microseconds=1)

    scoped = scoped[
        (scoped["decision_dt"] >= start_ts)
        & (scoped["decision_dt"] <= end_ts)
        & (scoped["persistence_level"] == spec["persistence"])
    ]

    mask = apply_condition_mask(scoped, spec["conditions"])
    subset = scoped.loc[mask]

    result = evaluate_subset(filter_name, subset, scoped)
    result["period_start"] = start
    result["period_end"] = end
    result["persistence_level"] = spec["persistence"]
    result["conditions"] = " + ".join(spec["conditions"]) or "NONE"

    return result


def compare_to_raw(period_results: pd.DataFrame) -> pd.DataFrame:
    out = period_results.copy()

    raw = out[out["filter"] == "RAW_CROSSOVER"][
        ["period_start", "period_end", "forward_5d_mean",
         "forward_5d_median", "positive_rate"]
    ].rename(
        columns={
            "forward_5d_mean": "raw_mean",
            "forward_5d_median": "raw_median",
            "positive_rate": "raw_positive_rate",
        }
    )

    out = out.merge(
        raw,
        on=["period_start", "period_end"],
        how="left",
    )

    out["mean_lift_vs_raw"] = out["forward_5d_mean"] - out["raw_mean"]
    out["median_lift_vs_raw"] = (
        out["forward_5d_median"] - out["raw_median"]
    )
    out["positive_rate_lift_vs_raw"] = (
        out["positive_rate"] - out["raw_positive_rate"]
    )

    return out


def robustness_summary(
    frozen_results: pd.DataFrame,
    selected_filter: str,
    min_test_signals: int,
) -> dict:
    x = frozen_results[
        (frozen_results["filter"] == selected_filter)
        & (frozen_results["period_type"] == "FROZEN_TEST")
    ].copy()

    eligible = x[x["signals"] >= min_test_signals].copy()

    if eligible.empty:
        return {
            "selected_filter": selected_filter,
            "eligible_test_periods": 0,
            "test_periods": len(x),
            "total_test_signals": int(x["signals"].sum()),
            "mean_lift_vs_raw": np.nan,
            "median_lift_vs_raw": np.nan,
            "positive_rate_lift_vs_raw": np.nan,
            "periods_positive_median_lift": 0,
            "periods_positive_mean_lift": 0,
            "periods_positive_rate_lift": 0,
            "robustness_score": np.nan,
            "robust": False,
        }

    median_lift = finite_mean(eligible["median_lift_vs_raw"])
    mean_lift = finite_mean(eligible["mean_lift_vs_raw"])
    rate_lift = finite_mean(eligible["positive_rate_lift_vs_raw"])

    score = (
        0.50 * median_lift
        + 0.30 * mean_lift
        + 0.20 * rate_lift
    )

    return {
        "selected_filter": selected_filter,
        "eligible_test_periods": len(eligible),
        "test_periods": len(x),
        "total_test_signals": int(eligible["signals"].sum()),
        "mean_lift_vs_raw": mean_lift,
        "median_lift_vs_raw": median_lift,
        "positive_rate_lift_vs_raw": rate_lift,
        "periods_positive_median_lift": int(
            (eligible["median_lift_vs_raw"] > 0).sum()
        ),
        "periods_positive_mean_lift": int(
            (eligible["mean_lift_vs_raw"] > 0).sum()
        ),
        "periods_positive_rate_lift": int(
            (eligible["positive_rate_lift_vs_raw"] > 0).sum()
        ),
        "robustness_score": score,
        "robust": bool(
            len(eligible) >= 2
            and (eligible["median_lift_vs_raw"] > 0).sum() >= 2
            and (eligible["positive_rate_lift_vs_raw"] > 0).sum() >= 2
        ),
    }


def build_symbol_summary(events: pd.DataFrame) -> pd.DataFrame:
    rows = []

    raw = events[events["persistence_level"] == 1].copy()

    for symbol, g in raw.groupby("symbol"):
        rows.append({
            "symbol": symbol,
            "raw_crossover_events": len(g),
            "raw_forward_5d_mean": finite_mean(g["forward_5d"]),
            "raw_forward_5d_median": finite_median(g["forward_5d"]),
            "raw_positive_rate": float(
                (pd.to_numeric(g["forward_5d"], errors="coerce") > 0).mean()
            ),
        })

    return pd.DataFrame(rows).sort_values(
        ["raw_forward_5d_median", "raw_positive_rate"],
        ascending=[False, False],
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", nargs="+")
    parser.add_argument("--universe", choices=["equity"], default=None)
    parser.add_argument("--start", default="2024-01-01")
    parser.add_argument("--end", default="2026-08-25")
    parser.add_argument("--train-end", default="2025-12-31")
    parser.add_argument("--min-train-signals", type=int, default=20)
    parser.add_argument("--min-test-signals", type=int, default=10)
    args = parser.parse_args()

    load_dotenv()

    api_key = os.getenv("ALPACA_API_KEY")
    secret_key = os.getenv("ALPACA_SECRET_KEY")

    if not api_key or not secret_key:
        raise SystemExit(
            "Missing ALPACA_API_KEY / ALPACA_SECRET_KEY in .env"
        )

    if args.symbols:
        symbols = args.symbols
    else:
        symbols = DEFAULT_EQUITY_SYMBOLS

    output_dir = Path("research")
    output_dir.mkdir(exist_ok=True)

    client = StockHistoricalDataClient(api_key, secret_key)

    print("=" * 78)
    print("AURA v0.4.2 — MULTI-PERIOD ROBUSTNESS TEST")
    print("=" * 78)
    print(f"Symbols:                 {len(symbols)}")
    print(f"Historical data:         {args.start} -> {args.end}")
    print(f"Frozen training:         {args.start} -> {args.train_end}")
    print("Frozen tests:")
    print("  Q1 2026:               2026-01-01 -> 2026-03-31")
    print("  Q2 2026:               2026-04-01 -> 2026-06-30")
    print("  Q3 2026:               2026-07-01 -> 2026-08-25")
    print(f"Min train signals:       {args.min_train_signals}")
    print(f"Min test signals:        {args.min_test_signals}")
    print("Mode:                    RESEARCH ONLY — NO ORDERS")
    print()
    print("Persistence is confirmation-based; no future-bar leakage is used.")
    print()

    all_frames = []
    failed = []

    for i, symbol in enumerate(symbols, start=1):
        print(f"[{i:>2}/{len(symbols)}] {symbol:<6}", end=" ")

        try:
            result = scan_symbol(client, symbol, args.start, args.end)

            if result.empty:
                print("no qualifying events")
                continue

            print(f"{len(result)} events")
            all_frames.append(result)

        except Exception as exc:
            print(f"FAILED: {exc}")
            failed.append((symbol, str(exc)))

    if not all_frames:
        raise SystemExit("No historical events were found.")

    events = pd.concat(all_frames, ignore_index=True)
    events = events.sort_values(
        ["symbol", "crossover_timestamp", "persistence_level"]
    ).reset_index(drop=True)

    # ------------------------------------------------------------------
    # TRAINING
    # ------------------------------------------------------------------
    train_ranking = evaluate_all_filters(
        events,
        start=args.start,
        end=args.train_end,
    )

    selected = choose_training_filter(
        train_ranking,
        args.min_train_signals,
    )

    if selected is None:
        raise SystemExit(
            "No filter met --min-train-signals. Lower the threshold or "
            "extend the training period."
        )

    selected_filter = str(selected["filter"])

    # ------------------------------------------------------------------
    # FROZEN MULTI-PERIOD TEST
    # ------------------------------------------------------------------
    frozen_periods = [
        ("2026-01-01", "2026-03-31"),
        ("2026-04-01", "2026-06-30"),
        ("2026-07-01", args.end),
    ]

    frozen_rows = []

    for start, end in frozen_periods:
        for name in ["RAW_CROSSOVER", selected_filter]:
            row = evaluate_named_filter(events, name, start, end)
            row["period_type"] = "FROZEN_TEST"
            frozen_rows.append(row)

    frozen_results = compare_to_raw(pd.DataFrame(frozen_rows))

    # ------------------------------------------------------------------
    # EXPANDING WALK-FORWARD TEST
    #
    # The filter may be re-selected at each step, but only using data
    # available before that test window.
    # ------------------------------------------------------------------
    walk_periods = [
        (args.start, "2025-12-31", "2026-01-01", "2026-03-31"),
        (args.start, "2026-03-31", "2026-04-01", "2026-06-30"),
        (args.start, "2026-06-30", "2026-07-01", args.end),
    ]

    walk_rows = []
    walk_selection_rows = []

    for train_start, train_end, test_start, test_end in walk_periods:
        ranking = evaluate_all_filters(
            events,
            start=train_start,
            end=train_end,
        )
        chosen = choose_training_filter(
            ranking,
            args.min_train_signals,
        )

        if chosen is None:
            continue

        chosen_name = str(chosen["filter"])

        walk_selection_rows.append({
            "train_start": train_start,
            "train_end": train_end,
            "test_start": test_start,
            "test_end": test_end,
            "selected_filter": chosen_name,
            "train_signals": int(chosen["signals"]),
            "train_5d_mean": float(chosen["forward_5d_mean"]),
            "train_5d_median": float(chosen["forward_5d_median"]),
            "train_positive_rate": float(chosen["positive_rate"]),
            "selection_score": float(chosen["selection_score"]),
        })

        for name in ["RAW_CROSSOVER", chosen_name]:
            row = evaluate_named_filter(events, name, test_start, test_end)
            row["period_type"] = "WALK_FORWARD_TEST"
            row["train_start"] = train_start
            row["train_end"] = train_end
            row["test_start"] = test_start
            row["test_end"] = test_end
            row["selected_filter"] = chosen_name
            walk_rows.append(row)

    walk_results = compare_to_raw(pd.DataFrame(walk_rows))

    # ------------------------------------------------------------------
    # ROBUSTNESS SUMMARY
    # ------------------------------------------------------------------
    frozen_summary = robustness_summary(
        frozen_results,
        selected_filter,
        args.min_test_signals,
    )

    frozen_summary_row = {
        "validation_type": "FROZEN_MULTI_PERIOD",
        **frozen_summary,
    }

    if not walk_results.empty:
        wf_selected = walk_results[
            walk_results["filter"] != "RAW_CROSSOVER"
        ].copy()

        wf_eligible = wf_selected[
            wf_selected["signals"] >= args.min_test_signals
        ]

        if len(wf_eligible):
            wf_median_lift = finite_mean(
                wf_eligible["median_lift_vs_raw"]
            )
            wf_mean_lift = finite_mean(
                wf_eligible["mean_lift_vs_raw"]
            )
            wf_rate_lift = finite_mean(
                wf_eligible["positive_rate_lift_vs_raw"]
            )

            wf_score = (
                0.50 * wf_median_lift
                + 0.30 * wf_mean_lift
                + 0.20 * wf_rate_lift
            )

            walk_summary_row = {
                "validation_type": "EXPANDING_WALK_FORWARD",
                "selected_filter": "PER_PERIOD_SELECTION",
                "eligible_test_periods": len(wf_eligible),
                "test_periods": len(wf_selected),
                "total_test_signals": int(wf_eligible["signals"].sum()),
                "mean_lift_vs_raw": wf_mean_lift,
                "median_lift_vs_raw": wf_median_lift,
                "positive_rate_lift_vs_raw": wf_rate_lift,
                "periods_positive_median_lift": int(
                    (wf_eligible["median_lift_vs_raw"] > 0).sum()
                ),
                "periods_positive_mean_lift": int(
                    (wf_eligible["mean_lift_vs_raw"] > 0).sum()
                ),
                "periods_positive_rate_lift": int(
                    (wf_eligible["positive_rate_lift_vs_raw"] > 0).sum()
                ),
                "robustness_score": wf_score,
                "robust": bool(
                    len(wf_eligible) >= 2
                    and (
                        (wf_eligible["median_lift_vs_raw"] > 0).sum()
                        >= 2
                    )
                    and (
                        (wf_eligible["positive_rate_lift_vs_raw"] > 0).sum()
                        >= 2
                    )
                ),
            }
        else:
            walk_summary_row = {
                "validation_type": "EXPANDING_WALK_FORWARD",
                "selected_filter": "PER_PERIOD_SELECTION",
                "eligible_test_periods": 0,
                "test_periods": len(wf_selected),
                "total_test_signals": 0,
                "mean_lift_vs_raw": np.nan,
                "median_lift_vs_raw": np.nan,
                "positive_rate_lift_vs_raw": np.nan,
                "periods_positive_median_lift": 0,
                "periods_positive_mean_lift": 0,
                "periods_positive_rate_lift": 0,
                "robustness_score": np.nan,
                "robust": False,
            }
    else:
        walk_summary_row = {
            "validation_type": "EXPANDING_WALK_FORWARD",
            "selected_filter": "PER_PERIOD_SELECTION",
            "eligible_test_periods": 0,
            "test_periods": 0,
            "total_test_signals": 0,
            "mean_lift_vs_raw": np.nan,
            "median_lift_vs_raw": np.nan,
            "positive_rate_lift_vs_raw": np.nan,
            "periods_positive_median_lift": 0,
            "periods_positive_mean_lift": 0,
            "periods_positive_rate_lift": 0,
            "robustness_score": np.nan,
            "robust": False,
        }

    robustness = pd.DataFrame(
        [frozen_summary_row, walk_summary_row]
    )

    symbol_summary = build_symbol_summary(events)

    # ------------------------------------------------------------------
    # OUTPUT
    # ------------------------------------------------------------------
    paths = {
        "events": output_dir / "v042_historical_events.csv",
        "train": output_dir / "v042_train_filter_ranking.csv",
        "frozen": output_dir / "v042_frozen_test_results.csv",
        "walk": output_dir / "v042_walk_forward_results.csv",
        "walk_selection": output_dir / "v042_walk_forward_selection.csv",
        "robustness": output_dir / "v042_robustness_summary.csv",
        "symbols": output_dir / "v042_symbol_summary.csv",
    }

    events.to_csv(paths["events"], index=False)
    train_ranking.to_csv(paths["train"], index=False)
    frozen_results.to_csv(paths["frozen"], index=False)
    walk_results.to_csv(paths["walk"], index=False)
    pd.DataFrame(walk_selection_rows).to_csv(
        paths["walk_selection"], index=False
    )
    robustness.to_csv(paths["robustness"], index=False)
    symbol_summary.to_csv(paths["symbols"], index=False)

    # ------------------------------------------------------------------
    # CONSOLE REPORT
    # ------------------------------------------------------------------
    print()
    print("=" * 78)
    print("TRAINING FILTER RANKING")
    print("=" * 78)

    display_cols = [
        "filter",
        "signals",
        "forward_5d_mean",
        "forward_5d_median",
        "positive_rate",
        "selection_score",
    ]

    print(
        train_ranking[
            display_cols
        ].head(15).to_string(index=False)
    )

    print()
    print("=" * 78)
    print("SELECTED FROZEN FILTER")
    print("=" * 78)
    print(selected_filter)
    print(
        f"TRAIN signals: {int(selected['signals'])} | "
        f"5D mean: {selected['forward_5d_mean']:.4f} | "
        f"5D median: {selected['forward_5d_median']:.4f} | "
        f"Positive rate: {selected['positive_rate']:.2%}"
    )

    print()
    print("=" * 78)
    print("FROZEN MULTI-PERIOD OUT-OF-SAMPLE TEST")
    print("=" * 78)

    frozen_display = [
        "period_start",
        "period_end",
        "filter",
        "signals",
        "forward_5d_mean",
        "forward_5d_median",
        "positive_rate",
        "mean_lift_vs_raw",
        "median_lift_vs_raw",
        "positive_rate_lift_vs_raw",
    ]

    print(
        frozen_results[
            frozen_display
        ].to_string(index=False)
    )

    print()
    print("=" * 78)
    print("ROBUSTNESS SUMMARY")
    print("=" * 78)
    print(robustness.to_string(index=False))

    print()
    print("=" * 78)
    print("EXPANDING WALK-FORWARD SELECTION")
    print("=" * 78)

    if walk_selection_rows:
        print(
            pd.DataFrame(walk_selection_rows).to_string(index=False)
        )
    else:
        print("No eligible walk-forward period.")

    print()
    print("=" * 78)
    print("CSV OUTPUT")
    print("=" * 78)

    for label, path in paths.items():
        print(f"{label:<16} {path}")

    if failed:
        print()
        print("=" * 78)
        print("FAILED SYMBOLS")
        print("=" * 78)
        for symbol, reason in failed:
            print(f"{symbol}: {reason}")

    print()
    print("=" * 78)
    print("AURA v0.4.2 COMPLETE — NO ORDERS WERE PLACED")
    print("=" * 78)


if __name__ == "__main__":
    main()
