"""
core/backtest_engine.py

The research backtest engine. Ports mexc_bot/backtester.py::simulate_symbol()'s
trade-management mechanics (stop, partial take-profit, ATR trailing stop,
time stop, fee/slippage, funding accrual) -- real, working logic, directly
portable as a pattern -- while fixing the ONE defect found in the
"inspect before coding" review: simulate_symbol() calls
`strat.evaluate_signal(window)` as a hard, direct import of a specific
strategy module. Here the engine depends only on the Strategy interface
(core/strategy_base.py); it never imports a concrete strategy.

Entry/window convention (kept identical to mexc_bot, and proven, not just
asserted, by tests/test_backtest_execution.py's no-lookahead test):
`window = ohlcv.iloc[:i+1]` includes the CURRENT bar, and a signal evaluated
on that window enters at that same bar's own close. This is self-consistent
(a strategy can only use its own bar and everything before it) but is NOT
"decide using bar i's close, execute at bar i+1's open" -- that distinction
is documented here explicitly rather than left implicit, per the Phase 5
command's requirement to prove "no look-ahead" rather than assume it.

Simplification documented per the command's requirement: the engine assumes
an `atr` column is already computed on `ohlcv` (see core/indicators.py::atr)
-- it does not compute indicators itself. This mirrors mexc_bot's own
separation between strategy.add_indicators() and the engine loop.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from core.costs import CostModel
from core.instrument import Instrument
from core.risk import (
    RiskConfig,
    RiskState,
    SimpleResearchRiskModel,
    calc_position_plan,
)
from core.strategy_base import Strategy


@dataclass(frozen=True)
class ExitConfig:
    """Trade-management parameters, ported from mexc_bot/config.py's
    module-level constants (TAKE_PROFIT_R_MULT_PARTIAL, PARTIAL_CLOSE_PCT,
    TRAIL_ATR_MULT, TIME_STOP_CANDLES) but passed explicitly instead of
    imported globally, so a run's exact configuration is always visible at
    the call site and hashable for reproducibility."""
    take_profit_r_mult: float = 3.0
    partial_close_pct: float = 0.5
    trail_atr_mult: float = 1.5
    time_stop_bars: int = 24


@dataclass
class BacktestRunResult:
    event_log: list[dict]
    final_equity: float
    final_risk_state: RiskState
    final_risk_reason: str
    bars_processed: int


def _r_multiple(open_trade: dict, price: float) -> float:
    risk_per_unit = abs(open_trade["entry_price"] - open_trade["initial_stop_price"])
    if risk_per_unit == 0:
        return 0.0
    move = (price - open_trade["entry_price"]) if open_trade["side"] == "long" \
        else (open_trade["entry_price"] - price)
    return move / risk_per_unit


def run_backtest(strategy: Strategy, instrument: Instrument, ohlcv: pd.DataFrame,
                  cost_model: CostModel, risk_cfg: RiskConfig, starting_capital: float,
                  exit_config: ExitConfig | None = None) -> BacktestRunResult:
    """
    Walks `ohlcv` (must be sorted ascending by timestamp, with a
    precomputed 'atr' column) forward bar by bar, holding at most one
    position at a time for this single instrument. `cost_model` carries
    this instrument's funding history (already loaded and coverage-flagged
    by core/dataset.py::load_funding). `starting_capital` is this
    instrument's OWN fixed allocation from core/portfolio.py::PortfolioAllocator
    -- never a shared pool, which is what fixes the shared-equity-pool bug.
    """
    if "atr" not in ohlcv.columns:
        raise ValueError(
            "ohlcv must have a precomputed 'atr' column (see core/indicators.py::atr) "
            "-- the backtest engine does not compute indicators itself.")
    exit_config = exit_config or ExitConfig()

    warmup = strategy.required_warmup_bars()
    risk_model = SimpleResearchRiskModel(starting_capital, risk_cfg)
    equity = starting_capital
    open_trade: dict | None = None
    last_funding_ts = None
    event_log: list[dict] = []
    instrument_id = instrument.canonical_id
    bars_processed = 0

    for i in range(len(ohlcv)):
        if i < warmup:
            continue
        row = ohlcv.iloc[i]
        if pd.isna(row["atr"]):
            continue
        bars_processed += 1

        window = ohlcv.iloc[: i + 1]  # includes current bar -- see module docstring
        price = float(row["close"])
        ts = row["timestamp"]

        # --- funding charge on any open position ---
        if open_trade is not None:
            if last_funding_ts is None:
                last_funding_ts = ts
            elapsed_h = (ts - last_funding_ts).total_seconds() / 3600.0
            if elapsed_h >= cost_model.funding_interval_hours:
                intervals = int(elapsed_h // cost_model.funding_interval_hours)
                notional = open_trade["remaining_qty"] * price
                funding_pnl, is_real = cost_model.funding_pnl(
                    open_trade["side"], notional, ts, intervals)
                equity += funding_pnl
                event_log.append({
                    "instrument": instrument_id, "timestamp": ts, "type": "funding",
                    "pnl": funding_pnl, "funding_rate_is_real": is_real,
                })
                last_funding_ts = ts

        # --- manage open trade ---
        if open_trade is not None:
            open_trade["candles_open"] += 1
            exit_price = None
            exit_reason = None

            hit_stop = (row["low"] <= open_trade["stop_price"]) if open_trade["side"] == "long" \
                else (row["high"] >= open_trade["stop_price"])
            if hit_stop:
                exit_price = open_trade["stop_price"]
                exit_reason = "stop"

            if exit_price is None and not open_trade["partial_taken"]:
                r = _r_multiple(open_trade, price)
                if r >= exit_config.take_profit_r_mult:
                    partial_qty = open_trade["quantity"] * exit_config.partial_close_pct
                    gross = (price - open_trade["entry_price"]) * partial_qty if open_trade["side"] == "long" \
                        else (open_trade["entry_price"] - price) * partial_qty
                    fee = cost_model.fee(price, partial_qty)
                    equity += gross - fee
                    open_trade["remaining_qty"] -= partial_qty
                    open_trade["partial_taken"] = True
                    event_log.append({
                        "instrument": instrument_id, "timestamp": ts, "type": "partial_tp",
                        "gross_pnl": gross, "fee": fee,
                    })
                    open_trade["stop_price"] = open_trade["entry_price"]  # move to breakeven

            if exit_price is None and open_trade["partial_taken"]:
                trail_dist = float(row["atr"]) * exit_config.trail_atr_mult
                if open_trade["side"] == "long":
                    open_trade["stop_price"] = max(open_trade["stop_price"], price - trail_dist)
                else:
                    open_trade["stop_price"] = min(open_trade["stop_price"], price + trail_dist)

            if exit_price is None and open_trade["candles_open"] >= exit_config.time_stop_bars:
                r = _r_multiple(open_trade, price)
                if r <= 0:
                    exit_price = price
                    exit_reason = "time_stop"

            if exit_price is not None:
                qty = open_trade["remaining_qty"]
                gross = (exit_price - open_trade["entry_price"]) * qty if open_trade["side"] == "long" \
                    else (open_trade["entry_price"] - exit_price) * qty
                fee = cost_model.fee(exit_price, qty)
                slip = cost_model.slippage(exit_price, qty)
                equity += gross - fee - slip
                event_log.append({
                    "instrument": instrument_id, "timestamp": ts, "type": f"exit_{exit_reason}",
                    "gross_pnl": gross, "fee": fee + slip,
                    "candles_held": open_trade["candles_open"],
                })
                open_trade = None

        # --- look for new entry ---
        if open_trade is None:
            today_key = ts.strftime("%Y-%m-%d")
            risk_model.update(equity, today_key)
            can_trade, _state, _reason = risk_model.can_open_new_trades(equity)
            if not can_trade:
                continue

            signal = strategy.evaluate(window, instrument)
            if signal is None:
                continue
            if signal.atr_at_signal is None or pd.isna(signal.atr_at_signal):
                continue

            plan = calc_position_plan(price, signal.atr_at_signal, signal.direction, equity, risk_cfg)
            if plan is None or plan.position_size_usdt <= 0:
                continue

            open_trade = {
                "side": plan.side, "entry_price": plan.entry_price,
                "initial_stop_price": plan.stop_price, "stop_price": plan.stop_price,
                "quantity": plan.quantity, "remaining_qty": plan.quantity,
                "partial_taken": False, "candles_open": 0, "entry_time": ts,
            }
            last_funding_ts = ts
            event_log.append({
                "instrument": instrument_id, "timestamp": ts, "type": "entry",
                "side": plan.side, "price": price, "leverage": plan.leverage,
            })

    return BacktestRunResult(
        event_log=event_log, final_equity=equity,
        final_risk_state=risk_model.state, final_risk_reason=risk_model.state_reason,
        bars_processed=bars_processed,
    )
