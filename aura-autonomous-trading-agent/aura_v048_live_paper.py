
"""
AURA v0.4.8 — Parallel Live Paper Laboratory
================================================

PAPER ONLY — NO ORDER SUBMISSION.

Purpose:
    Forward-test the frozen AURA entry hypothesis on a frozen S&P 100
    universe while running three exit philosophies in parallel and a
    buy-and-hold market baseline.

IMPORTANT SIGNAL-DEFINITION NOTE:
    Historical AURA code labelled the survivor "EMA38", but the actual
    implementation that generated v0.4.3-v0.4.7.1 results used an
    EMA3/EMA8 bullish crossover. v0.4.8 preserves the ACTUAL tested
    definition for methodological continuity:

        EMA3/EMA8 bullish crossover
        + MACD histogram > 0
        + relative volume >= 1

Frozen models:
    A = 10 complete trading sessions -> 10th-session CLOSE
        No stop/target.

    B = 2x ATR stop / 4x ATR target
        No trading time exit.
        60 trading sessions is ONLY a research/data-integrity horizon.

    C = 2x ATR stop / 4x ATR target
        If neither is hit, force exit at the 10th-session CLOSE.

Friction:
    Entry slippage  = 5 bps
    Exit slippage   = 5 bps
    Commission      = 1 bp per side
    Total modeled round trip = 12 bps

The script maintains CSV ledgers:
    signals.csv
    positions.csv
    position_snapshots.csv
    executions.csv
    daily_model_metrics.csv
    baseline.csv
    universe.csv

Run once per trading day after the market close:
    python aura_v048_live_paper.py --date YYYY-MM-DD

For the first run, use the current date. The script creates its state
files and then tracks positions forward on subsequent runs.

No Alpaca order endpoint is called anywhere in this file.
"""

from __future__ import annotations

import argparse
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from dotenv import load_dotenv
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import DataFeed


# ---------------------------------------------------------------------------
# Frozen research configuration
# ---------------------------------------------------------------------------

SIGNAL_VERSION = "AURA_v0.4.8"
UNIVERSE_VERSION = "S&P100_FROZEN_2026-08-26"

ENTRY_SLIPPAGE_BPS = 5.0
EXIT_SLIPPAGE_BPS = 5.0
COMMISSION_BPS_PER_SIDE = 1.0

RESEARCH_HORIZON_DAYS = 60

EXIT_A = "A_10D_TIME"
EXIT_B = "B_PURE_ATR_2x4x"
EXIT_C = "C_ATR_2x4x_10D"
BASELINE = "M_BUY_HOLD"

# Frozen universe. Store this exact list in universe.csv on initialization.
# This is the research universe used by v0.4.8; it must not be modified
# based on observed results.
S_AND_P_100 = [
    "AAPL", "ABBV", "ABT", "ACN", "ADBE", "AIG", "AMD", "AMGN", "AMT",
    "AMZN", "AVGO", "AXP", "BA", "BAC", "BK", "BKNG", "BLK", "BMY",
    "BRK.B", "C", "CAT", "CB", "C", "CHTR", "CI", "CMCSA", "COF", "COP",
    "COST", "CRM", "CSCO", "CVS", "CVX", "DE", "DHR", "DIS", "DOW",
    "DUK", "EMR", "EXC", "F", "FDX", "GD", "GE", "GILD", "GM", "GOOG",
    "GOOGL", "GS", "HD", "HON", "IBM", "INTC", "JNJ", "JPM", "KO",
    "LIN", "LLY", "LMT", "LOW", "MA", "MCD", "MDLZ", "MDT", "MET", "META",
    "MMM", "MO", "MRK", "MS", "MSFT", "NEE", "NFLX", "NKE", "NOW", "NVDA",
    "ORCL", "PEP", "PFE", "PG", "PLTR", "PM", "PYPL", "QCOM", "RTX",
    "SBUX", "SCHW", "SO", "SPG", "T", "TGT", "TMO", "TMUS", "TSLA",
    "TXN", "UNH", "UNP", "UPS", "USB", "V", "VZ", "WFC", "WMT", "XOM",
]

# Remove accidental duplicate while preserving order.
S_AND_P_100 = list(dict.fromkeys(S_AND_P_100))


# ---------------------------------------------------------------------------
# CSV schema helpers
# ---------------------------------------------------------------------------

SCHEMAS = {
    "universe.csv": [
        "universe_version", "symbol", "active", "frozen_date"
    ],
    "signals.csv": [
        "signal_id", "signal_version", "universe_version", "symbol",
        "decision_date", "entry_date", "signal_close", "ema3", "ema8",
        "macd_hist", "relative_volume", "atr14", "status", "created_at"
    ],
    "positions.csv": [
        "position_id", "signal_id", "model", "symbol", "status",
        "entry_date", "entry_price_raw", "entry_price_simulated",
        "entry_atr", "stop_price", "target_price",
        "sessions_held", "exit_date", "exit_type",
        "exit_price_raw", "exit_price_simulated",
        "gross_return", "slippage_return_penalty",
        "commission_return_penalty", "net_return",
        "mfe", "mae", "capital_days", "mfe_capture_ratio",
        "created_at", "updated_at"
    ],
    "position_snapshots.csv": [
        "snapshot_id", "position_id", "signal_id", "model", "symbol",
        "date", "open", "high", "low", "close", "sessions_held",
        "unrealized_gross_return", "unrealized_net_return",
        "mfe", "mae", "drawdown_from_peak", "created_at"
    ],
    "executions.csv": [
        "execution_id", "position_id", "signal_id", "model", "symbol",
        "side", "date", "price_raw", "price_simulated",
        "slippage_bps", "commission_bps", "friction_return",
        "exit_type", "created_at"
    ],
    "daily_model_metrics.csv": [
        "date", "model", "closed_trades", "open_positions",
        "gross_mean", "net_mean", "net_median", "win_rate",
        "profit_factor", "max_drawdown", "mean_mfe", "mean_mae",
        "mfe_mae_ratio", "capital_efficiency",
        "mfe_capture_ratio", "baseline_mean", "net_lift_vs_baseline",
        "created_at"
    ],
    "baseline.csv": [
        "baseline_id", "signal_id", "symbol", "entry_date",
        "entry_price_raw", "entry_price_simulated",
        "exit_date", "exit_price_raw", "exit_price_simulated",
        "gross_return", "net_return", "holding_sessions",
        "created_at", "updated_at"
    ],
}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def ensure_files(outdir: Path):
    outdir.mkdir(parents=True, exist_ok=True)
    for filename, columns in SCHEMAS.items():
        path = outdir / filename
        if not path.exists():
            pd.DataFrame(columns=columns).to_csv(path, index=False)


def read_csv(outdir, filename):
    path = outdir / filename
    if not path.exists():
        return pd.DataFrame(columns=SCHEMAS[filename])
    return pd.read_csv(path)


def append_rows(outdir, filename, rows):
    if not rows:
        return
    df = pd.DataFrame(rows)
    columns = SCHEMAS[filename]
    for col in columns:
        if col not in df.columns:
            df[col] = np.nan
    df = df[columns]
    path = outdir / filename
    df.to_csv(path, mode="a", header=not path.exists() or path.stat().st_size == 0,
              index=False)


def write_universe_once(outdir: Path, as_of_date: str):
    path = outdir / "universe.csv"
    existing = read_csv(outdir, "universe.csv")
    if not existing.empty:
        return
    rows = [
        {
            "universe_version": UNIVERSE_VERSION,
            "symbol": s,
            "active": True,
            "frozen_date": as_of_date,
        }
        for s in S_AND_P_100
    ]
    append_rows(outdir, "universe.csv", rows)


# ---------------------------------------------------------------------------
# Market data and indicators
# ---------------------------------------------------------------------------

def fetch_daily(client, symbol, start, end):
    req = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Day,
        start=start,
        end=end,
        feed=DataFeed.IEX,
    )
    data = client.get_stock_bars(req).df
    if data.empty:
        return pd.DataFrame()

    if isinstance(data.index, pd.MultiIndex):
        try:
            data = data.xs(symbol, level="symbol")
        except KeyError:
            return pd.DataFrame()

    data = data.copy()
    data.index = pd.to_datetime(data.index, utc=True)
    data = data.sort_index()
    return data[["open", "high", "low", "close", "volume"]]


def add_features(df):
    x = df.copy()

    x["ema3"] = x["close"].ewm(span=3, adjust=False).mean()
    x["ema8"] = x["close"].ewm(span=8, adjust=False).mean()

    ema12 = x["close"].ewm(span=12, adjust=False).mean()
    ema26 = x["close"].ewm(span=26, adjust=False).mean()
    x["macd"] = ema12 - ema26
    x["macd_signal"] = x["macd"].ewm(span=9, adjust=False).mean()
    x["macd_hist"] = x["macd"] - x["macd_signal"]

    x["volume_ma20"] = x["volume"].rolling(20, min_periods=20).mean()
    x["relative_volume"] = x["volume"] / x["volume_ma20"]

    prev_close = x["close"].shift(1)
    tr = pd.concat([
        x["high"] - x["low"],
        (x["high"] - prev_close).abs(),
        (x["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    x["atr14"] = tr.rolling(14, min_periods=14).mean()

    bullish = x["ema3"] > x["ema8"]
    x["bullish_crossover"] = bullish & (~bullish.shift(1, fill_value=False))

    return x


def make_signal_id(date, symbol, counter):
    return f"AURA-{date}-{symbol}-{counter:04d}"


# ---------------------------------------------------------------------------
# Friction
# ---------------------------------------------------------------------------

def entry_fill(raw):
    return raw * (1.0 + ENTRY_SLIPPAGE_BPS / 10000.0)


def exit_fill(raw):
    return raw * (1.0 - EXIT_SLIPPAGE_BPS / 10000.0)


def commission_penalty():
    return 2.0 * COMMISSION_BPS_PER_SIDE / 10000.0


# ---------------------------------------------------------------------------
# Position accounting
# ---------------------------------------------------------------------------

def model_parameters(model):
    if model == EXIT_A:
        return {"type": "TIME", "max_sessions": 10}
    if model == EXIT_B:
        return {
            "type": "PURE_ATR",
            "stop_atr": 2.0,
            "target_atr": 4.0,
            "research_horizon": RESEARCH_HORIZON_DAYS,
        }
    if model == EXIT_C:
        return {
            "type": "HYBRID",
            "stop_atr": 2.0,
            "target_atr": 4.0,
            "max_sessions": 10,
        }
    raise ValueError(model)


def is_open_position(row):
    return str(row.get("status", "")) == "OPEN"


def sessions_since(entry_date, current_index):
    dates = pd.DatetimeIndex(current_index)
    entry = pd.Timestamp(entry_date)
    return int((dates >= entry).sum())


def baseline_for_signal(signal, bars):
    entry_date = pd.Timestamp(signal["entry_date"])
    future = bars.loc[bars.index >= entry_date]
    if future.empty:
        return None

    entry_bar = future.iloc[0]
    entry_raw = float(entry_bar["open"])
    entry_sim = entry_fill(entry_raw)

    # Baseline is tracked through the same forward observation period as
    # the corresponding signal. It is updated on each daily run.
    return {
        "entry_raw": entry_raw,
        "entry_sim": entry_sim,
        "entry_date": future.index[0],
    }


# ---------------------------------------------------------------------------
# Daily update engine
# ---------------------------------------------------------------------------

def process_open_position(position, bars, as_of):
    """
    Update one virtual position with today's OHLC.
    Returns:
        updated position dict,
        optional snapshot dict,
        optional execution dict
    """
    model = position["model"]
    params = model_parameters(model)

    entry_date = pd.Timestamp(position["entry_date"])
    today = pd.Timestamp(as_of)
    future = bars.loc[(bars.index >= entry_date) & (bars.index <= today)]
    if future.empty:
        return position, None, None

    # The position is evaluated from the entry session forward.
    # Only process the current as-of bar once.
    current = future.iloc[-1]
    current_date = future.index[-1]

    entry_sim = float(position["entry_price_simulated"])
    sessions = len(future)

    prior_mfe = float(position.get("mfe") or 0.0)
    prior_mae = float(position.get("mae") or 0.0)

    high = float(current["high"])
    low = float(current["low"])
    close = float(current["close"])

    mfe = max(prior_mfe, high / entry_sim - 1.0)
    mae = min(prior_mae, low / entry_sim - 1.0)

    stop = float(position["stop_price"]) if pd.notna(position["stop_price"]) else np.nan
    target = float(position["target_price"]) if pd.notna(position["target_price"]) else np.nan

    hit_stop = np.isfinite(stop) and low <= stop
    hit_target = np.isfinite(target) and high >= target

    exit_type = None
    exit_raw = None

    if model in {EXIT_B, EXIT_C}:
        if hit_stop and hit_target:
            # Daily OHLC cannot reveal intraday order. Conservative rule:
            # stop first.
            exit_type = "STOP_FIRST_AMBIGUOUS"
            exit_raw = stop
        elif hit_stop:
            exit_type = "ATR_STOP"
            exit_raw = stop
        elif hit_target:
            exit_type = "ATR_TARGET"
            exit_raw = target

    if exit_type is None and model == EXIT_A and sessions >= 10:
        exit_type = "TIME_10D_CLOSE"
        exit_raw = close

    if exit_type is None and model == EXIT_C and sessions >= 10:
        exit_type = "TIME_10D_CLOSE"
        exit_raw = close

    if exit_type is None and model == EXIT_B and sessions >= RESEARCH_HORIZON_DAYS:
        # Data-integrity event only. It is NOT a normal trading exit.
        exit_type = "RESEARCH_HORIZON_60D"
        exit_raw = close

    gross_unrealized = close / entry_sim - 1.0
    friction = commission_penalty()
    net_unrealized = gross_unrealized - friction

    peak = max(1.0, 1.0 + mfe)
    drawdown_from_peak = (1.0 + gross_unrealized) / peak - 1.0

    snapshot = {
        "snapshot_id": str(uuid.uuid4()),
        "position_id": position["position_id"],
        "signal_id": position["signal_id"],
        "model": model,
        "symbol": position["symbol"],
        "date": current_date.isoformat(),
        "open": float(current["open"]),
        "high": high,
        "low": low,
        "close": close,
        "sessions_held": sessions,
        "unrealized_gross_return": gross_unrealized,
        "unrealized_net_return": net_unrealized,
        "mfe": mfe,
        "mae": mae,
        "drawdown_from_peak": drawdown_from_peak,
        "created_at": utc_now(),
    }

    position["mfe"] = mfe
    position["mae"] = mae
    position["sessions_held"] = sessions
    position["updated_at"] = utc_now()

    if exit_type is None:
        return position, snapshot, None

    exit_sim = exit_fill(float(exit_raw))
    gross = exit_sim / entry_sim - 1.0
    slip_penalty = (
        ENTRY_SLIPPAGE_BPS / 10000.0
        + EXIT_SLIPPAGE_BPS / 10000.0
    )
    comm_penalty = commission_penalty()
    net = gross - comm_penalty

    position.update({
        "status": "CLOSED",
        "exit_date": current_date.isoformat(),
        "exit_type": exit_type,
        "exit_price_raw": float(exit_raw),
        "exit_price_simulated": exit_sim,
        "gross_return": gross,
        "slippage_return_penalty": slip_penalty,
        "commission_return_penalty": comm_penalty,
        "net_return": net,
        "capital_days": max(1, sessions),
        "mfe_capture_ratio": (
            net / mfe if mfe > 0 else np.nan
        ),
        "updated_at": utc_now(),
    })

    execution = {
        "execution_id": str(uuid.uuid4()),
        "position_id": position["position_id"],
        "signal_id": position["signal_id"],
        "model": model,
        "symbol": position["symbol"],
        "side": "SELL",
        "date": current_date.isoformat(),
        "price_raw": float(exit_raw),
        "price_simulated": exit_sim,
        "slippage_bps": EXIT_SLIPPAGE_BPS,
        "commission_bps": COMMISSION_BPS_PER_SIDE,
        "friction_return": (
            EXIT_SLIPPAGE_BPS / 10000.0
            + COMMISSION_BPS_PER_SIDE / 10000.0
        ),
        "exit_type": exit_type,
        "created_at": utc_now(),
    }

    return position, snapshot, execution


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def profit_factor(returns):
    r = pd.Series(returns, dtype=float).dropna()
    gains = r[r > 0].sum()
    losses = -r[r < 0].sum()
    if losses == 0:
        return np.inf if gains > 0 else np.nan
    return float(gains / losses)


def max_drawdown(returns):
    r = pd.Series(returns, dtype=float).dropna()
    if r.empty:
        return np.nan
    equity = (1.0 + r).cumprod()
    return float((equity / equity.cummax() - 1.0).min())


def metric_row(date, model, closed, open_positions, baseline_mean):
    if closed.empty:
        return {
            "date": date,
            "model": model,
            "closed_trades": 0,
            "open_positions": len(open_positions),
            "gross_mean": np.nan,
            "net_mean": np.nan,
            "net_median": np.nan,
            "win_rate": np.nan,
            "profit_factor": np.nan,
            "max_drawdown": np.nan,
            "mean_mfe": np.nan,
            "mean_mae": np.nan,
            "mfe_mae_ratio": np.nan,
            "capital_efficiency": np.nan,
            "mfe_capture_ratio": np.nan,
            "baseline_mean": baseline_mean,
            "net_lift_vs_baseline": np.nan,
            "created_at": utc_now(),
        }

    net = pd.to_numeric(closed["net_return"], errors="coerce")
    gross = pd.to_numeric(closed["gross_return"], errors="coerce")
    mfe = pd.to_numeric(closed["mfe"], errors="coerce")
    mae = pd.to_numeric(closed["mae"], errors="coerce")
    capdays = pd.to_numeric(closed["capital_days"], errors="coerce")
    captures = pd.to_numeric(closed["mfe_capture_ratio"], errors="coerce")

    mean_mfe = mfe.mean()
    mean_mae = mae.mean()

    total_net = net.sum()
    total_capital_days = capdays.sum()

    # Capital efficiency is return generated per capital-day exposed.
    capital_efficiency = (
        total_net / total_capital_days
        if total_capital_days > 0 else np.nan
    )

    return {
        "date": date,
        "model": model,
        "closed_trades": len(closed),
        "open_positions": len(open_positions),
        "gross_mean": gross.mean(),
        "net_mean": net.mean(),
        "net_median": net.median(),
        "win_rate": (net > 0).mean(),
        "profit_factor": profit_factor(net),
        "max_drawdown": max_drawdown(net),
        "mean_mfe": mean_mfe,
        "mean_mae": mean_mae,
        "mfe_mae_ratio": (
            mean_mfe / abs(mean_mae)
            if np.isfinite(mean_mae) and mean_mae < 0 else np.nan
        ),
        "capital_efficiency": capital_efficiency,
        "mfe_capture_ratio": captures.mean(),
        "baseline_mean": baseline_mean,
        "net_lift_vs_baseline": (
            net.mean() - baseline_mean
            if np.isfinite(baseline_mean) else np.nan
        ),
        "created_at": utc_now(),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--date", default=None,
                   help="As-of trading date YYYY-MM-DD. Defaults to today UTC.")
    p.add_argument("--lookback-days", type=int, default=120,
                   help="Historical bars needed for indicators.")
    p.add_argument("--outdir", default="AURA_LIVE")
    return p.parse_args()


def main():
    args = parse_args()
    load_dotenv()

    key = os.getenv("ALPACA_API_KEY")
    secret = os.getenv("ALPACA_SECRET_KEY")
    if not key or not secret:
        raise RuntimeError(
            "Missing ALPACA_API_KEY / ALPACA_SECRET_KEY in .env"
        )

    as_of = args.date or datetime.now(timezone.utc).date().isoformat()
    as_of_ts = pd.Timestamp(as_of, tz="UTC")
    start = (as_of_ts - pd.Timedelta(days=args.lookback_days)).isoformat()
    end = (as_of_ts + pd.Timedelta(days=1)).isoformat()

    outdir = Path(args.outdir)
    ensure_files(outdir)
    write_universe_once(outdir, as_of)

    client = StockHistoricalDataClient(key, secret)

    signals = read_csv(outdir, "signals.csv")
    positions = read_csv(outdir, "positions.csv")
    snapshots_existing = read_csv(outdir, "position_snapshots.csv")

    # ------------------------------------------------------------------
    # 1. Scan frozen universe for today's signal.
    # ------------------------------------------------------------------
    signal_rows = []
    signal_counter = 1

    print("=" * 78)
    print("AURA v0.4.8 — PARALLEL LIVE PAPER LAB")
    print("=" * 78)
    print(f"As-of date: {as_of}")
    print(f"Universe: {UNIVERSE_VERSION} ({len(S_AND_P_100)} symbols)")
    print("Signal: EMA3/EMA8 crossover + MACD histogram > 0 + RelVol >= 1")
    print("A: 10D CLOSE | B: PURE ATR 2x/4x | C: ATR 2x/4x + 10D CLOSE")
    print("B horizon: 60 sessions — RESEARCH ONLY")
    print("Friction: 5bp entry + 5bp exit + 1bp commission per side")
    print("PAPER ONLY — NO ORDERS")
    print()

    bars_cache = {}

    for i, symbol in enumerate(S_AND_P_100, start=1):
        try:
            bars = fetch_daily(client, symbol, start, end)
            if bars.empty:
                print(f"[{i:03d}/{len(S_AND_P_100)}] {symbol}: no data")
                continue

            bars = add_features(bars)
            bars_cache[symbol] = bars

            # Need a completed decision bar on the as-of date.
            decision_candidates = bars.loc[bars.index.date <= as_of_ts.date()]
            if decision_candidates.empty:
                continue

            decision_idx = decision_candidates.index[-1]
            row = decision_candidates.iloc[-1]

            signal = (
                bool(row["bullish_crossover"])
                and float(row["macd_hist"]) > 0
                and float(row["relative_volume"]) >= 1.0
                and np.isfinite(float(row["atr14"]))
            )

            if not signal:
                continue

            # Entry is next trading session OPEN. We do not create a
            # pending trade if that next bar is not yet available.
            after = bars.loc[bars.index > decision_idx]
            if after.empty:
                status = "PENDING_NEXT_OPEN"
                entry_date = ""
            else:
                entry_date = after.index[0].isoformat()
                status = "PENDING_NEXT_OPEN"

            signal_id = make_signal_id(as_of, symbol, signal_counter)
            signal_counter += 1

            # Do not duplicate an immutable signal if the script is rerun.
            if not signals.empty and signal_id in signals["signal_id"].astype(str).values:
                continue

            signal_rows.append({
                "signal_id": signal_id,
                "signal_version": SIGNAL_VERSION,
                "universe_version": UNIVERSE_VERSION,
                "symbol": symbol,
                "decision_date": decision_idx.isoformat(),
                "entry_date": entry_date,
                "signal_close": float(row["close"]),
                "ema3": float(row["ema3"]),
                "ema8": float(row["ema8"]),
                "macd_hist": float(row["macd_hist"]),
                "relative_volume": float(row["relative_volume"]),
                "atr14": float(row["atr14"]),
                "status": status,
                "created_at": utc_now(),
            })

            print(
                f"[{i:03d}/{len(S_AND_P_100)}] {symbol}: SIGNAL "
                f"RelVol={row['relative_volume']:.2f} ATR={row['atr14']:.2f}"
            )

        except Exception as exc:
            print(f"[{i:03d}/{len(S_AND_P_100)}] {symbol}: FAILED — {exc}")

    append_rows(outdir, "signals.csv", signal_rows)

    # Refresh signals after append.
    signals = read_csv(outdir, "signals.csv")
    positions = read_csv(outdir, "positions.csv")

    # ------------------------------------------------------------------
    # 2. Materialize next-open virtual positions for any new signals.
    # ------------------------------------------------------------------
    new_positions = []
    execution_rows = []

    for _, sig in signals.iterrows():
        if not sig["entry_date"] or sig["status"] not in {"PENDING_NEXT_OPEN", "ENTERED"}:
            continue

        existing_for_signal = positions.loc[
            positions["signal_id"].astype(str) == str(sig["signal_id"])
        ] if not positions.empty else pd.DataFrame()

        if not existing_for_signal.empty:
            continue

        symbol = sig["symbol"]
        bars = bars_cache.get(symbol)
        if bars is None:
            # Fetch enough data again if this is an old signal.
            bars = fetch_daily(client, symbol, start, end)
            if bars.empty:
                continue
            bars = add_features(bars)
            bars_cache[symbol] = bars

        entry_date = pd.Timestamp(sig["entry_date"])
        entry_bar = bars.loc[bars.index == entry_date]
        if entry_bar.empty:
            continue

        raw = float(entry_bar.iloc[0]["open"])
        sim = entry_fill(raw)
        atr = float(sig["atr14"])

        for model in [EXIT_A, EXIT_B, EXIT_C]:
            params = model_parameters(model)

            stop = (
                sim - params["stop_atr"] * atr
                if "stop_atr" in params else np.nan
            )
            target = (
                sim + params["target_atr"] * atr
                if "target_atr" in params else np.nan
            )

            position_id = f"{sig['signal_id']}-{model}"

            new_positions.append({
                "position_id": position_id,
                "signal_id": sig["signal_id"],
                "model": model,
                "symbol": symbol,
                "status": "OPEN",
                "entry_date": entry_date.isoformat(),
                "entry_price_raw": raw,
                "entry_price_simulated": sim,
                "entry_atr": atr,
                "stop_price": stop,
                "target_price": target,
                "sessions_held": 0,
                "exit_date": "",
                "exit_type": "",
                "exit_price_raw": np.nan,
                "exit_price_simulated": np.nan,
                "gross_return": np.nan,
                "slippage_return_penalty": ENTRY_SLIPPAGE_BPS / 10000.0,
                "commission_return_penalty": np.nan,
                "net_return": np.nan,
                "mfe": 0.0,
                "mae": 0.0,
                "capital_days": 0,
                "mfe_capture_ratio": np.nan,
                "created_at": utc_now(),
                "updated_at": utc_now(),
            })

            execution_rows.append({
                "execution_id": str(uuid.uuid4()),
                "position_id": position_id,
                "signal_id": sig["signal_id"],
                "model": model,
                "symbol": symbol,
                "side": "BUY",
                "date": entry_date.isoformat(),
                "price_raw": raw,
                "price_simulated": sim,
                "slippage_bps": ENTRY_SLIPPAGE_BPS,
                "commission_bps": COMMISSION_BPS_PER_SIDE,
                "friction_return": (
                    ENTRY_SLIPPAGE_BPS / 10000.0
                    + COMMISSION_BPS_PER_SIDE / 10000.0
                ),
                "exit_type": "",
                "created_at": utc_now(),
            })

    append_rows(outdir, "positions.csv", new_positions)
    append_rows(outdir, "executions.csv", execution_rows)

    positions = read_csv(outdir, "positions.csv")

    # ------------------------------------------------------------------
    # 3. Update open positions with current market path.
    # ------------------------------------------------------------------
    snapshot_rows = []
    exit_execution_rows = []
    updated_positions = []

    for _, p in positions.iterrows():
        if str(p["status"]) != "OPEN":
            updated_positions.append(p.to_dict())
            continue

        symbol = p["symbol"]
        bars = bars_cache.get(symbol)
        if bars is None:
            bars = fetch_daily(client, symbol, start, end)
            if bars.empty:
                updated_positions.append(p.to_dict())
                continue
            bars = add_features(bars)
            bars_cache[symbol] = bars

        new_p, snapshot, execution = process_open_position(
            p.to_dict(), bars, as_of
        )

        if snapshot is not None:
            # Avoid duplicate same-position/same-date snapshot on rerun.
            duplicate = (
                not snapshots_existing.empty
                and (
                    (snapshots_existing["position_id"].astype(str)
                     == str(snapshot["position_id"]))
                    & (pd.to_datetime(
                        snapshots_existing["date"], errors="coerce"
                    ).dt.date
                       == pd.Timestamp(snapshot["date"]).date())
                ).any()
            )
            if not duplicate:
                snapshot_rows.append(snapshot)

        if execution is not None:
            exit_execution_rows.append(execution)

        updated_positions.append(new_p)

    # Rewrite positions atomically with the current state.
    positions_df = pd.DataFrame(updated_positions)
    positions_df = positions_df.reindex(columns=SCHEMAS["positions.csv"])
    positions_df.to_csv(outdir / "positions.csv", index=False)

    append_rows(outdir, "position_snapshots.csv", snapshot_rows)
    append_rows(outdir, "executions.csv", exit_execution_rows)

    # ------------------------------------------------------------------
    # 4. Update baseline records.
    # ------------------------------------------------------------------
    baseline_df = read_csv(outdir, "baseline.csv")
    baseline_rows = []

    signals = read_csv(outdir, "signals.csv")
    for _, sig in signals.iterrows():
        signal_id = str(sig["signal_id"])
        if not sig["entry_date"]:
            continue

        existing = baseline_df.loc[
            baseline_df["signal_id"].astype(str) == signal_id
        ] if not baseline_df.empty else pd.DataFrame()

        symbol = sig["symbol"]
        bars = bars_cache.get(symbol)
        if bars is None or bars.empty:
            continue

        future = bars.loc[bars.index >= pd.Timestamp(sig["entry_date"])]
        if future.empty:
            continue

        entry_bar = future.iloc[0]
        entry_raw = float(entry_bar["open"])
        entry_sim = entry_fill(entry_raw)

        # Baseline remains open while the signal's longest AURA model is
        # still within its observation window. Once all three close, close
        # the baseline on the same market close as the last model exit.
        related_positions = positions_df.loc[
            positions_df["signal_id"].astype(str) == signal_id
        ]

        closed = related_positions.loc[
            related_positions["status"].astype(str) == "CLOSED"
        ] if not related_positions.empty else pd.DataFrame()

        if not closed.empty:
            last_exit = pd.to_datetime(closed["exit_date"], errors="coerce").max()
            if pd.notna(last_exit):
                exit_candidates = bars.loc[bars.index <= last_exit]
                if not exit_candidates.empty:
                    exit_bar = exit_candidates.iloc[-1]
                    exit_date = exit_candidates.index[-1]
                    exit_raw = float(exit_bar["close"])
                    exit_sim = exit_fill(exit_raw)
                    gross = exit_sim / entry_sim - 1.0
                    net = gross - commission_penalty()

                    row = {
                        "baseline_id": f"{signal_id}-M",
                        "signal_id": signal_id,
                        "symbol": symbol,
                        "entry_date": future.index[0].isoformat(),
                        "entry_price_raw": entry_raw,
                        "entry_price_simulated": entry_sim,
                        "exit_date": exit_date.isoformat(),
                        "exit_price_raw": exit_raw,
                        "exit_price_simulated": exit_sim,
                        "gross_return": gross,
                        "net_return": net,
                        "holding_sessions": len(
                            bars.loc[
                                (bars.index >= future.index[0])
                                & (bars.index <= exit_date)
                            ]
                        ),
                        "created_at": utc_now(),
                        "updated_at": utc_now(),
                    }
                    if existing.empty:
                        baseline_rows.append(row)
                    continue

        # Open baseline record.
        row = {
            "baseline_id": f"{signal_id}-M",
            "signal_id": signal_id,
            "symbol": symbol,
            "entry_date": future.index[0].isoformat(),
            "entry_price_raw": entry_raw,
            "entry_price_simulated": entry_sim,
            "exit_date": "",
            "exit_price_raw": np.nan,
            "exit_price_simulated": np.nan,
            "gross_return": np.nan,
            "net_return": np.nan,
            "holding_sessions": len(
                bars.loc[
                    (bars.index >= future.index[0])
                    & (bars.index <= as_of_ts)
                ]
            ),
            "created_at": utc_now(),
            "updated_at": utc_now(),
        }

        if existing.empty:
            baseline_rows.append(row)

    append_rows(outdir, "baseline.csv", baseline_rows)

    # ------------------------------------------------------------------
    # 5. Daily model metrics.
    # ------------------------------------------------------------------
    positions_df = read_csv(outdir, "positions.csv")
    baseline_df = read_csv(outdir, "baseline.csv")

    for model in [EXIT_A, EXIT_B, EXIT_C]:
        closed = positions_df.loc[
            (positions_df["model"].astype(str) == model)
            & (positions_df["status"].astype(str) == "CLOSED")
        ].copy()

        open_p = positions_df.loc[
            (positions_df["model"].astype(str) == model)
            & (positions_df["status"].astype(str) == "OPEN")
        ]

        base_returns = pd.to_numeric(
            baseline_df["net_return"], errors="coerce"
        ).dropna() if not baseline_df.empty else pd.Series(dtype=float)

        base_mean = base_returns.mean() if not base_returns.empty else np.nan

        metrics = metric_row(
            as_of, model, closed, open_p, base_mean
        )

        # Replace existing metrics for same date/model to make reruns idempotent.
        metrics_path = outdir / "daily_model_metrics.csv"
        existing_metrics = read_csv(outdir, "daily_model_metrics.csv")
        if not existing_metrics.empty:
            existing_metrics = existing_metrics.loc[
                ~(
                    (existing_metrics["date"].astype(str) == str(as_of))
                    & (existing_metrics["model"].astype(str) == model)
                )
            ]
        existing_metrics = pd.concat(
            [existing_metrics, pd.DataFrame([metrics])],
            ignore_index=True
        )
        existing_metrics.to_csv(metrics_path, index=False)

    # ------------------------------------------------------------------
    # 6. Final console report.
    # ------------------------------------------------------------------
    positions_df = read_csv(outdir, "positions.csv")
    metrics_df = read_csv(outdir, "daily_model_metrics.csv")

    print()
    print("=" * 78)
    print("AURA v0.4.8 — DAILY PAPER-LAB STATUS")
    print("=" * 78)

    for model in [EXIT_A, EXIT_B, EXIT_C]:
        op = positions_df.loc[
            (positions_df["model"].astype(str) == model)
            & (positions_df["status"].astype(str) == "OPEN")
        ]
        cl = positions_df.loc[
            (positions_df["model"].astype(str) == model)
            & (positions_df["status"].astype(str) == "CLOSED")
        ]

        print(f"\n{model}")
        print(f"  Open positions : {len(op)}")
        print(f"  Closed trades  : {len(cl)}")

        if not cl.empty:
            net = pd.to_numeric(cl["net_return"], errors="coerce").dropna()
            print(f"  Net mean       : {net.mean():.4%}")
            print(f"  Net median     : {net.median():.4%}")
            print(f"  Win rate       : {(net > 0).mean():.2%}")
            print(f"  Profit factor  : {profit_factor(net):.3f}")

    print()
    print("Frozen signal definition:")
    print("  EMA3/EMA8 bullish crossover + MACD histogram > 0 + RelVol >= 1")
    print("Frozen friction: 12 bp round trip")
    print("S&P 100 universe is immutable once universe.csv exists.")
    print("No Alpaca order submission is implemented.")
    print()
    print("Files:")
    for filename in SCHEMAS:
        print(f"  {outdir / filename}")
    print()
    print("AURA v0.4.8 RUN COMPLETE — PAPER ONLY")


if __name__ == "__main__":
    main()
