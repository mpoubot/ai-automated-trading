"""
Frozen-baseline driver — runs the EXISTING, UNMODIFIED strategy/backtest
engine against the new verified native MEXC dataset
(data/native_v2_full720day_20260917.zip), WITHOUT touching any of:
    core/strategy.py, backtester.py, config.py, core/indicators.py,
    core/risk_manager.py, core/trade_metrics.py, core/data_fetcher.py

This script does NOT import ccxt and does NOT call core/data_fetcher.py at
all. It is new orchestration code only: it loads OHLCV + funding directly
from the new native parquet dataset and feeds them into the existing,
byte-for-byte unmodified functions:

    core.strategy.add_indicators()      (actually invoked internally by
                                          backtester.simulate_symbol() itself
                                          — see inspection note below)
    backtester.simulate_symbol()
    core.trade_metrics.compute_metrics()

INSPECTION NOTE (done before writing this driver, not assumed):
  backtester.simulate_symbol(df, symbol, equity_tracker, breaker, trade_log,
                              funding_df=None, entry_mode=None)
    - internally does `if "adx" not in df.columns: df = strat.add_indicators(df)`
      -- i.e. it already calls the unmodified add_indicators() itself if the
      caller hasn't. This driver passes the raw OHLCV frame and lets
      simulate_symbol() add indicators, rather than duplicating that call,
      so there is exactly one, unmodified code path for indicator computation.
    - requires df columns: timestamp (tz-naive pandas datetime — matches this
      dataset's schema exactly), open, high, low, close, volume.
    - funding_df, if given, must have columns timestamp (tz-naive, comparable
      to the OHLCV timestamp dtype) and funding_rate — matches this dataset's
      funding parquet schema exactly (timestamp, funding_rate,
      collect_cycle_hours; the extra column is harmless/unused).
    - symbol is used only as a plain label written into trade_log rows and
      into core.trade_metrics output — no ccxt-format requirement. This
      driver passes the native symbol strings (e.g. "BTC_USDT") unchanged.
    - equity/capital: driven by cfg.BACKTEST_STARTING_EQUITY, read by the
      caller (this driver), never modified in config.py.
    - Uses the SAME orchestration pattern as the existing, unmodified
      run_broad_backtest.py: one independent equity_tracker + CircuitBreaker
      PER SYMBOL (not one shared pool across symbols). This is a deliberate,
      disclosed choice — see BASELINE REPORT section "Aggregation convention"
      for why: backtester.py's OWN run_backtest() shares a single equity
      pool across symbols processed sequentially even though all symbols
      cover the same overlapping calendar period, which does not represent
      realistic concurrent multi-symbol exposure. run_broad_backtest.py's
      per-symbol-independent-capital convention is the existing, unmodified
      convention actually designed for testing this exact 11-symbol set
      (its own DEFAULT_SYMBOLS list matches Martin's frozen 11 exactly), so
      this driver reuses THAT existing convention rather than inventing a
      new one.

No parameter, entry/exit rule, or risk setting is changed anywhere. No
CCXT import. No network access. No writes to data/native/ (the old,
pre-pagination-fix dataset) — this driver reads only from
data/native_v2_full720day_20260917_extracted/.
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

import config as cfg                              # noqa: E402  (unmodified)
from core import strategy as strat                 # noqa: E402  (unmodified)
from core import trade_metrics                      # noqa: E402  (unmodified)
from core.risk_manager import CircuitBreaker         # noqa: E402  (unmodified)
from backtester import simulate_symbol               # noqa: E402  (unmodified)

DATASET_ROOT = Path(__file__).parent / "data" / "native_v2_full720day_20260917_extracted" / "mexc_native_v2"
OHLCV_DIR = DATASET_ROOT / "ohlcv"
FUNDING_DIR = DATASET_ROOT / "funding"
MANIFEST_PATH = DATASET_ROOT / "manifest.json"

FROZEN_SYMBOLS = [
    "BTC_USDT", "ETH_USDT", "BNB_USDT", "XRP_USDT", "ADA_USDT",
    "DOGE_USDT", "AVAX_USDT", "DOT_USDT", "SUI_USDT", "TIA_USDT", "COTI_USDT",
]

OUT_DIR = Path(__file__).parent / "baseline_results"
OUT_DIR.mkdir(exist_ok=True)


def load_symbol_data(symbol: str):
    ohlcv_path = OHLCV_DIR / f"{symbol}_1h.parquet"
    funding_path = FUNDING_DIR / f"{symbol}_funding.parquet"
    df = pd.read_parquet(ohlcv_path)
    funding_df = pd.read_parquet(funding_path) if funding_path.exists() else pd.DataFrame(columns=["timestamp", "funding_rate"])
    return df, funding_df, ohlcv_path, funding_path


def data_quality_checks(df: pd.DataFrame) -> dict:
    dup = int(df["timestamp"].duplicated().sum())
    sorted_ok = bool(df["timestamp"].is_monotonic_increasing)
    gaps = df["timestamp"].diff().dropna()
    non1h = gaps[gaps != pd.Timedelta(hours=1)]
    nan_counts = df[["open", "high", "low", "close", "volume"]].isna().sum().sum()
    inf_counts = 0
    for c in ["open", "high", "low", "close", "volume"]:
        inf_counts += int(((df[c] == float("inf")) | (df[c] == float("-inf"))).sum())
    hl_bad = int((df["high"] < df["low"]).sum())
    return {
        "rows": len(df),
        "duplicate_timestamps": dup,
        "chronological": sorted_ok,
        "gap_count": int(len(non1h)),
        "nan_count": int(nan_counts),
        "inf_count": inf_counts,
        "high_lt_low_count": hl_bad,
        "first_ts": str(df["timestamp"].min()),
        "last_ts": str(df["timestamp"].max()),
    }


def run():
    manifest = json.loads(MANIFEST_PATH.read_text())

    per_symbol_metrics = {}
    per_symbol_dataquality = {}
    per_symbol_funding_coverage = {}
    all_trade_logs = []
    halted_symbols = []

    for symbol in FROZEN_SYMBOLS:
        print(f"[{symbol}] loading native parquet ...")
        df, funding_df, ohlcv_path, funding_path = load_symbol_data(symbol)

        dq = data_quality_checks(df)
        per_symbol_dataquality[symbol] = dq

        funding_cov = {
            "funding_rows": len(funding_df),
            "funding_first_ts": str(funding_df["timestamp"].min()) if len(funding_df) else None,
            "funding_last_ts": str(funding_df["timestamp"].max()) if len(funding_df) else None,
            "ohlcv_first_ts": dq["first_ts"],
            "candles_before_earliest_funding": None,
        }
        if len(funding_df):
            earliest_funding = funding_df["timestamp"].min()
            funding_cov["candles_before_earliest_funding"] = int((df["timestamp"] < earliest_funding).sum())
        else:
            funding_cov["candles_before_earliest_funding"] = len(df)
        per_symbol_funding_coverage[symbol] = funding_cov

        # Independent equity pool + circuit breaker per symbol — matches the
        # existing, unmodified run_broad_backtest.py convention (see module
        # docstring for why this convention was chosen over backtester.py's
        # own shared-pool run_backtest()).
        equity_tracker = {"equity": cfg.BACKTEST_STARTING_EQUITY, "open_count": 0}
        breaker = CircuitBreaker(cfg.BACKTEST_STARTING_EQUITY)
        symbol_trade_log = []

        simulate_symbol(df, symbol, equity_tracker, breaker, symbol_trade_log, funding_df=funding_df)

        if breaker.halted:
            halted_symbols.append((symbol, breaker.halt_reason))
            print(f"  circuit breaker halted: {breaker.halt_reason}")

        all_trade_logs.extend(symbol_trade_log)
        sym_event_df = pd.DataFrame(symbol_trade_log)
        if not sym_event_df.empty:
            m = trade_metrics.compute_metrics(sym_event_df, cfg.BACKTEST_STARTING_EQUITY)
        else:
            m = trade_metrics.compute_metrics(pd.DataFrame(), cfg.BACKTEST_STARTING_EQUITY)
        per_symbol_metrics[symbol] = m
        print(f"  candles={dq['rows']} funding_rows={funding_cov['funding_rows']} trades={m['num_trades']}")

    log_df = pd.DataFrame(all_trade_logs)
    num_tested = len([s for s in FROZEN_SYMBOLS if per_symbol_metrics[s]["num_trades"] >= 0])
    combined_starting_capital = cfg.BACKTEST_STARTING_EQUITY * len(FROZEN_SYMBOLS)
    overall_metrics = trade_metrics.compute_metrics(log_df, combined_starting_capital)

    # --- extra breakdowns not produced by compute_metrics() itself ---
    trades_df = trade_metrics.reconstruct_trades(log_df) if not log_df.empty else pd.DataFrame()
    long_trades = int((trades_df["side"] == "long").sum()) if not trades_df.empty else 0
    short_trades = int((trades_df["side"] == "short").sum()) if not trades_df.empty else 0
    median_trade = float(trades_df["total_pnl"].median()) if not trades_df.empty else 0.0

    exit_counts = {}
    if not log_df.empty:
        exit_counts["partial_tp_events"] = int((log_df["type"] == "partial_tp").sum())
        exit_counts["exit_stop_events"] = int((log_df["type"] == "exit_stop").sum())
        exit_counts["exit_time_stop_events"] = int((log_df["type"] == "exit_time_stop").sum())
        exit_counts["funding_events"] = int((log_df["type"] == "funding").sum())
    else:
        exit_counts = {"partial_tp_events": 0, "exit_stop_events": 0, "exit_time_stop_events": 0, "funding_events": 0}

    per_symbol_long_short = {}
    for symbol in FROZEN_SYMBOLS:
        sym_events = pd.DataFrame([e for e in all_trade_logs if e["symbol"] == symbol])
        sym_trades = trade_metrics.reconstruct_trades(sym_events) if not sym_events.empty else pd.DataFrame()
        per_symbol_long_short[symbol] = {
            "long": int((sym_trades["side"] == "long").sum()) if not sym_trades.empty else 0,
            "short": int((sym_trades["side"] == "short").sum()) if not sym_trades.empty else 0,
            "median_trade": float(sym_trades["total_pnl"].median()) if not sym_trades.empty else 0.0,
        }

    result = {
        "run_timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "driver": "run_native_baseline.py",
        "dataset_root": str(DATASET_ROOT),
        "dataset_manifest_sha256": (DATASET_ROOT / "manifest_sha256.txt").read_text().strip(),
        "manifest_acquisition_id": manifest.get("acquisition_id"),
        "python_version": platform.python_version(),
        "pandas_version": pd.__version__,
        "starting_equity_per_symbol": cfg.BACKTEST_STARTING_EQUITY,
        "combined_starting_capital": combined_starting_capital,
        "config_snapshot": {
            k: getattr(cfg, k) for k in [
                "STRATEGY_MODE", "EMA_FAST", "EMA_MID", "EMA_SLOW", "BREAKOUT_LOOKBACK",
                "RSI_PERIOD", "RSI_LONG_MIN", "RSI_LONG_MAX", "RSI_SHORT_MIN", "RSI_SHORT_MAX",
                "VOLUME_LOOKBACK", "VOLUME_CONFIRM_MULT", "ALLOW_SHORTS", "USE_ADX_FILTER",
                "ADX_PERIOD", "ADX_MIN_THRESHOLD", "RISK_PER_TRADE_PCT", "ATR_STOP_MULT",
                "MAX_LEVERAGE", "MIN_LEVERAGE", "MAX_CONCURRENT_POSITIONS",
                "TAKE_PROFIT_R_MULT_PARTIAL", "PARTIAL_CLOSE_PCT", "TRAIL_ATR_MULT",
                "TIME_STOP_CANDLES", "DAILY_LOSS_LIMIT_PCT", "MAX_DRAWDOWN_LIMIT_PCT",
                "BACKTEST_STARTING_EQUITY", "BACKTEST_TAKER_FEE_PCT", "BACKTEST_SLIPPAGE_PCT",
                "MODEL_FUNDING_COSTS", "FUNDING_INTERVAL_HOURS", "FUNDING_RATE_FALLBACK",
                "USE_REAL_FUNDING_HISTORY", "TIMEFRAME",
            ]
        },
        "halted_symbols": halted_symbols,
        "overall_metrics": overall_metrics,
        "long_trades": long_trades,
        "short_trades": short_trades,
        "median_trade": median_trade,
        "exit_event_counts": exit_counts,
        "per_symbol_metrics": per_symbol_metrics,
        "per_symbol_long_short": per_symbol_long_short,
        "per_symbol_data_quality": per_symbol_dataquality,
        "per_symbol_funding_coverage": per_symbol_funding_coverage,
    }

    out_path = OUT_DIR / f"native_baseline_result_{int(time.time())}.json"
    out_path.write_text(json.dumps(result, indent=2, default=str))
    log_df.to_csv(OUT_DIR / f"native_baseline_trade_log_{int(time.time())}.csv", index=False)
    print(f"\nResult JSON written to {out_path}")

    trade_metrics.print_metrics_report(overall_metrics, combined_starting_capital,
                                        title="NATIVE DATASET BASELINE — AGGREGATE (independent per-symbol capital)")
    print(f"Long trades:  {long_trades}")
    print(f"Short trades: {short_trades}")
    print(f"Median trade: {median_trade}")
    return result, out_path


if __name__ == "__main__":
    run()
