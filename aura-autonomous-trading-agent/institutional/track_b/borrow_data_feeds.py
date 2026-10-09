#!/usr/bin/env python3
"""
AURA Track B -- Borrow / short-interest data-feed protocols.

WHAT THIS MODULE IS, AND IS DELIBERATELY NOT
------------------------------------------------------------------------
Pure interface + synthetic-fixture layer. It defines the two `Protocol`
shapes the real gates in this package (`borrow_fee_gate.py`,
`squeeze_crowding_gate.py`) consume, plus fixture implementations for
tests and local development.

This module does NOT implement a real borrow-fee feed (e.g. a live
Alpaca/IBKR borrow-rate endpoint) or a real short-interest vendor feed
(e.g. a FINRA/Ortex/S3-style vendor pull). Those are explicitly OUT OF
SCOPE here and flagged as separate follow-up work -- consistent with this
project's existing "never silently fake a real feed" discipline (see
`.335`'s own `fetch_asset_metadata` vs `submit()` split, and `.367`'s
earnings-calendar ingestion, both of which keep a hard, disclosed line
between "pure logic that consumes a feed" and "the network call that
would populate one in production"). Anyone wiring this package into a
live book MUST plug in a real implementation of `BorrowDataProvider`
and/or `ShortInterestDataProvider` -- the `SyntheticFixtureBorrowFeeds`
and `SyntheticFixtureShortInterestFeed` classes below exist ONLY for
tests and local development and both set `IS_REAL_MARKET_DATA = False`
so a caller can assert, at runtime, that it is not accidentally trading
real capital against fixture data.

Data tier (OBSERVED / DERIVED / CLAIMED), per this project's disclosure
convention
------------------------------------------------------------------------
  - `easy_to_borrow` (bool): OBSERVED when sourced from a real broker's
    asset metadata (e.g. `.334`/`.335`'s own Alpaca `Asset.easy_to_borrow`
    field) -- a direct broker-reported flag, not derived or inferred here.
    This module re-declares it on `BorrowDataProvider` because the
    borrow-fee gate needs its OWN read of this flag (for in-trade recall
    detection in `evaluate_open_short_for_recall`), independent of
    whatever `.335`'s own pre-trade shortability check already did --
    mirroring `.335`'s own "never trust a caller-supplied safety
    assertion alone, always independently recompute" discipline.
  - `borrow_fee_rate_annualized` (float | None): OBSERVED when sourced
    from a real borrow-rate feed, `None` when unavailable. This module
    never fabricates a fee rate when a feed cannot supply one -- a
    missing rate is surfaced as `None`, and it is the CALLER's job
    (`borrow_fee_gate.py`) to decide whether `None` fails open or closed.
  - `short_interest_pct_float` / `days_to_cover` (float | None): CLAIMED-
    ADJACENT and explicitly LAGGED. Real-world short-interest data (e.g.
    FINRA short-interest reports) is published on a bi-monthly settlement
    cadence, so by the time any consumer reads it, it describes a past
    settlement date, not "now". This module surfaces that lag honestly
    via `as_of_date` on `ShortInterestSnapshot` rather than presenting a
    stale number as current. Treat any non-`None` value here as "true as
    of `as_of_date`, not necessarily true today."

Why `Protocol` (not an ABC)
------------------------------------------------------------------------
Matches this repo's existing duck-typing discipline at module boundaries
(`.368` reads `calendar_state.status`/`calendar_by_symbol` via
`getattr`, never an isinstance check against a concrete base class).
`@runtime_checkable` lets a caller that wants to assert shape at a
boundary do `isinstance(x, BorrowDataProvider)` without forcing every
real implementation to inherit from anything in this file.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol, runtime_checkable

VERSION = "AURA Track B v0.1.0"
ENGINE = "BORROW_DATA_FEEDS"


# ============================================================================
# Snapshot value types returned by the providers below. Kept as plain
# frozen dataclasses (not the Protocols themselves) so a provider can
# return a fully-typed, immutable record rather than exposing many
# individual getter methods.
# ============================================================================


@dataclass(frozen=True, slots=True)
class BorrowSnapshot:
    """One symbol's current borrow picture. `fetched_at` is this
    snapshot's own fetch timestamp (UTC), for staleness checks by a
    caller -- this module does not itself enforce a staleness policy,
    it only carries the timestamp honestly."""

    symbol: str
    easy_to_borrow: bool | None  # OBSERVED (broker flag) when real; None if the feed cannot answer
    borrow_fee_rate_annualized: float | None  # OBSERVED when real; None if unavailable -- never fabricated
    fetched_at: datetime  # UTC. Staleness policy is the caller's job, not this module's.
    source_label: str

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "easy_to_borrow": self.easy_to_borrow,
            "borrow_fee_rate_annualized": self.borrow_fee_rate_annualized,
            "fetched_at": self.fetched_at.isoformat(),
            "source_label": self.source_label,
        }


@dataclass(frozen=True, slots=True)
class ShortInterestSnapshot:
    """One symbol's most recently published short-interest picture.
    EXPLICITLY LAGGED -- `as_of_date` is the settlement/reporting date
    this data describes, not today. A caller that needs freshness-aware
    behavior (e.g. discount or ignore data older than N days) must do
    that itself using `as_of_date`; this module makes no claim about
    how old is "too old"."""

    symbol: str
    short_interest_pct_float: float | None  # CLAIMED-adjacent, lagged. None if unavailable.
    days_to_cover: float | None  # CLAIMED-adjacent, lagged. None if unavailable.
    as_of_date: date | None  # the settlement date this data describes; None if truly unknown
    source_label: str

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "short_interest_pct_float": self.short_interest_pct_float,
            "days_to_cover": self.days_to_cover,
            "as_of_date": self.as_of_date.isoformat() if self.as_of_date else None,
            "source_label": self.source_label,
        }


# ============================================================================
# Protocols
# ============================================================================


@runtime_checkable
class BorrowDataProvider(Protocol):
    """Per-symbol borrow availability + cost. `DATA_SOURCE_LABEL` and
    `IS_REAL_MARKET_DATA` are class-level attributes every implementation
    must declare -- so a caller can inspect *what* it is actually wired
    to without calling anything, and so a fixture can never be mistaken
    for a real feed by a downstream consumer that checks the flag."""

    DATA_SOURCE_LABEL: str
    IS_REAL_MARKET_DATA: bool

    def get_borrow_snapshot(self, symbol: str) -> BorrowSnapshot:
        """Returns the current borrow picture for `symbol`. Implementations
        that cannot reach their underlying feed should return a snapshot
        with `easy_to_borrow=None` and `borrow_fee_rate_annualized=None`
        rather than raising, so callers get a uniform "data unavailable"
        shape to fail-closed against -- never a silent default."""
        ...


@runtime_checkable
class ShortInterestDataProvider(Protocol):
    """Per-symbol short-interest / days-to-cover, bi-monthly FINRA-cadence
    in the real world. See module docstring: LAGGED, CLAIMED-adjacent."""

    DATA_SOURCE_LABEL: str
    IS_REAL_MARKET_DATA: bool

    def get_short_interest_snapshot(self, symbol: str) -> ShortInterestSnapshot:
        """Returns the most recently published short-interest picture for
        `symbol`. Same "unavailable -> None fields, don't raise" contract
        as `BorrowDataProvider.get_borrow_snapshot`."""
        ...


# ============================================================================
# Synthetic fixtures -- TEST / LOCAL-DEV ONLY. Never point production code
# at these; `IS_REAL_MARKET_DATA = False` on both exists precisely so a
# caller can assert that at a wiring boundary.
# ============================================================================


class SyntheticFixtureBorrowFeeds:
    """Controllable fixture implementing `BorrowDataProvider`. Construct
    with an explicit per-symbol table; symbols not in the table return an
    "unavailable" snapshot (both fields `None`) rather than raising or
    guessing a default -- this mirrors how a real feed should behave on
    a symbol it has never heard of, so tests exercise the same fail-
    closed path a real integration would need to.

    NOT real market data. `IS_REAL_MARKET_DATA = False`.
    """

    DATA_SOURCE_LABEL = "SYNTHETIC_FIXTURE_BORROW_FEED"
    IS_REAL_MARKET_DATA = False

    def __init__(
        self,
        fixture_by_symbol: dict[str, tuple[bool | None, float | None]] | None = None,
        *,
        fetched_at: datetime | None = None,
    ) -> None:
        """`fixture_by_symbol` maps SYMBOL -> (easy_to_borrow, borrow_fee_rate_annualized).
        `fetched_at` lets a test pin the snapshot timestamp; defaults to
        a fixed epoch-adjacent constant (not `datetime.now()`) so fixture
        behavior is deterministic across test runs unless a test
        explicitly wants to exercise staleness."""
        self._fixture_by_symbol = dict(fixture_by_symbol or {})
        self._fetched_at = fetched_at or datetime(2026, 1, 1, tzinfo=None)

    def set_symbol(self, symbol: str, *, easy_to_borrow: bool | None, borrow_fee_rate_annualized: float | None) -> None:
        """Test convenience: mutate or add one symbol's fixture values in place."""
        self._fixture_by_symbol[symbol.upper()] = (easy_to_borrow, borrow_fee_rate_annualized)

    def get_borrow_snapshot(self, symbol: str) -> BorrowSnapshot:
        entry = self._fixture_by_symbol.get(symbol.upper())
        if entry is None:
            return BorrowSnapshot(
                symbol=symbol.upper(),
                easy_to_borrow=None,
                borrow_fee_rate_annualized=None,
                fetched_at=self._fetched_at,
                source_label=self.DATA_SOURCE_LABEL,
            )
        easy_to_borrow, fee_rate = entry
        return BorrowSnapshot(
            symbol=symbol.upper(),
            easy_to_borrow=easy_to_borrow,
            borrow_fee_rate_annualized=fee_rate,
            fetched_at=self._fetched_at,
            source_label=self.DATA_SOURCE_LABEL,
        )


class SyntheticFixtureShortInterestFeed:
    """Controllable fixture implementing `ShortInterestDataProvider`.
    Same "unknown symbol -> unavailable snapshot, never guess" discipline
    as `SyntheticFixtureBorrowFeeds`.

    NOT real market data. `IS_REAL_MARKET_DATA = False`.
    """

    DATA_SOURCE_LABEL = "SYNTHETIC_FIXTURE_SHORT_INTEREST_FEED"
    IS_REAL_MARKET_DATA = False

    def __init__(
        self,
        fixture_by_symbol: dict[str, tuple[float | None, float | None, date | None]] | None = None,
    ) -> None:
        """`fixture_by_symbol` maps SYMBOL -> (short_interest_pct_float, days_to_cover, as_of_date)."""
        self._fixture_by_symbol = dict(fixture_by_symbol or {})

    def set_symbol(
        self,
        symbol: str,
        *,
        short_interest_pct_float: float | None,
        days_to_cover: float | None,
        as_of_date: date | None,
    ) -> None:
        self._fixture_by_symbol[symbol.upper()] = (short_interest_pct_float, days_to_cover, as_of_date)

    def get_short_interest_snapshot(self, symbol: str) -> ShortInterestSnapshot:
        entry = self._fixture_by_symbol.get(symbol.upper())
        if entry is None:
            return ShortInterestSnapshot(
                symbol=symbol.upper(),
                short_interest_pct_float=None,
                days_to_cover=None,
                as_of_date=None,
                source_label=self.DATA_SOURCE_LABEL,
            )
        short_interest_pct_float, days_to_cover, as_of_date = entry
        return ShortInterestSnapshot(
            symbol=symbol.upper(),
            short_interest_pct_float=short_interest_pct_float,
            days_to_cover=days_to_cover,
            as_of_date=as_of_date,
            source_label=self.DATA_SOURCE_LABEL,
        )
