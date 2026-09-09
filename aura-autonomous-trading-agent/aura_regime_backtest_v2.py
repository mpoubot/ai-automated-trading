#!/usr/bin/env python3
"""
AURA — Multi-symbol regime candidate backtest, v2 (rigor pass).

RESEARCH ONLY. NEVER PLACES ORDERS. NEVER TOUCHES LIVE/PAPER EXECUTION.

Why this exists
----------------
aura_regime_backtest_multi_symbol.py (v1) ran the frozen candidate "BEAR x
LOW ATR x POSITIVE bar-2" across all 33 discovered symbols with a per-coin
ATR-threshold search and a single 70/30 train/test split. Reviewing v1's
real results turned up several problems that made its PROMISING/NOT_PROMISING
verdicts untrustworthy:

  1. Every hourly bar where the regime condition held was counted as a
     separate "match", even though the condition tends to stay true for
     many consecutive hours once it fires. That inflated apparent sample
     size and let noise clear the match-count bar easily.
  2. The per-coin threshold search picked whichever of 9 candidate
     percentiles maximized TRAIN mean 24h forward return, with no penalty
     for how unstable that choice was -- the chosen percentile jumped
     around almost randomly symbol to symbol, a classic overfitting sign.
  3. The verdict only checked "is the test-period mean positive and same
     sign as train" -- no comparison to what forward returns look like at
     a random time (baseline), and no significance test. USDT/USD, a
     stablecoin, cleared this bar with return values that are just noise.
  4. No trading cost was modeled at all.

v2 keeps the same underlying data/indicator code (.12's wilder_atr/build_4h,
unchanged) but changes the *evaluation* methodology:

  - Non-overlapping EPISODES instead of raw bar matches: a contiguous run of
    matching hourly bars counts once, sampled at its first (entry) bar. This
    is still not full statistical independence (episodes can cluster in
    time), but it removes the worst of the autocorrelation inflation.
  - A baseline: the unconditional mean forward return over the same period
    (i.e. "what happens after a random hour", not conditioned on the
    regime). The number that matters is EXCESS return over this baseline,
    not just "is it positive".
  - A block bootstrap (resampling episodes with replacement) on the TEST
    split's episode returns, reporting a confidence interval and P(mean>0)
    -- a soft significance check, not just a sign check.
  - Walk-forward validation: the full history is split into K contiguous
    blocks and evaluated with an expanding window (train on blocks 1..k-1,
    test on block k), so we can see whether the threshold and its direction
    hold up across more than one arbitrary split.
  - A flat round-trip cost assumption (in basis points), subtracted from
    every forward return used in the verdict, so a numerically positive but
    tiny edge doesn't get called "promising".
  - The default symbol set is a short list (BTC/USD, ETH/USD -- always
    required -- plus AVAX/USD) instead of the full universe, since running
    the same search-then-verdict procedure across 30+ symbols multiplies
    the chance of a spurious pass by chance alone. Pass --full-universe to
    opt into the wider, noisier scan explicitly.

Verdict per symbol (still a recommendation to read the numbers, not a rule):
  INSUFFICIENT_DATA : too few train/test episodes, or too few walk-forward
                       folds could even be evaluated.
  PROMISING          : test-period net return is positive AND beats the
                        baseline (excess > 0) AND the bootstrap says P(mean
                        excess-adjusted return > 0) is high AND a majority
                        of walk-forward folds agree on the sign.
  WEAK               : positive and beats baseline, but only one of
                        (significant / stable across folds) holds -- treat
                        as "not yet disproven", not as evidence of an edge.
  NOT_PROMISING      : fails the above.

This script does not choose a strategy or place any trade. Promoting any
symbol into the live .13 signal generator remains a separate, deliberate,
human decision after reading these numbers.

Output
------
research/regime_backtest_v2/multi_symbol_regime_backtest_v2.csv
research/regime_backtest_v2/multi_symbol_regime_backtest_v2_report.txt

Credentials: same as v1 -- ALPACA_PAPER_API_KEY / ALPACA_PAPER_SECRET_KEY
(or ALPACA_API_KEY / ALPACA_SECRET_KEY fallback), loaded from a local .env
via python-dotenv if present. Missing credentials fail closed (exit 2)
before any network call is made.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
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

# Default symbol scope: required symbols plus a short, deliberately small
# list of additional candidates. Widen with --symbols or --full-universe.
SHORTLIST_SYMBOLS = ["AVAX/USD"]

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

# Episode-count gates. These are deliberately smaller than v1's raw-bar
# gates (5 / 20 / 5) because episodes are, by construction, far fewer than
# raw matching bars for the same underlying signal.
MIN_TRAIN_EPISODES_FOR_SEARCH = 3
MIN_TRAIN_EPISODES_VERDICT = 8
MIN_TEST_EPISODES_VERDICT = 3
MIN_TEST_EPISODES_WALKFORWARD = 2
MIN_WALKFORWARD_FOLDS_CONSIDERED = 2
WALKFORWARD_MAJORITY_FRACTION = 0.5
BOOTSTRAP_P_POSITIVE_THRESHOLD = 0.85

DEFAULT_OUTDIR = ROOT / "research" / "regime_backtest_v2"
DEFAULT_LOOKBACK_DAYS = 730
DEFAULT_COST_BPS = 10.0
DEFAULT_N_BOOT = 2000
DEFAULT_ALPHA = 0.10  # 90% CI
DEFAULT_K_FOLDS = 4
DEFAULT_SEED = 42


# ------------------------------------------------------------------------
# Data fetch -- unchanged from v1, kept independent of the live .21 adapter.
# ------------------------------------------------------------------------

def _get_with_rate_limit_retry(url: str, *, headers: dict[str, str], params: dict[str, Any]):
    attempt = 0
    while True:
        response = requests.get(url, headers=headers, params=params, timeout=REQUEST_TIMEOUT_SECONDS)
        if response.status_code != 429 or attempt >= RATE_LIMIT_MAX_RETRIES:
            return response
        attempt += 1
        time.sleep(RATE_LIMIT_BACKOFF_SECONDS * attempt)


def discover_universe(headers: dict[str, str]) -> list[str]:
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


# ------------------------------------------------------------------------
# Indicators / regime series -- unchanged from v1.
# ------------------------------------------------------------------------

def compute_regime_series(g: pd.DataFrame) -> pd.DataFrame | None:
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

    g["_valid_row"] = g["ema50"].notna() & g["bar_2_return_pct"].notna() & g["atr14_pct"].notna()

    return g


def slice_by_range(g: pd.DataFrame, start_ts: pd.Timestamp | None = None, end_ts: pd.Timestamp | None = None) -> pd.DataFrame:
    """Return a copy of g whose _valid_row is further restricted to
    (start_ts, end_ts] -- half-open on the left so adjacent slices built
    from consecutive boundaries never double-count the boundary bar."""
    sliced = g.copy()
    mask = pd.Series(True, index=sliced.index)
    if start_ts is not None:
        mask &= sliced["timestamp"] > start_ts
    if end_ts is not None:
        mask &= sliced["timestamp"] <= end_ts
    sliced["_valid_row"] = sliced["_valid_row"] & mask
    return sliced


# ------------------------------------------------------------------------
# Episode detection + summarization (the core methodology change).
# ------------------------------------------------------------------------

def build_episode_table(g_slice: pd.DataFrame, atr_threshold_pct: float) -> pd.DataFrame:
    """One row per contiguous run of matching bars, taken at the run's
    first (entry) bar -- this is what turns "N matching hourly bars" into
    "N/duration roughly-independent episodes"."""
    valid = g_slice[g_slice["_valid_row"]].sort_values("timestamp").reset_index(drop=True)
    if valid.empty:
        return valid
    match = valid["trend_bear"] & valid["bar2_positive"] & (valid["atr14_pct"] < atr_threshold_pct)
    is_entry = match & ~match.shift(1, fill_value=False)
    return valid[is_entry].reset_index(drop=True)


def summarize_episodes(entries: pd.DataFrame, horizons: tuple[int, ...], cost_bps: float) -> dict[str, Any]:
    cost_pct = cost_bps / 100.0
    out: dict[str, Any] = {"episodes": int(len(entries))}
    for h in horizons:
        col = f"fwd_{h}h"
        vals = entries[col].dropna().to_numpy(dtype=float) if len(entries) else np.array([], dtype=float)
        out[f"gross_mean_fwd_{h}h"] = float(vals.mean()) if len(vals) else None
        out[f"gross_median_fwd_{h}h"] = float(np.median(vals)) if len(vals) else None
        net_vals = vals - cost_pct
        out[f"net_mean_fwd_{h}h"] = float(net_vals.mean()) if len(vals) else None
        out[f"net_median_fwd_{h}h"] = float(np.median(net_vals)) if len(vals) else None
    return out


def summarize_baseline(g_slice: pd.DataFrame, horizons: tuple[int, ...]) -> dict[str, Any]:
    """Unconditional mean/median forward return over every valid bar in the
    slice -- "what usually happens", not conditioned on the regime. This is
    the number the signal has to beat, not just clear zero."""
    valid = g_slice[g_slice["_valid_row"]]
    out: dict[str, Any] = {}
    for h in horizons:
        vals = valid[f"fwd_{h}h"].dropna()
        out[f"baseline_mean_fwd_{h}h"] = float(vals.mean()) if len(vals) else None
        out[f"baseline_median_fwd_{h}h"] = float(vals.median()) if len(vals) else None
    return out


def bootstrap_mean_ci(values: np.ndarray, *, n_boot: int, alpha: float, rng: np.random.Generator) -> dict[str, Any]:
    """Block-free bootstrap over episode-level returns (episodes are the
    unit being resampled, which is why episode detection above matters --
    resampling raw overlapping bars would understate the real uncertainty)."""
    values = np.asarray(values, dtype=float)
    values = values[~np.isnan(values)]
    n = len(values)
    if n == 0:
        return {"n": 0, "mean": None, "ci_low": None, "ci_high": None, "p_positive": None}
    if n == 1:
        m = float(values[0])
        p = 1.0 if m > 0 else (0.0 if m < 0 else 0.5)
        return {"n": 1, "mean": m, "ci_low": m, "ci_high": m, "p_positive": p}

    idx = rng.integers(0, n, size=(n_boot, n))
    boot_means = values[idx].mean(axis=1)
    lo, hi = np.percentile(boot_means, [100 * alpha / 2, 100 * (1 - alpha / 2)])
    p_positive = float((boot_means > 0).mean())
    return {"n": n, "mean": float(values.mean()), "ci_low": float(lo), "ci_high": float(hi), "p_positive": p_positive}


def search_threshold_episodes(
    train_g: pd.DataFrame, *, cost_bps: float
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Percentile-grid search on TRAIN only, exactly like v1, except the
    search objective is episode-level net mean 24h forward return instead
    of raw-bar mean -- so a threshold that merely stretches out one long
    regime episode into many "matches" no longer looks artificially good."""
    valid_train = train_g[train_g["_valid_row"]]
    if valid_train.empty:
        return None, []

    candidates = []
    for pct in ATR_PERCENTILE_GRID:
        threshold = float(np.nanpercentile(valid_train["atr14_pct"], pct))
        entries = build_episode_table(train_g, threshold)
        summary = summarize_episodes(entries, FORWARD_HORIZONS_HOURS, cost_bps)
        summary["percentile"] = pct
        summary["threshold_pct"] = threshold
        candidates.append(summary)

    objective_col = f"net_mean_fwd_{SEARCH_OBJECTIVE_HORIZON}h"
    eligible = [
        c for c in candidates
        if c["episodes"] >= MIN_TRAIN_EPISODES_FOR_SEARCH and c[objective_col] is not None
    ]
    if not eligible:
        return None, candidates
    chosen = max(eligible, key=lambda c: c[objective_col])
    return chosen, candidates


# ------------------------------------------------------------------------
# Walk-forward validation.
# ------------------------------------------------------------------------

def block_boundaries(valid_full: pd.DataFrame, k_folds: int) -> list[pd.Timestamp]:
    n = len(valid_full)
    boundaries = []
    for k in range(1, k_folds + 1):
        idx = int(round(k * n / k_folds)) - 1
        idx = max(0, min(idx, n - 1))
        boundaries.append(valid_full.iloc[idx]["timestamp"])
    return boundaries


def walk_forward(
    g: pd.DataFrame, valid_full: pd.DataFrame, *, k_folds: int, cost_bps: float
) -> dict[str, Any]:
    """Expanding-window walk-forward: fold k trains on blocks 1..k-1 and
    tests on block k. With k_folds=4 that's 3 folds. This checks whether
    the chosen threshold's direction is a one-off artifact of the single
    70/30 holdout split, or holds up across more than one time window."""
    boundaries = block_boundaries(valid_full, k_folds)
    folds: list[dict[str, Any]] = []

    for k in range(2, k_folds + 1):
        train_end = boundaries[k - 2]
        test_end = boundaries[k - 1]
        train_g = slice_by_range(g, start_ts=None, end_ts=train_end)
        test_g = slice_by_range(g, start_ts=train_end, end_ts=test_end)

        fold: dict[str, Any] = {"fold": k - 1, "train_end": train_end.isoformat(), "test_end": test_end.isoformat()}
        chosen, _ = search_threshold_episodes(train_g, cost_bps=cost_bps)
        if chosen is None:
            fold["status"] = "NO_ELIGIBLE_THRESHOLD_ON_TRAIN"
            folds.append(fold)
            continue

        fold["chosen_percentile"] = chosen["percentile"]
        fold["chosen_threshold_pct"] = chosen["threshold_pct"]
        fold["train_episodes"] = chosen["episodes"]

        test_entries = build_episode_table(test_g, chosen["threshold_pct"])
        test_summary = summarize_episodes(test_entries, (SEARCH_OBJECTIVE_HORIZON,), cost_bps)
        baseline = summarize_baseline(test_g, (SEARCH_OBJECTIVE_HORIZON,))

        fold["test_episodes"] = test_summary["episodes"]
        fold["test_net_mean_fwd_24h"] = test_summary.get(f"net_mean_fwd_{SEARCH_OBJECTIVE_HORIZON}h")
        fold["baseline_mean_fwd_24h"] = baseline.get(f"baseline_mean_fwd_{SEARCH_OBJECTIVE_HORIZON}h")

        if (
            test_summary["episodes"] >= MIN_TEST_EPISODES_WALKFORWARD
            and fold["test_net_mean_fwd_24h"] is not None
            and fold["baseline_mean_fwd_24h"] is not None
        ):
            fold["status"] = "EVALUATED"
            fold["excess_vs_baseline_24h"] = fold["test_net_mean_fwd_24h"] - fold["baseline_mean_fwd_24h"]
        else:
            fold["status"] = "TOO_FEW_TEST_EPISODES"
        folds.append(fold)

    considered = [f for f in folds if f.get("status") == "EVALUATED"]
    positive = [f for f in considered if f["excess_vs_baseline_24h"] > 0]
    return {"folds": folds, "folds_considered": len(considered), "folds_positive_excess": len(positive)}


# ------------------------------------------------------------------------
# Per-symbol orchestration + verdict.
# ------------------------------------------------------------------------

def backtest_symbol_v2(
    symbol: str,
    raw: pd.DataFrame,
    *,
    cost_bps: float,
    n_boot: int,
    alpha: float,
    k_folds: int,
    rng: np.random.Generator,
) -> dict[str, Any]:
    result: dict[str, Any] = {"symbol": symbol, "bars_fetched": int(len(raw))}

    g = compute_regime_series(raw)
    if g is None:
        result.update(verdict="INSUFFICIENT_DATA", reason=f"FEWER_THAN_{MIN_1H_BARS}_USABLE_1H_BARS_OR_NO_4H_WARMUP")
        return result

    valid_full = g[g["_valid_row"]].reset_index(drop=True)
    result["usable_bars"] = int(len(valid_full))
    if len(valid_full) < MIN_1H_BARS:
        result.update(verdict="INSUFFICIENT_DATA", reason="TOO_FEW_VALID_REGIME_BARS_AFTER_WARMUP")
        return result

    split_idx = int(len(valid_full) * TRAIN_FRACTION)
    train_valid = valid_full.iloc[:split_idx]
    test_valid = valid_full.iloc[split_idx:]
    result["train_bars"] = int(len(train_valid))
    result["test_bars"] = int(len(test_valid))
    if train_valid.empty or test_valid.empty:
        result.update(verdict="INSUFFICIENT_DATA", reason="EMPTY_TRAIN_OR_TEST_SPLIT")
        return result

    train_boundary_ts = train_valid["timestamp"].max()
    result["train_start"] = train_valid["timestamp"].min().isoformat()
    result["train_end"] = train_boundary_ts.isoformat()
    result["test_start"] = test_valid["timestamp"].min().isoformat()
    result["test_end"] = test_valid["timestamp"].max().isoformat()

    train_g = slice_by_range(g, end_ts=train_boundary_ts)
    test_g = slice_by_range(g, start_ts=train_boundary_ts)

    chosen, _candidates = search_threshold_episodes(train_g, cost_bps=cost_bps)
    if chosen is None:
        result.update(verdict="INSUFFICIENT_DATA", reason="NO_THRESHOLD_HAD_ENOUGH_TRAIN_EPISODES")
        return result

    result["chosen_percentile"] = chosen["percentile"]
    result["chosen_threshold_pct"] = chosen["threshold_pct"]
    result["train_episodes"] = chosen["episodes"]
    for h in FORWARD_HORIZONS_HOURS:
        result[f"train_gross_mean_fwd_{h}h"] = chosen[f"gross_mean_fwd_{h}h"]
        result[f"train_net_mean_fwd_{h}h"] = chosen[f"net_mean_fwd_{h}h"]

    test_entries = build_episode_table(test_g, chosen["threshold_pct"])
    test_summary = summarize_episodes(test_entries, FORWARD_HORIZONS_HOURS, cost_bps)
    result["test_episodes"] = test_summary["episodes"]
    for h in FORWARD_HORIZONS_HOURS:
        result[f"test_gross_mean_fwd_{h}h"] = test_summary[f"gross_mean_fwd_{h}h"]
        result[f"test_net_mean_fwd_{h}h"] = test_summary[f"net_mean_fwd_{h}h"]

    train_baseline = summarize_baseline(train_g, FORWARD_HORIZONS_HOURS)
    test_baseline = summarize_baseline(test_g, FORWARD_HORIZONS_HOURS)
    for h in FORWARD_HORIZONS_HOURS:
        result[f"train_baseline_mean_fwd_{h}h"] = train_baseline[f"baseline_mean_fwd_{h}h"]
        result[f"test_baseline_mean_fwd_{h}h"] = test_baseline[f"baseline_mean_fwd_{h}h"]

    test_net_obj = result[f"test_net_mean_fwd_{SEARCH_OBJECTIVE_HORIZON}h"]
    test_baseline_obj = result[f"test_baseline_mean_fwd_{SEARCH_OBJECTIVE_HORIZON}h"]
    result["excess_vs_baseline_24h"] = (
        (test_net_obj - test_baseline_obj) if test_net_obj is not None and test_baseline_obj is not None else None
    )

    cost_pct = cost_bps / 100.0
    test_vals = test_entries[f"fwd_{SEARCH_OBJECTIVE_HORIZON}h"].dropna().to_numpy(dtype=float) - cost_pct
    boot = bootstrap_mean_ci(test_vals, n_boot=n_boot, alpha=alpha, rng=rng)
    result["bootstrap_n"] = boot["n"]
    result["bootstrap_ci_low_24h"] = boot["ci_low"]
    result["bootstrap_ci_high_24h"] = boot["ci_high"]
    result["bootstrap_p_positive_24h"] = boot["p_positive"]

    frozen_train_entries = build_episode_table(train_g, FROZEN_ATR_THRESHOLD_PCT)
    frozen_test_entries = build_episode_table(test_g, FROZEN_ATR_THRESHOLD_PCT)
    frozen_train_summary = summarize_episodes(frozen_train_entries, (SEARCH_OBJECTIVE_HORIZON,), cost_bps)
    frozen_test_summary = summarize_episodes(frozen_test_entries, (SEARCH_OBJECTIVE_HORIZON,), cost_bps)
    result["frozen_0596_train_episodes"] = frozen_train_summary["episodes"]
    result["frozen_0596_train_net_mean_fwd_24h"] = frozen_train_summary.get(f"net_mean_fwd_{SEARCH_OBJECTIVE_HORIZON}h")
    result["frozen_0596_test_episodes"] = frozen_test_summary["episodes"]
    result["frozen_0596_test_net_mean_fwd_24h"] = frozen_test_summary.get(f"net_mean_fwd_{SEARCH_OBJECTIVE_HORIZON}h")

    wf = walk_forward(g, valid_full, k_folds=k_folds, cost_bps=cost_bps)
    result["walkforward_folds_considered"] = wf["folds_considered"]
    result["walkforward_folds_positive_excess"] = wf["folds_positive_excess"]
    result["walkforward_fold_detail"] = json.dumps(wf["folds"])

    train_episodes = result["train_episodes"]
    test_episodes = result["test_episodes"]

    if (
        train_episodes < MIN_TRAIN_EPISODES_VERDICT
        or test_episodes < MIN_TEST_EPISODES_VERDICT
        or wf["folds_considered"] < MIN_WALKFORWARD_FOLDS_CONSIDERED
    ):
        result["verdict"] = "INSUFFICIENT_DATA"
        result["reason"] = (
            f"train_episodes={train_episodes}<{MIN_TRAIN_EPISODES_VERDICT} or "
            f"test_episodes={test_episodes}<{MIN_TEST_EPISODES_VERDICT} or "
            f"walkforward_folds_considered={wf['folds_considered']}<{MIN_WALKFORWARD_FOLDS_CONSIDERED}"
        )
        return result

    passes_direction = test_net_obj is not None and test_net_obj > 0
    passes_excess = result["excess_vs_baseline_24h"] is not None and result["excess_vs_baseline_24h"] > 0
    passes_significance = boot["p_positive"] is not None and boot["p_positive"] >= BOOTSTRAP_P_POSITIVE_THRESHOLD
    wf_fraction = (wf["folds_positive_excess"] / wf["folds_considered"]) if wf["folds_considered"] else 0.0
    passes_walkforward = wf_fraction >= WALKFORWARD_MAJORITY_FRACTION
    result["walkforward_positive_fraction"] = wf_fraction

    if passes_direction and passes_excess and passes_significance and passes_walkforward:
        result["verdict"] = "PROMISING"
        result["reason"] = "positive_excess_significant_and_stable_across_walkforward_folds"
    elif passes_direction and passes_excess and (passes_significance or passes_walkforward):
        result["verdict"] = "WEAK"
        result["reason"] = "positive_excess_but_not_both_significant_and_stable"
    else:
        result["verdict"] = "NOT_PROMISING"
        result["reason"] = "no_positive_significant_stable_edge_over_baseline"

    return result


def main() -> int:
    load_dotenv()

    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS)
    p.add_argument("--outdir", type=Path, default=DEFAULT_OUTDIR)
    p.add_argument(
        "--symbols",
        nargs="*",
        default=None,
        help="Explicit symbol list. Overrides the default shortlist and --full-universe.",
    )
    p.add_argument(
        "--full-universe",
        action="store_true",
        help="Scan every discovered Alpaca crypto symbol instead of the default shortlist. "
        "Warning: testing many symbols with the same search-then-verdict procedure raises the "
        "chance of a spurious PROMISING/WEAK result by chance alone -- treat a wide-scan pass "
        "as a lead to re-check on the shortlist, not as a conclusion.",
    )
    p.add_argument("--cost-bps", type=float, default=DEFAULT_COST_BPS, help="Flat round-trip cost in basis points, subtracted from every forward return used in the verdict.")
    p.add_argument("--n-boot", type=int, default=DEFAULT_N_BOOT)
    p.add_argument("--alpha", type=float, default=DEFAULT_ALPHA, help="Bootstrap CI significance level (0.10 = 90%% CI).")
    p.add_argument("--k-folds", type=int, default=DEFAULT_K_FOLDS, help="Number of contiguous blocks for walk-forward validation (K-1 folds are evaluated).")
    p.add_argument("--seed", type=int, default=DEFAULT_SEED, help="Bootstrap RNG seed, for reproducible re-runs.")
    args = p.parse_args()

    key = os.getenv("ALPACA_PAPER_API_KEY") or os.getenv("ALPACA_API_KEY")
    secret = os.getenv("ALPACA_PAPER_SECRET_KEY") or os.getenv("ALPACA_SECRET_KEY")
    if not key or not secret:
        print("FAIL-CLOSED: Alpaca credentials are missing.", file=sys.stderr)
        return 2

    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret, "Accept": "application/json"}

    if args.symbols:
        symbols = sorted(set(args.symbols) | set(REQUIRED_SYMBOLS))
    elif args.full_universe:
        print("Discovering live Alpaca crypto universe (--full-universe)...")
        print("NOTE: a wide scan raises the chance of a spurious pass by chance alone. Treat any")
        print("      PROMISING/WEAK result here as a lead to re-verify, not a conclusion.")
        symbols = sorted(set(discover_universe(headers)) | set(REQUIRED_SYMBOLS))
    else:
        symbols = sorted(set(REQUIRED_SYMBOLS) | set(SHORTLIST_SYMBOLS))

    rng = np.random.default_rng(args.seed)

    print(f"Backtesting {len(symbols)} symbols, up to {args.lookback_days} days of history each.")
    print(f"cost_bps={args.cost_bps}  n_boot={args.n_boot}  alpha={args.alpha}  k_folds={args.k_folds}  seed={args.seed}")
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
            result = backtest_symbol_v2(
                symbol, raw, cost_bps=args.cost_bps, n_boot=args.n_boot, alpha=args.alpha, k_folds=args.k_folds, rng=rng
            )
        except Exception as exc:  # noqa: BLE001 - research script, report and continue
            result = {"symbol": symbol, "bars_fetched": 0, "verdict": "ERROR", "reason": f"{type(exc).__name__}: {exc}"}
        rows.append(result)
        print(f"[{i:02d}/{len(symbols)}] {symbol}: {result['verdict']} ({result.get('reason', '')})")

    df = pd.DataFrame(rows)
    args.outdir.mkdir(parents=True, exist_ok=True)
    csv_path = args.outdir / "multi_symbol_regime_backtest_v2.csv"
    df.to_csv(csv_path, index=False)

    report_path = args.outdir / "multi_symbol_regime_backtest_v2_report.txt"
    with report_path.open("w", encoding="utf-8") as f:
        f.write("=" * 96 + "\n")
        f.write("AURA MULTI-SYMBOL REGIME CANDIDATE BACKTEST -- v2 (episodes / baseline / bootstrap / walk-forward)\n")
        f.write("RESEARCH ONLY -- NO ORDERS PLACED\n")
        f.write("=" * 96 + "\n\n")
        f.write(f"Window: {start.isoformat()} -> {end.isoformat()}\n")
        f.write(f"Symbols: {len(symbols)}\n")
        f.write(f"cost_bps={args.cost_bps}  n_boot={args.n_boot}  alpha={args.alpha}  k_folds={args.k_folds}  seed={args.seed}\n\n")
        for verdict in ("PROMISING", "WEAK", "NOT_PROMISING", "INSUFFICIENT_DATA", "ERROR"):
            subset = df[df["verdict"] == verdict] if "verdict" in df.columns else df.iloc[0:0]
            f.write(f"{verdict}: {len(subset)}\n")
            for _, row in subset.iterrows():
                extra = ""
                if verdict in ("PROMISING", "WEAK", "NOT_PROMISING"):
                    extra = (
                        f" | test_net_24h={row.get('test_net_mean_fwd_24h')}"
                        f" excess_vs_baseline_24h={row.get('excess_vs_baseline_24h')}"
                        f" bootstrap_p_positive={row.get('bootstrap_p_positive_24h')}"
                        f" walkforward={row.get('walkforward_folds_positive_excess')}/{row.get('walkforward_folds_considered')}"
                    )
                f.write(f"  {row['symbol']}: {row.get('reason', '')}{extra}\n")
            f.write("\n")

    print()
    print(f"CSV:    {csv_path.resolve()}")
    print(f"REPORT: {report_path.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
