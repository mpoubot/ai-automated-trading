"""
Runs the strategy across a broader, curated set of MEXC USDT-M perpetual
pairs (majors + higher-volatility alts, closer to what the live scanner
actually trades) over a longer default window than a quick 2-symbol test.

Usage:
    python run_broad_backtest.py                      # default pair set, 180 days
    python run_broad_backtest.py --days 365            # longer window
    python run_broad_backtest.py --symbols "SOL/USDT:USDT" "DOGE/USDT:USDT"

This reuses the exact same simulate_symbol() engine as backtester.py — same
signal logic, same risk management, same fees/slippage — just run across
more pairs and reported with true per-trade metrics (see core/trade_metrics.py).
"""
import argparse
import os
import time

import pandas as pd

import config as cfg
from core import data_fetcher as dfetch
from core import trade_metrics
from backtester import simulate_symbol
from core.risk_manager import CircuitBreaker
from datetime import datetime, timedelta, timezone

# A mix of majors (lower volatility, more efficient) and mid/higher-volatility
# alts (closer to what the wide live scan actually tends to catch, like the
# COTI signal you saw live).
#
# NOTE: SOL, NEAR, LINK, INJ, APT, ARB, and OP were removed from this default
# list after three independent real backtests (21-day, ~82-day, ~365-day
# windows) showed ALL SEVEN losing money in EVERY run — not just a bad
# window, a consistent pattern. No other pair was consistently profitable
# across all three runs either, which is worth remembering: this list is
# "not proven to lose" more than it's "proven to win." Re-add symbols here
# freely to re-test them as the strategy evolves.
DEFAULT_SYMBOLS = [
    "BTC/USDT:USDT", "ETH/USDT:USDT", "BNB/USDT:USDT",
    "XRP/USDT:USDT", "ADA/USDT:USDT", "DOGE/USDT:USDT", "AVAX/USDT:USDT",
    "DOT/USDT:USDT", "SUI/USDT:USDT",
    "TIA/USDT:USDT", "COTI/USDT:USDT",
]

# Pairs excluded from DEFAULT_SYMBOLS because they lost money in all three
# independent MOMENTUM-strategy backtests. That finding is strategy-specific —
# it says those pairs didn't suit breakout entries, not that they're untradeable.
# When testing a DIFFERENT entry signal (e.g. --strategy macd), use
# WIDE_SYMBOLS below so the new signal is judged on the full universe rather
# than one filtered by another strategy's failures.
EXCLUDED_SYMBOLS_CONSISTENTLY_NEGATIVE = [
    "SOL/USDT:USDT", "NEAR/USDT:USDT", "LINK/USDT:USDT", "INJ/USDT:USDT",
    "APT/USDT:USDT", "ARB/USDT:USDT", "OP/USDT:USDT",
]

# Full universe: the default list plus the momentum-excluded pairs. Use this
# when evaluating a new strategy from scratch.
WIDE_SYMBOLS = DEFAULT_SYMBOLS + EXCLUDED_SYMBOLS_CONSISTENTLY_NEGATIVE


def run_broad_backtest(symbols: list[str], days: int, verbose_per_symbol: bool = True):
    """
    Each symbol is tested with its OWN independent starting capital and its
    OWN circuit breaker — not a shared pool across all pairs. This matters:
    with a shared pool, poor early results on one or two pairs (e.g. BTC/ETH
    breaching the max-drawdown circuit breaker) would permanently halt
    trading for every pair tested after them, silently zeroing out the rest
    of the test. Independent capital per pair means every pair actually gets
    a fair, fully-evaluated run regardless of how earlier pairs performed.
    """
    exchange = dfetch.build_exchange()
    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=days)
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)

    all_trade_logs = []
    per_symbol_results = {}
    halted_symbols = []

    for symbol in symbols:
        print(f"Fetching {symbol} ({cfg.TIMEFRAME}, {days}d)...")
        try:
            df = dfetch.fetch_ohlcv_range(exchange, symbol, cfg.TIMEFRAME, start_ms, end_ms)
        except Exception as e:
            print(f"  skipped — fetch error: {e}")
            continue
        if df.empty or len(df) < 250:
            print(f"  skipped — insufficient data ({len(df)} candles)")
            continue

        # Fresh equity pool and fresh circuit breaker for THIS symbol only.
        equity_tracker = {"equity": cfg.BACKTEST_STARTING_EQUITY, "open_count": 0}
        breaker = CircuitBreaker(cfg.BACKTEST_STARTING_EQUITY)
        symbol_trade_log = []

        simulate_symbol(df, symbol, equity_tracker, breaker, symbol_trade_log)

        if breaker.halted:
            halted_symbols.append((symbol, breaker.halt_reason))
            print(f"  ⚠ circuit breaker halted on {symbol}: {breaker.halt_reason}")

        all_trade_logs.extend(symbol_trade_log)
        symbol_events = pd.DataFrame(symbol_trade_log)
        if not symbol_events.empty:
            sym_metrics = trade_metrics.compute_metrics(symbol_events, cfg.BACKTEST_STARTING_EQUITY)
            per_symbol_results[symbol] = sym_metrics

        time.sleep(exchange.rateLimit / 1000)

    log_df = pd.DataFrame(all_trade_logs)

    # Aggregate metrics treat this as "if you'd run one independent bot per
    # pair, each with its own $X capital" — so the combined starting capital
    # for return-% purposes is (num pairs tested) * BACKTEST_STARTING_EQUITY.
    num_tested = len(per_symbol_results)
    combined_starting_capital = cfg.BACKTEST_STARTING_EQUITY * max(num_tested, 1)

    print("\n" + "#" * 55)
    print(f"AGGREGATE RESULTS — {len(symbols)} pairs requested, {num_tested} tested, "
          f"{days} days, {cfg.TIMEFRAME} timeframe")
    print("#" * 55)
    if halted_symbols:
        print(f"\n⚠ {len(halted_symbols)} pair(s) hit their own max-drawdown circuit breaker "
              f"during this test (each pair's breaker is independent, so this only affected "
              f"that one pair's results, not the others):")
        for sym, reason in halted_symbols:
            print(f"   - {sym}: {reason}")

    overall_metrics = trade_metrics.compute_metrics(log_df, combined_starting_capital)
    trade_metrics.print_metrics_report(
        overall_metrics, combined_starting_capital,
        title="AGGREGATE (as if running one independent bot per pair)")

    if verbose_per_symbol and per_symbol_results:
        print("\nPer-symbol breakdown (each with its own independent starting capital):")
        print(f"{'Symbol':<18}{'Trades':>8}{'WinRate%':>10}{'Expectancy':>12}"
              f"{'TotalPnL':>10}{'Return%':>10}")
        for sym, m in sorted(per_symbol_results.items(), key=lambda kv: kv[1]["total_pnl"], reverse=True):
            print(f"{sym:<18}{m['num_trades']:>8}{m['win_rate_pct']:>10}{m['expectancy']:>12}"
                  f"{m['total_pnl']:>10}{m['total_return_pct']:>10}")

    return log_df, overall_metrics, per_symbol_results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Broad multi-pair backtest")
    parser.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS,
                         help="pairs to test (default: curated major+alt mix)")
    parser.add_argument("--days", type=int, default=180, help="lookback window in days")
    args = parser.parse_args()

    log_df, overall_metrics, per_symbol = run_broad_backtest(args.symbols, args.days)

    os.makedirs("backtest_results", exist_ok=True)
    out_path = f"backtest_results/broad_backtest_{int(time.time())}.csv"
    log_df.to_csv(out_path, index=False)
    print(f"\nFull trade log saved to {out_path}")
