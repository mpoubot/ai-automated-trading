"""
Historical MEXC Futures/SWAP dataset builder — native adapter (no ccxt).

NOT EXECUTED YET. Built per the frozen requirements (11-pair
DEFAULT_SYMBOLS, 1h/Min60, 720-day target history, no strategy changes, no
optimization, no Alpaca, no live trading) and awaiting explicit approval to
run the full download, per instruction. Running this from this cloud
sandbox will currently fail outright — see "Known blocker" below — so
approval to run needs to also settle where it runs.

Writes, per symbol, into data/native/:
  ohlcv/<SYMBOL>_1h.parquet          — parsed OHLCV, ccxt-equivalent schema
                                        + amount/real_* extras
  funding/<SYMBOL>_funding.parquet   — parsed funding history
  raw/ohlcv/<SYMBOL>/*.json          — every raw kline page response,
                                        verbatim, for audit/reproducibility
  raw/funding/<SYMBOL>/*.json        — every raw funding page response
  manifest.json                      — one record per symbol: requested vs.
                                        actually-covered date range, row
                                        counts, fetch timestamp, adapter
                                        assumptions in force at fetch time

KNOWN BLOCKER (surfaced at the Step-3 checkpoint, not resolved here): this
Claude Cowork cloud sandbox's outbound network policy blocks
contract.mexc.com at the proxy level (confirmed via the proxy's own status
endpoint: "connect_rejected ... gateway answered 403 to CONNECT (policy
denial)"). This script cannot reach MEXC's API from here as-is. It CAN run
unmodified from Martin's linked Windows machine (device name "martinisma",
folder "AI agents\\AI automated trading" already connected) via the
device-bridge shell, where egress is governed by that machine's own network,
not this sandbox's — but that hasn't been tried yet either, and needs your
go-ahead like everything else past this checkpoint.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import config as cfg
from run_broad_backtest import DEFAULT_SYMBOLS
from core.mexc_native import (
    to_native_symbol,
    fetch_ohlcv_range_native,
    fetch_funding_history_native,
    TIMEFRAME_TO_NATIVE_INTERVAL,
)

TARGET_HISTORY_DAYS = 720
DATASET_ROOT = Path(__file__).parent / "data" / "native"


def run(symbols=None, history_days: int = TARGET_HISTORY_DAYS, dataset_root: Path = DATASET_ROOT):
    symbols = symbols or DEFAULT_SYMBOLS
    interval = TIMEFRAME_TO_NATIVE_INTERVAL[cfg.TIMEFRAME]  # "1h" -> "Min60"

    now = datetime.now(timezone.utc)
    end_ms = int(now.timestamp() * 1000)
    start_ms = end_ms - history_days * 24 * 3600 * 1000

    ohlcv_dir = dataset_root / "ohlcv"
    funding_dir = dataset_root / "funding"
    raw_ohlcv_root = dataset_root / "raw" / "ohlcv"
    raw_funding_root = dataset_root / "raw" / "funding"
    for d in (ohlcv_dir, funding_dir, raw_ohlcv_root, raw_funding_root):
        d.mkdir(parents=True, exist_ok=True)

    manifest = {
        "fetched_at_utc": now.isoformat(),
        "adapter": "core/mexc_native.py",
        "timeframe": cfg.TIMEFRAME,
        "native_interval": interval,
        "target_history_days": history_days,
        "requested_start_utc": datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc).isoformat(),
        "requested_end_utc": now.isoformat(),
        "assumptions_in_force": {
            "kline_time_unit": "seconds (live-verified against BTC/ETH/SUI/DOGE, 2026-09-17)",
            "kline_price_fields_used": "open/high/low/close (plain, not real_*; live-verified distinct from real_*)",
            "funding_settleTime_unit": "milliseconds (live-verified against BTC/ETH/SUI/DOGE, 2026-09-17)",
            "funding_page_order": "newest-first / descending settleTime (live-verified)",
            "funding_page_size_cap": "1000, confirmed live for BTC_USDT (pageSize=1000 honored in full; "
                                      "ccxt's inline 'maximum is 100' comment is stale) — still read back "
                                      "from the response's own pageSize per call rather than hardcoded",
            "timestamp_tz": "naive (UTC instant, no tzinfo) — deliberately matches core/data_fetcher.py's "
                             "existing convention; see core/mexc_native.py module docstring",
        },
        "symbols": {},
    }

    for ccxt_symbol in symbols:
        native_symbol = to_native_symbol(ccxt_symbol)
        print(f"[{native_symbol}] fetching OHLCV ({interval}) ...")
        ohlcv_df = fetch_ohlcv_range_native(
            native_symbol, interval, start_ms, end_ms,
            raw_dump_dir=raw_ohlcv_root / native_symbol,
        )
        ohlcv_path = ohlcv_dir / f"{native_symbol}_1h.parquet"
        ohlcv_df.to_parquet(ohlcv_path, index=False)

        print(f"[{native_symbol}] fetching funding history ...")
        funding_df = fetch_funding_history_native(
            native_symbol, cutoff_ms=start_ms,
            raw_dump_dir=raw_funding_root / native_symbol,
        )
        funding_path = funding_dir / f"{native_symbol}_funding.parquet"
        funding_df.to_parquet(funding_path, index=False)

        manifest["symbols"][native_symbol] = {
            "ccxt_symbol": ccxt_symbol,
            "ohlcv_rows": len(ohlcv_df),
            "ohlcv_first_ts": str(ohlcv_df["timestamp"].min()) if len(ohlcv_df) else None,
            "ohlcv_last_ts": str(ohlcv_df["timestamp"].max()) if len(ohlcv_df) else None,
            "funding_rows": len(funding_df),
            "funding_first_ts": str(funding_df["timestamp"].min()) if len(funding_df) else None,
            "funding_last_ts": str(funding_df["timestamp"].max()) if len(funding_df) else None,
        }
        print(f"[{native_symbol}] done: {len(ohlcv_df)} candles, {len(funding_df)} funding rows")

    (dataset_root / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"\nManifest written to {dataset_root / 'manifest.json'}")
    return manifest


if __name__ == "__main__":
    run()
