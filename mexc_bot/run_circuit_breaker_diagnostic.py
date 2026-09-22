"""
RESEARCH-ONLY DIAGNOSTIC — not a production strategy result.

Purpose (per explicit instruction): quantify exactly how much the existing,
PERMANENT circuit-breaker halt (core/risk_manager.py::CircuitBreaker, once
halted it never un-halts) affects the frozen 720-day baseline
(NATIVE_BASELINE_REPORT_2026-09-17.md), and separately (a) compute a
corrected, equity-based drawdown metric alongside the existing
cumulative-PnL-based one, and (b) report funding-history coverage per
symbol without inventing any missing data.

STRICT — nothing below modifies, monkeypatches, or reimplements:
    core/strategy.py, backtester.py, config.py, core/indicators.py,
    core/risk_manager.py, core/trade_metrics.py
Every entry/exit/sizing/stop/TP/trailing/time-stop decision in this script
is made by the SAME unmodified backtester.simulate_symbol() used for the
frozen baseline. No CCXT. No new market data (same dataset as the frozen
baseline: data/native_v2_full720day_20260917_extracted/). No parameter
sweep, no walk-forward, no permutation test, no live trading, no commit.

HOW THE "AFTER CIRCUIT BREAKER" COUNTERFACTUAL IS PRODUCED (methodology,
not a modification of protected code):
  1. Indicators are computed ONCE via the unmodified core.strategy.add_indicators()
     on each symbol's FULL, continuous 17,280-candle series. This means every
     EMA/RSI/ATR/ADX value reflects true, full history at that point in time
     -- not a value recomputed cold from a shorter slice.
  2. "Segment 1" = simulate_symbol() run on the full, indicator-populated
     series with a fresh CircuitBreaker. This reproduces the frozen baseline
     exactly (same function, same inputs) and is the BEFORE-halt evidence.
  3. Because simulate_symbol() takes a plain DataFrame and a caller-supplied
     CircuitBreaker object -- it has no internal concept of "resume" -- the
     ONLY way to continue evaluating the strategy past a halt without
     touching risk_manager.py or backtester.py is to call simulate_symbol()
     AGAIN on the remaining candles (already indicator-populated, so no
     re-warm-up distortion to the indicator VALUES themselves) with a NEW,
     non-halted CircuitBreaker instance, continuing the SAME equity balance
     forward. This is exactly what run_broad_backtest.py already does
     between DIFFERENT symbols (one fresh breaker per symbol); here it is
     applied between time SEGMENTS of the same symbol, purely for this
     diagnostic. Each time the new breaker halts again, the process repeats
     until either the remaining data runs out or a segment produces no
     further trades. Every such reset is logged explicitly as a
     counterfactual re-enable event -- this is NOT how the production bot
     runs; the production risk manager's halt is correctly permanent.
  4. One disclosed, unavoidable side effect of reusing simulate_symbol()
     unmodified: it always skips the first
     max(EMA_SLOW, BREAKOUT_LOOKBACK) + 5 = 205 rows of WHATEVER DataFrame
     it is given (an index-based warm-up skip, not a "wait for valid
     indicators" check). So each post-halt segment loses its first ~205
     hours (~8.5 days) to this skip even though the indicator VALUES there
     are already valid. This is a known, minor, disclosed limitation of
     reusing the function as-is, not an error in this script.
"""
from __future__ import annotations

import json
import sys
import time
import platform
from pathlib import Path
from datetime import datetime, timezone

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

import config as cfg                              # noqa: E402  (unmodified, read-only)
from core import strategy as strat                 # noqa: E402  (unmodified)
from core import trade_metrics                      # noqa: E402  (unmodified)
from core.risk_manager import CircuitBreaker         # noqa: E402  (unmodified)
from backtester import simulate_symbol               # noqa: E402  (unmodified)

DATASET_ROOT = Path(__file__).parent / "data" / "native_v2_full720day_20260917_extracted" / "mexc_native_v2"
OHLCV_DIR = DATASET_ROOT / "ohlcv"
FUNDING_DIR = DATASET_ROOT / "funding"

FROZEN_SYMBOLS = [
    "BTC_USDT", "ETH_USDT", "BNB_USDT", "XRP_USDT", "ADA_USDT",
    "DOGE_USDT", "AVAX_USDT", "DOT_USDT", "SUI_USDT", "TIA_USDT", "COTI_USDT",
]

OUT_DIR = Path(__file__).parent / "diagnostic_results"
OUT_DIR.mkdir(exist_ok=True)

MAX_RESET_SEGMENTS = 30  # safety cap only; not expected to be hit


def load_symbol_data(symbol: str):
    df = pd.read_parquet(OHLCV_DIR / f"{symbol}_1h.parquet")
    funding_path = FUNDING_DIR / f"{symbol}_funding.parquet"
    funding_df = pd.read_parquet(funding_path) if funding_path.exists() else pd.DataFrame(columns=["timestamp", "funding_rate"])
    return df, funding_df


def equity_curve_from_log(trade_log: list, starting_equity: float, series_start_ts) -> pd.DataFrame:
    """Research-only equity curve: [t0, starting_equity] followed by every
    logged event's (time, equity) in chronological order. Uses the ACTUAL
    equity values produced by the existing, unmodified simulate_symbol()."""
    rows = [{"time": series_start_ts, "equity": starting_equity}]
    for e in trade_log:
        rows.append({"time": e["time"], "equity": e["equity"]})
    curve = pd.DataFrame(rows).sort_values("time").reset_index(drop=True)
    return curve


def corrected_drawdown(curve: pd.DataFrame) -> dict:
    """Research-only equity-based drawdown, per explicit spec:
    equity_peak = running max; drawdown_amount = equity - equity_peak;
    drawdown_pct = drawdown_amount / equity_peak.
    NOT a modification of core/trade_metrics.py -- a separate calculation."""
    if curve.empty:
        return {"max_drawdown_amount": 0.0, "max_drawdown_pct": 0.0,
                "peak_equity": None, "peak_ts": None,
                "trough_equity": None, "trough_ts": None}
    curve = curve.copy()
    curve["equity_peak"] = curve["equity"].cummax()
    curve["drawdown_amount"] = curve["equity"] - curve["equity_peak"]
    curve["drawdown_pct"] = curve["drawdown_amount"] / curve["equity_peak"]
    trough_idx = curve["drawdown_amount"].idxmin()
    trough_row = curve.loc[trough_idx]
    peak_idx = curve.loc[:trough_idx, "equity"].idxmax()
    peak_row = curve.loc[peak_idx]
    return {
        "max_drawdown_amount": round(float(trough_row["drawdown_amount"]), 2),
        "max_drawdown_pct": round(float(trough_row["drawdown_pct"]) * 100, 2),
        "peak_equity": round(float(peak_row["equity"]), 2),
        "peak_ts": str(peak_row["time"]),
        "trough_equity": round(float(trough_row["equity"]), 2),
        "trough_ts": str(trough_row["time"]),
    }


def run_symbol_diagnostic(symbol: str) -> dict:
    df, funding_df = load_symbol_data(symbol)
    series_start_ts = df["timestamp"].iloc[0]
    series_end_ts = df["timestamp"].iloc[-1]
    total_window_days = (series_end_ts - series_start_ts).total_seconds() / 86400.0

    # Indicators computed ONCE on the full, continuous series via the
    # unmodified core.strategy.add_indicators() -- see module docstring.
    df_ind = strat.add_indicators(df)

    # ---------------- Segment 1: BEFORE (== frozen baseline for this symbol) ----------------
    tracker1 = {"equity": cfg.BACKTEST_STARTING_EQUITY, "open_count": 0}
    breaker1 = CircuitBreaker(cfg.BACKTEST_STARTING_EQUITY)
    log1 = []
    simulate_symbol(df_ind.copy(), symbol, tracker1, breaker1, log1, funding_df=funding_df)

    metrics1 = trade_metrics.compute_metrics(pd.DataFrame(log1), cfg.BACKTEST_STARTING_EQUITY) if log1 else \
        trade_metrics.compute_metrics(pd.DataFrame(), cfg.BACKTEST_STARTING_EQUITY)
    curve1 = equity_curve_from_log(log1, cfg.BACKTEST_STARTING_EQUITY, series_start_ts)
    dd1 = corrected_drawdown(curve1)

    trades1_df = trade_metrics.reconstruct_trades(pd.DataFrame(log1)) if log1 else pd.DataFrame()
    long1 = int((trades1_df["side"] == "long").sum()) if not trades1_df.empty else 0
    short1 = int((trades1_df["side"] == "short").sum()) if not trades1_df.empty else 0

    halted = breaker1.halted
    halt_event = {}
    segments_after = []
    trades_after_all = []

    if halted and log1:
        halt_time = pd.Timestamp(log1[-1]["time"])
        halt_equity = log1[-1]["equity"]
        # equity immediately preceding the halt-triggering event, for reference
        halt_event = {
            "halt_timestamp": str(halt_time),
            "triggering_equity": round(halt_equity, 2),
            "halt_reason_recorded_by_risk_manager": breaker1.halt_reason,
            "configured_threshold_pct": cfg.MAX_DRAWDOWN_LIMIT_PCT * 100,
            "trades_already_taken_before_halt": metrics1["num_trades"],
            "days_into_window": round((halt_time - series_start_ts).total_seconds() / 86400.0, 1),
        }

        # ---------------- C. AFTER: counterfactual reset-and-continue ----------------
        cutoff_time = halt_time
        running_equity = halt_equity
        for reset_i in range(MAX_RESET_SEGMENTS):
            seg_df = df_ind[df_ind["timestamp"] > cutoff_time].reset_index(drop=True)
            if len(seg_df) < 210:
                break
            seg_tracker = {"equity": running_equity, "open_count": 0}
            seg_breaker = CircuitBreaker(running_equity)  # reset: disclosed choice, see module docstring
            seg_log = []
            simulate_symbol(seg_df.copy(), symbol, seg_tracker, seg_breaker, seg_log, funding_df=funding_df)

            seg_metrics = trade_metrics.compute_metrics(pd.DataFrame(seg_log), running_equity) if seg_log else None
            segments_after.append({
                "reset_index": reset_i + 1,
                "segment_start": str(seg_df["timestamp"].iloc[0]),
                "segment_end_available": str(seg_df["timestamp"].iloc[-1]),
                "starting_equity_this_segment": round(running_equity, 2),
                "trades": (seg_metrics["num_trades"] if seg_metrics else 0),
                "final_equity_this_segment": round(seg_tracker["equity"], 2),
                "halted_again": seg_breaker.halted,
                "halt_reason": seg_breaker.halt_reason if seg_breaker.halted else None,
            })
            trades_after_all.extend(seg_log)

            if not seg_log:
                break
            if seg_breaker.halted:
                cutoff_time = pd.Timestamp(seg_log[-1]["time"])
                running_equity = seg_tracker["equity"]
                continue
            else:
                break

    metrics_after = trade_metrics.compute_metrics(pd.DataFrame(trades_after_all), halt_event.get("triggering_equity", cfg.BACKTEST_STARTING_EQUITY)) \
        if trades_after_all else trade_metrics.compute_metrics(pd.DataFrame(), cfg.BACKTEST_STARTING_EQUITY)

    trades_after_df = trade_metrics.reconstruct_trades(pd.DataFrame(trades_after_all)) if trades_after_all else pd.DataFrame()
    long_after = int((trades_after_df["side"] == "long").sum()) if not trades_after_df.empty else 0
    short_after = int((trades_after_df["side"] == "short").sum()) if not trades_after_df.empty else 0

    active_days_before_halt = halt_event.get("days_into_window", total_window_days)
    inactive_days_after_halt = round(total_window_days - active_days_before_halt, 1)
    pct_active = round(active_days_before_halt / total_window_days * 100, 1) if total_window_days else 0.0

    # ---------------- Funding coverage diagnostic ----------------
    if len(funding_df):
        funding_first = funding_df["timestamp"].min()
        funding_last = funding_df["timestamp"].max()
        days_without_native_funding = round((funding_first - series_start_ts).total_seconds() / 86400.0, 1)
        fallback_used = days_without_native_funding > 0
    else:
        funding_first = funding_last = None
        days_without_native_funding = round(total_window_days, 1)
        fallback_used = True

    return {
        "symbol": symbol,
        "ohlcv_first_ts": str(series_start_ts),
        "ohlcv_last_ts": str(series_end_ts),
        "total_window_days": round(total_window_days, 1),
        "A_before_circuit_breaker": {
            "first_trade_date": str(log1[0]["time"]) if log1 else None,
            "last_trade_date_before_halt": str(log1[-1]["time"]) if log1 else None,
            "num_trades": metrics1["num_trades"],
            "long_trades": long1,
            "short_trades": short1,
            "total_pnl": metrics1["total_pnl"],
            "total_return_pct": metrics1["total_return_pct"],
            "win_rate_pct": metrics1["win_rate_pct"],
            "profit_factor": metrics1["profit_factor"],
            "equity_immediately_before_halt": round(tracker1["equity"], 2),
            "max_actual_equity_drawdown_before_halt_pct": dd1["max_drawdown_pct"],
            "max_actual_equity_drawdown_before_halt_amount": dd1["max_drawdown_amount"],
        },
        "B_circuit_breaker_event": halt_event if halted else {"halted": False, "note": "did not halt in this run"},
        "C_after_circuit_breaker_counterfactual": {
            "label": "research diagnostic / counterfactual -- NOT a production strategy result",
            "reset_segments": segments_after,
            "total_trades_counterfactual": metrics_after["num_trades"],
            "long_trades_counterfactual": long_after,
            "short_trades_counterfactual": short_after,
            "total_pnl_counterfactual": metrics_after["total_pnl"],
            "win_rate_pct_counterfactual": metrics_after["win_rate_pct"],
            "profit_factor_counterfactual": metrics_after["profit_factor"],
        },
        "D_active_window": {
            "active_days_before_halt": active_days_before_halt,
            "inactive_days_after_halt": inactive_days_after_halt,
            "percentage_of_720_day_window_active": pct_active,
        },
        "corrected_drawdown_before_halt": dd1,
        "existing_compute_drawdown_before_halt_for_comparison": {
            "max_drawdown_pct_cumulative_pnl_basis": metrics1["max_drawdown_pct"],
            "max_drawdown_amount": metrics1["max_drawdown"],
        },
        "funding_coverage": {
            "funding_rows": len(funding_df),
            "funding_coverage_start": str(funding_first) if funding_first is not None else None,
            "funding_coverage_end": str(funding_last) if funding_last is not None else None,
            "days_without_native_funding_history": days_without_native_funding,
            "fallback_funding_used": fallback_used,
            "configured_fallback_funding_rate": cfg.FUNDING_RATE_FALLBACK,
        },
    }


def run():
    per_symbol = {}
    for symbol in FROZEN_SYMBOLS:
        print(f"[{symbol}] running circuit-breaker diagnostic ...")
        per_symbol[symbol] = run_symbol_diagnostic(symbol)
        b = per_symbol[symbol]["B_circuit_breaker_event"]
        print(f"  halted={bool(b.get('halt_timestamp'))} day={b.get('days_into_window')} "
              f"before_trades={per_symbol[symbol]['A_before_circuit_breaker']['num_trades']} "
              f"after_trades={per_symbol[symbol]['C_after_circuit_breaker_counterfactual']['total_trades_counterfactual']}")

    halted_symbols = [s for s in FROZEN_SYMBOLS if per_symbol[s]["B_circuit_breaker_event"].get("halt_timestamp")]
    halt_days = sorted(per_symbol[s]["B_circuit_breaker_event"]["days_into_window"] for s in halted_symbols)

    total_trades_before = sum(per_symbol[s]["A_before_circuit_breaker"]["num_trades"] for s in FROZEN_SYMBOLS)
    total_trades_after = sum(per_symbol[s]["C_after_circuit_breaker_counterfactual"]["total_trades_counterfactual"] for s in FROZEN_SYMBOLS)
    total_pnl_before = round(sum(per_symbol[s]["A_before_circuit_breaker"]["total_pnl"] for s in FROZEN_SYMBOLS), 2)
    total_pnl_after = round(sum(per_symbol[s]["C_after_circuit_breaker_counterfactual"]["total_pnl_counterfactual"] for s in FROZEN_SYMBOLS), 2)
    aggregate_counterfactual_pnl = round(total_pnl_before + total_pnl_after, 2)

    import statistics
    aggregate = {
        "total_trades_before_halt": total_trades_before,
        "total_trades_after_halt_diagnostic": total_trades_after,
        "total_pnl_before_halt": total_pnl_before,
        "total_pnl_after_halt_diagnostic": total_pnl_after,
        "aggregate_counterfactual_pnl": aggregate_counterfactual_pnl,
        "frozen_baseline_total_pnl": total_pnl_before,  # segment-1-only, i.e. the actual frozen baseline
        "difference_counterfactual_minus_frozen_baseline": round(aggregate_counterfactual_pnl - total_pnl_before, 2),
        "num_symbols_halted": len(halted_symbols),
        "median_halt_day": statistics.median(halt_days) if halt_days else None,
        "earliest_halt_day": min(halt_days) if halt_days else None,
        "latest_halt_day": max(halt_days) if halt_days else None,
    }

    result = {
        "run_timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "driver": "run_circuit_breaker_diagnostic.py",
        "label": "RESEARCH DIAGNOSTIC -- sections C (counterfactual) are NOT production results",
        "dataset_root": str(DATASET_ROOT),
        "python_version": platform.python_version(),
        "pandas_version": pd.__version__,
        "per_symbol": per_symbol,
        "aggregate": aggregate,
    }

    out_path = OUT_DIR / f"circuit_breaker_diagnostic_{int(time.time())}.json"
    out_path.write_text(json.dumps(result, indent=2, default=str))
    print(f"\nDiagnostic JSON written to {out_path}")

    # Flat per-symbol CSV summary for quick review
    rows = []
    for s in FROZEN_SYMBOLS:
        r = per_symbol[s]
        rows.append({
            "symbol": s,
            "before_trades": r["A_before_circuit_breaker"]["num_trades"],
            "before_pnl": r["A_before_circuit_breaker"]["total_pnl"],
            "before_win_rate_pct": r["A_before_circuit_breaker"]["win_rate_pct"],
            "before_profit_factor": r["A_before_circuit_breaker"]["profit_factor"],
            "corrected_dd_pct_before_halt": r["corrected_drawdown_before_halt"]["max_drawdown_pct"],
            "existing_dd_pct_before_halt": r["existing_compute_drawdown_before_halt_for_comparison"]["max_drawdown_pct_cumulative_pnl_basis"],
            "halt_day": r["B_circuit_breaker_event"].get("days_into_window"),
            "after_trades_counterfactual": r["C_after_circuit_breaker_counterfactual"]["total_trades_counterfactual"],
            "after_pnl_counterfactual": r["C_after_circuit_breaker_counterfactual"]["total_pnl_counterfactual"],
            "active_pct_of_window": r["D_active_window"]["percentage_of_720_day_window_active"],
            "funding_rows": r["funding_coverage"]["funding_rows"],
            "days_without_native_funding": r["funding_coverage"]["days_without_native_funding_history"],
        })
    csv_path = OUT_DIR / f"circuit_breaker_diagnostic_summary_{int(time.time())}.csv"
    pd.DataFrame(rows).to_csv(csv_path, index=False)
    print(f"Summary CSV written to {csv_path}")

    return result, out_path, csv_path


if __name__ == "__main__":
    run()
