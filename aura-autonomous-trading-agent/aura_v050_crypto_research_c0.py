"""
AURA v0.5.0 — Crypto Research Engine
Experiment C0 — Frozen Crypto Baseline

RESEARCH ONLY. NO ORDER SUBMISSION.

Purpose:
- Establish a clean, reproducible crypto control experiment.
- Use MEXC USDT perpetual market data for BTC_USDT and ETH_USDT by default.
- 1H bars.
- Frozen signal carried forward from AURA v0.4.8 as a CONTROL:
    EMA3/EMA8 bullish crossover + MACD histogram > 0 + relative volume >= 1.
- Long-only, 1x notional.
- Entry: next bar OPEN after a confirmed signal.
- Exit: CLOSE of the 10th complete held 1H bar.
- Friction: 5 bp entry slippage + 1 bp entry commission,
            5 bp exit slippage + 1 bp exit commission.
- Funding is NOT hidden inside price P&L; it is recorded separately and
  deducted into net P&L.
- MEXC public contract API only. No API key is required.

Important research rule:
C0 is a CONTROL, not a claim that this signal has a crypto edge.
Do not optimize it in this version.
"""

from __future__ import annotations

import argparse
import json
import math
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd
import requests


BASE = "https://contract.mexc.com"
DEFAULT_SYMBOLS = ["BTC_USDT", "ETH_USDT"]
DEFAULT_INTERVAL = "Min60"
DEFAULT_HOLD_BARS = 10
DEFAULT_FEE_BPS = 1.0
DEFAULT_SLIPPAGE_BPS = 5.0
MAX_KLINES = 2000

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "AURA-v0.5.0-research/1.0"})


@dataclass(frozen=True)
class Config:
    version: str = "AURA_v0.5.0"
    experiment: str = "C0"
    venue: str = "MEXC_CONTRACT"
    symbols: tuple[str, ...] = tuple(DEFAULT_SYMBOLS)
    interval: str = DEFAULT_INTERVAL
    hold_bars: int = DEFAULT_HOLD_BARS
    fee_bps: float = DEFAULT_FEE_BPS
    slippage_bps: float = DEFAULT_SLIPPAGE_BPS
    leverage: float = 1.0
    signal_definition: str = (
        "EMA3/EMA8 bullish crossover + MACD histogram > 0 + RelVol >= 1"
    )


def get_json(path: str, params: dict | None = None, retries: int = 4) -> dict:
    url = BASE + path
    last = None
    for attempt in range(retries):
        try:
            r = SESSION.get(url, params=params, timeout=20)
            r.raise_for_status()
            data = r.json()
            if not data.get("success", True):
                raise RuntimeError(f"MEXC returned failure: {data}")
            return data
        except Exception as exc:
            last = exc
            if attempt < retries - 1:
                time.sleep(1.0 + attempt)
    raise RuntimeError(f"API request failed: {url} params={params}: {last}")


def fetch_klines(symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
    """Fetch 1H contract klines in chunks. MEXC allows up to 2000 bars/request."""
    start_ts = int(start.timestamp())
    end_ts = int(end.timestamp())
    rows: list[dict] = []

    # Walk forward. One 1H bar = 3600 seconds.
    cursor = start_ts
    while cursor < end_ts:
        chunk_end = min(end_ts, cursor + MAX_KLINES * 3600 - 1)
        data = get_json(
            f"/api/v1/contract/kline/{symbol}",
            {
                "interval": "Min60",
                "start": cursor,
                "end": chunk_end,
            },
        ).get("data", {})

        if not data or not data.get("time"):
            break

        n = len(data["time"])
        for i in range(n):
            rows.append(
                {
                    "symbol": symbol,
                    "timestamp": pd.to_datetime(int(data["time"][i]), unit="s", utc=True),
                    "open": float(data["open"][i]),
                    "high": float(data["high"][i]),
                    "low": float(data["low"][i]),
                    "close": float(data["close"][i]),
                    "volume": float(data["vol"][i]),
                    "amount": float(data["amount"][i]),
                }
            )

        last_ts = int(data["time"][-1])
        next_cursor = last_ts + 3600
        if next_cursor <= cursor:
            break
        cursor = next_cursor
        time.sleep(0.12)

    df = pd.DataFrame(rows)
    if df.empty:
        return df

    df = df.drop_duplicates(["symbol", "timestamp"]).sort_values("timestamp")
    return df.reset_index(drop=True)


def fetch_funding_history(
    symbol: str, start: datetime, end: datetime
) -> pd.DataFrame:
    """Fetch public funding history and retain settlement timestamps."""
    rows: list[dict] = []
    page = 1

    while True:
        data = get_json(
            "/api/v1/contract/funding_rate/history",
            {
                "symbol": symbol,
                "page_num": page,
                "page_size": 1000,
            },
        ).get("data", {})

        items = data.get("resultList", []) if isinstance(data, dict) else []
        if not items:
            break

        for item in items:
            ts = pd.to_datetime(int(item["settleTime"]), unit="ms", utc=True)
            if start - timedelta(days=2) <= ts <= end + timedelta(days=2):
                rows.append(
                    {
                        "symbol": symbol,
                        "settle_time": ts,
                        "funding_rate": float(item["fundingRate"]),
                    }
                )

        total_pages = int(data.get("totalPage", page))
        if page >= total_pages:
            break
        page += 1
        time.sleep(0.12)

    df = pd.DataFrame(rows)
    if df.empty:
        return df

    return (
        df.drop_duplicates(["symbol", "settle_time"])
        .sort_values("settle_time")
        .reset_index(drop=True)
    )


def add_features(df: pd.DataFrame) -> pd.DataFrame:
    x = df.copy()

    x["ema_3"] = x["close"].ewm(span=3, adjust=False).mean()
    x["ema_8"] = x["close"].ewm(span=8, adjust=False).mean()

    ema12 = x["close"].ewm(span=12, adjust=False).mean()
    ema26 = x["close"].ewm(span=26, adjust=False).mean()
    x["macd"] = ema12 - ema26
    x["macd_signal"] = x["macd"].ewm(span=9, adjust=False).mean()
    x["macd_hist"] = x["macd"] - x["macd_signal"]

    # 20-bar relative volume. Current bar / trailing mean excluding current bar.
    vol_base = x["volume"].rolling(20, min_periods=20).mean().shift(1)
    x["rel_volume"] = x["volume"] / vol_base

    x["atr_14"] = (
        pd.concat(
            [
                x["high"] - x["low"],
                (x["high"] - x["close"].shift(1)).abs(),
                (x["low"] - x["close"].shift(1)).abs(),
            ],
            axis=1,
        )
        .max(axis=1)
        .rolling(14, min_periods=14)
        .mean()
    )

    x["bullish_crossover"] = (
        (x["ema_3"] > x["ema_8"])
        & (x["ema_3"].shift(1) <= x["ema_8"].shift(1))
    )

    x["signal"] = (
        x["bullish_crossover"]
        & (x["macd_hist"] > 0)
        & (x["rel_volume"] >= 1.0)
    )

    return x


def funding_for_trade(
    funding: pd.DataFrame,
    entry_time: pd.Timestamp,
    exit_time: pd.Timestamp,
) -> float:
    """
    Long funding return:
      positive funding rate => long pays => negative contribution.
      negative funding rate => long receives => positive contribution.

    The return is the sum of settlement funding rates occurring while
    the position is open. This is deliberately kept separate in the ledger.
    """
    if funding.empty:
        return 0.0

    m = funding[
        (funding["settle_time"] > entry_time)
        & (funding["settle_time"] <= exit_time)
    ]
    if m.empty:
        return 0.0

    return float(-m["funding_rate"].sum())


def simulate_symbol(
    x: pd.DataFrame,
    funding: pd.DataFrame,
    config: Config,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    trades: list[dict] = []
    snapshots: list[dict] = []

    # Signal is known at close t. Entry is next bar open.
    i = 0
    trade_id = 0

    while i < len(x) - config.hold_bars:
        row = x.iloc[i]
        if not bool(row["signal"]):
            i += 1
            continue

        entry_i = i + 1
        exit_i = entry_i + config.hold_bars - 1
        if exit_i >= len(x):
            break

        entry_bar = x.iloc[entry_i]
        exit_bar = x.iloc[exit_i]

        raw_entry = float(entry_bar["open"])
        raw_exit = float(exit_bar["close"])

        entry_exec = raw_entry * (1.0 + config.slippage_bps / 10000.0)
        exit_exec = raw_exit * (1.0 - config.slippage_bps / 10000.0)

        price_return = exit_exec / entry_exec - 1.0
        commission_return = -2.0 * config.fee_bps / 10000.0
        funding_return = funding_for_trade(
            funding, entry_bar["timestamp"], exit_bar["timestamp"]
        )
        net_return = price_return + commission_return + funding_return

        path = x.iloc[entry_i : exit_i + 1]
        mfe = float(path["high"].max() / entry_exec - 1.0)
        mae = float(path["low"].min() / entry_exec - 1.0)

        trade_id += 1
        trade = {
            "trade_id": f"C0-{x.iloc[i]['symbol']}-{trade_id:05d}",
            "symbol": x.iloc[i]["symbol"],
            "signal_timestamp": x.iloc[i]["timestamp"].isoformat(),
            "entry_timestamp": entry_bar["timestamp"].isoformat(),
            "exit_timestamp": exit_bar["timestamp"].isoformat(),
            "entry_price_raw": raw_entry,
            "entry_price_exec": entry_exec,
            "exit_price_raw": raw_exit,
            "exit_price_exec": exit_exec,
            "price_return": price_return,
            "commission_return": commission_return,
            "funding_return": funding_return,
            "net_return": net_return,
            "mfe": mfe,
            "mae": mae,
            "mfe_mae_ratio": (
                mfe / abs(mae) if mae < 0 else np.nan
            ),
            "hold_bars": config.hold_bars,
            "exit_reason": "TIME_10H_CLOSE",
        }
        trades.append(trade)

        equity = 1.0
        for j, (_, bar) in enumerate(path.iterrows(), start=1):
            mark = float(bar["close"]) / entry_exec - 1.0
            snap = {
                "trade_id": trade["trade_id"],
                "symbol": trade["symbol"],
                "snapshot_timestamp": bar["timestamp"].isoformat(),
                "held_bar": j,
                "mark_price": float(bar["close"]),
                "path_return_before_costs": mark,
                "mfe_to_date": float(path.iloc[:j]["high"].max() / entry_exec - 1.0),
                "mae_to_date": float(path.iloc[:j]["low"].min() / entry_exec - 1.0),
                "funding_return_to_date": funding_for_trade(
                    funding, entry_bar["timestamp"], bar["timestamp"]
                ),
            }
            snapshots.append(snap)

        # Baseline deliberately does not overlap positions on the same symbol.
        i = exit_i + 1

    return pd.DataFrame(trades), pd.DataFrame(snapshots)


def safe_mean(s: pd.Series) -> float:
    z = pd.to_numeric(s, errors="coerce").dropna()
    return float(z.mean()) if len(z) else np.nan


def safe_median(s: pd.Series) -> float:
    z = pd.to_numeric(s, errors="coerce").dropna()
    return float(z.median()) if len(z) else np.nan


def profit_factor(returns: pd.Series) -> float:
    z = pd.to_numeric(returns, errors="coerce").dropna()
    gains = z[z > 0].sum()
    losses = -z[z < 0].sum()
    if losses == 0:
        return np.inf if gains > 0 else np.nan
    return float(gains / losses)


def max_drawdown(returns: pd.Series) -> float:
    z = pd.to_numeric(returns, errors="coerce").fillna(0.0)
    equity = (1.0 + z).cumprod()
    peak = equity.cummax()
    dd = equity / peak - 1.0
    return float(dd.min()) if len(dd) else np.nan


def summarize(
    trades: pd.DataFrame,
    symbol_count: int,
    config: Config,
) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame(
            [
                {
                    "experiment": config.experiment,
                    "trades": 0,
                    "symbols": symbol_count,
                    "gross_mean": np.nan,
                    "net_mean": np.nan,
                    "net_median": np.nan,
                    "win_rate": np.nan,
                    "profit_factor": np.nan,
                    "max_drawdown": np.nan,
                    "mean_mfe": np.nan,
                    "mean_mae": np.nan,
                    "mfe_mae_ratio": np.nan,
                    "mean_funding_return": np.nan,
                    "mean_commission_return": np.nan,
                }
            ]
        )

    return pd.DataFrame(
        [
            {
                "experiment": config.experiment,
                "trades": len(trades),
                "symbols": symbol_count,
                "gross_mean": safe_mean(trades["price_return"]),
                "net_mean": safe_mean(trades["net_return"]),
                "net_median": safe_median(trades["net_return"]),
                "win_rate": float((trades["net_return"] > 0).mean()),
                "profit_factor": profit_factor(trades["net_return"]),
                "max_drawdown": max_drawdown(trades["net_return"]),
                "mean_mfe": safe_mean(trades["mfe"]),
                "mean_mae": safe_mean(trades["mae"]),
                "mfe_mae_ratio": safe_mean(trades["mfe_mae_ratio"]),
                "mean_funding_return": safe_mean(trades["funding_return"]),
                "mean_commission_return": safe_mean(trades["commission_return"]),
            }
        ]
    )


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="AURA v0.5.0 Crypto C0 research baseline")
    p.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS)
    p.add_argument("--days", type=int, default=180)
    p.add_argument("--output", default="AURA_CRYPTO")
    p.add_argument("--fee-bps", type=float, default=DEFAULT_FEE_BPS)
    p.add_argument("--slippage-bps", type=float, default=DEFAULT_SLIPPAGE_BPS)
    p.add_argument("--hold-bars", type=int, default=DEFAULT_HOLD_BARS)
    return p.parse_args()


def main() -> None:
    args = parse_args()

    end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(days=args.days)

    config = Config(
        symbols=tuple(args.symbols),
        hold_bars=args.hold_bars,
        fee_bps=args.fee_bps,
        slippage_bps=args.slippage_bps,
    )

    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)

    all_bars: list[pd.DataFrame] = []
    all_funding: list[pd.DataFrame] = []
    all_trades: list[pd.DataFrame] = []
    all_snapshots: list[pd.DataFrame] = []

    print("=" * 78)
    print("AURA v0.5.0 — CRYPTO RESEARCH ENGINE")
    print("Experiment C0 — FROZEN BASELINE")
    print("=" * 78)
    print(f"Venue       : {config.venue}")
    print(f"Symbols     : {', '.join(config.symbols)}")
    print(f"Timeframe   : {config.interval}")
    print(f"History     : {start.date()} -> {end.date()}")
    print(f"Exit        : {config.hold_bars} complete 1H bars, CLOSE")
    print(
        f"Friction    : {config.slippage_bps:g} bp entry + "
        f"{config.fee_bps:g} bp fee + "
        f"{config.slippage_bps:g} bp exit + "
        f"{config.fee_bps:g} bp fee"
    )
    print("Leverage    : 1x")
    print("Mode        : RESEARCH ONLY — NO ORDERS")
    print()

    for symbol in config.symbols:
        print(f"[DATA] {symbol}")
        bars = fetch_klines(symbol, start, end)
        if bars.empty:
            print("  WARNING: no kline data")
            continue

        bars = add_features(bars)
        funding = fetch_funding_history(symbol, start, end)

        trades, snapshots = simulate_symbol(bars, funding, config)

        all_bars.append(bars)
        all_funding.append(funding)
        if not trades.empty:
            all_trades.append(trades)
            all_snapshots.append(snapshots)

        print(
            f"  bars={len(bars):,} "
            f"signals={int(bars['signal'].sum())} "
            f"trades={len(trades)} "
            f"funding_records={len(funding)}"
        )

    bars_all = pd.concat(all_bars, ignore_index=True) if all_bars else pd.DataFrame()
    funding_all = (
        pd.concat(all_funding, ignore_index=True) if all_funding else pd.DataFrame()
    )
    trades_all = (
        pd.concat(all_trades, ignore_index=True) if all_trades else pd.DataFrame()
    )
    snapshots_all = (
        pd.concat(all_snapshots, ignore_index=True)
        if all_snapshots
        else pd.DataFrame()
    )

    summary = summarize(trades_all, len(all_bars), config)

    # Universe is deliberately tiny and immutable for C0.
    universe = pd.DataFrame(
        [
            {
                "universe_version": "CRYPTO_C0_FROZEN",
                "symbol": s,
                "market": "MEXC_USDT_PERPETUAL",
                "active": True,
                "frozen_at": end.isoformat(),
            }
            for s in config.symbols
        ]
    )

    run = {
        "config": asdict(config),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "research_status": "C0_BASELINE_COMPLETE",
        "execution": "NONE",
        "notes": [
            "C0 is a control experiment, not evidence of crypto alpha.",
            "Signal is frozen and intentionally not optimized in v0.5.0.",
            "Entry occurs on the next bar OPEN after a signal bar closes.",
            "Funding is tracked separately and included in net_return.",
            "Historical OI is not inferred from the current ticker; no fake OI history is created.",
        ],
    }

    universe.to_csv(out / "universe.csv", index=False)
    bars_all.to_csv(out / "bars_1h.csv", index=False)
    funding_all.to_csv(out / "funding_history.csv", index=False)
    trades_all.to_csv(out / "trades.csv", index=False)
    snapshots_all.to_csv(out / "position_snapshots.csv", index=False)
    summary.to_csv(out / "daily_model_metrics.csv", index=False)

    with open(out / "research_run.json", "w", encoding="utf-8") as f:
        json.dump(run, f, indent=2)

    print()
    print("=" * 78)
    print("C0 RESULT")
    print("=" * 78)
    print(summary.to_string(index=False))
    print()
    print("Files:")
    for name in [
        "universe.csv",
        "bars_1h.csv",
        "funding_history.csv",
        "trades.csv",
        "position_snapshots.csv",
        "daily_model_metrics.csv",
        "research_run.json",
    ]:
        print(f"  {out / name}")
    print()
    print("AURA v0.5.0 C0 COMPLETE — RESEARCH ONLY")


if __name__ == "__main__":
    main()
