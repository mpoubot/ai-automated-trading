
"""
AURA v0.4.7 — Fresh Holdout & Exit Validation Engine

RESEARCH ONLY — NO ORDERS.

Frozen entry survivor:
    EMA38_CROSSOVER + MACD_HIST_GT_0 + REL_VOLUME_GE_1

Frozen exit philosophies:
    A — 10 trading-day time exit
    B — 2x ATR stop / 4x ATR target
    C — 2x ATR stop / 4x ATR target / maximum 10 trading-day hold

Fresh symbol holdout:
    GOOGL AMD INTC JPM MS LLY PFE AMZN HD HON PG XOM

These symbols are intentionally not loaded from v0.4.3 events. Fresh OHLC
data are fetched directly from Alpaca.

Execution-friction model:
    entry slippage = 5 bps
    exit slippage  = 5 bps
    commission is reported separately as an optional basis-point assumption.

No parameter optimization is performed in this version.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import numpy as np
import pandas as pd
from dotenv import load_dotenv
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import DataFeed


SYMBOLS = [
    "GOOGL", "AMD", "INTC",
    "JPM", "MS",
    "LLY", "PFE",
    "AMZN", "HD", "HON",
    "PG", "XOM",
]

ENTRY_SLIPPAGE_BPS = 5.0
EXIT_SLIPPAGE_BPS = 5.0
COMMISSION_BPS = 0.0  # Reported separately; set only if competition fee is known.

START_DATE = "2024-01-01"
END_DATE = "2026-08-25"

# Frozen exit philosophies — DO NOT OPTIMIZE.
RESEARCH_HORIZON_DAYS = 60

EXIT_POLICIES = {
    # A: exactly 10 complete trading sessions; exit at the 10th
    # session CLOSE. No stop and no target.
    "A_10D_TIME": {"type": "TIME", "max_days": 10},

    # B: pure volatility exit. No time exit. The 60-session limit is
    # ONLY a research/data-integrity horizon, not a trading rule.
    "B_ATR_2_4": {
        "type": "ATR_TARGET",
        "stop_atr": 2.0,
        "target_atr": 4.0,
        "max_days": RESEARCH_HORIZON_DAYS,
        "research_horizon": True,
    },

    # C: ATR stop/target, but force-close at the 10th complete
    # trading session CLOSE if neither is hit first.
    "C_ATR_2_4_10D": {
        "type": "HYBRID",
        "stop_atr": 2.0,
        "target_atr": 4.0,
        "max_days": 10,
        "research_horizon": False,
    },
}


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--start", default=START_DATE)
    p.add_argument("--end", default=END_DATE)
    p.add_argument("--outdir", default="research")
    p.add_argument("--commission-bps", type=float, default=COMMISSION_BPS)
    return p.parse_args()


def atr14(df):
    prev = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev).abs(),
        (df["low"] - prev).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(14, min_periods=14).mean()


def add_features(df):
    x = df.copy()
    x["ema3"] = x["close"].ewm(span=3, adjust=False).mean()
    x["ema8"] = x["close"].ewm(span=8, adjust=False).mean()

    ema_fast = x["close"].ewm(span=12, adjust=False).mean()
    ema_slow = x["close"].ewm(span=26, adjust=False).mean()
    x["macd"] = ema_fast - ema_slow
    x["macd_signal"] = x["macd"].ewm(span=9, adjust=False).mean()
    x["macd_hist"] = x["macd"] - x["macd_signal"]

    x["vol_ma20"] = x["volume"].rolling(20, min_periods=20).mean()
    x["rel_volume"] = x["volume"] / x["vol_ma20"]

    x["ema38_bull"] = x["ema3"] > x["ema8"]
    x["bullish_crossover"] = (
        x["ema38_bull"]
        & (~x["ema38_bull"].shift(1, fill_value=False))
    )
    x["atr14"] = atr14(x)

    return x


def fetch_bars(client, symbol, start, end):
    req = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Day,
        start=start,
        end=end,
        feed=DataFeed.IEX,
    )
    df = client.get_stock_bars(req).df
    if df.empty:
        return pd.DataFrame()

    if isinstance(df.index, pd.MultiIndex):
        try:
            df = df.xs(symbol, level="symbol")
        except KeyError:
            return pd.DataFrame()

    df = df.copy()
    df.index = pd.to_datetime(df.index, utc=True)
    return df[["open", "high", "low", "close", "volume"]].sort_index()


def discover_signals(symbol, bars):
    x = add_features(bars)

    signal_mask = (
        x["bullish_crossover"].fillna(False).astype(bool)
        & (x["macd_hist"] > 0)
        & (x["rel_volume"] >= 1.0)
    )

    positions = np.flatnonzero(signal_mask.to_numpy())
    rows = []

    # Signal is known on decision bar; entry is next trading day's open.
    for pos in positions:
        if pos + 1 >= len(x):
            continue

        decision_time = x.index[pos]
        entry_bar = x.iloc[pos + 1]

        if not np.isfinite(entry_bar["open"]):
            continue

        rows.append({
            "symbol": symbol,
            "decision_time": decision_time,
            "entry_time": x.index[pos + 1],
            "signal_close": float(x.iloc[pos]["close"]),
            "entry_open_raw": float(entry_bar["open"]),
            "entry_atr": float(entry_bar["atr14"]) if np.isfinite(entry_bar["atr14"]) else np.nan,
            "macd_hist": float(x.iloc[pos]["macd_hist"]),
            "rel_volume": float(x.iloc[pos]["rel_volume"]),
        })

    return pd.DataFrame(rows)


def apply_entry_slippage(price):
    return price * (1.0 + ENTRY_SLIPPAGE_BPS / 10000.0)


def apply_exit_slippage(price):
    return price * (1.0 - EXIT_SLIPPAGE_BPS / 10000.0)


def simulate(symbol, bars, signal, policy, commission_bps):
    entry_time = signal["entry_time"]
    future = bars.loc[bars.index >= entry_time].copy()
    if future.empty:
        return None

    # Use the actual next-day opening bar.
    entry_bar = future.iloc[0]
    raw_entry = float(entry_bar["open"])
    entry_price = apply_entry_slippage(raw_entry)

    entry_atr = float(entry_bar["atr14"])
    if not np.isfinite(entry_atr):
        return None

    max_days = policy["max_days"]
    segment = future.iloc[:max_days]
    if segment.empty:
        return None

    stop = entry_price - policy.get("stop_atr", np.nan) * entry_atr
    target = entry_price + policy.get("target_atr", np.nan) * entry_atr

    mfe = 0.0
    mae = 0.0
    ambiguous = False
    stop_hit = False
    target_hit = False
    exit_type = "TIME_CAP"
    exit_time = segment.index[-1]
    exit_raw = float(segment.iloc[-1]["close"])

    for i, (ts, row) in enumerate(segment.iterrows(), start=1):
        high = float(row["high"])
        low = float(row["low"])

        mfe = max(mfe, high / entry_price - 1.0)
        mae = min(mae, low / entry_price - 1.0)

        use_stop = policy["type"] in {"ATR_TARGET", "HYBRID"}
        use_target = policy["type"] in {"ATR_TARGET", "HYBRID"}

        hit_stop = use_stop and low <= stop
        hit_target = use_target and high >= target

        if hit_stop and hit_target:
            # Daily OHLC cannot establish intraday order.
            ambiguous = True
            stop_hit = True
            target_hit = True
            exit_type = "STOP_FIRST_AMBIGUOUS"
            exit_time = ts
            exit_raw = stop
            break

        if hit_stop:
            stop_hit = True
            exit_type = "STOP"
            exit_time = ts
            exit_raw = stop
            break

        if hit_target:
            target_hit = True
            exit_type = "TARGET"
            exit_time = ts
            exit_raw = target
            break

    exit_price = apply_exit_slippage(float(exit_raw))
    gross_return = exit_price / entry_price - 1.0

    # Commission is modelled as round-trip percentage friction.
    commission_roundtrip = 2.0 * commission_bps / 10000.0
    net_return = gross_return - commission_roundtrip

    if policy.get("research_horizon", False) and exit_type == "TIME_CAP":
        exit_type = "RESEARCH_HORIZON_60D"

    return {
        "symbol": symbol,
        "decision_time": signal["decision_time"],
        "entry_time": entry_time,
        "exit_time": exit_time,
        "policy": policy_name(policy),
        "entry_price": entry_price,
        "exit_price": exit_price,
        "gross_return": gross_return,
        "commission_return_penalty": commission_roundtrip,
        "net_return": net_return,
        "mfe": mfe,
        "mae": mae,
        "stop_hit": stop_hit,
        "target_hit": target_hit,
        "ambiguous": ambiguous,
        "exit_type": exit_type,
        "holding_days": int(
            max(1, (pd.Timestamp(exit_time) - pd.Timestamp(entry_time)).days)
        ),
        "entry_atr": entry_atr,
    }


def policy_name(policy):
    if policy["type"] == "TIME":
        return "A_10D_TIME"
    if policy["type"] == "ATR_TARGET":
        return "B_ATR_2x_4x"
    if policy["type"] == "HYBRID":
        return "C_ATR_2x_4x_MAX10D"
    return "UNKNOWN"


def max_drawdown(returns):
    r = pd.Series(returns).dropna()
    if r.empty:
        return np.nan
    equity = (1.0 + r).cumprod()
    return float((equity / equity.cummax() - 1.0).min())


def profit_factor(returns):
    r = pd.Series(returns).dropna()
    gains = r[r > 0].sum()
    losses = -r[r < 0].sum()
    if losses == 0:
        return np.inf if gains > 0 else np.nan
    return float(gains / losses)


def summarize(df):
    if df.empty:
        return {}

    r = df["net_return"].astype(float)
    gross = df["gross_return"].astype(float)
    mfe = df["mfe"].astype(float)
    mae = df["mae"].astype(float)

    mfe_mean = mfe.mean()
    mae_mean = mae.mean()

    return {
        "signals": len(df),
        "symbols": df["symbol"].nunique(),
        "gross_mean": gross.mean(),
        "gross_median": gross.median(),
        "net_mean": r.mean(),
        "net_median": r.median(),
        "net_positive_rate": (r > 0).mean(),
        "net_profit_factor": profit_factor(r),
        "net_max_drawdown": max_drawdown(r),
        "mfe_mean": mfe_mean,
        "mfe_median": mfe.median(),
        "mae_mean": mae_mean,
        "mae_median": mae.median(),
        "mfe_mae_ratio": (
            mfe_mean / abs(mae_mean)
            if np.isfinite(mfe_mean) and np.isfinite(mae_mean) and mae_mean < 0
            else np.nan
        ),
        "stop_hit_rate": df["stop_hit"].mean(),
        "target_hit_rate": df["target_hit"].mean(),
        "ambiguous_rate": df["ambiguous"].mean(),
        "research_horizon_exit_rate": (
            (df["exit_type"] == "RESEARCH_HORIZON_60D").mean()
            if "exit_type" in df.columns else np.nan
        ),
        "p10_net": r.quantile(.10),
        "p25_net": r.quantile(.25),
        "p75_net": r.quantile(.75),
        "p90_net": r.quantile(.90),
    }


def sector_map():
    return {
        "GOOGL": "Technology",
        "AMD": "Technology",
        "INTC": "Technology",
        "JPM": "Financials",
        "MS": "Financials",
        "LLY": "Healthcare",
        "PFE": "Healthcare",
        "AMZN": "Consumer",
        "HD": "Consumer",
        "HON": "Industrials",
        "PG": "Defensive",
        "XOM": "Energy",
    }


def sector_results(trades):
    smap = sector_map()
    rows = []

    for (policy, sector), g in trades.assign(
        sector=trades["symbol"].map(smap)
    ).groupby(["policy", "sector"]):
        s = summarize(g)
        s.update({
            "policy": policy,
            "sector": sector,
        })
        rows.append(s)

    return pd.DataFrame(rows)


def main():
    args = parse_args()
    load_dotenv()

    key = os.getenv("ALPACA_API_KEY")
    secret = os.getenv("ALPACA_SECRET_KEY")
    if not key or not secret:
        raise RuntimeError("Missing ALPACA_API_KEY / ALPACA_SECRET_KEY in .env")

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    client = StockHistoricalDataClient(key, secret)

    all_bars = {}
    all_signals = []

    print("=" * 72)
    print("AURA v0.4.7 — FRESH HOLDOUT & EXIT VALIDATION")
    print("=" * 72)
    print("ENTRY: EMA38 + MACD histogram > 0 + relative volume >= 1")
    print("EXITS: A=10D CLOSE | B=PURE ATR 2x/4x | C=ATR 2x/4x + 10D CLOSE")
    print("B RESEARCH HORIZON: 60 trading sessions (NOT a trading exit rule)")
    print(f"HOLDOUT: {', '.join(SYMBOLS)}")
    print(f"FRICTION: {ENTRY_SLIPPAGE_BPS:.1f} bps entry + {EXIT_SLIPPAGE_BPS:.1f} bps exit")
    print(f"COMMISSION: {args.commission_bps:.1f} bps per side")
    print("MODE: RESEARCH ONLY — NO ORDERS")
    print()

    for i, symbol in enumerate(SYMBOLS, start=1):
        try:
            bars = fetch_bars(client, symbol, args.start, args.end)
            if bars.empty:
                print(f"[{i:02d}/{len(SYMBOLS)}] {symbol}: NO DATA")
                continue

            bars = add_features(bars)
            all_bars[symbol] = bars

            signals = discover_signals(symbol, bars)
            if not signals.empty:
                all_signals.append(signals)

            print(
                f"[{i:02d}/{len(SYMBOLS)}] {symbol}: "
                f"{len(bars)} bars, {len(signals)} frozen-entry signals"
            )
        except Exception as exc:
            print(f"[{i:02d}/{len(SYMBOLS)}] {symbol}: FAILED — {exc}")

    if not all_signals:
        raise RuntimeError("No fresh-holdout signals were generated.")

    signals = pd.concat(all_signals, ignore_index=True)
    signals.to_csv(outdir / "v047_fresh_holdout_signals.csv", index=False)

    trade_rows = []
    for _, signal in signals.iterrows():
        bars = all_bars.get(signal["symbol"])
        if bars is None:
            continue

        for policy in EXIT_POLICIES.values():
            result = simulate(
                signal["symbol"],
                bars,
                signal,
                policy,
                args.commission_bps,
            )
            if result:
                trade_rows.append(result)

    trades = pd.DataFrame(trade_rows)
    if trades.empty:
        raise RuntimeError("No exit trades could be simulated.")

    summary_rows = []
    for policy, g in trades.groupby("policy"):
        s = summarize(g)
        s["policy"] = policy
        summary_rows.append(s)

    summary = pd.DataFrame(summary_rows)
    summary.to_csv(outdir / "v047_fresh_holdout_summary.csv", index=False)
    trades.to_csv(outdir / "v047_fresh_holdout_trade_results.csv", index=False)

    sectors = sector_results(trades)
    sectors.to_csv(outdir / "v047_sector_results.csv", index=False)

    # Per-symbol diagnostic: useful for checking whether one stock dominates.
    symbol_rows = []
    for (policy, symbol), g in trades.groupby(["policy", "symbol"]):
        s = summarize(g)
        s["policy"] = policy
        s["symbol"] = symbol
        symbol_rows.append(s)
    pd.DataFrame(symbol_rows).to_csv(
        outdir / "v047_symbol_results.csv", index=False
    )

    print()
    print("=" * 72)
    print("FRESH HOLDOUT VERDICT")
    print("=" * 72)
    cols = [
        "policy", "signals", "symbols",
        "gross_mean", "net_mean", "net_median",
        "net_positive_rate", "net_profit_factor",
        "net_max_drawdown", "mfe_mae_ratio",
        "stop_hit_rate", "target_hit_rate", "ambiguous_rate",
    ]
    print(summary[cols].to_string(index=False, float_format=lambda x: f"{x:.4f}"))

    print()
    print("=" * 72)
    print("SECTOR RESULTS")
    print("=" * 72)
    if sectors.empty:
        print("No sector results.")
    else:
        cols2 = ["policy", "sector", "signals", "net_mean", "net_median", "net_positive_rate"]
        print(sectors[cols2].to_string(index=False, float_format=lambda x: f"{x:.4f}"))

    print()
    print("=" * 72)
    print("IMPORTANT")
    print("=" * 72)
    print("This is the first validation on the locked 12-symbol holdout.")
    print("Do NOT replace symbols after seeing these results.")
    print("Do NOT optimize A/B/C after seeing these results.")
    print("A exits at the CLOSE of the 10th complete trading session.")
    print("B has NO time-based trading exit; 60 sessions is only a research/data-integrity horizon.")
    print("C exits at the CLOSE of the 10th complete trading session if ATR stop/target has not fired.")
    print("Daily OHLC cannot determine stop-vs-target order on an ambiguous bar.")
    print("Ambiguous bars are treated conservatively as stop-first.")
    print("A positive result is a validation signal, not proof of a universal edge.")
    print()
    print("OUTPUT:")
    for fn in [
        "v047_fresh_holdout_signals.csv",
        "v047_fresh_holdout_summary.csv",
        "v047_fresh_holdout_trade_results.csv",
        "v047_sector_results.csv",
        "v047_symbol_results.csv",
    ]:
        print(outdir / fn)

    print()
    print("AURA v0.4.7 COMPLETE — NO ORDERS WERE PLACED")


if __name__ == "__main__":
    main()
