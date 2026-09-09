"""
AURA — Alpaca C0 control backtest

Research only. No order submission.

This is a venue-adaptation of the frozen AURA v0.5.0 C0 control:
  Signal: EMA3/EMA8 bullish crossover + MACD histogram > 0 + RelVol >= 1
  Entry : next 1H bar OPEN after confirmed signal
  Exit  : CLOSE of the 10th complete held 1H bar
  Long-only, 1x notional
  Friction: 5 bp entry slippage + 1 bp entry fee
             5 bp exit slippage  + 1 bp exit fee

Important:
- The MEXC C0 control remains untouched.
- Alpaca crypto is spot market data, so this is a venue/data replication,
  not a claim of economic equivalence with the MEXC perpetual control.
- No optimization, threshold selection, stop, target, time filter, or OI
  filter is implemented here.
- The default comparison universe is BTC/USD and ETH/USD because those are
  the closest Alpaca spot symbols to the frozen MEXC BTC_USDT/ETH_USDT control.
- --all-usd is deliberately opt-in and is a separate universe experiment.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from dotenv import load_dotenv
from alpaca.data.historical import CryptoHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient


DEFAULT_SYMBOLS = ["BTC/USD", "ETH/USD"]
FEE_BPS = 1.0
SLIPPAGE_BPS = 5.0
HOLD_BARS = 10


@dataclass(frozen=True)
class C0Config:
    experiment: str = "C0_ALPACA_SPOT"
    venue: str = "ALPACA_CRYPTO_SPOT"
    timeframe: str = "1H"
    symbols: tuple[str, ...] = tuple(DEFAULT_SYMBOLS)
    hold_bars: int = HOLD_BARS
    fee_bps: float = FEE_BPS
    slippage_bps: float = SLIPPAGE_BPS
    leverage: float = 1.0
    signal: str = "EMA3/EMA8 bullish crossover + MACD histogram > 0 + RelVol >= 1"
    execution: str = "NONE"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS)
    p.add_argument("--all-usd", action="store_true", help="Use all active/tradable */USD crypto pairs")
    p.add_argument("--out", default="AURA_ALPACA_C0")
    return p.parse_args()


def add_features(df: pd.DataFrame) -> pd.DataFrame:
    x = df.copy().sort_values(["symbol", "timestamp"]).reset_index(drop=True)
    pieces: list[pd.DataFrame] = []
    for symbol, g in x.groupby("symbol", sort=False):
        g = g.copy()
        g["ema_3"] = g["close"].ewm(span=3, adjust=False).mean()
        g["ema_8"] = g["close"].ewm(span=8, adjust=False).mean()
        ema12 = g["close"].ewm(span=12, adjust=False).mean()
        ema26 = g["close"].ewm(span=26, adjust=False).mean()
        g["macd"] = ema12 - ema26
        g["macd_signal"] = g["macd"].ewm(span=9, adjust=False).mean()
        g["macd_hist"] = g["macd"] - g["macd_signal"]
        vol_base = g["volume"].rolling(20, min_periods=20).mean().shift(1)
        g["rel_volume"] = g["volume"] / vol_base
        g["bullish_crossover"] = (
            (g["ema_3"] > g["ema_8"])
            & (g["ema_3"].shift(1) <= g["ema_8"].shift(1))
        )
        g["signal"] = (
            g["bullish_crossover"]
            & (g["macd_hist"] > 0)
            & (g["rel_volume"] >= 1.0)
        )
        pieces.append(g)
    return pd.concat(pieces, ignore_index=True) if pieces else x


def fetch_bars(
    client: CryptoHistoricalDataClient,
    symbols: list[str],
    start: datetime,
    end: datetime,
) -> pd.DataFrame:
    """Fetch the requested historical window separately for each symbol.

    Fetching symbols independently avoids truncating a long multi-symbol
    request at Alpaca's per-request bar limit.
    """
    frames: list[pd.DataFrame] = []

    for symbol in symbols:
        req = CryptoBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Hour,
            start=start,
            end=end,
            limit=10000,
        )
        result = client.get_crypto_bars(req)
        df = result.df.reset_index()

        if df.empty:
            print(f"warning: no bars returned for {symbol}")
            continue

        keep = [
            c
            for c in [
                "symbol",
                "timestamp",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "trade_count",
                "vwap",
            ]
            if c in df.columns
        ]

        df = df[keep].sort_values("timestamp").reset_index(drop=True)
        frames.append(df)

        print(f"fetched {symbol}: {len(df)} bars")

    if not frames:
        return pd.DataFrame()

    return (
        pd.concat(frames, ignore_index=True)
        .sort_values(["symbol", "timestamp"])
        .reset_index(drop=True)
    )


def funding_for_spot() -> float:
    # Alpaca spot has no perpetual funding payment. Keep the field explicit
    # so the comparison ledger cannot silently mix funding into price P&L.
    return 0.0


def simulate_symbol(x: pd.DataFrame, config: C0Config) -> list[dict]:
    trades: list[dict] = []
    i = 0
    trade_no = 0
    while i < len(x) - config.hold_bars:
        if not bool(x.iloc[i]["signal"]):
            i += 1
            continue
        entry_i = i + 1
        exit_i = entry_i + config.hold_bars - 1
        if exit_i >= len(x):
            break
        entry = x.iloc[entry_i]
        exit_ = x.iloc[exit_i]
        entry_raw = float(entry["open"])
        exit_raw = float(exit_["close"])
        entry_exec = entry_raw * (1 + config.slippage_bps / 10000)
        exit_exec = exit_raw * (1 - config.slippage_bps / 10000)
        price_return = exit_exec / entry_exec - 1
        commission_return = -2 * config.fee_bps / 10000
        funding_return = funding_for_spot()
        net_return = price_return + commission_return + funding_return
        path = x.iloc[entry_i:exit_i + 1]
        trade_no += 1
        trades.append({
            "trade_id": f"C0-{x.iloc[i]['symbol'].replace('/', '_')}-{trade_no:05d}",
            "symbol": x.iloc[i]["symbol"],
            "signal_timestamp": pd.Timestamp(x.iloc[i]["timestamp"]).isoformat(),
            "entry_timestamp": pd.Timestamp(entry["timestamp"]).isoformat(),
            "exit_timestamp": pd.Timestamp(exit_["timestamp"]).isoformat(),
            "entry_price_raw": entry_raw,
            "entry_price_exec": entry_exec,
            "exit_price_raw": exit_raw,
            "exit_price_exec": exit_exec,
            "price_return": price_return,
            "commission_return": commission_return,
            "funding_return": funding_return,
            "net_return": net_return,
            "mfe": float(path["high"].max() / entry_exec - 1),
            "mae": float(path["low"].min() / entry_exec - 1),
            "hold_bars": config.hold_bars,
            "exit_reason": "TIME_10H_CLOSE",
        })
        # Frozen C0 behavior: no overlapping positions per symbol.
        i = exit_i + 1
    return trades


def max_drawdown(r: pd.Series) -> float:
    if r.empty:
        return float("nan")
    eq = (1 + r.fillna(0)).cumprod()
    return float((eq / eq.cummax() - 1).min())


def summarize(trades: pd.DataFrame, config: C0Config) -> dict:
    if trades.empty:
        return {"experiment": config.experiment, "trades": 0}
    r = pd.to_numeric(trades["net_return"], errors="coerce").dropna()
    gains = r[r > 0].sum()
    losses = -r[r < 0].sum()
    return {
        "experiment": config.experiment,
        "venue": config.venue,
        "symbols": int(trades["symbol"].nunique()),
        "trades": int(len(trades)),
        "winners": int((r > 0).sum()),
        "losers": int((r <= 0).sum()),
        "win_rate": float((r > 0).mean()),
        "net_mean": float(r.mean()),
        "net_median": float(r.median()),
        "profit_factor": float(gains / losses) if losses else None,
        "max_drawdown": max_drawdown(r),
        "mean_mfe": float(pd.to_numeric(trades["mfe"], errors="coerce").mean()),
        "mean_mae": float(pd.to_numeric(trades["mae"], errors="coerce").mean()),
        "funding_model": "SPOT_NONE",
    }


def discover_usd_symbols() -> list[str]:
    load_dotenv()
    key = os.getenv("ALPACA_API_KEY")
    secret = os.getenv("ALPACA_SECRET_KEY")
    if not key or not secret:
        raise RuntimeError("Missing ALPACA_API_KEY or ALPACA_SECRET_KEY in .env")
    client = TradingClient(key, secret, paper=True)
    assets = client.get_all_assets()
    rows = []
    for a in assets:
        symbol = str(getattr(a, "symbol", ""))
        if str(getattr(a, "asset_class", "")).lower() != "crypto":
            continue
        if not bool(getattr(a, "tradable", False)):
            continue
        if not symbol.endswith("/USD"):
            continue
        rows.append(symbol)
    return sorted(set(rows))


def main() -> None:
    args = parse_args()
    symbols = discover_usd_symbols() if args.all_usd else list(args.symbols)
    config = C0Config(symbols=tuple(symbols))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(days=args.days)

    load_dotenv()
    # Crypto historical market data can be requested without trading credentials.
    client = CryptoHistoricalDataClient()
    bars = fetch_bars(client, symbols, start, end)
    if bars.empty:
        raise SystemExit("No Alpaca crypto bars returned.")

    bars = add_features(bars)
    all_trades: list[dict] = []
    for symbol, g in bars.groupby("symbol", sort=False):
        all_trades.extend(simulate_symbol(g.reset_index(drop=True), config))
    trades = pd.DataFrame(all_trades)
    summary = summarize(trades, config)

    bars.to_csv(out / "bars_1h.csv", index=False)
    trades.to_csv(out / "trades_c0.csv", index=False)
    (out / "summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    run = {
        "config": asdict(config),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "bars": int(len(bars)),
        "symbols_requested": symbols,
        "research_status": "C0_ALPACA_SPOT_REPLICATION",
        "execution": "NONE",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "note": "Venue/data replication only; Alpaca crypto is spot and C0 source control is MEXC perpetual.",
    }
    (out / "research_run.json").write_text(json.dumps(run, indent=2, default=str), encoding="utf-8")

    print(f"symbols={len(symbols)}")
    print(f"bars={len(bars)}")
    print(json.dumps(summary, indent=2, default=str))
    print(f"output={out.resolve()}")


if __name__ == "__main__":
    main()
