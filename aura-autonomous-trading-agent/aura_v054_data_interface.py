#!/usr/bin/env python3
"""
AURA v0.5.4 -- Data ingestion interface (pluggable, real-data-ready)

Purpose
-------
Martin's .54 specification requires that "the same frozen .54
implementation must be able to run against [a real S&P 100 daily OHLCV
dataset] without changing the strategy parameters" once one is supplied,
and that supplying it "does not require modifying the strategy, risk,
exit or sizing logic." This module is the single seam that isolates
"where do bars come from" from everything downstream (ATR, exit engine,
sizing, portfolio risk, cost model, backtest runner) -- none of those
modules import pandas-reading/network code directly; they all consume
the same `BarsFrame` shape from whatever `BarsProvider` is handed to
`aura_v054_backtest.py`.

Confirmed sandbox status at implementation time (2026-09-15), verified by
direct testing, not assumed:
  - No general internet access: `curl` to both `data.alpaca.markets` and
    a generic external host failed identically
    (`curl: (56) CONNECT tunnel failed, response 403`).
  - No real Alpaca credentials: only `.env.example` exists, and it is
    itself scoped to CRYPTO keys per its own header comment.
  - No pinned/cached continuous daily equities OHLCV dataset anywhere in
    this repository (only crypto `bars_1h.csv` files exist).
  - `research/*_historical_signals.csv` files are sparse per-symbol
    signal-EVENT snapshots (a handful of rows per symbol) from an older,
    differently-defined scanner -- not continuous OHLC bar series, and
    therefore unusable as raw walk-forward backtest input.

Three `BarsProvider` implementations are provided:

  1. `SyntheticBarsProvider` -- deterministic, formula-based (no RNG, so
     it is exactly reproducible across runs/machines without seeding
     concerns) synthetic daily bars. TEST/PIPELINE-VALIDATION USE ONLY.
     Every result this provider touches is tagged
     `DATA_SOURCE_LABEL = "SYNTHETIC_TEST_FIXTURE"` and must never be
     reported as, or confused with, real market performance.

  2. `CSVBarsProvider` -- reads real daily OHLCV bars from local CSV
     files (one file per symbol, columns timestamp,open,high,low,close,
     volume). This is the concrete, ready-to-use path for when Martin
     supplies a real S&P 100 dataset: drop per-symbol CSVs into a
     directory and point this provider at it. No code in
     `aura_v054_backtest.py` or any strategy/risk/exit/sizing module
     needs to change.

  3. `UnavailableRealEquityDataSource` -- the explicit, honest "no real
     data configured" provider. Any attempt to fetch bars raises
     `RealEquityDataNotAvailableError`, carrying the exact
     REAL_EQUITY_BACKTEST=NOT_RUN / REASON status text this session's
     final report uses. This is the default the backtest runner falls
     back to when nothing else is configured -- it fails loudly and
     explicitly, never silently substituting synthetic or crypto data.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

import pandas as pd

VERSION = "AURA v0.5.4"

BARS_SCHEMA = ("timestamp", "open", "high", "low", "close", "volume")

REAL_EQUITY_BACKTEST_NOT_RUN_REASON = (
    "No real S&P 100 daily OHLCV dataset available in current environment."
)


class DataInterfaceError(Exception):
    pass


class RealEquityDataNotAvailableError(DataInterfaceError):
    """Raised by `UnavailableRealEquityDataSource` on any fetch attempt.
    This is the ONLY code path that may cause the backtest runner to set
    REAL_EQUITY_BACKTEST=NOT_RUN -- never a fabricated/synthetic
    substitution presented as real.
    """

    def __init__(self, reason: str = REAL_EQUITY_BACKTEST_NOT_RUN_REASON) -> None:
        self.reason = reason
        super().__init__(reason)


@runtime_checkable
class BarsProvider(Protocol):
    """The one seam. Anything satisfying this protocol can be handed to
    `aura_v054_backtest.py` unchanged -- a real Alpaca client wrapper
    (e.g. adapting .51's own `AlpacaHistoricalBarsClient`/
    `fetch_recent_bars`), a CSV loader, or a synthetic fixture.
    """

    DATA_SOURCE_LABEL: str
    IS_REAL_MARKET_DATA: bool

    def get_daily_bars(self, symbol: str) -> pd.DataFrame:
        """Return a DataFrame with columns exactly `BARS_SCHEMA`, one row
        per trading day, sorted ascending by `timestamp` (tz-aware UTC),
        with no duplicate timestamps and no forward-filled/interpolated
        rows (a missing day must be genuinely absent, never invented).
        """
        ...


def validate_bars_frame(df: pd.DataFrame, *, symbol: str) -> None:
    """Shared validation every provider's output must pass before it is
    used anywhere downstream -- fails closed on malformed input rather
    than letting a schema mismatch silently propagate into ATR/exit/
    sizing math.
    """
    missing = set(BARS_SCHEMA) - set(df.columns)
    if missing:
        raise DataInterfaceError(f"MISSING_COLUMNS:{symbol}:{sorted(missing)}")
    if df["timestamp"].duplicated().any():
        raise DataInterfaceError(f"DUPLICATE_TIMESTAMPS:{symbol}")
    if not df["timestamp"].is_monotonic_increasing:
        raise DataInterfaceError(f"TIMESTAMPS_NOT_SORTED_ASCENDING:{symbol}")
    for col in ("open", "high", "low", "close", "volume"):
        if df[col].isna().any():
            raise DataInterfaceError(f"NULL_VALUES_IN_{col.upper()}:{symbol}")
        if (df[col] < 0).any():
            raise DataInterfaceError(f"NEGATIVE_VALUES_IN_{col.upper()}:{symbol}")
    bad_range = (df["high"] < df["low"]) | (df["high"] < df["close"]) | (df["low"] > df["close"]) | (
        df["high"] < df["open"]
    ) | (df["low"] > df["open"])
    if bad_range.any():
        raise DataInterfaceError(f"INCONSISTENT_OHLC_RANGE:{symbol}")


# ============================================================================
# 1. Synthetic provider -- deterministic, formula-based, TEST-ONLY.
# ============================================================================


@dataclass
class SyntheticBarsProvider:
    """Deterministic daily bars generated from a closed-form formula (no
    RNG seeding to manage, exactly reproducible). Produces a mild upward
    drift with a bounded oscillation and a periodic wider-range day so
    ATR and trailing-stop logic both have real, varying, non-degenerate
    input to exercise -- NOT calibrated to look like any real security
    and NOT a claim about real market behavior in any way.

    THIS IS A TEST FIXTURE, NOT A TRADING RESEARCH DATASET. Every
    downstream consumer must treat `DATA_SOURCE_LABEL` as a hard gate on
    ever reporting results from this provider as real performance.
    """

    n_days: int = 400
    start: str = "2024-01-02"
    base_price: float = 100.0

    DATA_SOURCE_LABEL: str = "SYNTHETIC_TEST_FIXTURE"
    IS_REAL_MARKET_DATA: bool = False

    def get_daily_bars(self, symbol: str) -> pd.DataFrame:
        # Deterministic per-symbol offset so different symbols in a
        # synthetic universe don't produce byte-identical series, while
        # remaining exactly reproducible run to run (sum of character
        # codes -- no RNG, no seed state).
        symbol_offset = sum(ord(c) for c in symbol) % 17

        dates = pd.bdate_range(start=self.start, periods=self.n_days, tz="UTC")
        rows = []
        price = self.base_price + symbol_offset
        for i, ts in enumerate(dates):
            # Smooth drift + bounded oscillation, deterministic in i.
            drift = 0.03 * math.sin(i / 9.0 + symbol_offset) + 0.01
            price = max(1.0, price * (1.0 + drift / 100.0))
            # Every 23rd bar is a deliberately wider-range day so ATR
            # visibly reacts (Wilder smoothing vs. a flat series is
            # otherwise untestable).
            wide = (i % 23 == 0)
            day_range_pct = 0.045 if wide else 0.012
            half_range = price * day_range_pct / 2.0
            open_px = price * (1.0 + 0.001 * math.sin(i / 5.0))
            close_px = price
            high_px = max(open_px, close_px) + half_range
            low_px = max(0.01, min(open_px, close_px) - half_range)
            volume = 1_000_000 + (i % 7) * 25_000
            rows.append(
                {
                    "timestamp": ts,
                    "open": round(open_px, 4),
                    "high": round(high_px, 4),
                    "low": round(low_px, 4),
                    "close": round(close_px, 4),
                    "volume": volume,
                }
            )
        df = pd.DataFrame(rows, columns=list(BARS_SCHEMA))
        validate_bars_frame(df, symbol=symbol)
        return df


# ============================================================================
# 2. CSV provider -- the concrete, ready-to-use real-data path.
# ============================================================================


@dataclass
class CSVBarsProvider:
    """Reads real daily OHLCV bars from `<directory>/<symbol>.csv`, each
    file with a header row `timestamp,open,high,low,close,volume`
    (timestamp any pandas-parseable date/datetime string). This is the
    interface a real S&P 100 dataset plugs into: drop the per-symbol CSV
    files in `directory` and pass this provider to
    `aura_v054_backtest.py` -- no change to any strategy/risk/exit/
    sizing module is required.
    """

    directory: Path
    DATA_SOURCE_LABEL: str = "CSV_REAL_EQUITY_DATA"
    IS_REAL_MARKET_DATA: bool = True

    def get_daily_bars(self, symbol: str) -> pd.DataFrame:
        path = Path(self.directory) / f"{symbol}.csv"
        if not path.exists():
            raise RealEquityDataNotAvailableError(
                f"No CSV file found for {symbol!r} at {path} -- "
                f"{REAL_EQUITY_BACKTEST_NOT_RUN_REASON}"
            )
        df = pd.read_csv(path)
        missing = set(BARS_SCHEMA) - set(df.columns)
        if missing:
            raise DataInterfaceError(f"MISSING_COLUMNS:{symbol}:{sorted(missing)}")
        df = df[list(BARS_SCHEMA)].copy()
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        df = df.sort_values("timestamp").reset_index(drop=True)
        validate_bars_frame(df, symbol=symbol)
        return df


# ============================================================================
# 3. Explicit "no real data configured" provider -- the honest default.
# ============================================================================


@dataclass
class UnavailableRealEquityDataSource:
    """The default `BarsProvider` when no real dataset has been
    configured. Fails loudly on every call -- this is what makes
    REAL_EQUITY_BACKTEST=NOT_RUN an enforced status rather than a
    reporting convention that could be silently bypassed.
    """

    reason: str = REAL_EQUITY_BACKTEST_NOT_RUN_REASON
    DATA_SOURCE_LABEL: str = "REAL_EQUITY_DATA_NOT_CONFIGURED"
    IS_REAL_MARKET_DATA: bool = True  # would be real IF configured; declares intent, not availability

    def get_daily_bars(self, symbol: str) -> pd.DataFrame:
        raise RealEquityDataNotAvailableError(self.reason)
