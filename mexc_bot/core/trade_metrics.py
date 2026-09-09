"""
Reconstructs true per-TRADE performance metrics from a raw trade-event log
(the kind backtester.py and live_bot.py produce, with one row per entry/
partial_tp/exit_stop/exit_time_stop event).

Why this exists: counting "closed events" (partial_tp + final exit as two
separate wins/losses) inflates win rate — a trade that partially profits
then gets stopped at breakeven looks like "1 win" in the event log even
though its NET result was roughly zero. This module groups events back
into whole trades and scores each trade by its total realized PnL.
"""
import pandas as pd


def reconstruct_trades(event_log: pd.DataFrame) -> pd.DataFrame:
    """
    Groups a flat event log (entry/partial_tp/exit_stop/exit_time_stop rows,
    in chronological order) into one row per full trade with net PnL.
    Expects columns: symbol, time (or timestamp), type, pnl.
    """
    time_col = "time" if "time" in event_log.columns else "timestamp"
    trades = []
    current = None

    for _, row in event_log.iterrows():
        if row["type"] == "entry":
            if current is not None:
                trades.append(current)
            current = {
                "symbol": row["symbol"],
                "entry_time": row[time_col],
                "side": row.get("side"),
                "total_pnl": 0.0,
                "num_events": 0,
                "hit_partial": False,
            }
        elif current is not None:
            pnl = row["pnl"] if pd.notna(row.get("pnl")) else 0.0
            current["total_pnl"] += pnl
            # Funding is a holding COST attributed to the trade's net result,
            # not a trade outcome of its own — count it in PnL but don't let
            # it inflate the event count or the win/loss classification.
            if row["type"] == "funding":
                current["funding_pnl"] = current.get("funding_pnl", 0.0) + pnl
                continue
            current["num_events"] += 1
            if row["type"] == "partial_tp":
                current["hit_partial"] = True
            if row["type"] in ("exit_stop", "exit_time_stop"):
                current["exit_type"] = row["type"]
                current["exit_time"] = row[time_col]
                current["mae_r"] = row.get("mae_r", 0.0)
                current["mfe_r"] = row.get("mfe_r", 0.0)
                current["candles_held"] = row.get("candles_held", 0)

    if current is not None:
        trades.append(current)

    return pd.DataFrame(trades)


def compute_drawdown(trades_df: pd.DataFrame) -> dict:
    """
    Max drawdown measured on the cumulative PnL of the trade sequence
    (ordered by entry time), expressed both in currency and as a percent of
    the running peak. Drawdown matters more than total return for judging a
    system: +20% with -4% max DD is a very different proposition from +35%
    with -28%.
    """
    if trades_df.empty:
        return {"max_drawdown": 0.0, "max_drawdown_pct": 0.0,
                "max_dd_duration_trades": 0}

    df = trades_df.sort_values("entry_time")
    cum = df["total_pnl"].cumsum()
    running_peak = cum.cummax()
    drawdown = cum - running_peak

    max_dd = float(drawdown.min()) if len(drawdown) else 0.0

    # longest run of consecutive trades spent below a prior peak
    below = drawdown < 0
    longest, current = 0, 0
    for flag in below:
        current = current + 1 if flag else 0
        longest = max(longest, current)

    peak_at_trough = float(running_peak.iloc[drawdown.idxmin()]) if len(drawdown) else 0.0
    dd_pct = (abs(max_dd) / peak_at_trough * 100) if peak_at_trough > 0 else 0.0

    return {"max_drawdown": round(max_dd, 2),
            "max_drawdown_pct": round(dd_pct, 2),
            "max_dd_duration_trades": int(longest)}


def compute_metrics(event_log: pd.DataFrame, starting_equity: float) -> dict:
    """Returns a dict of honest, per-trade performance metrics."""
    trades_df = reconstruct_trades(event_log)

    if trades_df.empty:
        return {
            "num_trades": 0, "win_rate_pct": 0, "avg_win": 0, "avg_loss": 0,
            "profit_factor": 0, "total_pnl": 0, "expectancy": 0,
            "total_return_pct": 0, "avg_mae_r": 0.0, "avg_mfe_r": 0.0,
            "avg_candles_held": 0.0, "max_drawdown": 0.0, "max_drawdown_pct": 0.0,
            "max_dd_duration_trades": 0, "return_over_maxdd": 0.0,
        }

    wins = trades_df[trades_df["total_pnl"] > 0]
    losses = trades_df[trades_df["total_pnl"] <= 0]

    total_pnl = trades_df["total_pnl"].sum()
    gross_profit = wins["total_pnl"].sum() if not wins.empty else 0
    gross_loss = abs(losses["total_pnl"].sum()) if not losses.empty else 0

    dd = compute_drawdown(trades_df)

    def _mean(col):
        if col not in trades_df.columns:
            return 0.0
        vals = pd.to_numeric(trades_df[col], errors="coerce").dropna()
        return round(float(vals.mean()), 3) if len(vals) else 0.0

    out = {
        "num_trades": len(trades_df),
        "win_rate_pct": round(len(wins) / len(trades_df) * 100, 1),
        "avg_win": round(wins["total_pnl"].mean(), 2) if not wins.empty else 0,
        "avg_loss": round(losses["total_pnl"].mean(), 2) if not losses.empty else 0,
        "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss > 0 else float("inf"),
        "total_pnl": round(total_pnl, 2),
        "expectancy": round(total_pnl / len(trades_df), 2),
        "total_return_pct": round(total_pnl / starting_equity * 100, 2) if starting_equity else 0,
        "avg_mae_r": _mean("mae_r"),
        "avg_mfe_r": _mean("mfe_r"),
        "avg_candles_held": _mean("candles_held"),
    }
    out.update(dd)
    # Return per unit of drawdown — the risk-adjusted view. High is good.
    out["return_over_maxdd"] = round(total_pnl / abs(dd["max_drawdown"]), 2) \
        if dd["max_drawdown"] < 0 else 0.0
    return out


def print_metrics_report(metrics: dict, starting_equity: float, title: str = "PERFORMANCE REPORT"):
    print("\n" + "=" * 55)
    print(title)
    print("=" * 55)
    print(f"Trades:            {metrics['num_trades']}")
    print(f"True win rate:     {metrics['win_rate_pct']}%  (per-trade, not per-event)")
    print(f"Avg win:           ${metrics['avg_win']}")
    print(f"Avg loss:          ${metrics['avg_loss']}")
    print(f"Profit factor:     {metrics['profit_factor']}  (gross profit / gross loss)")
    print(f"Expectancy/trade:  ${metrics['expectancy']}")
    print(f"Total PnL:         ${metrics['total_pnl']}")
    print(f"Total return:      {metrics['total_return_pct']}%  (on ${starting_equity} starting equity)")
    print("=" * 55)
