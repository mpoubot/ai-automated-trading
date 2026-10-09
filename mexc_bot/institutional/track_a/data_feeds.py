"""
Protocol interfaces for the OBSERVED market-data inputs that Track A's
institutional filter layer (liquidity_regime_gate.py,
derivatives_positioning_overlay.py) needs but that DO NOT YET EXIST anywhere
in the mexc_bot codebase: order-book depth, a cross-exchange reference
price, open interest, a liquidation feed, and spot price (for basis).

CONFIRMED ABSENT from mexc_bot as of this writing: core/mexc_native.py and
core/data_fetcher.py provide OHLCV and funding-rate history only. No
depth/order-book endpoint, no open-interest endpoint, no liquidation feed,
and no cross-exchange or spot price source are wired anywhere in that repo.

Because of that, every Protocol below except FundingRateProvider ships ONLY
as a `typing.Protocol` contract plus a synthetic/fixture implementation
(`SyntheticFixtureDataFeeds`) for deterministic unit testing. WIRING A REAL
VENDOR FOR ORDER BOOK / OPEN INTEREST / LIQUIDATIONS / CROSS-EXCHANGE /
SPOT PRICE IS EXPLICITLY OUT OF SCOPE AND LEFT FOR FUTURE, SEPARATE WORK —
do not call any unverified third-party HTTP endpoint from this module.

FundingRateProvider is the one exception: MEXC native funding-rate fetching
ALREADY EXISTS and is verified working in core/mexc_native.py
(`fetch_funding_history_native`), so `NativeMexcFundingProvider` below wraps
that real capability instead of a fixture.

Convention (matches this project's own `.054`-era `BarsProvider` pattern):
every provider class carries two class-level attributes —
    DATA_SOURCE_LABEL: str   — human-readable name of the data source
    IS_REAL_MARKET_DATA: bool — False for synthetic/fixture, True for a
                                real, network-backed implementation
so callers and audit logs can always tell what kind of data gated a trade.

Lazy-import discipline: any method that could someday touch the network
(only NativeMexcFundingProvider, today) imports `core.mexc_native` INSIDE
the method body, not at module top level — matching core/mexc_native.py's
own stated policy of "no live network call as an import-time side effect."
This module's other classes are pure in-memory fixtures and have nothing to
lazy-import.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable

import pandas as pd


# ---------------------------------------------------------------------------
# Order book / depth
# ---------------------------------------------------------------------------

@runtime_checkable
class OrderBookDepthProvider(Protocol):
    """OBSERVED input: live order-book depth and top-of-book spread.

    NOT present anywhere in mexc_bot today. A real implementation (future,
    separate work) would poll MEXC's depth/order-book endpoint; this
    Protocol exists so the liquidity gate can be written and tested against
    it NOW, without committing to an unverified vendor integration.
    """

    DATA_SOURCE_LABEL: str
    IS_REAL_MARKET_DATA: bool

    def get_depth_notional_usd(self, symbol: str, bps_band: float) -> float:
        """Cumulative order-book notional (USD) resting within `bps_band`
        basis points of the current mid price, both sides combined.
        OBSERVED (read directly off the book, not derived)."""
        ...

    def get_top_of_book_spread_bps(self, symbol: str) -> float:
        """Best-bid/best-ask spread in basis points of mid price.
        OBSERVED."""
        ...


# ---------------------------------------------------------------------------
# Cross-exchange reference price
# ---------------------------------------------------------------------------

@runtime_checkable
class CrossExchangeReferenceProvider(Protocol):
    """OBSERVED input: a reference mid price for `symbol` sourced from a
    SECOND venue (e.g. Binance), used to sanity-check MEXC's own price
    isn't a stale/erroneous outlier. NOT present anywhere in mexc_bot today;
    a real implementation is future, separate work."""

    DATA_SOURCE_LABEL: str
    IS_REAL_MARKET_DATA: bool

    def get_reference_mid_price(self, symbol: str) -> float | None:
        """Current reference mid price, or None if unavailable. OBSERVED.
        Returning None (or raising) is how a caller signals "no usable
        reference" — see liquidity_regime_gate.py's fail-CLOSED handling
        of this specific signal."""
        ...


# ---------------------------------------------------------------------------
# Open interest
# ---------------------------------------------------------------------------

@runtime_checkable
class OpenInterestProvider(Protocol):
    """OBSERVED input: current open interest and, where available, a recent
    OI history series. NOT present anywhere in mexc_bot today; a real
    implementation is future, separate work."""

    DATA_SOURCE_LABEL: str
    IS_REAL_MARKET_DATA: bool

    def get_open_interest(self, symbol: str) -> float | None:
        """Current open interest (contracts or USD notional, provider's
        choice — document the unit in the concrete implementation).
        OBSERVED. None if unavailable."""
        ...

    def get_open_interest_history(self, symbol: str, lookback_hours: float) -> pd.Series:
        """OI history over the trailing `lookback_hours`, as a pandas
        Series indexed by timestamp (ascending, oldest first), same unit as
        `get_open_interest`. OBSERVED. Returns an empty Series (not None,
        not a raise) when no history is available, so callers can rely on
        `len(series)` rather than a None-check — matches this project's
        existing `fetch_funding_history_native`-style "empty, don't raise"
        failure contract."""
        ...


# ---------------------------------------------------------------------------
# Liquidation feed
# ---------------------------------------------------------------------------

@runtime_checkable
class LiquidationFeedProvider(Protocol):
    """OBSERVED input: recent forced-liquidation notional for `symbol`. NOT
    present anywhere in mexc_bot today; a real implementation is future,
    separate work."""

    DATA_SOURCE_LABEL: str
    IS_REAL_MARKET_DATA: bool

    def get_liquidation_notional_usd(self, symbol: str, lookback_minutes: float) -> float:
        """Total liquidated notional (USD) across both sides over the
        trailing `lookback_minutes`. OBSERVED. 0.0 when none / unavailable
        — never raises, matching this project's "fail with an empty/zero
        result rather than an exception" convention for data feeds."""
        ...


# ---------------------------------------------------------------------------
# Spot price (for basis = perp vs spot divergence)
# ---------------------------------------------------------------------------

@runtime_checkable
class SpotPriceProvider(Protocol):
    """OBSERVED input: current spot price for `symbol`'s underlying, used
    by derivatives_positioning_overlay.py to compute perp/spot basis. NOT
    present anywhere in mexc_bot today (which only ever fetches
    futures/swap OHLCV); a real implementation is future, separate work."""

    DATA_SOURCE_LABEL: str
    IS_REAL_MARKET_DATA: bool

    def get_spot_price(self, symbol: str) -> float | None:
        """Current spot mid/last price, or None if unavailable. OBSERVED."""
        ...


# ---------------------------------------------------------------------------
# Funding rate — REAL implementation available (core/mexc_native.py)
# ---------------------------------------------------------------------------

@runtime_checkable
class FundingRateProvider(Protocol):
    """OBSERVED input: current and historical perpetual funding rate.
    Unlike the Protocols above, this one has a REAL, verified-working
    implementation already in this codebase (core/mexc_native.py's
    `fetch_funding_history_native`) — see `NativeMexcFundingProvider`
    below."""

    DATA_SOURCE_LABEL: str
    IS_REAL_MARKET_DATA: bool

    def get_current_funding_rate(self, symbol: str) -> float | None:
        """Most recent funding rate (fraction, e.g. 0.0001 = 0.01%) for
        `symbol`, or None if unavailable. OBSERVED."""
        ...

    def get_funding_rate_history(self, symbol: str, cutoff: datetime) -> pd.DataFrame:
        """Funding history back to `cutoff`, as a DataFrame with columns
        [timestamp, funding_rate] (same schema as
        core.mexc_native.fetch_funding_history_native). OBSERVED. Empty
        DataFrame (not a raise) when unavailable."""
        ...


@dataclass(frozen=True, slots=True)
class NativeMexcFundingProvider:
    """Real FundingRateProvider backed by MEXC's native contract API, via
    core.mexc_native.fetch_funding_history_native — REUSES that existing,
    verified-working fetch logic rather than reimplementing it. This is the
    one provider in this module that is not a synthetic fixture.

    Import discipline: `core.mexc_native` is imported LAZILY inside each
    method, not at module top level, matching core/mexc_native.py's own
    documented policy that nothing in this layer makes a live network call
    as an import-time side effect. This also means importing
    `aura_code.track_a.data_feeds` never requires `core` (or `requests`) to
    be on the path unless this class's methods are actually called.

    `raw_dump_dir`: optional path forwarded to fetch_funding_history_native
    for raw-response auditing, same convention as the wrapped function.
    """

    DATA_SOURCE_LABEL: str = "mexc_native_contract_api"
    IS_REAL_MARKET_DATA: bool = True
    raw_dump_dir: str | None = None

    def get_current_funding_rate(self, symbol: str) -> float | None:
        from core import mexc_native  # lazy: avoid network-touching import at module load

        native_symbol = mexc_native.to_native_symbol(symbol)
        try:
            raw = mexc_native.fetch_funding_page_native(native_symbol, page_num=1, page_size=1)
        except Exception:
            # Fail with None, never raise — matches this project's existing
            # "a single bad symbol can't abort the caller" data-feed policy.
            return None
        rows = (raw.get("data") or {}).get("resultList") or []
        if not rows:
            return None
        return float(rows[0].get("fundingRate")) if rows[0].get("fundingRate") is not None else None

    def get_funding_rate_history(self, symbol: str, cutoff: datetime) -> pd.DataFrame:
        from core import mexc_native  # lazy, see class docstring

        native_symbol = mexc_native.to_native_symbol(symbol)
        cutoff_ms = int(cutoff.timestamp() * 1000)
        df = mexc_native.fetch_funding_history_native(
            native_symbol, cutoff_ms, raw_dump_dir=self.raw_dump_dir
        )
        return df[["timestamp", "funding_rate"]] if not df.empty else df


# ---------------------------------------------------------------------------
# Synthetic fixture implementation — for tests only, IS_REAL_MARKET_DATA=False
# ---------------------------------------------------------------------------

@dataclass
class SyntheticFixtureDataFeeds:
    """Simple, controllable, injectable fixture data implementing
    OrderBookDepthProvider, CrossExchangeReferenceProvider,
    OpenInterestProvider, LiquidationFeedProvider and SpotPriceProvider all
    at once, for deterministic unit tests (test_track_a_institutional_filters.py).

    NOT real market data (IS_REAL_MARKET_DATA = False everywhere below) —
    never use this outside tests. Per-symbol values are injected via the
    dict fields at construction time; a lookup miss falls back to the
    matching `default_*` field rather than raising, so tests only need to
    set the symbols they care about.
    """

    DATA_SOURCE_LABEL: str = "synthetic_fixture"
    IS_REAL_MARKET_DATA: bool = False

    # OrderBookDepthProvider fixtures: keyed by (symbol, bps_band) -> USD
    depth_notional_usd: dict[tuple[str, float], float] = field(default_factory=dict)
    default_depth_notional_usd: float = 0.0
    spread_bps: dict[str, float] = field(default_factory=dict)
    default_spread_bps: float = 0.0

    # CrossExchangeReferenceProvider fixtures
    reference_mid_price: dict[str, float | None] = field(default_factory=dict)

    # OpenInterestProvider fixtures
    open_interest: dict[str, float | None] = field(default_factory=dict)
    open_interest_history: dict[str, pd.Series] = field(default_factory=dict)

    # LiquidationFeedProvider fixtures: keyed by (symbol, lookback_minutes) -> USD
    liquidation_notional_usd: dict[tuple[str, float], float] = field(default_factory=dict)
    default_liquidation_notional_usd: float = 0.0

    # SpotPriceProvider fixtures
    spot_price: dict[str, float | None] = field(default_factory=dict)

    # -- OrderBookDepthProvider --
    def get_depth_notional_usd(self, symbol: str, bps_band: float) -> float:
        return self.depth_notional_usd.get((symbol, bps_band), self.default_depth_notional_usd)

    def get_top_of_book_spread_bps(self, symbol: str) -> float:
        return self.spread_bps.get(symbol, self.default_spread_bps)

    # -- CrossExchangeReferenceProvider --
    def get_reference_mid_price(self, symbol: str) -> float | None:
        return self.reference_mid_price.get(symbol)

    # -- OpenInterestProvider --
    def get_open_interest(self, symbol: str) -> float | None:
        return self.open_interest.get(symbol)

    def get_open_interest_history(self, symbol: str, lookback_hours: float) -> pd.Series:
        series = self.open_interest_history.get(symbol)
        if series is None:
            return pd.Series(dtype="float64")
        cutoff = series.index.max() - pd.Timedelta(hours=lookback_hours)
        return series[series.index >= cutoff]

    # -- LiquidationFeedProvider --
    def get_liquidation_notional_usd(self, symbol: str, lookback_minutes: float) -> float:
        return self.liquidation_notional_usd.get(
            (symbol, lookback_minutes), self.default_liquidation_notional_usd
        )

    # -- SpotPriceProvider --
    def get_spot_price(self, symbol: str) -> float | None:
        return self.spot_price.get(symbol)
