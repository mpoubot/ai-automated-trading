"""
Backtester for the Volatility-Adjusted Momentum Scanner strategy.

Run this BEFORE the live bot touches real money. It replays historical
candles pair-by-pair, applies the exact same signal/risk logic the live
bot uses, and reports performance including fees and assumed slippage.

Usage:
    python backtester.py --symbols BTC/USDT:USDT ETH/USDT:USDT --days 90

This is a single-pass, sequential-candle simulator (not vectorized) so that
the exact same evaluate_signal()/calc_position_plan() functions used live
are exercised here — avoids subtle "backtest logic differs from live logic" bugs.
"""
import argparse
import os
import time
from datetime import datetime, timedelta, timezone

import pandas as pd

import config as cfg
from core import data_fetcher as dfetch
from core import strategy as strat
from core.risk_manager import calc_position_plan, CircuitBreaker
from core import trade_metrics


class Trade:
    def __init__(self, symbol, side, entry_time, entry_price, stop_price,
                 quantity, leverage, risk_amount):
        self.symbol = symbol
        self.side = side
        self.entry_time = entry_time
        self.entry_price = entry_price
        self.stop_price = stop_price
        self.initial_stop_price = stop_price
        self.quantity = quantity
        self.leverage = leverage
        self.risk_amount = risk_amount
        self.remaining_qty = quantity
        self.partial_taken = False
        self.candles_open = 0
        self.exit_records = []  # list of (time, price, qty, pnl)
        # Excursion tracking (in R multiples): how far the trade went against
        # us (MAE) and in our favour (MFE) before finally closing. Comparing
        # these against random controls shows whether the signal finds good
        # entries that the exit logic then fails to harvest.
        self.mae_r = 0.0
        self.mfe_r = 0.0

    def update_excursions(self, high, low):
        risk_per_unit = abs(self.entry_price - self.initial_stop_price)
        if risk_per_unit == 0:
            return
        if self.side == "long":
            fav = (high - self.entry_price) / risk_per_unit
            adv = (low - self.entry_price) / risk_per_unit
        else:
            fav = (self.entry_price - low) / risk_per_unit
            adv = (self.entry_price - high) / risk_per_unit
        self.mfe_r = max(self.mfe_r, fav)
        self.mae_r = min(self.mae_r, adv)

    def r_multiple(self, price):
        risk_per_unit = abs(self.entry_price - self.initial_stop_price)
        if risk_per_unit == 0:
            return 0
        move = (price - self.entry_price) if self.side == "long" else (self.entry_price - price)
        return move / risk_per_unit


def _funding_rate_at(funding_df, ts):
    """Most recent funding rate at or before `ts`; falls back to the configured
    constant when no real data is available."""
    if funding_df is None or len(funding_df) == 0:
        return cfg.FUNDING_RATE_FALLBACK
    prior = funding_df[funding_df["timestamp"] <= ts]
    if prior.empty:
        return cfg.FUNDING_RATE_FALLBACK
    return float(prior.iloc[-1]["funding_rate"])


def simulate_symbol(df: pd.DataFrame, symbol: str, equity_tracker: dict,
                     breaker: CircuitBreaker, trade_log: list, funding_df=None,
                     entry_mode: dict = None):
    """Walk forward candle-by-candle for one symbol, opening/managing at most
    one position at a time for that symbol (simplification: cross-symbol
    concurrency cap is enforced by the caller via equity_tracker['open_count']).

    If cfg.MODEL_FUNDING_COSTS is on, holding a position across a funding
    interval charges (or credits) funding on the position's notional value —
    longs pay when the rate is positive, shorts receive, and vice versa.

    `entry_mode` supports the randomized controls used by permutation_test.py.
    Everything downstream of entry — sizing, stops, TP, trailing, fees,
    funding, slippage — is IDENTICAL in every mode, so any difference in
    results is attributable purely to entry timing/direction:
        None                          -> normal strategy signals
        {"mode": "random_entry"}      -> scheduled timing + random direction
        {"mode": "random_timing"}     -> scheduled timing + strategy direction
        {"mode": "random_direction"}  -> baseline timing + random direction
    All three take {"schedule": [sorted bar indices], "rng": rng}. Entries are
    opened at the scheduled bars; if a scheduled bar arrives while a position
    is still open, the entry is taken at the next flat bar instead ("catch
    up") rather than being dropped. This GUARANTEES the realized trade count
    matches the baseline, so exposure, opportunity count and cumulative costs
    stay matched — a control that quietly takes fewer trades is not a
    controlled comparison.
    """
    # Indicators are expensive; permutation runs reuse a pre-computed frame.
    if "adx" not in df.columns:
        df = strat.add_indicators(df)
    entry_mode = entry_mode or {}
    mode = entry_mode.get("mode")
    rng = entry_mode.get("rng")
    schedule = entry_mode.get("schedule") or []
    sched_ptr = 0

    open_trade = None
    last_funding_ts = None

    for i in range(len(df)):
        if i < max(cfg.EMA_SLOW, cfg.BREAKOUT_LOOKBACK) + 5:
            continue

        window = df.iloc[: i + 1]
        row = df.iloc[i]
        price = row["close"]
        equity = equity_tracker["equity"]

        # --- funding charge on any open position ---
        if cfg.MODEL_FUNDING_COSTS and open_trade is not None:
            ts = row["timestamp"]
            if last_funding_ts is None:
                last_funding_ts = ts
            elapsed_h = (ts - last_funding_ts).total_seconds() / 3600.0
            if elapsed_h >= cfg.FUNDING_INTERVAL_HOURS:
                intervals = int(elapsed_h // cfg.FUNDING_INTERVAL_HOURS)
                rate = _funding_rate_at(funding_df, ts)
                notional = open_trade.remaining_qty * price
                # Positive rate: longs pay, shorts receive.
                sign = -1.0 if open_trade.side == "long" else 1.0
                funding_pnl = sign * rate * notional * intervals
                equity_tracker["equity"] += funding_pnl
                trade_log.append({"symbol": symbol, "time": ts, "type": "funding",
                                   "pnl": funding_pnl, "equity": equity_tracker["equity"]})
                last_funding_ts = ts

        # --- manage open trade ---
        if open_trade is not None:
            open_trade.candles_open += 1
            open_trade.update_excursions(row["high"], row["low"])
            exit_price = None
            exit_reason = None

            hit_stop = (row["low"] <= open_trade.stop_price) if open_trade.side == "long" \
                else (row["high"] >= open_trade.stop_price)
            if hit_stop:
                exit_price = open_trade.stop_price
                exit_reason = "stop"

            if exit_price is None and not open_trade.partial_taken:
                r = open_trade.r_multiple(price)
                if r >= cfg.TAKE_PROFIT_R_MULT_PARTIAL:
                    partial_qty = open_trade.quantity * cfg.PARTIAL_CLOSE_PCT
                    pnl = (price - open_trade.entry_price) * partial_qty if open_trade.side == "long" \
                        else (open_trade.entry_price - price) * partial_qty
                    fee = price * partial_qty * cfg.BACKTEST_TAKER_FEE_PCT
                    equity_tracker["equity"] += pnl - fee
                    open_trade.remaining_qty -= partial_qty
                    open_trade.partial_taken = True
                    trade_log.append({"symbol": symbol, "time": row["timestamp"], "type": "partial_tp",
                                       "pnl": pnl - fee, "equity": equity_tracker["equity"]})
                    # move stop to breakeven after partial
                    open_trade.stop_price = open_trade.entry_price

            if exit_price is None and open_trade.partial_taken:
                trail_dist = row["atr"] * cfg.TRAIL_ATR_MULT
                if open_trade.side == "long":
                    new_stop = price - trail_dist
                    open_trade.stop_price = max(open_trade.stop_price, new_stop)
                else:
                    new_stop = price + trail_dist
                    open_trade.stop_price = min(open_trade.stop_price, new_stop)

            if exit_price is None and open_trade.candles_open >= cfg.TIME_STOP_CANDLES:
                r = open_trade.r_multiple(price)
                if r <= 0:
                    exit_price = price
                    exit_reason = "time_stop"

            if exit_price is not None:
                qty = open_trade.remaining_qty
                pnl = (exit_price - open_trade.entry_price) * qty if open_trade.side == "long" \
                    else (open_trade.entry_price - exit_price) * qty
                fee = exit_price * qty * cfg.BACKTEST_TAKER_FEE_PCT
                slippage = exit_price * qty * cfg.BACKTEST_SLIPPAGE_PCT
                equity_tracker["equity"] += pnl - fee - slippage
                trade_log.append({"symbol": symbol, "time": row["timestamp"], "type": f"exit_{exit_reason}",
                                   "pnl": pnl - fee - slippage, "equity": equity_tracker["equity"],
                                   "mae_r": round(open_trade.mae_r, 3),
                                   "mfe_r": round(open_trade.mfe_r, 3),
                                   "candles_held": open_trade.candles_open})
                equity_tracker["open_count"] -= 1
                open_trade = None

        # --- look for new entry ---
        if open_trade is None:
            today_key = row["timestamp"].strftime("%Y-%m-%d")
            breaker.update(equity_tracker["equity"], today_key)
            can_trade, reason = breaker.can_open_new_trades(equity_tracker["equity"])
            if not can_trade:
                continue
            if equity_tracker["open_count"] >= cfg.MAX_CONCURRENT_POSITIONS:
                continue

            sig = strat.evaluate_signal(window)

            # --- randomized controls -------------------------------------
            # Only the ENTRY decision differs; everything downstream is shared.
            # A scheduled entry that arrives while a position is open is taken
            # at the next flat bar (catch-up), never dropped — that is what
            # keeps the trade count exactly matched to the baseline.
            if mode is not None:
                if sched_ptr >= len(schedule) or i < schedule[sched_ptr]:
                    continue
                if mode == "random_entry":
                    side = "long" if (not cfg.ALLOW_SHORTS or rng.random() < 0.5) else "short"
                    reason = "scheduled timing + random direction"
                elif mode == "random_timing":
                    side = strat.direction_rule(row)
                    reason = "scheduled timing + strategy direction"
                else:  # random_direction — baseline timing, coin-flip side
                    side = "long" if (not cfg.ALLOW_SHORTS or rng.random() < 0.5) else "short"
                    reason = "baseline timing + random direction"
                sig = {"signal": side, "reason": reason, "atr": row["atr"]}

            if sig["signal"] is not None:
                if sig["atr"] is None or pd.isna(sig["atr"]):
                    continue
                plan = calc_position_plan(price, sig["atr"], sig["signal"], equity_tracker["equity"])
                if plan is None or plan.position_size_usdt <= 0:
                    continue
                open_trade = Trade(symbol, sig["signal"], row["timestamp"], price,
                                    plan.stop_price, plan.quantity, plan.leverage,
                                    plan.risk_amount_usdt)
                last_funding_ts = row["timestamp"]
                equity_tracker["open_count"] += 1
                if mode is not None:
                    sched_ptr += 1
                trade_log.append({"symbol": symbol, "time": row["timestamp"], "type": "entry",
                                   "side": sig["signal"], "price": price,
                                   "leverage": plan.leverage, "equity": equity_tracker["equity"]})


def run_backtest(symbols: list[str], days: int):
    exchange = dfetch.build_exchange()
    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=days)
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)

    equity_tracker = {"equity": cfg.BACKTEST_STARTING_EQUITY, "open_count": 0}
    breaker = CircuitBreaker(cfg.BACKTEST_STARTING_EQUITY)
    trade_log = []

    for symbol in symbols:
        print(f"Fetching {symbol} ({cfg.TIMEFRAME}, {days}d)...")
        df = dfetch.fetch_ohlcv_range(exchange, symbol, cfg.TIMEFRAME, start_ms, end_ms)
        if df.empty or len(df) < 250:
            print(f"  skipped — insufficient data ({len(df)} candles)")
            continue
        simulate_symbol(df, symbol, equity_tracker, breaker, trade_log)
        time.sleep(exchange.rateLimit / 1000)

    log_df = pd.DataFrame(trade_log)
    print_report(log_df, equity_tracker["equity"])
    return log_df


def print_report(log_df: pd.DataFrame, final_equity: float):
    print("\n" + "=" * 50)
    print("BACKTEST REPORT")
    print("=" * 50)
    print(f"Starting equity: ${cfg.BACKTEST_STARTING_EQUITY:.2f}")
    print(f"Final equity:    ${final_equity:.2f}")
    ret_pct = (final_equity - cfg.BACKTEST_STARTING_EQUITY) / cfg.BACKTEST_STARTING_EQUITY * 100
    print(f"Return:          {ret_pct:+.2f}%")

    if log_df.empty:
        print("No trades were generated in this window.")
        return

    entries = log_df[log_df["type"] == "entry"]
    print(f"\nEntries:         {len(entries)}")

    # NOTE: the metrics below are per-TRADE (entry through final exit),
    # not per-event — a trade that partial-profits then stops at breakeven
    # is correctly scored by its NET result, not double-counted as a "win".
    metrics = trade_metrics.compute_metrics(log_df, cfg.BACKTEST_STARTING_EQUITY)
    trade_metrics.print_metrics_report(metrics, cfg.BACKTEST_STARTING_EQUITY,
                                        title="TRUE PER-TRADE PERFORMANCE")

    exits = log_df[log_df["type"].str.startswith("exit_", na=False)]
    stops = exits[exits["type"] == "exit_stop"]
    time_stops = exits[exits["type"] == "exit_time_stop"]
    print(f"Stopped out:     {len(stops)}")
    print(f"Time-stopped:    {len(time_stops)}")
    print("=" * 50)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backtest the momentum scanner strategy")
    parser.add_argument("--symbols", nargs="+", required=True,
                         help="e.g. BTC/USDT:USDT ETH/USDT:USDT (swap) or BTC/USDT (spot)")
    parser.add_argument("--days", type=int, default=90, help="lookback window in days")
    args = parser.parse_args()

    log_df = run_backtest(args.symbols, args.days)
    os.makedirs("backtest_results", exist_ok=True)
    out_path = f"backtest_results/backtest_{int(time.time())}.csv"
    log_df.to_csv(out_path, index=False)
    print(f"\nFull trade log saved to {out_path}")
