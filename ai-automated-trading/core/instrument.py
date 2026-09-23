"""
core/instrument.py

Canonical instrument representation for the Phase 5 research core.

This module is an independent, from-scratch implementation. It does NOT
import anything from mexc_bot. The MEXC native-symbol mapping rule below is
a deliberate re-implementation of the mapping already verified live in
mexc_bot/core/mexc_native.py::to_native_symbol() (ccxt "BASE/QUOTE:QUOTE" ->
native "BASE_QUOTE"), reproduced here as a pure function so this package has
no code dependency on mexc_bot -- per the Phase 5 instruction that the new
research core "must remain independent from the existing mexc_bot ...
implementations. Reuse existing components only through clearly defined
interfaces."

Scope for Phase 5: MEXC USDT-margined perpetual swaps only. Instrument is
deliberately generic (a `venue` field) so a later, real Alpaca boundary can
be added without reshaping this type -- matching the target architecture's
"MEXC-first, multi-venue-ready" boundary.
"""
from __future__ import annotations

from dataclasses import dataclass

SUPPORTED_VENUES = ("MEXC",)


def to_native_mexc_symbol(ccxt_style_or_native: str) -> str:
    """
    Converts a ccxt-style symbol ("BTC/USDT:USDT") to MEXC's native futures
    symbol format ("BTC_USDT"). Idempotent: an already-native symbol
    ("BTC_USDT") passes through unchanged.

    Re-implemented from scratch against the behavior verified in
    mexc_bot/core/mexc_native.py::to_native_symbol() -- not imported from it.
    """
    s = ccxt_style_or_native.strip().upper()
    if "/" not in s:
        return s  # already native, e.g. "BTC_USDT"
    base_quote, _, _settle = s.partition(":")
    base, _, quote = base_quote.partition("/")
    if not base or not quote:
        raise ValueError(f"Cannot parse symbol: {ccxt_style_or_native!r}")
    return f"{base}_{quote}"


@dataclass(frozen=True)
class Instrument:
    """
    Canonical, venue-qualified instrument identity used throughout the
    research core. All internal modules (dataset, strategy, backtest engine,
    results) key off this type rather than a bare string, so a future
    multi-venue extension (Alpaca) only has to add a new venue, not touch
    every consumer.
    """
    venue: str                  # e.g. "MEXC"
    base: str                   # e.g. "BTC"
    quote: str                  # e.g. "USDT"
    market_type: str = "swap"   # "swap" | "spot" -- MEXC-first: only "swap" is used in Phase 5

    def __post_init__(self):
        if self.venue not in SUPPORTED_VENUES:
            raise ValueError(f"Unsupported venue {self.venue!r}; supported: {SUPPORTED_VENUES}")
        if not self.base or not self.quote:
            raise ValueError(f"Instrument requires non-empty base/quote, got base={self.base!r} quote={self.quote!r}")

    @property
    def canonical_id(self) -> str:
        """Stable, venue-qualified identifier used in result records and file names."""
        return f"{self.venue}:{self.base}_{self.quote}:{self.market_type}"

    @property
    def native_symbol(self) -> str:
        """Venue-native symbol string, e.g. MEXC's 'BTC_USDT'."""
        if self.venue == "MEXC":
            return f"{self.base}_{self.quote}"
        raise NotImplementedError(f"native_symbol not implemented for venue {self.venue!r}")

    @classmethod
    def mexc_swap(cls, base: str, quote: str = "USDT") -> "Instrument":
        return cls(venue="MEXC", base=base.upper(), quote=quote.upper(), market_type="swap")

    @classmethod
    def from_native_mexc(cls, native_symbol: str) -> "Instrument":
        """e.g. 'BTC_USDT' -> Instrument(MEXC, BTC, USDT, swap)."""
        native = to_native_mexc_symbol(native_symbol)
        base, _, quote = native.partition("_")
        if not base or not quote:
            raise ValueError(f"Cannot parse native MEXC symbol: {native_symbol!r}")
        return cls.mexc_swap(base, quote)

    def __str__(self) -> str:
        return self.canonical_id
