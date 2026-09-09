"""
EXIT SENSITIVITY / MFE MONETIZATION TEST
========================================

Purpose
-------
Test whether a single pre-declared early post-entry confirmation/rejection rule
can improve the CURRENT strategy without changing the entry inventory.

This is deliberately a post-entry decision experiment. It first runs the existing
strategy exactly as the current permutation baseline does, records the exact
entry inventory (symbol, window, timestamp, side, entry price, ATR-derived
stop, quantity), and then replays THOSE SAME ENTRIES through several
pre-declared exit models.

Therefore:
  - no RSI/MACD/EMA/ADX tuning
  - no new entry signals
  - no NQ/BTC regime filter
  - no universe changes
  - no parameter optimizer
  - same entry count and entry timestamps for CURRENT and candidate

Experiment models
-----------------
CURRENT          : exact existing exit architecture.
EARLY_CONFIRM_3C : after the third completed candle, keep the trade only if
                   its close is favorable relative to entry (positive R).
                   If not, exit at the third candle close. The original ATR
                   stop remains active before that decision.

The rule is fixed at 3 candles and zero-R confirmation BEFORE results are seen.
No other confirmation variables or thresholds are tested in this experiment.

Important execution convention for NEW static-target models:
  - intrabar target/stop detection uses candle high/low
  - if target AND stop are both inside the same candle, STOP WINS
    (conservative, same convention used by the permutation methodology)

The CURRENT model is intentionally reproduced as closely as possible from
backtester.py: its partial TP is triggered from candle CLOSE, not high/low,
and its trail is updated from CLOSE. This means CURRENT is the actual
baseline, while the alternative models are explicit alternative execution
rules.

Outputs
-------
  backtest_results/exit_sensitivity_summary_<stamp>.csv
  backtest_results/exit_sensitivity_trades_<stamp>.csv

The trade-level file contains MFE/MAE, realized R, MFE capture, time to
milestones and exit reason, making it possible to inspect whether the current
strategy reaches +1R/+1.5R/+2R and subsequently gives the move back.

Usage
-----
  python early_confirmation_test.py --days 720 --research-days 480
  python exit_sensitivity_test.py --days 720 --symbols "BTC/USDT:USDT" "ETH/USDT:USDT"

The default universe is the same DEFAULT_SYMBOLS used by permutation_test.py.
No random permutations are performed here, so this should be much faster than
the 200-run permutation experiment.
"""

import argparse
import os
import time
from dataclasses import dataclass
from datetime import timedelta

import numpy as np
import pandas as pd

import config as cfg
from core import strategy as strat
from core.risk_manager import CircuitBreaker, calc_position_plan
from core import trade_metrics
from param_sweep import fetch_all_data, fetch_all_funding
from permutation_test import build_windows, _warmup_bars, run_real
from run_broad_backtest import DEFAULT_SYMBOLS


MODELS = [
    "CURRENT",
    "EARLY_CONFIRM_3C",
]


@dataclass
class Entry:
    symbol: str
    window: int
    entry_time: pd.Timestamp
    side: str
    entry_price: float
    stop_price: float
    quantity: float
    risk_amount: float
    leverage: float
    atr: float


def _safe_float(v, default=0.0):
    try:
        x = float(v)
        return x if np.isfinite(x) else default
    except Exception:
        return default


def _risk_per_unit(entry: Entry) -> float:
    return abs(entry.entry_price - entry.stop_price)


def _r_from_price(entry: Entry, price: float) -> float:
    d = _risk_per_unit(entry)
    if d <= 0:
        return 0.0
    if entry.side == "long":
        return (price - entry.entry_price) / d
    return (entry.entry_price - price) / d


def _high_r_low_r(entry: Entry, row):
    d = _risk_per_unit(entry)
    if d <= 0:
        return 0.0, 0.0
    if entry.side == "long":
        return (row["high"] - entry.entry_price) / d, (row["low"] - entry.entry_price) / d
    return (entry.entry_price - row["low"]) / d, (entry.entry_price - row["high"]) / d


def _exit_cost(price, qty):
    fee = price * qty * cfg.BACKTEST_TAKER_FEE_PCT
    slippage = price * qty * cfg.BACKTEST_SLIPPAGE_PCT
    return fee + slippage


def _pnl(entry: Entry, price: float, qty: float) -> float:
    gross = ((price - entry.entry_price) * qty
             if entry.side == "long"
             else (entry.entry_price - price) * qty)
    return gross - _exit_cost(price, qty)


def _funding_pnl(entry: Entry, price: float, rate: float, intervals: int, qty: float) -> float:
    notional = qty * price
    sign = -1.0 if entry.side == "long" else 1.0
    return sign * rate * notional * intervals


def _funding_rate_at(funding_df, ts):
    if funding_df is None or funding_df.empty:
        return cfg.FUNDING_RATE_FALLBACK
    prior = funding_df[funding_df["timestamp"] <= ts]
    if prior.empty:
        return cfg.FUNDING_RATE_FALLBACK
    return _safe_float(prior.iloc[-1]["funding_rate"], cfg.FUNDING_RATE_FALLBACK)


def _make_entry_inventory(data_cache, funding_cache, windows):
    """Run the EXISTING baseline and turn its entry events into immutable
    entry instructions for the exit-only replay."""
    baseline_log, _schedules = run_real(data_cache, funding_cache, windows)
    if baseline_log.empty:
        return [], baseline_log

    entries = []
    for symbol, df in data_cache.items():
        for w, (t0, t1) in enumerate(windows):
            seg = df[(df["timestamp"] >= t0) & (df["timestamp"] < t1)].reset_index(drop=True)
            if len(seg) < _warmup_bars() + 30:
                continue

            window_entries = baseline_log[
                (baseline_log["symbol"] == symbol)
                & (baseline_log["type"] == "entry")
                & (baseline_log["time"] >= t0)
                & (baseline_log["time"] < t1)
            ].copy()

            for _, ev in window_entries.iterrows():
                ts = pd.Timestamp(ev["time"])
                matches = np.where(seg["timestamp"].values == ts.to_datetime64())[0]
                if len(matches) == 0:
                    continue
                i = int(matches[0])
                row = seg.iloc[i]
                equity_at_entry = _safe_float(ev.get("equity"), cfg.BACKTEST_STARTING_EQUITY)
                side = str(ev.get("side", "long"))
                price = _safe_float(ev.get("price"), row["close"])
                atr = _safe_float(row.get("atr"), np.nan)
                plan = calc_position_plan(price, atr, side, equity_at_entry)
                if plan is None:
                    continue
                entries.append(Entry(
                    symbol=symbol,
                    window=w,
                    entry_time=ts,
                    side=side,
                    entry_price=price,
                    stop_price=plan.stop_price,
                    quantity=plan.quantity,
                    risk_amount=plan.risk_amount_usdt,
                    leverage=plan.leverage,
                    atr=atr,
                ))

    entries.sort(key=lambda e: (e.entry_time, e.symbol, e.window))
    return entries, baseline_log


def _simulate_current(entry, seg, entry_idx, funding_df):
    """Close reproduction of current backtester exit logic for ONE fixed entry."""
    stop = entry.stop_price
    remaining = entry.quantity
    partial = False
    candles = 0
    last_funding_ts = entry.entry_time
    total_pnl = 0.0
    mfe = 0.0
    mae = 0.0
    time_to_mfe = None
    hit_times = {0.5: None, 1.0: None, 1.5: None, 2.0: None, 3.0: None}

    for j in range(entry_idx + 1, len(seg)):
        row = seg.iloc[j]
        candles += 1
        ts = pd.Timestamp(row["timestamp"])

        if cfg.MODEL_FUNDING_COSTS:
            elapsed_h = (ts - last_funding_ts).total_seconds() / 3600.0
            if elapsed_h >= cfg.FUNDING_INTERVAL_HOURS:
                intervals = int(elapsed_h // cfg.FUNDING_INTERVAL_HOURS)
                rate = _funding_rate_at(funding_df, ts)
                total_pnl += _funding_pnl(entry, row["close"], rate, intervals, remaining)
                last_funding_ts = ts

        high_r, low_r = _high_r_low_r(entry, row)
        if high_r > mfe:
            mfe = high_r
            time_to_mfe = (ts - entry.entry_time).total_seconds() / 3600.0
        mae = min(mae, low_r)
        for t in hit_times:
            if high_r >= t and hit_times[t] is None:
                hit_times[t] = (ts - entry.entry_time).total_seconds() / 3600.0

        # Existing engine: stop checked first, using intrabar high/low.
        hit_stop = row["low"] <= stop if entry.side == "long" else row["high"] >= stop
        if hit_stop:
            total_pnl += _pnl(entry, stop, remaining)
            return _trade_result(entry, ts, total_pnl, mfe, mae, candles,
                                 "stop", time_to_mfe, hit_times)

        # Existing engine: partial TP is based on CLOSE, not candle high/low.
        if not partial:
            r_close = _r_from_price(entry, row["close"])
            if r_close >= cfg.TAKE_PROFIT_R_MULT_PARTIAL:
                partial_qty = entry.quantity * cfg.PARTIAL_CLOSE_PCT
                fee = row["close"] * partial_qty * cfg.BACKTEST_TAKER_FEE_PCT
                gross = ((row["close"] - entry.entry_price) * partial_qty
                         if entry.side == "long"
                         else (entry.entry_price - row["close"]) * partial_qty)
                total_pnl += gross - fee
                remaining -= partial_qty
                partial = True
                stop = entry.entry_price

        if partial:
            trail_dist = _safe_float(row["atr"]) * cfg.TRAIL_ATR_MULT
            if trail_dist > 0:
                if entry.side == "long":
                    stop = max(stop, row["close"] - trail_dist)
                else:
                    stop = min(stop, row["close"] + trail_dist)

        if candles >= cfg.TIME_STOP_CANDLES and _r_from_price(entry, row["close"]) <= 0:
            total_pnl += _pnl(entry, row["close"], remaining)
            return _trade_result(entry, ts, total_pnl, mfe, mae, candles,
                                 "time_stop", time_to_mfe, hit_times)

    return _trade_result(entry, pd.Timestamp(seg.iloc[-1]["timestamp"]), total_pnl,
                         mfe, mae, candles, "end_of_data", time_to_mfe, hit_times)


def _simulate_static_tp(entry, seg, entry_idx, target_r, funding_df):
    stop = entry.stop_price
    last_funding_ts = entry.entry_time
    total_pnl = 0.0
    candles = 0
    mfe = 0.0
    mae = 0.0
    time_to_mfe = None
    hit_times = {0.5: None, 1.0: None, 1.5: None, 2.0: None, 3.0: None}

    for j in range(entry_idx + 1, len(seg)):
        row = seg.iloc[j]
        ts = pd.Timestamp(row["timestamp"])
        candles += 1

        if cfg.MODEL_FUNDING_COSTS:
            elapsed_h = (ts - last_funding_ts).total_seconds() / 3600.0
            if elapsed_h >= cfg.FUNDING_INTERVAL_HOURS:
                intervals = int(elapsed_h // cfg.FUNDING_INTERVAL_HOURS)
                rate = _funding_rate_at(funding_df, ts)
                total_pnl += _funding_pnl(entry, row["close"], rate, intervals, entry.quantity)
                last_funding_ts = ts

        high_r, low_r = _high_r_low_r(entry, row)
        mfe = max(mfe, high_r)
        if high_r >= mfe:
            time_to_mfe = (ts - entry.entry_time).total_seconds() / 3600.0
        mae = min(mae, low_r)
        for t in hit_times:
            if high_r >= t and hit_times[t] is None:
                hit_times[t] = (ts - entry.entry_time).total_seconds() / 3600.0

        # Conservative stop-first if both target and stop occur in one candle.
        stop_hit = low_r <= -1.0
        target_hit = high_r >= target_r
        if stop_hit:
            total_pnl += _pnl(entry, stop, entry.quantity)
            return _trade_result(entry, ts, total_pnl, mfe, mae, candles,
                                 "stop", time_to_mfe, hit_times)
        if target_hit:
            target_price = entry.entry_price + target_r * _risk_per_unit(entry) \
                if entry.side == "long" else entry.entry_price - target_r * _risk_per_unit(entry)
            total_pnl += _pnl(entry, target_price, entry.quantity)
            return _trade_result(entry, ts, total_pnl, mfe, mae, candles,
                                 f"tp_{target_r:g}R", time_to_mfe, hit_times)

    return _trade_result(entry, pd.Timestamp(seg.iloc[-1]["timestamp"]), total_pnl,
                         mfe, mae, candles, "end_of_data", time_to_mfe, hit_times)


def _simulate_be_trail(entry, seg, entry_idx, funding_df, trigger_r=1.0, partial_r=None):
    stop = entry.stop_price
    remaining = entry.quantity
    partial = False
    armed = False
    last_funding_ts = entry.entry_time
    total_pnl = 0.0
    candles = 0
    mfe = 0.0
    mae = 0.0
    time_to_mfe = None
    hit_times = {0.5: None, 1.0: None, 1.5: None, 2.0: None, 3.0: None}

    for j in range(entry_idx + 1, len(seg)):
        row = seg.iloc[j]
        ts = pd.Timestamp(row["timestamp"])
        candles += 1

        if cfg.MODEL_FUNDING_COSTS:
            elapsed_h = (ts - last_funding_ts).total_seconds() / 3600.0
            if elapsed_h >= cfg.FUNDING_INTERVAL_HOURS:
                intervals = int(elapsed_h // cfg.FUNDING_INTERVAL_HOURS)
                rate = _funding_rate_at(funding_df, ts)
                total_pnl += _funding_pnl(entry, row["close"], rate, intervals, remaining)
                last_funding_ts = ts

        high_r, low_r = _high_r_low_r(entry, row)
        if high_r > mfe:
            mfe = high_r
            time_to_mfe = (ts - entry.entry_time).total_seconds() / 3600.0
        mae = min(mae, low_r)
        for t in hit_times:
            if high_r >= t and hit_times[t] is None:
                hit_times[t] = (ts - entry.entry_time).total_seconds() / 3600.0

        # Stop-first. If the stop is still the original stop and the candle also
        # reaches the trigger, the conservative assumption is that the stop hit
        # before we had evidence to move it.
        stop_hit = row["low"] <= stop if entry.side == "long" else row["high"] >= stop
        if stop_hit:
            total_pnl += _pnl(entry, stop, remaining)
            return _trade_result(entry, ts, total_pnl, mfe, mae, candles,
                                 "stop", time_to_mfe, hit_times)

        # Trigger is based on intrabar excursion. Once armed, move stop to BE.
        if not armed and high_r >= trigger_r:
            if partial_r is not None and high_r >= partial_r:
                partial_qty = entry.quantity * cfg.PARTIAL_CLOSE_PCT
                target_price = entry.entry_price + partial_r * _risk_per_unit(entry) \
                    if entry.side == "long" else entry.entry_price - partial_r * _risk_per_unit(entry)
                total_pnl += _pnl(entry, target_price, partial_qty)
                remaining -= partial_qty
                partial = True
            armed = True
            stop = entry.entry_price

        if partial_r is not None and not partial and high_r >= partial_r:
            partial_qty = entry.quantity * cfg.PARTIAL_CLOSE_PCT
            target_price = entry.entry_price + partial_r * _risk_per_unit(entry) \
                if entry.side == "long" else entry.entry_price - partial_r * _risk_per_unit(entry)
            total_pnl += _pnl(entry, target_price, partial_qty)
            remaining -= partial_qty
            partial = True
            armed = True
            stop = entry.entry_price

        if armed:
            trail_dist = _safe_float(row["atr"]) * cfg.TRAIL_ATR_MULT
            if trail_dist > 0:
                if entry.side == "long":
                    stop = max(stop, row["close"] - trail_dist)
                else:
                    stop = min(stop, row["close"] + trail_dist)

        if candles >= cfg.TIME_STOP_CANDLES and _r_from_price(entry, row["close"]) <= 0:
            total_pnl += _pnl(entry, row["close"], remaining)
            return _trade_result(entry, ts, total_pnl, mfe, mae, candles,
                                 "time_stop", time_to_mfe, hit_times)

    return _trade_result(entry, pd.Timestamp(seg.iloc[-1]["timestamp"]), total_pnl,
                         mfe, mae, candles, "end_of_data", time_to_mfe, hit_times)


def _trade_result(entry, exit_time, total_pnl, mfe, mae, candles, exit_reason,
                  time_to_mfe, hit_times):
    risk = max(entry.risk_amount, 1e-12)
    realized_r = total_pnl / risk
    return {
        "symbol": entry.symbol,
        "window": entry.window,
        "entry_time": entry.entry_time,
        "exit_time": exit_time,
        "side": entry.side,
        "entry_price": entry.entry_price,
        "initial_stop": entry.stop_price,
        "risk_amount": entry.risk_amount,
        "realized_pnl": total_pnl,
        "realized_r": realized_r,
        "mfe_r": mfe,
        "mae_r": mae,
        "mfe_capture_pct": (realized_r / mfe * 100.0) if mfe > 0 else np.nan,
        "time_to_mfe_hours": time_to_mfe,
        "candles_held": candles,
        "exit_reason": exit_reason,
        "hit_0.5R_hours": hit_times[0.5],
        "hit_1.0R_hours": hit_times[1.0],
        "hit_1.5R_hours": hit_times[1.5],
        "hit_2.0R_hours": hit_times[2.0],
        "hit_3.0R_hours": hit_times[3.0],
    }


def _simulate_early_confirm_3c(entry, seg, entry_idx, funding_df):
    """Final clean-room candidate: keep the exact frozen entry, but require
    directional confirmation by the CLOSE of the third completed candle.

    Rule is deliberately fixed and non-optimized:
      LONG  -> close at/after candle 3 must be strictly above entry price
      SHORT -> close at/after candle 3 must be strictly below entry price

    If confirmation is absent, exit at the third candle CLOSE. The normal
    initial ATR stop remains active throughout. If the stop is hit first,
    the stop wins. No RSI/MACD/ADX/NQ/BTC/volume information is used.
    """
    stop = entry.stop_price
    last_funding_ts = entry.entry_time
    total_pnl = 0.0
    candles = 0
    mfe = 0.0
    mae = 0.0
    time_to_mfe = None
    hit_times = {0.5: None, 1.0: None, 1.5: None, 2.0: None, 3.0: None}

    for j in range(entry_idx + 1, len(seg)):
        row = seg.iloc[j]
        ts = pd.Timestamp(row["timestamp"])
        candles += 1

        if cfg.MODEL_FUNDING_COSTS:
            elapsed_h = (ts - last_funding_ts).total_seconds() / 3600.0
            if elapsed_h >= cfg.FUNDING_INTERVAL_HOURS:
                intervals = int(elapsed_h // cfg.FUNDING_INTERVAL_HOURS)
                rate = _funding_rate_at(funding_df, ts)
                total_pnl += _funding_pnl(entry, row["close"], rate, intervals, entry.quantity)
                last_funding_ts = ts

        high_r, low_r = _high_r_low_r(entry, row)
        if high_r > mfe:
            mfe = high_r
            time_to_mfe = (ts - entry.entry_time).total_seconds() / 3600.0
        mae = min(mae, low_r)
        for t in hit_times:
            if high_r >= t and hit_times[t] is None:
                hit_times[t] = (ts - entry.entry_time).total_seconds() / 3600.0

        # Initial stop remains active. Conservative stop-first convention.
        stop_hit = row["low"] <= stop if entry.side == "long" else row["high"] >= stop
        if stop_hit:
            total_pnl += _pnl(entry, stop, entry.quantity)
            result = _trade_result(entry, ts, total_pnl, mfe, mae, candles,
                                   "stop", time_to_mfe, hit_times)
            result["confirmation_candle"] = candles
            result["confirmation_close_r"] = _r_from_price(entry, row["close"])
            result["confirmation_passed"] = False
            return result

        # Make the decision only after the third candle has CLOSED.
        if candles >= 3:
            close_r = _r_from_price(entry, row["close"])
            passed = close_r > 0.0
            # For shorts, _r_from_price already converts a favorable move to +R.
            if not passed:
                total_pnl += _pnl(entry, row["close"], entry.quantity)
                result = _trade_result(entry, ts, total_pnl, mfe, mae, candles,
                                       "early_reject_3c", time_to_mfe, hit_times)
                result["confirmation_candle"] = candles
                result["confirmation_close_r"] = close_r
                result["confirmation_passed"] = False
                return result
            # Confirmation has passed; continue with the EXISTING CURRENT exit
            # architecture from this point, without changing the entry.
            break

    # If confirmation passed, replay the remaining candles with the CURRENT
    # exit logic, but starting from the third candle and carrying the measured
    # excursion/funding state forward. This keeps the candidate's only change
    # the early rejection decision.
    if candles >= 3:
        # Re-run CURRENT from the entry but prevent early rejection: this is
        # intentionally simple and exact, because the first three candles are
        # already part of the normal CURRENT path. To avoid double-counting
        # fees/funding, use the CURRENT simulation directly and tag the result.
        third_row = seg.iloc[entry_idx + 3]
        third_close_r = _r_from_price(entry, third_row["close"])
        result = _simulate_current(entry, seg, entry_idx, funding_df)
        result["confirmation_candle"] = 3
        result["confirmation_close_r"] = third_close_r
        third_ts = pd.Timestamp(third_row["timestamp"])
        stopped_before_confirmation = pd.Timestamp(result["exit_time"]) < third_ts
        result["confirmation_passed"] = False if stopped_before_confirmation else True
        if not stopped_before_confirmation:
            result["exit_reason"] = "confirmed_3c_" + str(result.get("exit_reason", "unknown"))
        return result

    return _trade_result(entry, pd.Timestamp(seg.iloc[-1]["timestamp"]), total_pnl,
                         mfe, mae, candles, "end_of_data", time_to_mfe, hit_times)


def simulate_model(model, entry, seg, entry_idx, funding_df):
    if model == "CURRENT":
        return _simulate_current(entry, seg, entry_idx, funding_df)
    if model == "EARLY_CONFIRM_3C":
        return _simulate_early_confirm_3c(entry, seg, entry_idx, funding_df)
    if model == "TP_1R":
        return _simulate_static_tp(entry, seg, entry_idx, 1.0, funding_df)
    if model == "TP_1_5R":
        return _simulate_static_tp(entry, seg, entry_idx, 1.5, funding_df)
    if model == "TP_2R":
        return _simulate_static_tp(entry, seg, entry_idx, 2.0, funding_df)
    if model == "TP_3R":
        return _simulate_static_tp(entry, seg, entry_idx, 3.0, funding_df)
    if model == "BE_1R_TRAIL":
        return _simulate_be_trail(entry, seg, entry_idx, funding_df, trigger_r=1.0, partial_r=None)
    if model == "PARTIAL_1_5R":
        return _simulate_be_trail(entry, seg, entry_idx, funding_df, trigger_r=1.0, partial_r=1.5)
    raise ValueError(f"Unknown exit model: {model}")


def summarize_trades(df):
    if df.empty:
        return {
            "num_trades": 0, "win_rate_pct": 0.0, "avg_win": 0.0,
            "avg_loss": 0.0, "profit_factor": 0.0, "total_pnl": 0.0,
            "expectancy": 0.0, "total_return_pct": 0.0, "avg_realized_r": 0.0, "mfe_capture_winners_pct": 0.0, "pct_reached_1R": 0.0,
            "avg_mfe_r": 0.0, "avg_mae_r": 0.0, "avg_candles_held": 0.0,
            "mfe_capture_pct": 0.0,
        }
    wins = df[df.realized_pnl > 0]
    losses = df[df.realized_pnl <= 0]
    gp = wins.realized_pnl.sum()
    gl = abs(losses.realized_pnl.sum())
    starting = cfg.BACKTEST_STARTING_EQUITY * max(df["symbol"].nunique(), 1)
    return {
        "num_trades": len(df),
        "win_rate_pct": len(wins) / len(df) * 100,
        "avg_win": wins.realized_pnl.mean() if len(wins) else 0.0,
        "avg_loss": losses.realized_pnl.mean() if len(losses) else 0.0,
        "profit_factor": gp / gl if gl > 0 else np.inf,
        "total_pnl": df.realized_pnl.sum(),
        "expectancy": df.realized_pnl.mean(),
        "total_return_pct": df.realized_pnl.sum() / starting * 100,
        "avg_realized_r": df.realized_r.mean(),
        "avg_mfe_r": df.mfe_r.mean(),
        "avg_mae_r": df.mae_r.mean(),
        "avg_candles_held": df.candles_held.mean(),
        # MFE capture is only meaningful where a trade actually went favourable.
        # On a trade that goes straight to the stop, MFE ~ 0 and realized/MFE
        # explodes, so a naive mean over all trades is garbage. Restrict to
        # trades that reached >= 0.5R favourable and use the MEDIAN, which is
        # robust to the remaining tail.
        "mfe_capture_pct": _median_capture(df, min_mfe_r=0.5),
        "mfe_capture_winners_pct": _median_capture(df[df.realized_pnl > 0], min_mfe_r=0.5),
        "pct_reached_1R": (df.mfe_r >= 1.0).mean() * 100 if len(df) else 0.0,
    }


def _median_capture(df, min_mfe_r=0.5):
    """Median realized_R / MFE_R over trades that reached min_mfe_r favourable."""
    if df is None or df.empty:
        return 0.0
    sub = df[pd.to_numeric(df["mfe_r"], errors="coerce") >= min_mfe_r]
    if sub.empty:
        return 0.0
    ratio = pd.to_numeric(sub["realized_r"], errors="coerce") / \
        pd.to_numeric(sub["mfe_r"], errors="coerce")
    ratio = ratio.replace([np.inf, -np.inf], np.nan).dropna()
    return float(ratio.median() * 100) if len(ratio) else 0.0


def diagnostic_matrix(df):
    rows = []
    for threshold in [0.5, 1.0, 1.5, 2.0, 3.0]:
        hit_col = f"hit_{threshold:.1f}R_hours"
        reached = df[df["mfe_r"] >= threshold]
        count = len(reached)
        roundtrip = reached[reached["realized_pnl"] <= 0]
        rows.append({
            "MFE_threshold_R": threshold,
            "trades_reached": count,
            "pct_total": count / len(df) * 100 if len(df) else 0,
            "pct_roundtripped_to_loss_or_flat": len(roundtrip) / count * 100 if count else 0,
            "median_time_to_threshold_hours": pd.to_numeric(reached[hit_col], errors="coerce").median() if count else np.nan,
        })
    return pd.DataFrame(rows)


def _run_models_on_period(models, data_cache, funding_cache, windows, label):
    """Build the frozen entry inventory for this period and replay it through
    each exit model. Returns (summary_df, list_of_trade_dfs)."""
    print(f"\nBuilding immutable baseline entry inventory for {label}...")
    entries, _baseline_log = _make_entry_inventory(data_cache, funding_cache, windows)
    print(f"{label} inventory: {len(entries)} entries")

    seg_cache = {}
    for symbol, df in data_cache.items():
        for w, (t0, t1) in enumerate(windows):
            seg = df[(df["timestamp"] >= t0) & (df["timestamp"] < t1)].reset_index(drop=True)
            if len(seg):
                seg_cache[(symbol, w)] = seg

    all_results = []
    for model in models:
        print(f"\n[{label}] exit model: {model}")
        rows = []
        for entry in entries:
            seg = seg_cache.get((entry.symbol, entry.window))
            if seg is None:
                continue
            idxs = np.where(seg["timestamp"].values == entry.entry_time.to_datetime64())[0]
            if len(idxs) == 0:
                continue
            result = simulate_model(model, entry, seg, int(idxs[0]), funding_cache.get(entry.symbol))
            result["exit_model"] = model
            result["period"] = label
            rows.append(result)
        model_df = pd.DataFrame(rows)
        all_results.append(model_df)
        m = summarize_trades(model_df)
        print(f"  trades {m['num_trades']} | win {m['win_rate_pct']:.1f}% | "
              f"PF {m['profit_factor']:.3f} | expectancy ${m['expectancy']:.3f} | "
              f"avg R {m['avg_realized_r']:.3f} | MFE capture {m['mfe_capture_pct']:.1f}%")

    summary_rows = []
    for df in all_results:
        if df.empty:
            continue
        m = summarize_trades(df)
        row = {"period": label, "exit_model": df.iloc[0]["exit_model"], **m}
        for reason, count in df["exit_reason"].value_counts().items():
            row[f"exit_{reason}_count"] = int(count)
        summary_rows.append(row)
    return pd.DataFrame(summary_rows), all_results


def main():
    parser = argparse.ArgumentParser(description="Exit-only sensitivity test using the exact baseline entry inventory")
    parser.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS)
    parser.add_argument("--days", type=int, default=720)
    parser.add_argument("--research-days", type=int, default=480,
                         help="chronological research period; the remainder is an UNTOUCHED holdout")
    parser.add_argument("--windows", type=int, default=8,
                         help="windows within the RESEARCH period (holdout is split proportionally)")
    parser.add_argument("--timeframe", default=None)
    parser.add_argument("--models", nargs="+", choices=MODELS, default=MODELS)
    parser.add_argument("--no-holdout", action="store_true",
                         help="run all models over the whole period with no holdout (NOT recommended)")
    args = parser.parse_args()

    timeframe = args.timeframe or cfg.TIMEFRAME
    print("=" * 78)
    print("EXIT-ONLY SENSITIVITY TEST")
    print("=" * 78)
    print(f"Timeframe: {timeframe} | Days: {args.days} | Windows: {args.windows}")
    print(f"Strategy: {cfg.STRATEGY_MODE} | ADX: {cfg.USE_ADX_FILTER} | Funding: {cfg.MODEL_FUNDING_COSTS}")
    print(f"Entry parameters are FROZEN. Models: {', '.join(args.models)}")
    print("No entry optimization. No new signals. Same baseline entry inventory for every model.")

    data_cache = fetch_all_data(args.symbols, args.days, timeframe)
    if not data_cache:
        raise SystemExit("No usable historical data fetched.")
    funding_cache = fetch_all_funding(list(data_cache.keys()), args.days)
    data_cache = {s: strat.add_indicators(df) for s, df in data_cache.items()}

    # ---- strict chronological split -------------------------------------
    all_start = min(df["timestamp"].min() for df in data_cache.values())
    all_end = max(df["timestamp"].max() for df in data_cache.values())
    span_days = (all_end - all_start).total_seconds() / 86400.0
    research_days = min(args.research_days, args.days)
    split_ts = all_start + timedelta(days=span_days * (research_days / args.days))

    if args.no_holdout:
        print("\n⚠ RUNNING WITHOUT A HOLDOUT — every model sees all data. "
              "The best of N models will look good by chance alone.")
        research_cache = data_cache
        holdout_cache = {}
    else:
        research_cache = {s: df[df["timestamp"] < split_ts].reset_index(drop=True)
                          for s, df in data_cache.items()}
        holdout_cache = {s: df[df["timestamp"] >= split_ts].reset_index(drop=True)
                         for s, df in data_cache.items()}
        research_cache = {s: d for s, d in research_cache.items() if len(d) > _warmup_bars() + 60}
        holdout_cache = {s: d for s, d in holdout_cache.items() if len(d) > _warmup_bars() + 60}

    print("\n" + "=" * 78)
    print("EXPERIMENT PROTOCOL (declared BEFORE any results are seen)")
    print("=" * 78)
    print(f"Full data span : {all_start.date()} .. {all_end.date()}")
    if not args.no_holdout:
        print(f"RESEARCH period: {all_start.date()} .. {split_ts.date()}  "
              f"({research_days}d) — all {len(args.models)} models run here")
        print(f"HOLDOUT period : {split_ts.date()} .. {all_end.date()}  "
              f"({args.days - research_days}d) — UNTOUCHED until the fixed comparison is run")
    print("Hypothesis: after 3 completed candles, reject trades whose close is not favorable.")
    print("The rule is fixed; no model selection or parameter tuning is performed.")
    print("The holdout must NOT be used to modify the 3-candle rule or anything else.")

    research_windows = build_windows(research_cache, args.windows)
    research_summary, research_trades = _run_models_on_period(
        args.models, research_cache, funding_cache, research_windows, "RESEARCH")

    print("\n" + "=" * 78)
    print("RESEARCH RESULTS — CURRENT vs PRE-DECLARED EARLY_CONFIRM_3C")
    print("=" * 78)
    cols = ["exit_model", "num_trades", "win_rate_pct", "profit_factor", "expectancy",
            "total_pnl", "avg_realized_r", "avg_mfe_r", "avg_mae_r",
            "avg_candles_held", "mfe_capture_pct", "mfe_capture_winners_pct", "pct_reached_1R"]
    if not research_summary.empty:
        print(research_summary[[c for c in cols if c in research_summary.columns]]
              .round(3).to_string(index=False))

    # MFE monetization diagnostic from the CURRENT model on research data.
    cur_df = next((d for d in research_trades
                   if not d.empty and d.iloc[0]["exit_model"] == "CURRENT"), None)
    if cur_df is not None:
        print("\nMFE monetization matrix (CURRENT model, RESEARCH period):")
        print(diagnostic_matrix(cur_df).to_string(index=False))
        print("  'pct_roundtripped_to_loss_or_flat' is the key number: trades that reached")
        print("  the threshold in profit and still finished at or below breakeven.")

    # ---- evaluate BOTH models on the untouched holdout ---------------------
    # There is deliberately NO model selection here. The hypothesis is fixed:
    # does the 3-candle confirmation rule beat CURRENT out-of-sample?
    holdout_summary = pd.DataFrame()
    holdout_trades = []
    if not args.no_holdout and holdout_cache:
        holdout_windows = build_windows(holdout_cache, max(2, args.windows // 2))
        holdout_summary, holdout_trades = _run_models_on_period(
            args.models, holdout_cache, funding_cache, holdout_windows, "HOLDOUT")

        print("\n" + "=" * 78)
        print("HOLDOUT RESULT — PRE-DECLARED CANDIDATE VS CURRENT")
        print("=" * 78)
        if not holdout_summary.empty:
            print(holdout_summary[[c for c in cols if c in holdout_summary.columns]]
                  .round(3).to_string(index=False))

        def _get(df, model, field):
            r = df[df["exit_model"] == model]
            return float(r.iloc[0][field]) if len(r) else float("nan")

        for field in ["profit_factor", "expectancy", "avg_candles_held"]:
            cur = _get(holdout_summary, "CURRENT", field)
            cand = _get(holdout_summary, "EARLY_CONFIRM_3C", field)
            print(f"Holdout {field}: EARLY_CONFIRM_3C - CURRENT = {cand - cur:+.3f}")

        cur_pf = _get(holdout_summary, "CURRENT", "profit_factor")
        cand_pf = _get(holdout_summary, "EARLY_CONFIRM_3C", "profit_factor")
        cur_exp = _get(holdout_summary, "CURRENT", "expectancy")
        cand_exp = _get(holdout_summary, "EARLY_CONFIRM_3C", "expectancy")
        print("\nInterpretation:")
        print("  - The 3-candle rule was fixed before seeing the results.")
        print("  - HOLDOUT is the decisive comparison; do not tune the rule from it.")
        print(f"  - Candidate PF vs CURRENT: {cand_pf:.3f} vs {cur_pf:.3f}")
        print(f"  - Candidate expectancy vs CURRENT: {cand_exp:.3f}R vs {cur_exp:.3f}R")
        print("  - If the candidate is not clearly better on HOLDOUT, stop this line of research.")
    elif args.no_holdout:
        print("\nNo holdout was run. These numbers are research/in-sample only.")

    trades_all = [d for d in research_trades if not d.empty]
    if not args.no_holdout and holdout_cache:
        trades_all += [d for d in holdout_trades if not d.empty]
    trades_df = pd.concat(trades_all, ignore_index=True) if trades_all else pd.DataFrame()
    summary_df = pd.concat([research_summary, holdout_summary], ignore_index=True) \
        if not holdout_summary.empty else research_summary

    os.makedirs("backtest_results", exist_ok=True)
    stamp = int(time.time())
    summary_path = f"backtest_results/exit_sensitivity_summary_{stamp}.csv"
    trades_path = f"backtest_results/exit_sensitivity_trades_{stamp}.csv"
    summary_df.to_csv(summary_path, index=False)
    trades_df.to_csv(trades_path, index=False)

    print(f"\nSaved summary: {summary_path}")
    print(f"Saved trade diagnostics: {trades_path}")
    print("\nInterpretation rule:")
    print("  EARLY_CONFIRM_3C is only interesting if its advantage over CURRENT SURVIVES the holdout.")
    print("  Do not tune the 3-candle rule or entry parameters from this run.")
    print("  Reported returns assume frozen position sizing — compare models to each")
    print("  other, not to an achievable equity curve.")


if __name__ == "__main__":
    main()
