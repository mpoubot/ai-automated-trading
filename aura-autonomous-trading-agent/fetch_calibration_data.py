#!/usr/bin/env python3
"""
AURA -- Calibration data-sourcing utility (2026-10-09)

Fetches real daily OHLCV bars for the pinned 39-symbol universe
(aura_v05351_equity_universe_v1.json) and writes them to --output-dir as
<SYMBOL>.csv files in exactly the real aura_v054_data_interface.
CSVBarsProvider contract -- clearing deployment-checklist item 3's data
blocker (`python calibrate_institutional_limits.py --bars-dir ...`).

TWO CORRECTIONS TO THE ORIGINAL SPEC, disclosed rather than silently
applied or silently ignored -- same discipline as every other factual
check in this build:

1. CSV COLUMNS. The spec asked for `Date, Open, High, Low, Close,
   Volume`. The REAL, authoritative contract -- confirmed directly
   against `aura_v054_data_interface.py`'s own `BARS_SCHEMA` and
   `CSVBarsProvider.get_daily_bars()` -- is lowercase
   `timestamp,open,high,low,close,volume`; `CSVBarsProvider` reads a
   literal `df["timestamp"]` column and raises `MISSING_COLUMNS` if the
   header doesn't match exactly. This script writes the real schema, not
   the one originally specified -- `Date,Open,...` would make every file
   `calibrate_institutional_limits.py` tries to read fail closed.

2. FALLBACK IMPLEMENTATION. The spec suggested "yfinance or a standard
   raw CSV fetch helper aligned with the 2026-10-03 sector-rotation
   sweep precedent." That precedent (`fetch_data.py`, read directly, not
   assumed) did NOT use the `yfinance` package -- it called Yahoo
   Finance's public chart API directly via `urllib`, no extra
   dependency. This script reuses that exact, already-proven request
   shape (re-verified reachable from this build environment, see
   delivery notes) rather than adding an unproven new dependency.

PRIMARY FEED -- ALPACA (reuse-first, not reimplemented)
------------------------------------------------------------------------
Wraps `aura_v054_alpaca_bars_provider.AlpacaEquityBarsProvider`, the
same real, already-tested provider `run_track_b_alpaca_90day_backtest.py`
uses. Requires `ALPACA_EQUITY_PAPER_API_KEY`/`ALPACA_EQUITY_PAPER_
SECRET_KEY` in `.env` (the dedicated equity/ETF paper pair -- see
`.env.example`; this script never falls back to the crypto account's
`ALPACA_PAPER_API_KEY`/`SECRET_KEY` pair). If these are unset, or Alpaca
fails/returns nothing for a symbol, that symbol falls through to the
Yahoo fallback below -- the run never aborts just because Alpaca
credentials are absent.

FALLBACK FEED -- YAHOO FINANCE PUBLIC CHART API
------------------------------------------------------------------------
No credentials needed. Used for any symbol Alpaca didn't return data
for (including every symbol, if Alpaca credentials are unset entirely,
or with --skip-alpaca).

USAGE
------------------------------------------------------------------------
    python fetch_calibration_data.py
    python fetch_calibration_data.py --years 3 --output-dir ./historical_bars
    python fetch_calibration_data.py --skip-alpaca   # Yahoo fallback only
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

UNIVERSE_FILE = ROOT / "aura_v05351_equity_universe_v1.json"
# Mirrors aura_v054_data_interface.BARS_SCHEMA exactly -- the real,
# authoritative CSVBarsProvider contract, not the originally-specified one.
BARS_SCHEMA = ("timestamp", "open", "high", "low", "close", "volume")

YAHOO_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def load_pinned_universe() -> tuple[str, ...]:
    """Mirrors calibrate_institutional_limits.py's own load_pinned_universe()
    exactly -- same file, same key, never hand-typed."""
    with UNIVERSE_FILE.open() as f:
        data = json.load(f)
    return tuple(data["symbols"])


# ============================================================================
# Primary feed: Alpaca (reused, not reimplemented)
# ============================================================================

def fetch_via_alpaca(symbols: tuple[str, ...], *, years: float) -> tuple[dict[str, "pd.DataFrame"], dict[str, str]]:
    """Returns (ok, errors). ok[symbol] is already in exactly BARS_SCHEMA
    shape -- AlpacaEquityBarsProvider validates this itself (see its own
    _reshape()). Returns ({}, {s: reason for every s}) wholesale if
    credentials are missing, rather than raising -- the caller falls
    through to Yahoo for everything in that case."""
    api_key = os.getenv("ALPACA_EQUITY_PAPER_API_KEY")
    secret_key = os.getenv("ALPACA_EQUITY_PAPER_SECRET_KEY")
    if not api_key or not secret_key:
        reason = (
            "ALPACA_EQUITY_PAPER_API_KEY/ALPACA_EQUITY_PAPER_SECRET_KEY not set in .env "
            "-- skipping Alpaca, falling back to Yahoo for this symbol."
        )
        return {}, {s: reason for s in symbols}

    from aura_v054_alpaca_bars_provider import AlpacaEquityBarsProvider

    lookback_bars = max(1, round(years * 252))  # ~252 trading days/year
    provider = AlpacaEquityBarsProvider(api_key=api_key, secret_key=secret_key, lookback_bars=lookback_bars)
    print(f"[alpaca] requesting ~{lookback_bars} trading days (~{years:g}y) for {len(symbols)} symbols ...")
    try:
        provider.prefetch(symbols)
    except Exception as exc:  # noqa: BLE001 -- a whole-batch Alpaca failure must not abort the run; fall through to Yahoo for everything
        reason = f"ALPACA_PREFETCH_FAILED:{exc}"
        return {}, {s: reason for s in symbols}

    ok = dict(provider._cache)
    errors = dict(provider._fetch_errors)
    return ok, errors


# ============================================================================
# Fallback feed: Yahoo Finance public chart API (reused from the proven
# 2026-10-03 sector-rotation-sweep precedent's fetch_data.py, same request
# shape, not reimplemented from scratch)
# ============================================================================

def fetch_via_yahoo(symbol: str, *, years: float, retries: int = 4) -> "pd.DataFrame":
    range_ = f"{years:g}y"
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range={range_}&interval=1d"
    last_err: str | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": YAHOO_USER_AGENT})
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode())
            result = data["chart"]["result"][0]
            ts = result["timestamp"]
            quote = result["indicators"]["quote"][0]
            df = pd.DataFrame({
                "timestamp": pd.to_datetime(ts, unit="s", utc=True),
                "open": quote["open"],
                "high": quote["high"],
                "low": quote["low"],
                "close": quote["close"],
                "volume": quote["volume"],
            })
            df = df.dropna(subset=["open", "high", "low", "close", "volume"]).reset_index(drop=True)
            df["timestamp"] = df["timestamp"].dt.floor("D")
            df = df.drop_duplicates(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
            if df.empty:
                raise RuntimeError("EMPTY_RESPONSE")
            return df
        except Exception as exc:  # noqa: BLE001 -- retried below; final failure raises to the caller
            last_err = str(exc)
            time.sleep(2 + attempt * 2)
    raise RuntimeError(f"YAHOO_FETCH_FAILED:{symbol}:after {retries} attempts:{last_err}")


# ============================================================================
# Orchestration
# ============================================================================

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--years", type=float, default=5.0, help="Lookback window in years (default 5.0, the upper bound of the 3-5yr ask -- override with e.g. --years 3).")
    parser.add_argument("--output-dir", type=Path, default=Path("./historical_bars"), help="Directory to write <SYMBOL>.csv files into (default ./historical_bars/).")
    parser.add_argument("--skip-alpaca", action="store_true", help="Skip the Alpaca primary feed entirely and use the Yahoo fallback for every symbol (e.g. no .env credentials configured).")
    parser.add_argument("--initial-equity", type=float, default=100000.0, help="Value only used in the final printed calibrate_institutional_limits.py command (default 100000).")
    args = parser.parse_args()

    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        print("[warn] python-dotenv not installed -- relying on the process environment only (no .env file read).")

    universe = load_pinned_universe()
    print(f"Pinned universe: {len(universe)} symbols")
    args.output_dir.mkdir(parents=True, exist_ok=True)

    alpaca_ok: dict[str, "pd.DataFrame"] = {}
    alpaca_errors: dict[str, str] = {s: "SKIPPED (--skip-alpaca)" for s in universe}
    if not args.skip_alpaca:
        alpaca_ok, alpaca_errors = fetch_via_alpaca(universe, years=args.years)
        print(f"[alpaca] {len(alpaca_ok)}/{len(universe)} symbols fetched.")

    remaining = tuple(s for s in universe if s not in alpaca_ok)

    yahoo_ok: dict[str, "pd.DataFrame"] = {}
    yahoo_errors: dict[str, str] = {}
    if remaining:
        print(f"[yahoo] falling back for {len(remaining)} symbol(s): {', '.join(remaining)}")
        for symbol in remaining:
            try:
                yahoo_ok[symbol] = fetch_via_yahoo(symbol, years=args.years)
                print(f"  [yahoo] OK {symbol}: {len(yahoo_ok[symbol])} rows")
            except Exception as exc:  # noqa: BLE001 -- one symbol's failure must not abort the whole run
                yahoo_errors[symbol] = str(exc)
                print(f"  [yahoo] FAIL {symbol}: {exc}")
            time.sleep(0.6)  # same politeness delay as the proven 2026-10-03 precedent

    all_ok: dict[str, "pd.DataFrame"] = {**alpaca_ok, **yahoo_ok}
    failed = tuple(s for s in universe if s not in all_ok)

    print(f"\n=== Writing {len(all_ok)}/{len(universe)} CSV files to {args.output_dir} ===")
    rows_summary = []
    for symbol, df in all_ok.items():
        df = df[list(BARS_SCHEMA)].copy()
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        out_path = args.output_dir / f"{symbol}.csv"
        df.to_csv(out_path, index=False)
        source = "ALPACA" if symbol in alpaca_ok else "YAHOO_FALLBACK"
        rows_summary.append((symbol, source, len(df), str(df["timestamp"].min().date()), str(df["timestamp"].max().date())))
        print(f"  {symbol:6s} [{source:14s}] {len(df):4d} rows  {rows_summary[-1][3]} -> {rows_summary[-1][4]}")

    if failed:
        print(f"\n=== FAILED ({len(failed)}/{len(universe)}) -- not written, calibration will be missing these ===")
        for s in failed:
            reason = yahoo_errors.get(s) or alpaca_errors.get(s, "unknown")
            print(f"  {s}: {reason}")

    print(f"\n=== SUMMARY: {len(all_ok)}/{len(universe)} symbols written to {args.output_dir} ===")

    if len(all_ok) < len(universe):
        print(
            f"\nWARNING: {len(failed)} symbol(s) missing. calibrate_institutional_limits.py's "
            f"run_baseline will still run against whatever CSVs are present, but the universe "
            f"it sees will be smaller than the full pinned 39 -- check the FAILED list above "
            f"before treating a calibration result as covering the whole universe."
        )

    print("\n=== Ready-to-execute calibration command ===")
    print(
        f"python calibrate_institutional_limits.py --bars-dir {args.output_dir} "
        f"--initial-equity {args.initial_equity:g} --output calibration_result.json"
    )

    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
