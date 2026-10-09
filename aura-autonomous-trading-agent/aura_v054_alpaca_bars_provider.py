#!/usr/bin/env python3
"""
AURA v0.5.4 -- Real Alpaca equity BarsProvider (NEW, 2026-10-08)

Satisfies `aura_v054_data_interface.BarsProvider` so
`aura_v054_backtest.run_backtest`/`run_baseline` can run against real
Alpaca daily OHLCV data with ZERO changes to the strategy/risk/exit/
sizing modules -- this is exactly the seam `aura_v054_data_interface.py`'s
own module docstring names as "a future live-Alpaca-backed provider
satisfying BarsProvider."

Built per Martin's request (2026-10-08) for a 90-trading-day Track B
backtest, after three rounds of AskUserQuestion clarification settled the
scope, signal source, data source, universe, and (once the pipeline's
60-bar warmup floor was surfaced) what "90 days" should actually mean --
see `run_track_b_alpaca_90day_backtest.py`'s own module docstring for the
full decision trail.

Wraps the EXISTING, already-working real Alpaca equity fetch code in
`aura_v05351_live_alpaca_equity_signal_source.py`
(`AlpacaHistoricalBarsClient`, `fetch_recent_bars_batch`) rather than
reimplementing Alpaca API access -- reuse-first, matching this project's
own established convention (see that module's "Live data fetch -- adapted
from historical_signal_scanner.py's load_data() (the only genuine,
working Alpaca equity market-data fetch code in this repo)" comment).

CREDENTIALS
-----------
This module never reads `os.environ` itself and never reads/writes a
`.env` file. The caller (the runner script) loads `ALPACA_EQUITY_PAPER_
API_KEY` / `ALPACA_EQUITY_PAPER_SECRET_KEY` -- the DEDICATED equity/ETF
paper credential pair established 2026-09-23 ("Track B Stage 3 kickoff",
see `.env.example`) -- and passes them in as explicit constructor
arguments, mirroring `AlpacaHistoricalBarsClient`'s own "no credentials
read or validated at import time" discipline. Deliberately NO fallback to
the crypto-account `ALPACA_PAPER_API_KEY`/`ALPACA_PAPER_SECRET_KEY` pair,
so this can never be accidentally pointed at crypto-account keys.

BATCHED PREFETCH, NOT PER-SYMBOL LAZY FETCH
--------------------------------------------
Call `.prefetch(universe)` ONCE before handing this provider to
`run_backtest`/`run_permutation_test`, so the whole universe is fetched in
as few Alpaca requests as `fetch_recent_bars_batch`'s own chunking allows
(200 symbols/request by default), rather than one HTTP round-trip per
symbol per call. `get_daily_bars()` then only ever serves from the
in-memory cache `prefetch()` populated -- it raises
`RealEquityDataNotAvailableError` (never a silent empty frame) for a
symbol that was never prefetched or came back with no bars, matching
`aura_v054_data_interface.py`'s own "fails loudly and explicitly, never
silently substituting" discipline for real-data providers.

WHY `DEFAULT_LOOKBACK_BARS = 160`
-----------------------------------
`aura_v054_backtest.run_backtest` never scans for an entry before
`config.warmup_bars` (default `MIN_WARMUP_BARS = 60`), and its holdout
boundary is a trailing 20% of the TOTAL bar count (warmup included). So
of `n` total trading-day bars fetched, only `n - 60` are ever actually
live-scanned for a signal. To get Martin's requested ~90 real trading
days of live signal-scanning (his explicit choice, AskUserQuestion
2026-10-08, after the 60-bar floor was surfaced as a reason "literally
just 90 calendar days" would produce a near-empty, meaningless result):

    n = 160  ->  warmup = 60, live-scanned = 100 bars
    holdout_boundary = floor(160 * 0.8) = 128
    RESEARCH-period scanning window: bars 60..127  (68 bars)
    HOLDOUT-period scanning window:  bars 128..159 (32 bars)

100 live-scanned bars (comfortably over Martin's ~90-day ask) split into
a non-trivial RESEARCH and HOLDOUT segment, rather than exactly 90 which
would leave the HOLDOUT segment razor-thin. `fetch_recent_bars_batch`
over-fetches calendar days (`lookback_bars * 7/5 + calendar_buffer_days`)
and trims to the last `lookback_bars` TRADING days actually returned, so
this is ~160 trading days, not 160 calendar days -- roughly 7-9 calendar
months depending on holidays, per symbol.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

import pandas as pd

import aura_v054_data_interface as DATA
from aura_v05351_live_alpaca_equity_signal_source import (
    AlpacaHistoricalBarsClient,
    fetch_recent_bars_batch,
)

VERSION = "AURA v0.5.4"

# See module docstring "WHY DEFAULT_LOOKBACK_BARS = 160" for the exact
# warmup/holdout math behind this default.
DEFAULT_LOOKBACK_BARS = 160
DEFAULT_CALENDAR_BUFFER_DAYS = 15


class AlpacaBarsProviderError(DATA.DataInterfaceError):
    pass


@dataclass
class AlpacaEquityBarsProvider:
    """Real Alpaca daily OHLCV, reshaped to `aura_v054_data_interface.
    BARS_SCHEMA`. Call `.prefetch(universe)` before the first
    `get_daily_bars()` call -- see module docstring.
    """

    api_key: str
    secret_key: str
    lookback_bars: int = DEFAULT_LOOKBACK_BARS
    calendar_buffer_days: int = DEFAULT_CALENDAR_BUFFER_DAYS
    end: datetime | None = None  # defaults to now (UTC) at prefetch() time

    DATA_SOURCE_LABEL: str = "ALPACA_REAL_EQUITY_DATA"
    IS_REAL_MARKET_DATA: bool = True

    def __post_init__(self) -> None:
        if not self.api_key or not self.secret_key:
            raise AlpacaBarsProviderError(
                "MISSING_CREDENTIALS: api_key/secret_key must both be non-empty. "
                "This module never reads os.environ itself -- the caller must load "
                "ALPACA_EQUITY_PAPER_API_KEY/ALPACA_EQUITY_PAPER_SECRET_KEY and pass them in."
            )
        self._client = AlpacaHistoricalBarsClient(self.api_key, self.secret_key)
        self._cache: dict[str, pd.DataFrame] = {}
        self._fetch_errors: dict[str, str] = {}

    def prefetch(self, symbols: tuple[str, ...]) -> None:
        """Fetches real daily bars for every symbol in `symbols` in as
        few Alpaca requests as `fetch_recent_bars_batch`'s chunking
        allows, reshapes each to `BARS_SCHEMA`, and populates the
        in-memory cache `get_daily_bars()` serves from. Safe to call more
        than once (e.g. to refresh `end`); each call fully replaces the
        prior cache rather than merging with it.
        """
        end_dt = self.end or datetime.now(timezone.utc)
        raw_by_symbol = fetch_recent_bars_batch(
            symbols,
            client=self._client,
            lookback_bars=self.lookback_bars,
            end=end_dt,
            calendar_buffer_days=self.calendar_buffer_days,
        )
        cache: dict[str, pd.DataFrame] = {}
        errors: dict[str, str] = {}
        for symbol in symbols:
            if symbol not in raw_by_symbol:
                errors[symbol] = f"NO_BARS_RETURNED:{symbol}"
                continue
            try:
                cache[symbol] = self._reshape(raw_by_symbol[symbol], symbol=symbol)
            except DATA.DataInterfaceError as exc:
                errors[symbol] = str(exc)
        self._cache = cache
        self._fetch_errors = errors

    def _reshape(self, raw: pd.DataFrame, *, symbol: str) -> pd.DataFrame:
        """Reshapes one symbol's alpaca-py bars DataFrame (indexed by a
        `timestamp` DatetimeIndex, columns include open/high/low/close/
        volume plus alpaca-py extras like trade_count/vwap this pipeline
        does not use) into exactly `DATA.BARS_SCHEMA`, tz-aware UTC,
        ascending, de-duplicated -- then runs it through the SAME
        `validate_bars_frame` every other provider's output must pass,
        so a malformed real-data response fails closed here rather than
        propagating into ATR/exit/sizing math.
        """
        df = raw.reset_index()
        if "timestamp" not in df.columns:
            raise AlpacaBarsProviderError(
                f"UNEXPECTED_SCHEMA:{symbol}:no 'timestamp' column after reset_index "
                f"(columns={list(df.columns)}) -- alpaca-py's bars index shape may have changed."
            )
        missing = set(DATA.BARS_SCHEMA) - set(df.columns)
        if missing:
            raise AlpacaBarsProviderError(f"MISSING_COLUMNS:{symbol}:{sorted(missing)}")

        df = df[list(DATA.BARS_SCHEMA)].copy()
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        df = (
            df.sort_values("timestamp")
            .drop_duplicates(subset="timestamp", keep="last")
            .reset_index(drop=True)
        )
        DATA.validate_bars_frame(df, symbol=symbol)
        return df

    def get_daily_bars(self, symbol: str) -> pd.DataFrame:
        if symbol in self._cache:
            return self._cache[symbol]
        reason = self._fetch_errors.get(
            symbol,
            f"{symbol!r} was never prefetched -- call .prefetch(universe) before using this "
            f"provider with run_backtest/run_permutation_test.",
        )
        raise DATA.RealEquityDataNotAvailableError(reason)
