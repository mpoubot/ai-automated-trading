"""
Walk-forward validation — the honest test for overfitting.

How it works:
  1. The full history is split into rolling windows: [train N days][test M days],
     stepped forward by M days each fold.
  2. In each fold, every parameter combination in the grid is evaluated on the
     TRAIN window only, and the best (by expectancy, with a minimum trade count)
     is selected.
  3. That winning combination is then applied to the TEST window — data it was
     never tuned on. The test-window results are recorded.
  4. After all folds, the aggregated OUT-OF-SAMPLE results are reported, along
     with which parameters each fold chose (stable choices across folds = a
     robust parameter region; wildly different choices each fold = the "edge"
     is likely noise).

Interpretation guide (printed at the end too):
  - Aggregate out-of-sample expectancy > 0 across folds  -> genuinely promising
  - In-sample great, out-of-sample flat/negative         -> overfitting
  - Different parameters chosen every fold               -> unstable, distrust it

Usage:
    python walk_forward.py --days 720 --train-days 180 --test-days 60
    python walk_forward.py --days 540 --train-days 180 --test-days 60 --quick

Uses the same data fetch, grid, simulation engine, and per-symbol-independent
capital model as param_sweep.py — no separate logic to drift out of sync.
"""
import argparse
import itertools
import os
import time

import pandas as pd

import config as cfg
from core import trade_metrics
from core.risk_manager import CircuitBreaker
from backtester import simulate_symbol
from param_sweep import fetch_all_data, fetch_all_funding, FULL_GRID, QUICK_GRID
from run_broad_backtest import DEFAULT_SYMBOLS, WIDE_SYMBOLS

GRID_KEYS = ["ATR_STOP_MULT", "TAKE_PROFIT_R_MULT_PARTIAL", "TRAIL_ATR_MULT",
             "RSI_BAND", "ADX_MIN"]
MIN_TRAIN_TRADES = 15   # a combo must produce at least this many train trades to be selectable


def apply_params(params: dict):
    cfg.ATR_STOP_MULT = params["ATR_STOP_MULT"]
    cfg.TAKE_PROFIT_R_MULT_PARTIAL = params["TAKE_PROFIT_R_MULT_PARTIAL"]
    cfg.TRAIL_ATR_MULT = params["TRAIL_ATR_MULT"]
    cfg.RSI_LONG_MIN, cfg.RSI_LONG_MAX = params["RSI_BAND"]
    adx_min = params.get("ADX_MIN", 0)
    cfg.USE_ADX_FILTER = adx_min > 0
    cfg.ADX_MIN_THRESHOLD = adx_min


def run_segment(data_cache: dict, start_ts, end_ts, funding_cache: dict = None) -> pd.DataFrame:
    """Run the simulation on a time slice of the cached data (per-symbol
    independent capital, same as the fixed param_sweep/broad_backtest)."""
    funding_cache = funding_cache or {}
    trade_log = []
    for symbol, df in data_cache.items():
        seg = df[(df["timestamp"] >= start_ts) & (df["timestamp"] < end_ts)]
        # need enough candles for the EMA200 warmup plus some trading room
        if len(seg) < cfg.EMA_SLOW + 60:
            continue
        seg = seg.reset_index(drop=True)
        equity_tracker = {"equity": cfg.BACKTEST_STARTING_EQUITY, "open_count": 0}
        breaker = CircuitBreaker(cfg.BACKTEST_STARTING_EQUITY)
        simulate_symbol(seg, symbol, equity_tracker, breaker, trade_log,
                        funding_df=funding_cache.get(symbol))
    return pd.DataFrame(trade_log)


def evaluate_combo_on_segment(data_cache, params, start_ts, end_ts, n_symbols,
                               funding_cache=None) -> dict:
    apply_params(params)
    log_df = run_segment(data_cache, start_ts, end_ts, funding_cache)
    metrics = trade_metrics.compute_metrics(log_df, cfg.BACKTEST_STARTING_EQUITY * max(n_symbols, 1))
    return metrics


def walk_forward(symbols, days, train_days, test_days, grid, timeframe=None):
    timeframe = timeframe or cfg.TIMEFRAME
    data_cache = fetch_all_data(symbols, days, timeframe)
    if not data_cache:
        print("No usable data fetched — aborting.")
        return None

    funding_cache = fetch_all_funding(list(data_cache.keys()), days)
    print(f"Timeframe: {timeframe} | Funding modeled: {cfg.MODEL_FUNDING_COSTS}")

    # establish overall time range from the cached data
    all_start = min(df["timestamp"].min() for df in data_cache.values())
    all_end = max(df["timestamp"].max() for df in data_cache.values())
    print(f"\nData range: {all_start} -> {all_end}  ({len(data_cache)} symbols)")

    train_delta = pd.Timedelta(days=train_days)
    test_delta = pd.Timedelta(days=test_days)

    combos = [dict(zip(GRID_KEYS, c)) for c in itertools.product(*[grid[k] for k in GRID_KEYS])]
    print(f"Grid size: {len(combos)} combinations per fold")

    folds = []
    fold_start = all_start
    while fold_start + train_delta + test_delta <= all_end:
        folds.append((fold_start, fold_start + train_delta, fold_start + train_delta + test_delta))
        fold_start += test_delta

    if not folds:
        print("Not enough data for even one train+test fold — reduce --train-days/--test-days "
              "or increase --days.")
        return None

    print(f"Folds: {len(folds)}  (train {train_days}d -> test {test_days}d, stepping {test_days}d)\n")

    fold_records = []
    oos_logs = []

    for i, (t0, t1, t2) in enumerate(folds, 1):
        print(f"Fold {i}/{len(folds)}: train {t0.date()}..{t1.date()}  test {t1.date()}..{t2.date()}")

        # --- tune on train window ---
        best = None
        for params in combos:
            m = evaluate_combo_on_segment(data_cache, params, t0, t1, len(data_cache),
                                           funding_cache)
            if m["num_trades"] < MIN_TRAIN_TRADES:
                continue
            if best is None or m["expectancy"] > best[1]["expectancy"]:
                best = (params, m)

        if best is None:
            print("  no combo reached the minimum train trade count — skipping fold")
            continue

        best_params, train_m = best

        # --- validate on unseen test window ---
        apply_params(best_params)
        test_log = run_segment(data_cache, t1, t2, funding_cache)
        test_m = trade_metrics.compute_metrics(
            test_log, cfg.BACKTEST_STARTING_EQUITY * len(data_cache))
        if not test_log.empty:
            test_log = test_log.copy()
            test_log["fold"] = i
            oos_logs.append(test_log)

        print(f"  chosen: stop={best_params['ATR_STOP_MULT']} tp={best_params['TAKE_PROFIT_R_MULT_PARTIAL']} "
              f"trail={best_params['TRAIL_ATR_MULT']} rsi={best_params['RSI_BAND']} "
              f"adx_min={best_params.get('ADX_MIN', 0)}")
        print(f"  train:  {train_m['num_trades']} trades, expectancy ${train_m['expectancy']}, "
              f"PF {train_m['profit_factor']}")
        print(f"  TEST:   {test_m['num_trades']} trades, expectancy ${test_m['expectancy']}, "
              f"PF {test_m['profit_factor']}, return {test_m['total_return_pct']}%")

        fold_records.append({
            "fold": i,
            "train_start": t0, "test_start": t1, "test_end": t2,
            "stop": best_params["ATR_STOP_MULT"],
            "tp": best_params["TAKE_PROFIT_R_MULT_PARTIAL"],
            "trail": best_params["TRAIL_ATR_MULT"],
            "rsi_band": f"{best_params['RSI_BAND'][0]}-{best_params['RSI_BAND'][1]}",
            "adx_min": best_params.get("ADX_MIN", 0),
            "train_expectancy": train_m["expectancy"],
            "train_pf": train_m["profit_factor"],
            "test_trades": test_m["num_trades"],
            "test_expectancy": test_m["expectancy"],
            "test_pf": test_m["profit_factor"],
            "test_return_pct": test_m["total_return_pct"],
        })

    if not fold_records:
        print("No valid folds completed.")
        return None

    folds_df = pd.DataFrame(fold_records)

    # --- aggregate out-of-sample view ---
    print("\n" + "=" * 70)
    print("WALK-FORWARD SUMMARY (what matters is the TEST columns)")
    print("=" * 70)
    show_cols = ["fold", "stop", "tp", "trail", "rsi_band", "adx_min",
                 "train_expectancy", "test_trades", "test_expectancy", "test_pf", "test_return_pct"]
    print(folds_df[show_cols].to_string(index=False))

    if oos_logs:
        combined_oos = pd.concat(oos_logs, ignore_index=True)
        oos_m = trade_metrics.compute_metrics(
            combined_oos, cfg.BACKTEST_STARTING_EQUITY * len(data_cache))
        print()
        trade_metrics.print_metrics_report(
            oos_m, cfg.BACKTEST_STARTING_EQUITY * len(data_cache),
            title="AGGREGATE OUT-OF-SAMPLE PERFORMANCE (all test windows combined)")

    positive_folds = (folds_df["test_expectancy"] > 0).sum()
    unique_param_sets = folds_df[["stop", "tp", "trail", "rsi_band", "adx_min"]].drop_duplicates()
    print(f"\nFolds with positive out-of-sample expectancy: {positive_folds}/{len(folds_df)}")
    print(f"Distinct parameter sets chosen across folds:  {len(unique_param_sets)}/{len(folds_df)}")

    # Correlation between train and test expectancy is the single clearest
    # overfitting diagnostic: near zero means tuning on the past tells you
    # nothing about the future, no matter how good the train numbers look.
    corr = folds_df["train_expectancy"].corr(folds_df["test_expectancy"])
    print(f"Train->test expectancy correlation:           {corr:.3f}  "
          f"(near 0 = tuning has no predictive power)")

    # --- Pre-committed pass/fail bar (agreed BEFORE seeing results) ---
    if oos_logs:
        pf = oos_m["profit_factor"]
        passed = (pf > 1.10) and (positive_folds > len(folds_df) / 2)
        print("\n" + "-" * 70)
        print(f"PRE-COMMITTED BAR: out-of-sample profit factor > 1.10 AND "
              f">50% of folds positive")
        print(f"ACTUAL:            profit factor {pf}, {positive_folds}/{len(folds_df)} folds positive")
        print(f"VERDICT:           {'PASS' if passed else 'FAIL'}")
        if not passed:
            print("\nThis bar was set before running to avoid rationalizing a marginal\n"
                  "result after the fact. A FAIL means this configuration does not\n"
                  "have a demonstrated edge — not that it needs one more adjustment.")
        print("-" * 70)
    print("""
How to read this:
  - The train column will almost always look good — that's the tuning, not evidence.
  - The TEST columns are the honest measure: performance on data the parameters never saw.
  - Positive aggregate out-of-sample expectancy across most folds -> promising.
  - Great train + poor test -> overfitting: the strategy memorizes the past, not a real edge.
  - Same/similar parameters chosen fold after fold -> robust region. Different every fold -> noise.""")

    return folds_df


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Walk-forward validation")
    parser.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS)
    parser.add_argument("--days", type=int, default=720,
                         help="total history to fetch (train+test folds carved from this)")
    parser.add_argument("--train-days", type=int, default=180)
    parser.add_argument("--test-days", type=int, default=60)
    parser.add_argument("--quick", action="store_true", help="smaller/faster grid per fold")
    parser.add_argument("--timeframe", default=None,
                         help="candle timeframe, e.g. 1h or 4h (default: config.TIMEFRAME)")
    parser.add_argument("--strategy", default=None, choices=["momentum", "macd"],
                         help="entry signal to validate (default: config.STRATEGY_MODE)")
    parser.add_argument("--wide", action="store_true",
                         help="use the FULL pair universe (18 pairs) instead of the "
                              "momentum-filtered default (11) — use when testing a new strategy")
    args = parser.parse_args()

    if args.wide and args.symbols == DEFAULT_SYMBOLS:
        args.symbols = WIDE_SYMBOLS

    if args.strategy:
        cfg.STRATEGY_MODE = args.strategy
    print(f"Strategy mode: {cfg.STRATEGY_MODE}")

    grid = QUICK_GRID if args.quick else FULL_GRID
    folds_df = walk_forward(args.symbols, args.days, args.train_days, args.test_days,
                             grid, args.timeframe)

    if folds_df is not None:
        os.makedirs("backtest_results", exist_ok=True)
        out_path = f"backtest_results/walk_forward_{int(time.time())}.csv"
        folds_df.to_csv(out_path, index=False)
        print(f"\nFold-by-fold results saved to {out_path}")
