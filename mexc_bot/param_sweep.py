"""
Parameter sweep: fetches historical data ONCE across a pair set, then tests
many combinations of the risk/exit parameters against that same cached data
(no repeated network calls — this is what makes a real sweep practical).

Sweeps the parameters most directly responsible for risk:reward shape:
  - ATR_STOP_MULT          (how far the stop sits from entry)
  - TAKE_PROFIT_R_MULT_PARTIAL  (how early partial profit is taken)
  - TRAIL_ATR_MULT         (how tight the trailing stop is after partial)
  - RSI_LONG_MIN/MAX       (entry timing band)

Usage:
    python param_sweep.py --days 180                      # default pair set
    python param_sweep.py --days 180 --quick               # smaller grid, faster
    python param_sweep.py --symbols "BTC/USDT:USDT" "ETH/USDT:USDT" --days 365

Results print as a ranked table (by expectancy per trade) and save in full
to a CSV so you can inspect every combination tested, not just the top ones.
"""
import argparse
import itertools
import time
import os

import pandas as pd

import config as cfg
from core import data_fetcher as dfetch
from core import strategy as strat
from core import trade_metrics
from core.risk_manager import CircuitBreaker
from backtester import simulate_symbol
from run_broad_backtest import DEFAULT_SYMBOLS
from datetime import datetime, timedelta, timezone


# ADX_MIN: 0 means "filter effectively off" — deliberately included so every
# sweep and walk-forward directly compares no-filter against filter settings.
# If the filter genuinely helps, higher thresholds should win on their own merit.
FULL_GRID = {
    "ATR_STOP_MULT": [1.0, 1.5, 2.0, 2.5],
    "TAKE_PROFIT_R_MULT_PARTIAL": [1.0, 1.5, 2.0, 3.0],
    "TRAIL_ATR_MULT": [1.5, 2.0, 3.0],
    "RSI_BAND": [(45, 65), (50, 70), (55, 75)],  # (RSI_LONG_MIN, RSI_LONG_MAX)
    "ADX_MIN": [0, 20, 25, 30],
}

QUICK_GRID = {
    "ATR_STOP_MULT": [1.0, 1.5, 2.0],
    "TAKE_PROFIT_R_MULT_PARTIAL": [1.5, 2.0],
    "TRAIL_ATR_MULT": [2.0],
    "RSI_BAND": [(50, 70)],
    "ADX_MIN": [0, 20, 25, 30],
}


def fetch_all_data(symbols: list[str], days: int, timeframe: str = None) -> dict:
    """Fetch OHLCV once per symbol; reused across every parameter combo."""
    timeframe = timeframe or cfg.TIMEFRAME
    exchange = dfetch.build_exchange()
    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=days)
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)

    cache = {}
    for symbol in symbols:
        print(f"Fetching {symbol} ({timeframe}, {days}d)...")
        try:
            df = dfetch.fetch_ohlcv_range(exchange, symbol, timeframe, start_ms, end_ms)
        except Exception as e:
            print(f"  skipped — fetch error: {e}")
            continue
        if not df.empty and len(df) >= 250:
            cache[symbol] = df
        time.sleep(exchange.rateLimit / 1000)
    return cache


def fetch_all_funding(symbols: list[str], days: int) -> dict:
    """Fetch historical funding rates once per symbol. Symbols with no
    available history are simply absent from the dict — the simulator then
    falls back to cfg.FUNDING_RATE_FALLBACK rather than assuming zero."""
    if not (cfg.MODEL_FUNDING_COSTS and cfg.USE_REAL_FUNDING_HISTORY):
        return {}

    exchange = dfetch.build_exchange()
    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=days)
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)

    funding = {}
    n_real, n_fallback = 0, 0
    for symbol in symbols:
        try:
            fdf = dfetch.fetch_funding_history(exchange, symbol, start_ms, end_ms)
        except Exception:
            fdf = None
        if fdf is not None and len(fdf) > 0:
            funding[symbol] = fdf
            n_real += 1
        else:
            n_fallback += 1
    print(f"Funding history: {n_real} symbols with real data, "
          f"{n_fallback} using fallback rate ({cfg.FUNDING_RATE_FALLBACK:.4%}/interval)")
    return funding


def run_one_combo(data_cache: dict, params: dict, funding_cache: dict = None) -> dict:
    """Apply a parameter combo to config (in-memory, this-process-only),
    run the full simulation across all cached symbols, return metrics.

    Each symbol gets its OWN independent equity pool and circuit breaker for
    this combo — a bad early symbol (in whatever order the cache dict
    iterates) can't silently zero out the test for every symbol after it.
    Aggregate stats are computed as if running one independent bot per
    symbol, each with the same starting capital.
    """
    cfg.ATR_STOP_MULT = params["ATR_STOP_MULT"]
    cfg.TAKE_PROFIT_R_MULT_PARTIAL = params["TAKE_PROFIT_R_MULT_PARTIAL"]
    cfg.TRAIL_ATR_MULT = params["TRAIL_ATR_MULT"]
    cfg.RSI_LONG_MIN, cfg.RSI_LONG_MAX = params["RSI_BAND"]
    adx_min = params.get("ADX_MIN", 0)
    cfg.USE_ADX_FILTER = adx_min > 0
    cfg.ADX_MIN_THRESHOLD = adx_min

    funding_cache = funding_cache or {}
    trade_log = []
    for symbol, df in data_cache.items():
        equity_tracker = {"equity": cfg.BACKTEST_STARTING_EQUITY, "open_count": 0}
        breaker = CircuitBreaker(cfg.BACKTEST_STARTING_EQUITY)
        simulate_symbol(df, symbol, equity_tracker, breaker, trade_log,
                        funding_df=funding_cache.get(symbol))

    log_df = pd.DataFrame(trade_log)
    combined_starting_capital = cfg.BACKTEST_STARTING_EQUITY * len(data_cache)
    metrics = trade_metrics.compute_metrics(log_df, combined_starting_capital)
    metrics.update(params)
    metrics["rsi_band"] = f"{params['RSI_BAND'][0]}-{params['RSI_BAND'][1]}"
    return metrics


def run_sweep(symbols: list[str], days: int, grid: dict, timeframe: str = None):
    timeframe = timeframe or cfg.TIMEFRAME
    data_cache = fetch_all_data(symbols, days, timeframe)
    if not data_cache:
        print("No usable data fetched — aborting sweep.")
        return pd.DataFrame()

    funding_cache = fetch_all_funding(list(data_cache.keys()), days)

    print(f"\nData cached for {len(data_cache)} symbols ({timeframe}). "
          f"Funding modeled: {cfg.MODEL_FUNDING_COSTS}. Running parameter sweep...\n")

    keys = [k for k in ["ATR_STOP_MULT", "TAKE_PROFIT_R_MULT_PARTIAL", "TRAIL_ATR_MULT",
                        "RSI_BAND", "ADX_MIN"] if k in grid]
    combos = list(itertools.product(*[grid[k] for k in keys]))
    print(f"Testing {len(combos)} parameter combinations across {len(data_cache)} pairs "
          f"(no re-fetching — using cached data)...")

    results = []
    for i, combo in enumerate(combos, 1):
        params = dict(zip(keys, combo))
        metrics = run_one_combo(data_cache, params, funding_cache)
        metrics["timeframe"] = timeframe
        results.append(metrics)
        if i % 10 == 0 or i == len(combos):
            print(f"  {i}/{len(combos)} combinations tested...")

    return pd.DataFrame(results)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Parameter sweep for the momentum strategy")
    parser.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS)
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--quick", action="store_true", help="smaller/faster grid")
    parser.add_argument("--timeframe", default=None,
                         help="candle timeframe, e.g. 1h or 4h (default: config.TIMEFRAME)")
    parser.add_argument("--strategy", default=None, choices=["momentum", "macd"],
                         help="entry signal to test (default: config.STRATEGY_MODE)")
    args = parser.parse_args()

    if args.strategy:
        cfg.STRATEGY_MODE = args.strategy
    print(f"Strategy mode: {cfg.STRATEGY_MODE}")

    grid = QUICK_GRID if args.quick else FULL_GRID
    results_df = run_sweep(args.symbols, args.days, grid, args.timeframe)

    if results_df.empty:
        raise SystemExit(1)

    # Rank by expectancy per trade, but require a minimum sample size so a
    # combo with 3 lucky trades doesn't rank above one with real evidence.
    MIN_TRADES = 15
    ranked = results_df[results_df["num_trades"] >= MIN_TRADES].sort_values(
        "expectancy", ascending=False)

    pd.set_option("display.width", 140)
    print("\n" + "=" * 70)
    print(f"TOP 10 PARAMETER COMBINATIONS (min {MIN_TRADES} trades, ranked by expectancy/trade)")
    print("=" * 70)
    cols = [c for c in ["ATR_STOP_MULT", "TAKE_PROFIT_R_MULT_PARTIAL", "TRAIL_ATR_MULT",
                        "rsi_band", "ADX_MIN", "num_trades", "win_rate_pct", "expectancy",
                        "profit_factor", "total_return_pct"] if c in ranked.columns]
    print(ranked[cols].head(10).to_string(index=False))

    if ranked.empty:
        print(f"\nNo combination reached {MIN_TRADES}+ trades — try a longer --days window "
              f"or more symbols for a meaningful sample.")

    os.makedirs("backtest_results", exist_ok=True)
    out_path = f"backtest_results/param_sweep_{int(time.time())}.csv"
    results_df.to_csv(out_path, index=False)
    print(f"\nFull sweep results (all {len(results_df)} combos) saved to {out_path}")
