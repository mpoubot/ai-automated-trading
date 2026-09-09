#!/usr/bin/env python3
"""
AURA — Multi-symbol regime candidate backtest.

RESEARCH ONLY. NEVER PLACES ORDERS. NEVER TOUCHES LIVE/PAPER EXECUTION.

Purpose
-------
The live v0.5.3 pipeline (.12/.13/.14) only evaluates the frozen candidate
"BEAR x LOW ATR x POSITIVE bar-2" for BTC/USD and ETH/USD -- that threshold
(0.596% ATR) was tuned/backtested specifically for those two symbols. This
script is the deliberate, separate research step for deciding whether any
other symbol in the discovered Alpaca crypto universe should ever be
promoted into that validated set. It does not change what the live pipeline
trades; promoting a symbol still requires a human to edit .13's SYMBOLS
constant after reviewing this output.

It reuses .12's own wilder_atr() and build_4h() functions directly (imported,
not reimplemented) so the backtest's indicator math is guaranteed identical
to what live trading actually computes.

Methodology
-----------
For each symbol:
  1. Fetch the longest available 1H closed-bar history from Alpaca (lenient:
     unlike the live .21 adapter, this never fails closed on a short history
     -- a symbol with too little data for even one warmup window is reported
     as INSUFFICIENT_DATA, not treated as an error).
  2. Compute ATR14 (1H, Wilder's method) and CLOSED 4H candles + EMA50 on 4H
     closes, exactly as .12 does live.
  3. For every 1H bar (not just "the latest", as the live engine does),
     attach the most recently COMPLETED 4H EMA50 as of that bar (never a
     look-ahead), and compute the frozen candidate's three regime labels:
     trend (BEAR/BULL_OR_NEUTRAL via EMA50), bar-2 (POSITIVE/NON_POSITIVE via
     2H close-to-close), and ATR regime (LOW/HIGH_OR_EQUAL) -- but ATR regime
     is evaluated across a GRID of percentile-based thresholds rather than
     the single frozen 0.596%, since a fixed percentage threshold does not
     generalize across symbols with very different volatility profiles.
  4. Split each symbol's history chronologically: first ~70% TRAIN, last
     ~30% TEST (held out, never used for threshold selection). This is the
     overfitting guard -- searching per-coin thresholds without a genuine
     out-of-sample check would find spurious "edges" by chance alone,
     especially across 33 symbols.
  5. On TRAIN only, search ATR-threshold candidates at the 10th/20th/.../90th
     percentile of that symbol's own TRAIN ATR% distribution. Among
     candidates with >=5 train matches, pick the one with the best mean
     24H forward return. This is the single search objective, fixed in
     advance -- not picked after looking at multiple metrics.
  6. Evaluate the CHOSEN threshold on the untouched TEST split and report
     both. Also report the original frozen 0.596% threshold's own
     train/test performance for direct comparison, unselected.
  7. Since v0.5.3 has no defined exit rule (.15 only ever produces FLAT or
     ENTRY_CANDIDATE, never a timed exit), forward returns are reported at
     four fixed horizons (4H/12H/24H/48H) as descriptive research metrics,
     not a claimed strategy.

Verdict per symbol (recommended bar, not a hard rule -- read the numbers):
  INSUFFICIENT_DATA : fewer than 20 train matches or fewer than 5 test
                       matches for the chosen threshold.
  PROMISING          : passes the data-volume bar AND the test-period mean
                        24H forward return is positive AND has the same
                        sign as the train-period mean 24H forward return.
  NOT_PROMISING      : passes the data-volume bar but fails the above.

Output
------
research/regime_backtest/multi_symbol_regime_backtest.csv  -- one row/symbol
research/regime_backtest/multi_symbol_regime_backtest_report.txt -- summary

Credentials: same as aura_v05321_alpaca_market_data.py -- ALPACA_PAPER_API_KEY
/ ALPACA_PAPER_SECRET_KEY (or ALPACA_API_KEY / ALPACA_SECRET_KEY fallback),
loaded from a local .env via python-dotenv if present.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent


def _load_market_state_engine():
    """Import .12 as a module so we reuse its exact indicator functions
    (wilder_atr, build_4h) rather than reimplementing them and risking
    drift from what live trading actually computes."""
    spec = importlib.util.spec_from_file_location(
        "aura_v05312_market_state_engine",
        ROOT / "aura_v05312_market_state_engine.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MSE = _load_market_state_engine()

REQUIRED_SYMBOLS = list(MSE.REQUIRED_SYMBOLS)  # BTC/USD, ETH/USD -- always included
FROZEN_ATR_THRESHOLD_PCT = MSE.FROZEN_ATR_THRESHOLD_PCT  # 0.596, reference only
EMA_PERIOD_4H = MSE.FROZEN_EMA_PERIOD_4H  # 50
BAR_2_LAG_HOURS = MSE.BAR_2_LAG_HOURS  # 2
MIN_1H_BARS = MSE.MIN_1H_BARS  # 204, same warmup floor as live

ALPACA_CRYPTO_BARS_URL = "https://data.alpaca.markets/v1beta3/crypto/us/bars"
ALPACA_ASSETS_URL = "https://paper-api.alpaca.markets/v2/assets"
REQUEST_LIMIT = 1000
REQUEST_TIMEOUT_SECONDS = 30
RATE_LIMIT_MAX_RETRIES = 3
RATE_LIMIT_BACKOFF_SECONDS = 2.0
INTER_SYMBOL_DELAY_SECONDS = 0.3

FORWARD_HORIZONS_HOURS = (4, 12, 24, 48)
SEARCH_OBJECTIVE_HORIZON = 24
ATR_PERCENTILE_GRID = (10, 20, 30, 40, 50, 60, 70, 80, 90)
TRAIN_FRACTION = 0.70

MIN_TRAIN_MATCHES_FOR_SEARCH = 5
MIN_TRAIN_MATCHES_FOR_VERDICT = 20
MIN_TEST_MATCHES_FOR_VERDICT = 5

DEFAULT_OUTDIR = ROOT / "research" / "regime_backtest"
DEFAULT_LOOKBACK_DAYS = 730


def _get_with_rate_limit_retry(url: str, *, headers: dict[str, str], params: dict[str, Any]):
    attempt = 0
    while True:
        response = requests.get(url, headers=headers, params=params, timeout=REQUEST_TIMEOUT_SECONDS)
        if response.status_code != 429 or attempt >= RATE_LIMIT_MAX_RETRIES:
            return response
        attempt += 1
        time.sleep(RATE_LIMIT_BACKOFF_SECONDS * attempt)


def discover_universe(headers: dict[str, str]) -> list[str]:
    """Same discovery approach as .21, kept independent so this script has
    no runtime dependency on the live adapter's internals."""
    import re

    response = _get_with_rate_limit_retry(
        ALPACA_ASSETS_URL, headers=headers, params={"asset_class": "crypto", "status": "active"}
    )
    if response.status_code != 200:
        raise RuntimeError(f"Alpaca assets HTTP {response.status_code}: {response.text[:500]}")

    payload = response.json()
    universe: set[str] = set()
    usd_re = re.compile(r"^[A-Z0-9]{2,10}/USD$")
    for asset in payload:
        if not isinstance(asset, dict) or asset.get("tradable") is not True:
            continue
        raw = asset.get("symbol")
        if not isinstance(raw, str) or not raw:
            continue
        symbol = raw.strip().upper()
        if "/" not in symbol and symbol.endswith("USD") and len(symbol) > 3:
            symbol = f"{symbol[:-3]}/USD"
        if usd_re.match(symbol):
            universe.add(symbol)
    return sorted(universe)


def fetch_history(symbol: str, *, start: pd.Timestamp, end: pd.Timestamp, headers: dict[str, str]) -> pd.DataFrame:
    """Fetch as much closed 1H history as Alpaca has in [start, end). Lenient:
    returns whatever it gets (possibly short or empty), never raises on a
    thin history -- this is research, not the live fail-closed adapter."""
    rows: list[dict] = []
    page_token: str | None = None
    pages = 0

    while True:
        params: dict[str, Any] = {
            "symbols": symbol,
            "timeframe": "1Hour",
            "start": start.isoformat().replace("+00:00", "Z"),
            "end": end.isoformat().replace("+00:00", "Z"),
            "limit": REQUEST_LIMIT,
            "sort": "asc",
        }
        if page_token:
            params["page_token"] = page_token

        response = _get_with_rate_limit_retry(ALPACA_CRYPTO_BARS_URL, headers=headers, params=params)
        if response.status_code != 200:
            raise RuntimeError(f"Alpaca bars HTTP {response.status_code} for {symbol}: {response.text[:300]}")

        payload = response.json()
        symbol_rows = payload.get("bars", {}).get(symbol, [])
        if isinstance(symbol_rows, list):
            rows.extend(symbol_rows)
        pages += 1

        page_token = payload.get("next_page_token")
        if not page_token or pages >= 200:
            break

    if not rows:
        return pd.DataFrame(columns=["timestamp", "symbol", "open", "high", "low", "close", "volume"])

    df = pd.DataFrame(rows).rename(
        columns={"t": "timestamp", "o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"}
    )
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    df["symbol"] = symbol
    df = df[df["timestamp"] < end].copy()
    df = df[["timestamp", "symbol", "open", "high", "low", "close", "volume"]]
    df = df.sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)
    return df


def compute_regime_series(g: pd.DataFrame) -> pd.DataFrame | None:
    """
    Compute, for every 1H bar in g (sorted, reset index), the trend regime
    and bar-2 regime, plus the raw ATR14% series -- everything needed to
    evaluate ANY ATR threshold without recomputing indicators per candidate.

    Reuses .12's wilder_atr() and build_4h() exactly. Returns None if there
    isn't enough history for even one 4H EMA50 warmup.
    """
    if len(g) < MIN_1H_BARS:
        return None

    g = g.sort_values("timestamp").reset_index(drop=True).copy()
    g["atr14"] = MSE.wilder_atr(g)
    g["atr14_pct"] = 100.0 * g["atr14"] / g["close"]

    h4, h4_errors = MSE.build_4h(g)
    if h4.empty or len(h4) < EMA_PERIOD_4H:
        return None

    h4 = h4.sort_values("timestamp").reset_index(drop=True)
    h4["ema50"] = h4["close"].ewm(span=EMA_PERIOD_4H, adjust=False, min_periods=EMA_PERIOD_4H).mean()

    # Attach the most recently COMPLETED 4H EMA50 as of each 1H bar -- never
    # a look-ahead. merge_asof with direction='backward' is exactly that.
    h4_ema = h4[["timestamp", "ema50"]].dropna().rename(columns={"timestamp": "h4_timestamp"})
    g = pd.merge_asof(
        g.sort_values("timestamp"),
        h4_ema.sort_values("h4_timestamp"),
        left_on="timestamp",
        right_on="h4_timestamp",
        direction="backward",
    )

    g["bar_2_return_pct"] = 100.0 * (g["close"] / g["close"].shift(BAR_2_LAG_HOURS) - 1.0)

    for h in FORWARD_HORIZONS_HOURS:
        g[f"fwd_{h}h"] = 100.0 * (g["close"].shift(-h) / g["close"] - 1.0)

    g["trend_bear"] = g["ema50"].notna() & (g["close"] < g["ema50"])
    g["bar2_positive"] = g["bar_2_return_pct"] > 0

    # Only rows with a valid EMA50 (post 4H warmup) and a valid bar-2 return
    # can ever be evaluated -- matches the live engine's own requirement
    # that every field be available before a regime_state is meaningful.
    g["_valid_row"] = g["ema50"].notna() & g["bar_2_return_pct"].notna() & g["atr14_pct"].notna()

    return g


def evaluate_threshold(g: pd.DataFrame, atr_threshold_pct: float) -> dict[str, Any]:
    """Given a precomputed regime series and one ATR threshold, return match
    count and mean/median forward returns at every horizon."""
    valid = g[g["_valid_row"]]
    matches = valid[valid["trend_bear"] & valid["bar2_positive"] & (valid["atr14_pct"] < atr_threshold_pct)]

    out: dict[str, Any] = {"matches": int(len(matches))}
    for h in FORWARD_HORIZONS_HOURS:
        col = f"fwd_{h}h"
        vals = matches[col].dropna()
        out[f"mean_fwd_{h}h"] = float(vals.mean()) if len(vals) else None
        out[f"median_fwd_{h}h"] = float(vals.median()) if len(vals) else None
    return out


def backtest_symbol(symbol: str, raw: pd.DataFrame) -> dict[str, Any]:
    result: dict[str, Any] = {
        "symbol": symbol,
        "bars_fetched": int(len(raw)),
        "verdict": "INSUFFICIENT_DATA",
        "reason": None,
    }

    g = compute_regime_series(raw)
    if g is None:
        result["reason"] = f"FEWER_THAN_{MIN_1H_BARS}_USABLE_1H_BARS_OR_NO_4H_WARMUP"
        return result

    valid = g[g["_valid_row"]].reset_index(drop=True)
    result["usable_bars"] = int(len(valid))
    if len(valid) < MIN_1H_BARS:
        result["reason"] = "TOO_FEW_VALID_REGIME_BARS_AFTER_WARMUP"
        return result

    split_idx = int(len(valid) * TRAIN_FRACTION)
    train = valid.iloc[:split_idx]
    test = valid.iloc[split_idx:]
    result["train_bars"] = int(len(train))
    result["test_bars"] = int(len(test))
    result["train_start"] = train["timestamp"].min().isoformat() if len(train) else None
    result["train_end"] = train["timestamp"].max().isoformat() if len(train) else None
    result["test_start"] = test["timestamp"].min().isoformat() if len(test) else None
    result["test_end"] = test["timestamp"].max().isoformat() if len(test) else None

    if train.empty or test.empty:
        result["reason"] = "EMPTY_TRAIN_OR_TEST_SPLIT"
        return result

    # --- Percentile-based threshold search on TRAIN only ---
    train_g = g.loc[g["timestamp"] <= train["timestamp"].max()].copy()
    train_g["_valid_row"] = train_g["_valid_row"] & (train_g["timestamp"] <= train["timestamp"].max())

    candidates = []
    for pct in ATR_PERCENTILE_GRID:
        threshold = float(np.nanpercentile(train["atr14_pct"], pct))
        perf = evaluate_threshold(train_g, threshold)
        perf["percentile"] = pct
        perf["threshold_pct"] = threshold
        candidates.append(perf)

    eligible = [c for c in candidates if c["matches"] >= MIN_TRAIN_MATCHES_FOR_SEARCH]
    objective_col = f"mean_fwd_{SEARCH_OBJECTIVE_HORIZON}h"
    eligible = [c for c in eligible if c[objective_col] is not None]

    if not eligible:
        result["reason"] = "NO_THRESHOLD_HAD_ENOUGH_TRAIN_MATCHES"
        result["train_candidates"] = candidates
        return result

    chosen = max(eligible, key=lambda c: c[objective_col])
    result["chosen_percentile"] = chosen["percentile"]
    result["chosen_threshold_pct"] = chosen["threshold_pct"]
    result["train_matches"] = chosen["matches"]
    for h in FORWARD_HORIZONS_HOURS:
        result[f"train_mean_fwd_{h}h"] = chosen[f"mean_fwd_{h}h"]
        result[f"train_median_fwd_{h}h"] = chosen[f"median_fwd_{h}h"]

    # --- Evaluate chosen threshold out-of-sample on TEST ---
    test_g = g.loc[g["timestamp"] > train["timestamp"].max()].copy()
    test_g["_valid_row"] = test_g["_valid_row"] & (test_g["timestamp"] > train["timestamp"].max())
    test_perf = evaluate_threshold(test_g, chosen["threshold_pct"])
    result["test_matches"] = test_perf["matches"]
    for h in FORWARD_HORIZONS_HOURS:
        result[f"test_mean_fwd_{h}h"] = test_perf[f"mean_fwd_{h}h"]
        result[f"test_median_fwd_{h}h"] = test_perf[f"median_fwd_{h}h"]

    # --- Reference: the frozen 0.596% threshold, unselected, same splits ---
    frozen_train = evaluate_threshold(train_g, FROZEN_ATR_THRESHOLD_PCT)
    frozen_test = evaluate_threshold(test_g, FROZEN_ATR_THRESHOLD_PCT)
    result["frozen_0596_train_matches"] = frozen_train["matches"]
    result["frozen_0596_train_mean_fwd_24h"] = frozen_train["mean_fwd_24h"]
    result["frozen_0596_test_matches"] = frozen_test["matches"]
    result["frozen_0596_test_mean_fwd_24h"] = frozen_test["mean_fwd_24h"]

    # --- Verdict ---
    train_matches = result["train_matches"]
    test_matches = result["test_matches"]
    train_obj = result[f"train_mean_fwd_{SEARCH_OBJECTIVE_HORIZON}h"]
    test_obj = result[f"test_mean_fwd_{SEARCH_OBJECTIVE_HORIZON}h"]

    if train_matches < MIN_TRAIN_MATCHES_FOR_VERDICT or test_matches < MIN_TEST_MATCHES_FOR_VERDICT:
        result["verdict"] = "INSUFFICIENT_DATA"
        result["reason"] = (
            f"train_matches={train_matches}<{MIN_TRAIN_MATCHES_FOR_VERDICT} or "
            f"test_matches={test_matches}<{MIN_TEST_MATCHES_FOR_VERDICT}"
        )
    elif test_obj is not None and train_obj is not None and test_obj > 0 and (train_obj > 0) == (test_obj > 0):
        result["verdict"] = "PROMISING"
        result["reason"] = "out_of_sample_forward_return_positive_and_consistent_with_train"
    else:
        result["verdict"] = "NOT_PROMISING"
        result["reason"] = "out_of_sample_forward_return_not_positive_or_inconsistent_with_train"

    return result


def main() -> int:
    load_dotenv()

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS)
    p.add_argument("--outdir", type=Path, default=DEFAULT_OUTDIR)
    p.add_argument(
        "--symbols",
        nargs="*",
        default=None,
        help="Optional explicit symbol list. Default: discover the full live Alpaca crypto universe.",
    )
    args = p.parse_args()

    key = os.getenv("ALPACA_PAPER_API_KEY") or os.getenv("ALPACA_API_KEY")
    secret = os.getenv("ALPACA_PAPER_SECRET_KEY") or os.getenv("ALPACA_SECRET_KEY")
    if not key or not secret:
        print("FAIL-CLOSED: Alpaca credentials are missing.", file=sys.stderr)
        return 2

    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret, "Accept": "application/json"}

    if args.symbols:
        symbols = sorted(set(args.symbols) | set(REQUIRED_SYMBOLS))
    else:
        print("Discovering live Alpaca crypto universe...")
        symbols = sorted(set(discover_universe(headers)) | set(REQUIRED_SYMBOLS))

    print(f"Backtesting {len(symbols)} symbols, up to {args.lookback_days} days of history each.")
    print("MODE: RESEARCH ONLY -- NO ORDERS")
    print()

    now = pd.Timestamp.now(tz="UTC")
    end = now.floor("h")
    start = end - pd.Timedelta(days=args.lookback_days)

    rows = []
    for i, symbol in enumerate(symbols, start=1):
        time.sleep(INTER_SYMBOL_DELAY_SECONDS)
        try:
            raw = fetch_history(symbol, start=start, end=end, headers=headers)
            result = backtest_symbol(symbol, raw)
        except Exception as exc:  # noqa: BLE001 - research script, report and continue
            result = {"symbol": symbol, "bars_fetched": 0, "verdict": "ERROR", "reason": f"{type(exc).__name__}: {exc}"}
        rows.append(result)
        print(f"[{i:02d}/{len(symbols)}] {symbol}: {result['verdict']} ({result.get('reason', '')})")

    df = pd.DataFrame(rows)
    args.outdir.mkdir(parents=True, exist_ok=True)
    csv_path = args.outdir / "multi_symbol_regime_backtest.csv"
    df.to_csv(csv_path, index=False)

    report_path = args.outdir / "multi_symbol_regime_backtest_report.txt"
    with report_path.open("w", encoding="utf-8") as f:
        f.write("=" * 96 + "\n")
        f.write("AURA MULTI-SYMBOL REGIME CANDIDATE BACKTEST\n")
        f.write("RESEARCH ONLY -- NO ORDERS PLACED\n")
        f.write("=" * 96 + "\n\n")
        f.write(f"Window: {start.isoformat()} -> {end.isoformat()}\n")
        f.write(f"Symbols: {len(symbols)}\n\n")
        for verdict in ("PROMISING", "NOT_PROMISING", "INSUFFICIENT_DATA", "ERROR"):
            subset = df[df["verdict"] == verdict]
            f.write(f"{verdict}: {len(subset)}\n")
            for _, row in subset.iterrows():
                f.write(f"  {row['symbol']}: {row.get('reason', '')}\n")
            f.write("\n")

    print()
    print(f"CSV:    {csv_path.resolve()}")
    print(f"REPORT: {report_path.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
