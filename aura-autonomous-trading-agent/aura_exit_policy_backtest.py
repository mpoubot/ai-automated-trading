#!/usr/bin/env python3
"""
AURA — Exit-policy (trailing-stop / take-profit) backtest.

RESEARCH ONLY. NEVER PLACES ORDERS. NEVER TOUCHES LIVE/PAPER EXECUTION.
Does not read or write anything under .19/.22/.23's live execution path,
and does not change VALIDATED_LONG_ENTRY_REGIME_LABELS /
VALIDATED_SHORT_ENTRY_REGIME_LABELS in aura_v05323_execution_specification_
builder.py -- those stay empty exactly as they are today. This script only
asks "if a trade were opened on each .13-recognized candidate, what would
different stop/target combinations have done to it historically", nothing
more.

Why this exists (Phase 2 of the agreed 5-phase plan)
-----------------------------------------------------
aura_regime_backtest_v2.py already asks whether the FROZEN entry candidate
itself has an edge over a random-time baseline, using a *fixed* forward-
return horizon (4h/12h/24h/48h) to measure the outcome. It never asks what
should actually happen to a trade once it's open -- when to take profit,
when to cut a loser, how far to trail a stop. That is what this script
adds: for every historical entry episode of both .13-recognized candidates,
FROZEN_CANDIDATE ("BEAR x LOW ATR x POSITIVE bar-2") and MIRROR_CANDIDATE
("BULL_OR_NEUTRAL x LOW x NON_POSITIVE"), it simulates a full trade under a
grid of trailing-stop / take-profit combinations and reports which
combination would have performed best, with the same anti-overfitting
discipline as v2: search on TRAIN only, evaluate the chosen policy
out-of-sample on TEST, bootstrap a confidence interval on the TEST trade
returns, and check the chosen policy's direction survives an expanding-
window walk-forward split -- never just report the single best number a
grid search happened to find.

Direction (for SIMULATION ONLY -- this does not authorize or configure any
live/paper order):
  FROZEN_CANDIDATE  -> simulated as a LONG entry
  MIRROR_CANDIDATE  -> simulated as a SHORT entry
This mapping is Martin's explicit choice for this backtest, matching the
naming already used when the mirror candidate was added to .13 ("recognize
mirrored SHORT candidate"). It has no effect on any live file -- .23's
direction allowlists remain frozenset() until Martin separately reviews and
populates them.

Entry definition (reused from .12/.13, not reimplemented)
-----------------------------------------------------------
This script imports aura_regime_backtest_v2.py (for its exact indicator
computation, which itself reuses .12's wilder_atr/build_4h) and reuses its
episode logic unchanged for FROZEN_CANDIDATE, adding the mirrored condition
for MIRROR_CANDIDATE. Both candidates use the SAME single global ATR
threshold (.12's FROZEN_ATR_THRESHOLD_PCT, 0.596%) for the LOW/HIGH_OR_EQUAL
label, exactly as .12 computes it live -- there is no separate threshold
search here; Phase 2 is exit-policy research, not further entry tuning.

Exit-policy simulation
-----------------------
For each entry episode, price is walked forward bar-by-bar (1H OHLC) from
the bar immediately after entry:
  - A trailing stop trails the best price seen since entry (running high
    for a LONG, running low for a SHORT) by --trailing-stops percent.
  - A take-profit is a FIXED target measured from the entry price by
    --take-profits percent (not trailing). A "trail-only" variant with no
    fixed take-profit is also tested for every trailing-stop level.
  - Each bar's exit check uses the stop/target levels computed BEFORE that
    bar (i.e. from the running extreme as of the end of the prior bar) to
    avoid look-ahead bias; the running extreme is only updated using a
    bar's high/low AFTER that bar has been checked for an exit.
  - If both the stop and the target would be hit within the same bar (only
    possible with intrabar high/low, since we don't have tick data), the
    STOP is assumed to have been hit first -- the same conservative,
    documented assumption used in this repo's older stock exit-policy
    research (historical_signal_quality_scanner_v04x.py).
  - A trade that never exits within --max-hold-hours (default 240h / 10
    days) is closed at the last available bar's close and marked TIMEOUT --
    this is a research safety cap, not a claim about what a live position
    would do; the TIMEOUT frequency is reported per policy so an exit
    policy that rarely resolves isn't silently treated as "working".
  - A flat round-trip cost (--cost-bps, default 10bps, same convention as
    v2) is subtracted from every trade's gross return.
  - SEQUENTIAL, SINGLE-POSITION ACCOUNT (added after Martin's review of the
    first real run): an entry episode that fires while a trade already
    opened under the SAME policy is still open (per that policy's own
    trailing-stop/take-profit/timeout resolution) is skipped, not stacked
    into a second simulated concurrent position. This matches how a real
    account actually behaves -- one position per symbol/candidate at a
    time -- and is evaluated separately for every policy in the grid, since
    a wider trailing stop holds trades open longer and therefore skips more
    overlapping entries than a tighter one. Every result row reports
    test_entries_skipped_overlap / test_entries_total (and the equivalent
    train-side and per-walk-forward-fold counts) so the skip rate is always
    visible rather than silently lowering the trade count.

Policy search / validation discipline (mirrors v2 exactly, applied to
policy choice instead of ATR threshold choice)
------------------------------------------------------------------------
  - History split 70/30 (TRAIN/TEST) by valid-bar time order, same as v2.
  - Every (trailing_stop, take_profit) combination in the grid is simulated
    on TRAIN entries only; the combination with the best TRAIN net mean
    trade return is chosen (minimum trade count required to be eligible).
  - The CHOSEN policy (not the whole grid) is then evaluated on TEST
    entries, out-of-sample.
  - A baseline for comparison: the SAME test entries' plain 24h-hold net
    return (v2's fwd_24h column) -- i.e. does actively managing the exit
    with stops/targets beat simply holding for a fixed day. The number
    reported is EXCESS over this baseline, not just "is it positive".
  - A block bootstrap over TEST trade returns reports a confidence interval
    and P(mean > 0).
  - Expanding-window walk-forward (v2's block_boundaries/K folds): for each
    fold, the same TRAIN-only search is repeated fresh on that fold's
    training block, and the resulting policy is tested on that fold's own
    test block -- checking the result isn't an artifact of the single
    70/30 split.

Verdict per (symbol, candidate) -- a recommendation to read the numbers,
not a rule; promoting anything into a live sizing/execution config remains
a separate, later, human decision (Phase 4/5 of the agreed plan):
  INSUFFICIENT_DATA : too few train/test trades, or too few walk-forward
                       folds could even be evaluated.
  PROMISING          : TEST net expectancy is positive AND beats the 24h-
                        hold baseline AND the bootstrap says P(mean > 0) is
                        high AND a majority of walk-forward folds agree on
                        the sign.
  WEAK               : positive and beats baseline, but only one of
                        (significant / stable across folds) holds.
  NOT_PROMISING      : fails the above.

Symbol scope
------------
Default is the FULL discovered Alpaca crypto universe (Martin's explicit
choice for this backtest, unlike v2's narrower default shortlist) --
--symbols/--shortlist-only remain available to narrow it. Only BTC/USD and
ETH/USD currently have a separately-validated entry signal (v2's own
PROMISING/WEAK/NOT_PROMISING work is scoped there); every other symbol is
still backtested here, but every result row and the report are explicitly
labeled validated_entry=True/False so a promising exit policy on an
unvalidated-entry coin is never read as equivalent to one on BTC/ETH.

Output
------
research/exit_policy_backtest/exit_policy_backtest.csv
research/exit_policy_backtest/exit_policy_backtest_report.txt

Credentials: same as v2 -- ALPACA_PAPER_API_KEY / ALPACA_PAPER_SECRET_KEY
(or ALPACA_API_KEY / ALPACA_SECRET_KEY fallback), loaded from a local .env
via python-dotenv if present. Missing credentials fail closed (exit 2)
before any network call is made.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent


def _load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Reuse v2's exact data fetch, indicator computation, bootstrap, and
# walk-forward block-boundary logic rather than reimplementing any of it --
# v2 itself reuses .12's wilder_atr/build_4h the same way. Loaded via
# importlib (not a plain top-level import) to match this repo's existing
# convention for cross-file reuse of research scripts.
V2 = _load_module("aura_regime_backtest_v2", "aura_regime_backtest_v2.py")

# Loaded only for the exact candidate label strings .13 recognizes, purely
# for traceability in the report header -- the actual match logic below is
# built from .12's trend_bear/bar2_positive/atr14_pct columns (already
# computed by V2.compute_regime_series), not reimported from .13.
SDE = _load_module("aura_v05313_signal_decision_engine", "aura_v05313_signal_decision_engine.py")

MSE = V2.MSE
REQUIRED_SYMBOLS = list(V2.REQUIRED_SYMBOLS)  # BTC/USD, ETH/USD
SHORTLIST_SYMBOLS = list(V2.SHORTLIST_SYMBOLS)
FROZEN_ATR_THRESHOLD_PCT = V2.FROZEN_ATR_THRESHOLD_PCT

CANDIDATES = ("FROZEN", "MIRROR")
CANDIDATE_LABEL = {"FROZEN": SDE.FROZEN_CANDIDATE, "MIRROR": SDE.MIRROR_CANDIDATE}
# Simulation-only direction mapping -- see module docstring. Has no effect
# on any live file; .23's VALIDATED_LONG_ENTRY_REGIME_LABELS /
# VALIDATED_SHORT_ENTRY_REGIME_LABELS remain frozenset().
CANDIDATE_DIRECTION = {"FROZEN": "LONG", "MIRROR": "SHORT"}

TRAIN_FRACTION = V2.TRAIN_FRACTION  # 0.70, same split convention as v2

DEFAULT_TRAILING_STOPS_PCT = (1.0, 2.0, 3.0, 5.0)
DEFAULT_TAKE_PROFITS_PCT = (2.0, 4.0, 6.0, 10.0)  # a trail-only (None) variant is added per trailing-stop level
DEFAULT_MAX_HOLD_HOURS = 240  # 10-day research safety cap; TIMEOUT rate is reported per policy
BASELINE_HORIZON_HOURS = 24  # compared against v2's fwd_24h column, already present on every entry row

# Trade-count gates -- same shape and same values as v2's episode-count
# gates, just renamed for trades (one trade per episode here).
MIN_TRAIN_TRADES_FOR_SEARCH = 3
MIN_TRAIN_TRADES_VERDICT = 8
MIN_TEST_TRADES_VERDICT = 3
MIN_TEST_TRADES_WALKFORWARD = 2
MIN_WALKFORWARD_FOLDS_CONSIDERED = 2
WALKFORWARD_MAJORITY_FRACTION = 0.5
BOOTSTRAP_P_POSITIVE_THRESHOLD = 0.85

DEFAULT_OUTDIR = ROOT / "research" / "exit_policy_backtest"
DEFAULT_LOOKBACK_DAYS = V2.DEFAULT_LOOKBACK_DAYS
DEFAULT_COST_BPS = V2.DEFAULT_COST_BPS
DEFAULT_N_BOOT = V2.DEFAULT_N_BOOT
DEFAULT_ALPHA = V2.DEFAULT_ALPHA
DEFAULT_K_FOLDS = V2.DEFAULT_K_FOLDS
DEFAULT_SEED = V2.DEFAULT_SEED


# ------------------------------------------------------------------------
# Entry (episode) detection -- FROZEN reuses v2's exact condition; MIRROR
# is the literal complement of trend and bar-2, same ATR threshold, exactly
# matching .12's own regime_state construction (trend_bear / bar2_positive
# booleans, single global FROZEN_ATR_THRESHOLD_PCT for the LOW/HIGH_OR_EQUAL
# label -- verified against aura_v05312_market_state_engine.py directly).
# ------------------------------------------------------------------------

def build_entries(g_slice: pd.DataFrame, candidate: str, atr_threshold_pct: float) -> pd.DataFrame:
    """One row per contiguous run of matching bars, taken at the run's
    first (entry) bar -- identical episode-detection shape to v2's
    build_episode_table, generalized to either candidate direction."""
    valid = g_slice[g_slice["_valid_row"]].sort_values("timestamp").reset_index(drop=True)
    if valid.empty:
        return valid
    atr_low = valid["atr14_pct"] < atr_threshold_pct
    if candidate == "FROZEN":
        match = valid["trend_bear"] & valid["bar2_positive"] & atr_low
    elif candidate == "MIRROR":
        match = (~valid["trend_bear"]) & (~valid["bar2_positive"]) & atr_low
    else:
        raise ValueError(f"unknown candidate {candidate!r}")
    is_entry = match & ~match.shift(1, fill_value=False)
    return valid[is_entry].reset_index(drop=True)


# ------------------------------------------------------------------------
# Exit-policy trade simulation.
# ------------------------------------------------------------------------

def simulate_trade(
    g_full: pd.DataFrame,
    entry_ts: pd.Timestamp,
    entry_price: float,
    direction: str,
    trailing_stop_pct: float,
    take_profit_pct: float | None,
    max_hold_bars: int,
    cost_pct: float,
) -> dict[str, Any]:
    """Walk forward bar-by-bar from just after entry_ts, applying a
    trailing stop (and optional fixed take-profit) with no look-ahead:
    each bar is checked against the stop/target implied by the running
    extreme AS OF THE END OF THE PRIOR BAR, and the running extreme is
    only updated using this bar's own high/low after that check. A same-
    bar stop+target collision resolves to the stop (conservative)."""
    # Series.searchsorted (not np.searchsorted on a raw .to_numpy() array)
    # -- tz-aware timestamp columns round-trip through numpy as an object
    # array of Timestamps, and comparing that against a bare np.datetime64
    # raises "Cannot compare tz-naive and tz-aware timestamps". Series-level
    # searchsorted compares Timestamp-to-Timestamp directly and handles tz
    # correctly.
    pos = int(g_full["timestamp"].searchsorted(entry_ts, side="right"))
    post = g_full.iloc[pos: pos + max_hold_bars]

    if post.empty:
        return {
            "exit_reason": "NO_DATA_AFTER_ENTRY",
            "bars_held": 0,
            "gross_return_pct": None,
            "net_return_pct": None,
            "mfe_pct": None,
            "mae_pct": None,
            # No future bars exist at all after this entry (end of the
            # fetched series) -- exit_ts falls back to entry_ts so this
            # trade can never block a later entry from being simulated,
            # matching the fact that there is nothing left to block.
            "exit_ts": entry_ts,
        }

    running_extreme = entry_price
    worst_excursion = entry_price
    exit_reason: str | None = None
    exit_price: float | None = None
    exit_ts: pd.Timestamp | None = None
    bars_held = 0

    for _, bar in post.iterrows():
        bars_held += 1
        if direction == "LONG":
            stop_level = running_extreme * (1.0 - trailing_stop_pct / 100.0)
            target_level = entry_price * (1.0 + take_profit_pct / 100.0) if take_profit_pct is not None else None
            stop_hit = bar["low"] <= stop_level
            target_hit = target_level is not None and bar["high"] >= target_level
        else:  # SHORT
            stop_level = running_extreme * (1.0 + trailing_stop_pct / 100.0)
            target_level = entry_price * (1.0 - take_profit_pct / 100.0) if take_profit_pct is not None else None
            stop_hit = bar["high"] >= stop_level
            target_hit = target_level is not None and bar["low"] <= target_level

        if stop_hit:
            exit_reason, exit_price, exit_ts = "STOP", stop_level, bar["timestamp"]  # conservative: stop wins a same-bar collision
        elif target_hit:
            exit_reason, exit_price, exit_ts = "TARGET", target_level, bar["timestamp"]

        if direction == "LONG":
            worst_excursion = min(worst_excursion, bar["low"])
        else:
            worst_excursion = max(worst_excursion, bar["high"])

        if exit_reason is not None:
            break

        running_extreme = max(running_extreme, bar["high"]) if direction == "LONG" else min(running_extreme, bar["low"])

    if exit_reason is None:
        exit_reason = "TIMEOUT"
        exit_price = float(post.iloc[-1]["close"])
        exit_ts = post.iloc[-1]["timestamp"]

    if direction == "LONG":
        gross_return_pct = 100.0 * (exit_price / entry_price - 1.0)
        mfe_pct = 100.0 * (running_extreme / entry_price - 1.0)
        mae_pct = 100.0 * (worst_excursion / entry_price - 1.0)
    else:
        gross_return_pct = 100.0 * (entry_price / exit_price - 1.0)
        mfe_pct = 100.0 * (entry_price / running_extreme - 1.0)
        mae_pct = 100.0 * (entry_price / worst_excursion - 1.0)

    return {
        "exit_reason": exit_reason,
        "bars_held": bars_held,
        "gross_return_pct": gross_return_pct,
        "net_return_pct": gross_return_pct - cost_pct,
        "mfe_pct": mfe_pct,
        "mae_pct": mae_pct,
        "exit_ts": exit_ts,
    }


def policy_grid(trailing_stops: tuple[float, ...], take_profits: tuple[float, ...]) -> list[tuple[float, float | None]]:
    grid: list[tuple[float, float | None]] = []
    for ts in trailing_stops:
        for tp in take_profits:
            grid.append((ts, tp))
        grid.append((ts, None))  # trail-only variant at this trailing-stop level
    return grid


def policy_id(policy: tuple[float, float | None]) -> str:
    ts, tp = policy
    return f"TS{ts:g}_TP{tp:g}" if tp is not None else f"TS{ts:g}_TRAILONLY"


def simulate_trades_for_entries(
    g_full: pd.DataFrame,
    entries: pd.DataFrame,
    direction: str,
    policy: tuple[float, float | None],
    max_hold_bars: int,
    cost_pct: float,
) -> tuple[pd.DataFrame, int]:
    """Simulate trades for a SEQUENTIAL, single-position account: an entry
    episode that fires while a trade already opened under this policy is
    still running (per this policy's own trailing-stop/take-profit/timeout
    resolution) is skipped, not stacked into a second concurrent trade --
    matching the fact that AURA (and any real account) can only hold one
    position per symbol/candidate at a time. Without this, an entry episode
    that re-fires while a long-held prior trade is still open would be
    double-counted as if two independent positions were open simultaneously,
    which inflates trade counts and makes the aggregate trade-return curve
    (trade_summary's cumulative drawdown in particular) not correspond to
    anything a real account could have produced.

    A skipped entry is dropped entirely (not deferred to a later price) --
    the next trade taken is simply the next entry episode whose timestamp is
    at/after the prior trade's exit_ts, exactly as already detected by
    build_entries. Returns (trades, n_entries_skipped_overlap) so the skip
    rate is always visible in results, never silently absorbed into a lower
    trade count."""
    ts, tp = policy
    rows: list[dict[str, Any]] = []
    skipped = 0
    blocked_until: pd.Timestamp | None = None
    for _, row in entries.sort_values("timestamp").iterrows():
        entry_ts = row["timestamp"]
        if blocked_until is not None and entry_ts < blocked_until:
            skipped += 1
            continue
        trade = simulate_trade(g_full, entry_ts, float(row["close"]), direction, ts, tp, max_hold_bars, cost_pct)
        rows.append(trade)
        blocked_until = trade.get("exit_ts", entry_ts)
    return pd.DataFrame(rows), skipped


def trade_summary(trades: pd.DataFrame) -> dict[str, Any]:
    resolved = trades[trades["net_return_pct"].notna()] if len(trades) else trades
    n = int(len(resolved))
    if n == 0:
        return {
            "n_trades": 0, "mean_net_return_pct": None, "median_net_return_pct": None,
            "win_rate": None, "profit_factor": None, "max_drawdown_pct": None,
            "timeout_rate": None, "mean_bars_held": None,
        }
    net = resolved["net_return_pct"].to_numpy(dtype=float)
    wins = net[net > 0]
    losses = net[net <= 0]
    gross_win = float(wins.sum()) if len(wins) else 0.0
    gross_loss = float(-losses.sum()) if len(losses) else 0.0
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else (float("inf") if gross_win > 0 else None)
    cum = np.cumsum(net)
    running_max = np.maximum.accumulate(cum) if n else np.array([])
    drawdown = (cum - running_max) if n else np.array([])
    max_dd = float(drawdown.min()) if n else None
    timeout_rate = float((resolved["exit_reason"] == "TIMEOUT").mean())
    return {
        "n_trades": n,
        "mean_net_return_pct": float(net.mean()),
        "median_net_return_pct": float(np.median(net)),
        "win_rate": float((net > 0).mean()),
        "profit_factor": profit_factor,
        "max_drawdown_pct": max_dd,
        "timeout_rate": timeout_rate,
        "mean_bars_held": float(resolved["bars_held"].mean()),
    }


def search_best_policy(
    g_full: pd.DataFrame,
    train_entries: pd.DataFrame,
    direction: str,
    grid: list[tuple[float, float | None]],
    max_hold_bars: int,
    cost_pct: float,
) -> tuple[tuple[float, float | None] | None, dict[str, Any] | None]:
    """TRAIN-only search: simulate every policy on TRAIN entries, pick the
    one with the best mean net trade return among policies that clear the
    minimum-trade-count bar. Mirrors v2's search_threshold_episodes."""
    if len(train_entries) < MIN_TRAIN_TRADES_FOR_SEARCH:
        return None, None

    best_policy, best_summary = None, None
    for policy in grid:
        trades, skipped = simulate_trades_for_entries(g_full, train_entries, direction, policy, max_hold_bars, cost_pct)
        summary = trade_summary(trades)
        if summary["n_trades"] < MIN_TRAIN_TRADES_FOR_SEARCH or summary["mean_net_return_pct"] is None:
            continue
        summary["entries_skipped_overlap"] = skipped
        if best_summary is None or summary["mean_net_return_pct"] > best_summary["mean_net_return_pct"]:
            best_policy, best_summary = policy, summary

    return best_policy, best_summary


def walk_forward_policy(
    g: pd.DataFrame,
    g_full: pd.DataFrame,
    valid_full: pd.DataFrame,
    candidate: str,
    direction: str,
    grid: list[tuple[float, float | None]],
    *,
    k_folds: int,
    max_hold_bars: int,
    cost_pct: float,
) -> dict[str, Any]:
    boundaries = V2.block_boundaries(valid_full, k_folds)
    folds: list[dict[str, Any]] = []

    for k in range(2, k_folds + 1):
        train_end = boundaries[k - 2]
        test_end = boundaries[k - 1]
        fold: dict[str, Any] = {"fold": k - 1, "train_end": train_end.isoformat(), "test_end": test_end.isoformat()}

        train_g = V2.slice_by_range(g, start_ts=None, end_ts=train_end)
        test_g = V2.slice_by_range(g, start_ts=train_end, end_ts=test_end)
        fold_train_entries = build_entries(train_g, candidate, FROZEN_ATR_THRESHOLD_PCT)

        chosen, chosen_summary = search_best_policy(g_full, fold_train_entries, direction, grid, max_hold_bars, cost_pct)
        if chosen is None:
            fold["status"] = "NO_ELIGIBLE_POLICY_ON_TRAIN"
            folds.append(fold)
            continue

        fold["chosen_policy"] = policy_id(chosen)
        fold["train_trades"] = chosen_summary["n_trades"]
        fold["train_entries_skipped_overlap"] = chosen_summary.get("entries_skipped_overlap")

        fold_test_entries = build_entries(test_g, candidate, FROZEN_ATR_THRESHOLD_PCT)
        test_trades, test_skipped = simulate_trades_for_entries(g_full, fold_test_entries, direction, chosen, max_hold_bars, cost_pct)
        test_summary = trade_summary(test_trades)
        fold["test_trades"] = test_summary["n_trades"]
        fold["test_entries_skipped_overlap"] = test_skipped
        fold["test_mean_net_return_pct"] = test_summary["mean_net_return_pct"]

        if test_summary["n_trades"] >= MIN_TEST_TRADES_WALKFORWARD and test_summary["mean_net_return_pct"] is not None:
            fold["status"] = "EVALUATED"
        else:
            fold["status"] = "TOO_FEW_TEST_TRADES"
        folds.append(fold)

    considered = [f for f in folds if f.get("status") == "EVALUATED"]
    positive = [f for f in considered if f["test_mean_net_return_pct"] > 0]
    return {"folds": folds, "folds_considered": len(considered), "folds_positive": len(positive)}


# ------------------------------------------------------------------------
# Per-symbol, per-candidate orchestration + verdict.
# ------------------------------------------------------------------------

def backtest_symbol_candidate(
    symbol: str,
    candidate: str,
    g: pd.DataFrame,
    valid_full: pd.DataFrame,
    train_boundary_ts: pd.Timestamp,
    *,
    cost_bps: float,
    n_boot: int,
    alpha: float,
    k_folds: int,
    max_hold_hours: int,
    grid: list[tuple[float, float | None]],
    rng: np.random.Generator,
) -> dict[str, Any]:
    direction = CANDIDATE_DIRECTION[candidate]
    cost_pct = cost_bps / 100.0
    g_full = g.sort_values("timestamp").reset_index(drop=True)

    result: dict[str, Any] = {
        "symbol": symbol,
        "candidate": candidate,
        "candidate_label": CANDIDATE_LABEL[candidate],
        "direction": direction,
        "validated_entry": symbol in REQUIRED_SYMBOLS,
    }

    train_g = V2.slice_by_range(g, end_ts=train_boundary_ts)
    test_g = V2.slice_by_range(g, start_ts=train_boundary_ts)
    train_entries = build_entries(train_g, candidate, FROZEN_ATR_THRESHOLD_PCT)
    test_entries = build_entries(test_g, candidate, FROZEN_ATR_THRESHOLD_PCT)

    chosen, chosen_train_summary = search_best_policy(g_full, train_entries, direction, grid, max_hold_hours, cost_pct)
    if chosen is None:
        result.update(verdict="INSUFFICIENT_DATA", reason="NO_POLICY_HAD_ENOUGH_TRAIN_TRADES")
        return result

    result["chosen_policy"] = policy_id(chosen)
    result["chosen_trailing_stop_pct"], result["chosen_take_profit_pct"] = chosen
    result["train_trades"] = chosen_train_summary["n_trades"]
    result["train_mean_net_return_pct"] = chosen_train_summary["mean_net_return_pct"]
    result["train_entries_skipped_overlap"] = chosen_train_summary.get("entries_skipped_overlap")

    test_trades, test_entries_skipped_overlap = simulate_trades_for_entries(g_full, test_entries, direction, chosen, max_hold_hours, cost_pct)
    test_summary = trade_summary(test_trades)
    for key, value in test_summary.items():
        result[f"test_{key}"] = value
    result["test_entries_total"] = int(len(test_entries))
    result["test_entries_skipped_overlap"] = test_entries_skipped_overlap

    baseline_col = f"fwd_{BASELINE_HORIZON_HOURS}h"
    baseline_vals = test_entries[baseline_col].dropna().to_numpy(dtype=float) - cost_pct if len(test_entries) else np.array([])
    result["test_baseline_24h_hold_mean_net_return_pct"] = float(baseline_vals.mean()) if len(baseline_vals) else None
    result["excess_vs_baseline"] = (
        result["test_mean_net_return_pct"] - result["test_baseline_24h_hold_mean_net_return_pct"]
        if result["test_mean_net_return_pct"] is not None and result["test_baseline_24h_hold_mean_net_return_pct"] is not None
        else None
    )

    test_net_vals = test_trades["net_return_pct"].dropna().to_numpy(dtype=float)
    boot = V2.bootstrap_mean_ci(test_net_vals, n_boot=n_boot, alpha=alpha, rng=rng)
    result["bootstrap_n"] = boot["n"]
    result["bootstrap_ci_low"] = boot["ci_low"]
    result["bootstrap_ci_high"] = boot["ci_high"]
    result["bootstrap_p_positive"] = boot["p_positive"]

    wf = walk_forward_policy(
        g, g_full, valid_full, candidate, direction, grid,
        k_folds=k_folds, max_hold_bars=max_hold_hours, cost_pct=cost_pct,
    )
    result["walkforward_folds_considered"] = wf["folds_considered"]
    result["walkforward_folds_positive"] = wf["folds_positive"]
    result["walkforward_fold_detail"] = json.dumps(wf["folds"])

    train_trades_n = result["train_trades"]
    test_trades_n = result["test_n_trades"]

    if (
        train_trades_n < MIN_TRAIN_TRADES_VERDICT
        or test_trades_n < MIN_TEST_TRADES_VERDICT
        or wf["folds_considered"] < MIN_WALKFORWARD_FOLDS_CONSIDERED
    ):
        result["verdict"] = "INSUFFICIENT_DATA"
        result["reason"] = (
            f"train_trades={train_trades_n}<{MIN_TRAIN_TRADES_VERDICT} or "
            f"test_trades={test_trades_n}<{MIN_TEST_TRADES_VERDICT} or "
            f"walkforward_folds_considered={wf['folds_considered']}<{MIN_WALKFORWARD_FOLDS_CONSIDERED}"
        )
        return result

    passes_direction = result["test_mean_net_return_pct"] is not None and result["test_mean_net_return_pct"] > 0
    passes_excess = result["excess_vs_baseline"] is not None and result["excess_vs_baseline"] > 0
    passes_significance = boot["p_positive"] is not None and boot["p_positive"] >= BOOTSTRAP_P_POSITIVE_THRESHOLD
    wf_fraction = (wf["folds_positive"] / wf["folds_considered"]) if wf["folds_considered"] else 0.0
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
        result["reason"] = "no_positive_significant_stable_edge_over_24h_hold_baseline"

    return result


def backtest_symbol(
    symbol: str,
    raw: pd.DataFrame,
    *,
    cost_bps: float,
    n_boot: int,
    alpha: float,
    k_folds: int,
    max_hold_hours: int,
    grid: list[tuple[float, float | None]],
    rng: np.random.Generator,
) -> list[dict[str, Any]]:
    g = V2.compute_regime_series(raw)
    if g is None:
        return [
            {
                "symbol": symbol, "candidate": c, "candidate_label": CANDIDATE_LABEL[c],
                "direction": CANDIDATE_DIRECTION[c], "validated_entry": symbol in REQUIRED_SYMBOLS,
                "verdict": "INSUFFICIENT_DATA",
                "reason": f"FEWER_THAN_{V2.MIN_1H_BARS}_USABLE_1H_BARS_OR_NO_4H_WARMUP",
            }
            for c in CANDIDATES
        ]

    valid_full = g[g["_valid_row"]].reset_index(drop=True)
    if len(valid_full) < V2.MIN_1H_BARS:
        return [
            {
                "symbol": symbol, "candidate": c, "candidate_label": CANDIDATE_LABEL[c],
                "direction": CANDIDATE_DIRECTION[c], "validated_entry": symbol in REQUIRED_SYMBOLS,
                "verdict": "INSUFFICIENT_DATA", "reason": "TOO_FEW_VALID_REGIME_BARS_AFTER_WARMUP",
            }
            for c in CANDIDATES
        ]

    split_idx = int(len(valid_full) * TRAIN_FRACTION)
    train_valid = valid_full.iloc[:split_idx]
    test_valid = valid_full.iloc[split_idx:]
    if train_valid.empty or test_valid.empty:
        return [
            {
                "symbol": symbol, "candidate": c, "candidate_label": CANDIDATE_LABEL[c],
                "direction": CANDIDATE_DIRECTION[c], "validated_entry": symbol in REQUIRED_SYMBOLS,
                "verdict": "INSUFFICIENT_DATA", "reason": "EMPTY_TRAIN_OR_TEST_SPLIT",
            }
            for c in CANDIDATES
        ]

    train_boundary_ts = train_valid["timestamp"].max()
    max_hold_bars = int(max_hold_hours)  # 1H bars, so hours == bar count

    return [
        backtest_symbol_candidate(
            symbol, candidate, g, valid_full, train_boundary_ts,
            cost_bps=cost_bps, n_boot=n_boot, alpha=alpha, k_folds=k_folds,
            max_hold_hours=max_hold_bars, grid=grid, rng=rng,
        )
        for candidate in CANDIDATES
    ]


# ------------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------------

def main() -> int:
    load_dotenv()

    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS)
    p.add_argument("--outdir", type=Path, default=DEFAULT_OUTDIR)
    p.add_argument("--symbols", nargs="*", default=None, help="Explicit symbol list. Overrides --shortlist-only and the full-universe default.")
    p.add_argument(
        "--shortlist-only", action="store_true",
        help="Scope to BTC/USD, ETH/USD, AVAX/USD (v2's shortlist) instead of the default full discovered universe.",
    )
    p.add_argument("--cost-bps", type=float, default=DEFAULT_COST_BPS)
    p.add_argument("--n-boot", type=int, default=DEFAULT_N_BOOT)
    p.add_argument("--alpha", type=float, default=DEFAULT_ALPHA, help="Bootstrap CI significance level (0.10 = 90%% CI).")
    p.add_argument("--k-folds", type=int, default=DEFAULT_K_FOLDS)
    p.add_argument("--seed", type=int, default=DEFAULT_SEED)
    p.add_argument("--trailing-stops", type=float, nargs="*", default=list(DEFAULT_TRAILING_STOPS_PCT), help="Trailing-stop levels to test, in percent.")
    p.add_argument("--take-profits", type=float, nargs="*", default=list(DEFAULT_TAKE_PROFITS_PCT), help="Fixed take-profit levels to test, in percent (a trail-only variant is always added per trailing-stop level).")
    p.add_argument("--max-hold-hours", type=int, default=DEFAULT_MAX_HOLD_HOURS, help="Research safety cap: a trade still open after this many hours is closed TIMEOUT at the last bar's close.")
    args = p.parse_args()

    key = V2.os.getenv("ALPACA_PAPER_API_KEY") or V2.os.getenv("ALPACA_API_KEY")
    secret = V2.os.getenv("ALPACA_PAPER_SECRET_KEY") or V2.os.getenv("ALPACA_SECRET_KEY")
    if not key or not secret:
        print("FAIL-CLOSED: Alpaca credentials are missing.", file=sys.stderr)
        return 2

    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret, "Accept": "application/json"}

    if args.symbols:
        symbols = sorted(set(args.symbols) | set(REQUIRED_SYMBOLS))
    elif args.shortlist_only:
        symbols = sorted(set(REQUIRED_SYMBOLS) | set(SHORTLIST_SYMBOLS))
    else:
        print("Discovering live Alpaca crypto universe (default scope for this backtest)...")
        print("NOTE: only BTC/USD and ETH/USD have a separately-validated entry signal so far.")
        print("      Every result row below is labeled validated_entry=True/False -- read an")
        print("      unvalidated-entry symbol's exit-policy result as a lead, not a conclusion.")
        symbols = sorted(set(V2.discover_universe(headers)) | set(REQUIRED_SYMBOLS))

    grid = policy_grid(tuple(args.trailing_stops), tuple(args.take_profits))
    rng = np.random.default_rng(args.seed)

    print(f"Exit-policy backtest: {len(symbols)} symbols x {len(CANDIDATES)} candidates x {len(grid)} policies.")
    print(f"cost_bps={args.cost_bps}  n_boot={args.n_boot}  alpha={args.alpha}  k_folds={args.k_folds}  "
          f"max_hold_hours={args.max_hold_hours}  seed={args.seed}")
    print("MODE: RESEARCH ONLY -- NO ORDERS")
    print()

    now = pd.Timestamp.now(tz="UTC")
    end = now.floor("h")
    start = end - pd.Timedelta(days=args.lookback_days)

    rows: list[dict[str, Any]] = []
    for i, symbol in enumerate(symbols, start=1):
        time.sleep(V2.INTER_SYMBOL_DELAY_SECONDS)
        try:
            raw = V2.fetch_history(symbol, start=start, end=end, headers=headers)
            symbol_results = backtest_symbol(
                symbol, raw, cost_bps=args.cost_bps, n_boot=args.n_boot, alpha=args.alpha,
                k_folds=args.k_folds, max_hold_hours=args.max_hold_hours, grid=grid, rng=rng,
            )
        except Exception as exc:  # noqa: BLE001 - research script, report and continue
            symbol_results = [
                {
                    "symbol": symbol, "candidate": c, "candidate_label": CANDIDATE_LABEL[c],
                    "direction": CANDIDATE_DIRECTION[c], "validated_entry": symbol in REQUIRED_SYMBOLS,
                    "verdict": "ERROR", "reason": f"{type(exc).__name__}: {exc}",
                }
                for c in CANDIDATES
            ]
        rows.extend(symbol_results)
        for r in symbol_results:
            print(f"[{i:02d}/{len(symbols)}] {symbol} {r['candidate']}: {r['verdict']} ({r.get('reason', '')})")

    df = pd.DataFrame(rows)
    args.outdir.mkdir(parents=True, exist_ok=True)
    csv_path = args.outdir / "exit_policy_backtest.csv"
    df.to_csv(csv_path, index=False)

    report_path = args.outdir / "exit_policy_backtest_report.txt"
    with report_path.open("w", encoding="utf-8") as f:
        f.write("=" * 100 + "\n")
        f.write("AURA EXIT-POLICY (TRAILING-STOP / TAKE-PROFIT) BACKTEST\n")
        f.write("RESEARCH ONLY -- NO ORDERS PLACED\n")
        f.write("=" * 100 + "\n\n")
        f.write(f"Window: {start.isoformat()} -> {end.isoformat()}\n")
        f.write(f"Symbols: {len(symbols)}\n")
        f.write(f"cost_bps={args.cost_bps}  n_boot={args.n_boot}  alpha={args.alpha}  k_folds={args.k_folds}  "
                f"max_hold_hours={args.max_hold_hours}  seed={args.seed}\n")
        f.write(f"Policy grid: trailing_stops={args.trailing_stops}  take_profits={args.take_profits}  "
                f"(+ trail-only variant per trailing-stop level)\n\n")

        for segment_name, segment_filter in (
            ("VALIDATED-ENTRY SYMBOLS (BTC/USD, ETH/USD)", lambda d: d["validated_entry"] is True),
            ("UNVALIDATED-ENTRY SYMBOLS (entry itself not separately confirmed -- read as a lead only)", lambda d: d["validated_entry"] is False),
        ):
            f.write("-" * 100 + "\n")
            f.write(f"{segment_name}\n")
            f.write("-" * 100 + "\n")
            for verdict in ("PROMISING", "WEAK", "NOT_PROMISING", "INSUFFICIENT_DATA", "ERROR"):
                subset = [r for r in rows if r.get("verdict") == verdict and segment_filter(r)]
                f.write(f"{verdict}: {len(subset)}\n")
                for row in subset:
                    extra = ""
                    if verdict in ("PROMISING", "WEAK", "NOT_PROMISING"):
                        extra = (
                            f" | policy={row.get('chosen_policy')}"
                            f" test_mean_net={row.get('test_mean_net_return_pct')}"
                            f" excess_vs_24h_hold={row.get('excess_vs_baseline')}"
                            f" win_rate={row.get('test_win_rate')}"
                            f" profit_factor={row.get('test_profit_factor')}"
                            f" max_dd={row.get('test_max_drawdown_pct')}"
                            f" bootstrap_p_positive={row.get('bootstrap_p_positive')}"
                            f" walkforward={row.get('walkforward_folds_positive')}/{row.get('walkforward_folds_considered')}"
                            f" entries_skipped_overlap={row.get('test_entries_skipped_overlap')}/{row.get('test_entries_total')}"
                        )
                    f.write(f"  {row['symbol']} {row['candidate']} ({row['direction']}): {row.get('reason', '')}{extra}\n")
                f.write("\n")

    print()
    print(f"CSV:    {csv_path.resolve()}")
    print(f"REPORT: {report_path.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
