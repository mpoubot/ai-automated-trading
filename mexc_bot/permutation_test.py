"""
Permutation test — does the entry signal contain information at all?

Every test so far has compared the strategy against ZERO (is it profitable?).
This compares it against a DISTRIBUTION of randomized versions of itself,
which answers the sharper question: is the observed result distinguishable
from noise?

Three controls forming a 2x2 decomposition of the entry decision:

                    | real direction      | random direction
    ----------------|---------------------|--------------------
    real timing     | BASELINE            | CONTROL B
    random timing   | CONTROL C           | CONTROL A

  CONTROL A  random timing + random direction — tests the COMPLETE entry
             decision against a matched random process.
  CONTROL B  strategy timing + random direction — isolates whether DIRECTION
             selection carries information, given the strategy's timing.
  CONTROL C  random timing + strategy's directional bias — isolates whether
             TIMING carries information, given the strategy's direction rule.

Everything downstream of the entry decision is identical in all four:
position sizing, ATR stops, partial take-profit, trailing, time stop, fees,
slippage, funding, universe, circuit breakers. So any difference is
attributable to the entry component being randomized.

Matching is HIERARCHICAL (symbol -> window -> actual entry count), so a
high-signal pair like BTC can't swamp a low-signal small cap through a
global random process. Random-timing controls draw entries SEQUENTIALLY,
reserving room for trades still owed, which GUARANTEES the realized trade
count matches the baseline rather than merely approximating it — exposure
and cumulative costs stay matched.

Usage:
    python permutation_test.py --days 720 --runs 1000
    python permutation_test.py --days 720 --runs 200 --timeframe 4h
    python permutation_test.py --days 720 --runs 1000 --windows 8   # split into 8 windows

IMPORTANT — this is a DIAGNOSTIC, not an optimizer. Do not tune parameters
after seeing these results; that converts "is this distinguishable from
noise?" into "can I find a config that beats my controls?", which is just
multiple-testing with extra steps.
"""
import argparse
import os
import time

import numpy as np
import pandas as pd

import config as cfg
from core import strategy as strat
from core import trade_metrics
from core.risk_manager import CircuitBreaker
from backtester import simulate_symbol
from param_sweep import fetch_all_data, fetch_all_funding
from run_broad_backtest import DEFAULT_SYMBOLS, WIDE_SYMBOLS

WARMUP = None  # computed per-symbol from config


def _warmup_bars() -> int:
    return max(cfg.EMA_SLOW, cfg.BREAKOUT_LOOKBACK, cfg.MACD_SLOW) + 5


def build_windows(data_cache: dict, n_windows: int) -> list:
    """Split the full period into n contiguous windows. Random controls are
    matched to the real signal count WITHIN each window, so the controls can
    never borrow signal density from a different market period."""
    all_start = min(df["timestamp"].min() for df in data_cache.values())
    all_end = max(df["timestamp"].max() for df in data_cache.values())
    edges = pd.date_range(all_start, all_end, periods=n_windows + 1)
    return [(edges[i], edges[i + 1]) for i in range(n_windows)]


def run_real(data_cache, funding_cache, windows):
    """Baseline run. Returns (trade_log_df, schedules) where
    schedules[(symbol, window_idx)] = sorted list of the bar indices at which
    the real strategy actually opened trades. Controls reuse the LENGTH of
    that list (matched count) and, for Control B, the exact indices."""
    trade_log = []
    schedules = {}
    for symbol, df in data_cache.items():
        for w, (t0, t1) in enumerate(windows):
            seg = df[(df["timestamp"] >= t0) & (df["timestamp"] < t1)].reset_index(drop=True)
            if len(seg) < _warmup_bars() + 30:
                continue
            before = len(trade_log)
            eq = {"equity": cfg.BACKTEST_STARTING_EQUITY, "open_count": 0}
            br = CircuitBreaker(cfg.BACKTEST_STARTING_EQUITY)
            simulate_symbol(seg, symbol, eq, br, trade_log,
                            funding_df=funding_cache.get(symbol))
            ts_to_idx = {t: k for k, t in enumerate(seg["timestamp"])}
            idxs = [ts_to_idx[e["time"]] for e in trade_log[before:]
                    if e["type"] == "entry" and e["time"] in ts_to_idx]
            schedules[(symbol, w)] = sorted(idxs)
    return pd.DataFrame(trade_log), schedules


def stratified_schedule(n_entries: int, lo: int, hi: int, rng) -> list:
    """
    Draw `n_entries` bar indices spread across [lo, hi) by splitting the range
    into equal strata and drawing one index per stratum.

    Why stratified rather than uniform: drawing uniformly from the whole
    remaining range consumes roughly half the available space per draw, so
    later entries run out of room and the realized count falls short of the
    target. Stratification keeps entries spread across the window while
    preserving randomness within each stratum.
    """
    if n_entries <= 0 or hi <= lo:
        return []
    width = (hi - lo) / n_entries
    out = []
    for k in range(n_entries):
        a = lo + k * width
        b = lo + (k + 1) * width
        lo_k, hi_k = int(np.floor(a)), max(int(np.floor(b)), int(np.floor(a)) + 1)
        hi_k = min(hi_k, hi)
        if lo_k >= hi_k:
            out.append(min(lo_k, hi - 1))
        else:
            out.append(int(rng.integers(lo_k, hi_k)))
    return sorted(out)


def run_control(data_cache, funding_cache, windows, schedules, mode, rng):
    """One randomized run. mode in {random_entry, random_timing, random_direction}."""
    trade_log = []
    warm = _warmup_bars()
    for symbol, df in data_cache.items():
        for w, (t0, t1) in enumerate(windows):
            base_sched = schedules.get((symbol, w), [])
            if not base_sched:
                continue
            seg = df[(df["timestamp"] >= t0) & (df["timestamp"] < t1)].reset_index(drop=True)
            if len(seg) < warm + 30:
                continue

            if mode == "random_direction":
                # Control B keeps the baseline's exact entry bars.
                sched = base_sched
            else:
                # Controls A and C randomize WHEN, matching only the count.
                # Reserve tail room so the final scheduled entries still have
                # bars available to open in; without this the last few entries
                # fall off the end of the window and the count undershoots.
                hi = max(warm + len(base_sched), len(seg) - cfg.TIME_STOP_CANDLES)
                sched = stratified_schedule(len(base_sched), warm, min(hi, len(seg)), rng)

            entry_mode = {"mode": mode, "schedule": sched, "rng": rng}
            eq = {"equity": cfg.BACKTEST_STARTING_EQUITY, "open_count": 0}
            br = CircuitBreaker(cfg.BACKTEST_STARTING_EQUITY)
            simulate_symbol(seg, symbol, eq, br, trade_log,
                            funding_df=funding_cache.get(symbol), entry_mode=entry_mode)
    return pd.DataFrame(trade_log)


def summarize(log_df, n_symbols):
    return trade_metrics.compute_metrics(
        log_df, cfg.BACKTEST_STARTING_EQUITY * max(n_symbols, 1))


def percentile_of(value, distribution):
    """Fraction of the random distribution at or below `value`."""
    arr = np.asarray(distribution, dtype=float)
    arr = arr[np.isfinite(arr)]
    if len(arr) == 0:
        return float("nan")
    return float((arr <= value).mean() * 100)


def report_comparison(name, real_m, dist_df, metrics=("profit_factor", "expectancy",
                                                       "total_pnl", "max_drawdown_pct",
                                                       "avg_mae_r", "avg_mfe_r")):
    print("\n" + "=" * 78)
    print(f"{name}  —  real strategy vs {len(dist_df)} randomized runs")
    print("=" * 78)
    print(f"{'metric':<20}{'real':>10}{'rand mean':>12}{'rand p5':>10}"
          f"{'rand p95':>10}{'pctile':>9}{'p-value':>10}")
    for m in metrics:
        if m not in dist_df.columns:
            continue
        col = pd.to_numeric(dist_df[m], errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
        if col.empty:
            continue
        real_v = real_m.get(m, float("nan"))
        pct = percentile_of(real_v, col)
        # one-sided empirical p: P(random >= real)
        pval = float((col >= real_v).mean())
        print(f"{m:<20}{real_v:>10.3f}{col.mean():>12.3f}{col.quantile(0.05):>10.3f}"
              f"{col.quantile(0.95):>10.3f}{pct:>8.1f}%{pval:>10.4f}")

    pf_col = pd.to_numeric(dist_df["profit_factor"], errors="coerce")
    pf_col = pf_col.replace([np.inf, -np.inf], np.nan).dropna()
    p_pf = float((pf_col >= real_m["profit_factor"]).mean()) if len(pf_col) else float("nan")

    # Count matching is the premise of the whole comparison — show it rather
    # than assume it. Large deviation means exposure/costs are NOT matched and
    # the inference is correspondingly weaker.
    if "num_trades" in dist_df.columns:
        nt = pd.to_numeric(dist_df["num_trades"], errors="coerce").dropna()
        base_n = real_m.get("num_trades", 0)
        if len(nt) and base_n:
            dev = (nt - base_n).abs().mean() / base_n * 100
            print(f"\nCount matching: baseline {base_n} trades | random mean "
                  f"{nt.mean():.1f} (range {int(nt.min())}-{int(nt.max())}) | "
                  f"mean deviation {dev:.1f}%")
            if dev > 5:
                print("  ⚠ deviation >5% — exposure is not well matched; treat the "
                      "comparison as indicative rather than clean.")

    print(f"\nP(random profit factor >= real): {p_pf:.4f}")
    if p_pf > 0.10:
        print("-> The real result sits inside the random distribution. No evidence")
        print("   that this component of the signal carries information.")
    elif p_pf > 0.05:
        print("-> Marginal. Weak/ambiguous evidence at best; would not act on this.")
    else:
        print("-> The real result sits in the tail. Evidence the signal carries")
        print("   information — note this is separate from being PROFITABLE.")


def main():
    parser = argparse.ArgumentParser(description="Permutation test for entry-signal information")
    parser.add_argument("--symbols", nargs="+", default=None)
    parser.add_argument("--days", type=int, default=720)
    parser.add_argument("--runs", type=int, default=200,
                         help="randomized runs per control (200 fast / 1000 thorough)")
    parser.add_argument("--windows", type=int, default=8,
                         help="contiguous windows used for hierarchical signal matching")
    parser.add_argument("--timeframe", default=None)
    parser.add_argument("--strategy", default=None, choices=["momentum", "macd"])
    parser.add_argument("--wide", action="store_true", help="use the full 18-pair universe")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--no-adx", action="store_true",
                         help="disable the ADX filter for this run (config default is ON) — "
                              "use to compare against the original pre-ADX configuration")
    parser.add_argument("--no-funding", action="store_true",
                         help="disable funding cost modeling for this run")
    args = parser.parse_args()

    if args.no_adx:
        cfg.USE_ADX_FILTER = False
    if args.no_funding:
        cfg.MODEL_FUNDING_COSTS = False
    if args.strategy:
        cfg.STRATEGY_MODE = args.strategy
    symbols = args.symbols or (WIDE_SYMBOLS if args.wide else DEFAULT_SYMBOLS)
    timeframe = args.timeframe or cfg.TIMEFRAME

    # Echo the exact configuration under test — the result only means
    # something if you know which version of the strategy produced it.
    print(f"Strategy: {cfg.STRATEGY_MODE} | timeframe: {timeframe} | pairs: {len(symbols)}")
    print(f"ADX filter: {cfg.USE_ADX_FILTER}"
          + (f" (min {cfg.ADX_MIN_THRESHOLD})" if cfg.USE_ADX_FILTER else "")
          + f" | funding modeled: {cfg.MODEL_FUNDING_COSTS}")
    print(f"Risk/trade: {cfg.RISK_PER_TRADE_PCT:.1%} | stop {cfg.ATR_STOP_MULT}xATR | "
          f"TP {cfg.TAKE_PROFIT_R_MULT_PARTIAL}R | trail {cfg.TRAIL_ATR_MULT}xATR")
    print(f"Runs per control: {args.runs}")

    data_cache = fetch_all_data(symbols, args.days, timeframe)
    if not data_cache:
        raise SystemExit("No usable data fetched.")
    funding_cache = fetch_all_funding(list(data_cache.keys()), args.days)

    # Pre-compute indicators ONCE — simulate_symbol reuses them, which is what
    # makes thousands of permutation runs feasible rather than glacial.
    print("Pre-computing indicators...")
    data_cache = {s: strat.add_indicators(df) for s, df in data_cache.items()}

    windows = build_windows(data_cache, args.windows)
    print(f"Windows: {len(windows)}  ({windows[0][0].date()} .. {windows[-1][1].date()})")

    print("\nRunning BASELINE (real signal)...")
    real_log, schedules = run_real(data_cache, funding_cache, windows)
    real_m = summarize(real_log, len(data_cache))
    total_signals = sum(len(v) for v in schedules.values())
    print(f"Baseline: {real_m['num_trades']} trades from {total_signals} entries")
    trade_metrics.print_metrics_report(
        real_m, cfg.BACKTEST_STARTING_EQUITY * len(data_cache),
        title="BASELINE — REAL STRATEGY")
    print(f"Avg MAE: {real_m['avg_mae_r']}R | Avg MFE: {real_m['avg_mfe_r']}R | "
          f"Max DD: {real_m['max_drawdown_pct']}% | Return/MaxDD: {real_m['return_over_maxdd']}")

    rng = np.random.default_rng(args.seed)
    results = {}

    controls = [
        ("random_entry",
         "CONTROL A — random timing + random direction (the full entry decision)"),
        ("random_direction",
         "CONTROL B — strategy timing + random direction (isolates DIRECTION)"),
        ("random_timing",
         "CONTROL C — random timing + strategy direction (isolates TIMING)"),
    ]

    for mode, label in controls:
        print(f"\nRunning {args.runs} permutations: {mode} ...")
        rows = []
        t_start = time.time()
        for r in range(args.runs):
            log = run_control(data_cache, funding_cache, windows, schedules, mode, rng)
            rows.append(summarize(log, len(data_cache)))
            if (r + 1) % max(1, args.runs // 10) == 0:
                el = time.time() - t_start
                eta = el / (r + 1) * (args.runs - r - 1)
                print(f"  {r+1}/{args.runs}  (elapsed {el/60:.1f}m, eta {eta/60:.1f}m)")
        dist = pd.DataFrame(rows)
        results[mode] = dist
        report_comparison(label, real_m, dist)

    os.makedirs("backtest_results", exist_ok=True)
    stamp = int(time.time())
    for mode, dist in results.items():
        dist.to_csv(f"backtest_results/permutation_{mode}_{stamp}.csv", index=False)
    pd.DataFrame([real_m]).to_csv(f"backtest_results/permutation_baseline_{stamp}.csv", index=False)
    print(f"\nSaved to backtest_results/permutation_*_{stamp}.csv")

    print("""
Reading this:
  - p-value here is P(random >= real). Small p = real sits in the good tail.
  - Real inside the random distribution (p > 0.10) => the entry signal is not
    distinguishable from noise. That is a clean reason to stop, not a reason
    to tune.
  - Real in the tail but still unprofitable => the signal may carry
    information the exit/cost architecture is destroying. Different diagnosis.
  - Compare MFE too: markedly better MFE than random with equal expectancy
    suggests good entries harvested badly.
  - Do NOT re-optimize parameters against these controls.""")


if __name__ == "__main__":
    main()
