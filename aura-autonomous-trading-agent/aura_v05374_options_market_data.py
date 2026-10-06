#!/usr/bin/env python3
"""AURA v0.5.3.74 -- Options market data (Phase O2).

Read-only options-data adapter: fetches options chains (every contract
Alpaca lists for an underlying within a caller-supplied strike/expiry
window) together with each contract's latest quote, latest trade, and
Alpaca's own implied-volatility/Greeks figures. It never submits an
order and never mutates account state -- same scope boundary as .321's
crypto-bars adapter and .351's equity signal source.

This is a LIBRARY module, not a standalone script -- it has no main().
.351 (the closest analog: a live Alpaca data adapter with no downstream
consumer built yet beyond .054_signal_source.py) is the precedent for
that shape; .321 has a main() because its output is a CSV file nothing
else reaches via direct import. Nothing in Track B consumes O2's output
yet (O3-O9 aren't built), so inventing an output file format/CLI now
would be guessing at a consumer that doesn't exist. This module is
meant to be imported directly, the same way .054_signal_source.py
imports .351.

Scoping decisions made explicitly with Martin, 2026-10-06, before this
module was written (not guessed):

  1. **Feed: OPRA required, never silently downgraded.** Every chain
     request asks for OptionsFeed.OPRA explicitly. If Alpaca's account
     isn't entitled (confirmed via the installed alpaca-py 0.44.0 SDK,
     the exact version pinned in requirements.txt: an unentitled feed
     request raises alpaca.common.exceptions.APIError with
     status_code in (401, 403)), this module raises
     OptionsFeedNotEntitledError and stops -- it never falls back to
     the free INDICATIVE feed on its own initiative. Silently trading
     down data quality without telling anyone is exactly the kind of
     thing this project's fail-closed discipline exists to prevent.
  2. **Alpaca's own implied_volatility/greeks are carried through as
     explicitly-labeled broker-supplied fields** (broker_* prefix),
     not AURA's own computed values. O3 (.375, not yet built) will
     still independently derive Greeks from first principles, the same
     "never trust a single source blindly" philosophy as .054_atr.py's
     isolated reimplementation -- Alpaca's numbers become a future
     cross-check input for O3, never the source of truth.
  3. **Underlyings: the same pinned 27-symbol equity/ETF universe as
     .351/.352** (aura_v05351_equity_universe_v1.json), reused via
     .351's own load_pinned_universe(), not a separate options-specific
     list.

Cross-module dependencies and the test-loader hazard this module
deliberately avoids
------------------------------------------------------------------------
This module imports two other aura_v05NNN modules directly:
  - aura_v05373_options_instrument_metadata (O1) -- the ONE canonical
    OCC-symbol parser/contract-metadata builder (see O1's own
    docstring). Every contract symbol Alpaca returns is decoded via
    O1's parse_occ_symbol()/build_option_contract_metadata(), never a
    second, locally-reimplemented parser.
  - aura_v05351_live_alpaca_equity_signal_source -- reuses
    load_pinned_universe()/PinnedUniverse rather than re-reading the
    universe JSON a second way.

Both are resolved via a small _load_*_module() helper that tries a
real top-level `import aura_v05NNN_... as mod` first, falling back to
a manual importlib.util.spec_from_file_location()/exec_module() load
only if that plain import fails (e.g. running this file directly from
a working directory that doesn't have the repo root on sys.path). This
is .335's own established pattern for composing aura_v05NNN modules
(see aura_v05335_alpaca_equity_execution_adapter.py:171-198), reused
here verbatim -- deliberately NOT the bare importlib-exec_module
pattern used by .334's test file on its own.

The reason this distinction matters: .338's own flakiness bug,
diagnosed and fixed earlier this session (2026-10-05), was caused by
exactly this hazard -- the SAME module file loaded through two
different mechanisms within one pytest process creates two distinct
module/class objects, and isinstance()/identity checks between them
can then fail unpredictably depending on test collection order. O1
defines a frozen dataclass (OptionLegInput) that O1's own
build_option_structure() isinstance-checks; O2 doesn't construct
OptionLegInput itself, but it does import O1 as a live dependency, so
this module and its test file use ONLY the try-import-first pattern
above -- never a second, independent ad hoc loader for the same file,
anywhere in this module or its tests.

No real credentials, no network call, and no order submission occurs
at import time anywhere in this module -- only when a caller actually
constructs AlpacaOptionsDataClient and calls one of the fetch
functions below, mirroring .351's AlpacaHistoricalBarsClient.
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Protocol, Sequence

ROOT = Path(__file__).resolve().parent

VERSION = "AURA v0.5.3.74"
ENGINE = "OPTIONS_MARKET_DATA"
SCHEMA_VERSION = "1.0"


class OptionsMarketDataError(Exception):
    """Generic fail-closed error for this module. Never silently
    swallowed by any function below -- every caller either handles a
    specific subclass explicitly or lets this propagate."""


class OptionsFeedNotEntitledError(OptionsMarketDataError):
    """Raised when Alpaca rejects the required OPRA feed request as
    unentitled (HTTP 401/403 from the options data API). This module
    NEVER silently falls back to the INDICATIVE feed on this error --
    per Martin's explicit instruction, 2026-10-06: detect and fail
    closed rather than silently downgrade data quality."""


# ============================================================================
# Cross-module dependency loading -- see module docstring for why this
# specific try-import-first pattern (copied from .335) is used instead
# of a bare importlib-exec_module loader.
# ============================================================================


def _load_options_metadata_module():
    try:
        import aura_v05373_options_instrument_metadata as mod

        return mod
    except ImportError:
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "aura_v05373_options_instrument_metadata",
            ROOT / "aura_v05373_options_instrument_metadata.py",
        )
        mod = importlib.util.module_from_spec(spec)
        # Required: .373 defines a @dataclass under
        # `from __future__ import annotations`, which needs the module
        # registered in sys.modules *before* exec to resolve string
        # annotations (CPython 3.13). See .373's own test-loader fix,
        # 2026-10-05, for the full diagnosis.
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return mod


def _load_pinned_universe_module():
    try:
        import aura_v05351_live_alpaca_equity_signal_source as mod

        return mod
    except ImportError:
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "aura_v05351_live_alpaca_equity_signal_source",
            ROOT / "aura_v05351_live_alpaca_equity_signal_source.py",
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return mod


# ============================================================================
# Live data fetch -- isolated behind a Protocol so every function below
# this point is network-free and directly testable, mirroring .351's
# BarsClient/AlpacaHistoricalBarsClient isolation.
# ============================================================================


class OptionsDataClient(Protocol):
    def get_option_chain(self, request: Any) -> Any: ...


class AlpacaOptionsDataClient:
    """Thin wrapper over alpaca-py's OptionHistoricalDataClient. No
    credentials are read or validated at import time -- only when this
    class is actually constructed, mirroring .351's
    AlpacaHistoricalBarsClient."""

    def __init__(self, api_key: str, secret_key: str) -> None:
        from alpaca.data.historical.option import OptionHistoricalDataClient

        self._client = OptionHistoricalDataClient(api_key, secret_key)

    def get_option_chain(self, request: Any) -> Any:
        return self._client.get_option_chain(request)


# ============================================================================
# Snapshot field extraction -- converts whatever get_option_chain()
# returns (typed OptionsSnapshot objects in the normal case) into plain,
# JSON-serializable dicts. Uses getattr() throughout rather than
# assuming a concrete class, so a hand-built test fake only needs to
# expose the same attribute names, not inherit from any alpaca-py type.
# ============================================================================


def _quote_to_dict(quote: Any) -> dict[str, Any] | None:
    if quote is None:
        return None
    timestamp = getattr(quote, "timestamp", None)
    return {
        "bid_price": getattr(quote, "bid_price", None),
        "bid_size": getattr(quote, "bid_size", None),
        "ask_price": getattr(quote, "ask_price", None),
        "ask_size": getattr(quote, "ask_size", None),
        "timestamp": timestamp.isoformat() if timestamp is not None else None,
    }


def _trade_to_dict(trade: Any) -> dict[str, Any] | None:
    if trade is None:
        return None
    timestamp = getattr(trade, "timestamp", None)
    return {
        "price": getattr(trade, "price", None),
        "size": getattr(trade, "size", None),
        "timestamp": timestamp.isoformat() if timestamp is not None else None,
    }


def _greeks_to_dict(greeks: Any) -> dict[str, Any] | None:
    if greeks is None:
        return None
    return {
        "delta": getattr(greeks, "delta", None),
        "gamma": getattr(greeks, "gamma", None),
        "theta": getattr(greeks, "theta", None),
        "vega": getattr(greeks, "vega", None),
        "rho": getattr(greeks, "rho", None),
    }


def _classify_quote_status(
    quote: Any, *, now: datetime, max_quote_age_seconds: float
) -> tuple[str, float | None]:
    """Fail-closed classification of a single contract's quote,
    mirroring .351's TechnicalStatus discipline: never guess a missing
    or stale quote is close enough to usable."""
    if quote is None:
        return "MISSING_QUOTE", None

    bid = getattr(quote, "bid_price", None)
    ask = getattr(quote, "ask_price", None)
    if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
        return "INVALID_QUOTE", None

    timestamp = getattr(quote, "timestamp", None)
    if timestamp is None:
        return "MISSING_QUOTE_TIMESTAMP", None
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)

    age_seconds = (now - timestamp).total_seconds()
    if age_seconds < 0:
        return "FUTURE_TIMESTAMP", age_seconds
    if age_seconds > max_quote_age_seconds:
        return "STALE_QUOTE", age_seconds
    return "OK", age_seconds


# ============================================================================
# Chain fetch -- one underlying at a time.
# ============================================================================


def fetch_option_chain(
    underlying_symbol: str,
    *,
    client: OptionsDataClient,
    expiration_date_gte: date,
    expiration_date_lte: date,
    max_quote_age_seconds: float,
    strike_price_gte: float | None = None,
    strike_price_lte: float | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Fetches the OPRA options chain for `underlying_symbol` within the
    given expiry window (and optional strike bounds), decodes every
    returned contract symbol via O1's canonical OCC parser, and
    classifies each contract's quote freshness.

    `expiration_date_gte`/`expiration_date_lte`/`max_quote_age_seconds`
    are required, no-default parameters -- this module never invents a
    DTE window or staleness threshold, matching .351's
    TechnicalScoringParams discipline ("never invent numbers").

    A malformed or underlying-mismatched contract symbol is recorded in
    the returned `anomalies` list and skipped, not fatal to the whole
    chain -- one bad entry out of a chain of hundreds must not block
    every other contract, the same "required fatal / optional
    best-effort" shape as .321's REQUIRED_SYMBOLS vs. optional-universe
    split, applied here at the per-contract-within-one-chain level
    rather than per-symbol-within-a-universe.

    Raises OptionsFeedNotEntitledError if Alpaca rejects the OPRA feed
    request as unentitled (HTTP 401/403) -- never silently retried
    against the INDICATIVE feed. Raises OptionsMarketDataError on any
    other API failure, a malformed response shape, or an empty/entirely
    anomalous chain.
    """
    if not isinstance(underlying_symbol, str) or not underlying_symbol:
        raise OptionsMarketDataError("INVALID_UNDERLYING_SYMBOL")
    if not isinstance(expiration_date_gte, date) or not isinstance(
        expiration_date_lte, date
    ):
        raise OptionsMarketDataError("INVALID_EXPIRATION_DATE_TYPE")
    if expiration_date_gte > expiration_date_lte:
        raise OptionsMarketDataError("INVALID_EXPIRATION_WINDOW:gte>lte")
    if not isinstance(max_quote_age_seconds, (int, float)) or isinstance(
        max_quote_age_seconds, bool
    ) or max_quote_age_seconds <= 0:
        raise OptionsMarketDataError("INVALID_MAX_QUOTE_AGE:must be > 0")
    if strike_price_gte is not None and strike_price_lte is not None:
        if strike_price_gte > strike_price_lte:
            raise OptionsMarketDataError("INVALID_STRIKE_WINDOW:gte>lte")

    metadata = _load_options_metadata_module()
    now_dt = now or datetime.now(timezone.utc)

    from alpaca.data.enums import OptionsFeed
    from alpaca.data.requests import OptionChainRequest

    request = OptionChainRequest(
        underlying_symbol=underlying_symbol,
        feed=OptionsFeed.OPRA,
        expiration_date_gte=expiration_date_gte,
        expiration_date_lte=expiration_date_lte,
        strike_price_gte=strike_price_gte,
        strike_price_lte=strike_price_lte,
    )

    try:
        raw_chain = client.get_option_chain(request)
    except Exception as exc:
        status_code = getattr(exc, "status_code", None)
        if status_code in (401, 403):
            raise OptionsFeedNotEntitledError(
                f"OPRA_FEED_NOT_ENTITLED:{underlying_symbol}:"
                f"status={status_code}:{exc}"
            ) from exc
        raise OptionsMarketDataError(
            f"ALPACA_OPTION_CHAIN_FETCH_FAILED:{underlying_symbol}:{type(exc).__name__}:{exc}"
        ) from exc

    if not isinstance(raw_chain, dict):
        raise OptionsMarketDataError(f"MALFORMED_CHAIN_RESPONSE:{underlying_symbol}")
    if not raw_chain:
        raise OptionsMarketDataError(f"EMPTY_CHAIN:{underlying_symbol}")

    contracts: list[dict[str, Any]] = []
    anomalies: list[dict[str, str]] = []

    for occ_symbol, snapshot in raw_chain.items():
        if not isinstance(occ_symbol, str):
            anomalies.append(
                {"occ_symbol": repr(occ_symbol), "reason": "NON_STRING_SYMBOL_KEY"}
            )
            continue

        try:
            parsed = metadata.parse_occ_symbol(occ_symbol)
        except Exception as exc:
            anomalies.append(
                {"occ_symbol": occ_symbol, "reason": f"UNPARSEABLE_OCC_SYMBOL:{exc}"}
            )
            continue

        if parsed["underlying_symbol"] != underlying_symbol:
            anomalies.append(
                {"occ_symbol": occ_symbol, "reason": "UNDERLYING_MISMATCH"}
            )
            continue

        try:
            contract_record = metadata.build_option_contract_metadata(
                underlying_symbol=parsed["underlying_symbol"],
                strike=parsed["strike"],
                expiry=parsed["expiry"],
                right=parsed["right"],
            )
        except Exception as exc:
            anomalies.append(
                {
                    "occ_symbol": occ_symbol,
                    "reason": f"CONTRACT_METADATA_BUILD_FAILED:{exc}",
                }
            )
            continue

        if contract_record["occ_symbol"] != occ_symbol:
            anomalies.append(
                {"occ_symbol": occ_symbol, "reason": "OCC_SYMBOL_ROUND_TRIP_MISMATCH"}
            )
            continue

        quote = getattr(snapshot, "latest_quote", None)
        trade = getattr(snapshot, "latest_trade", None)
        broker_iv = getattr(snapshot, "implied_volatility", None)
        broker_greeks = getattr(snapshot, "greeks", None)

        data_status, quote_age_seconds = _classify_quote_status(
            quote, now=now_dt, max_quote_age_seconds=max_quote_age_seconds
        )

        contracts.append(
            {
                "contract": contract_record,
                "data_status": data_status,
                "quote_age_seconds": quote_age_seconds,
                "broker_latest_quote": _quote_to_dict(quote),
                "broker_latest_trade": _trade_to_dict(trade),
                "broker_implied_volatility": broker_iv,
                "broker_greeks": _greeks_to_dict(broker_greeks),
            }
        )

    if not contracts:
        raise OptionsMarketDataError(
            f"NO_USABLE_CONTRACTS:{underlying_symbol}:{len(anomalies)} anomalies"
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "engine": ENGINE,
        "underlying_symbol": underlying_symbol,
        "as_of": now_dt.isoformat(),
        "feed": "OPRA",
        "expiration_date_gte": expiration_date_gte.isoformat(),
        "expiration_date_lte": expiration_date_lte.isoformat(),
        "contract_count": len(contracts),
        "anomaly_count": len(anomalies),
        "contracts": contracts,
        "anomalies": anomalies,
    }


# ============================================================================
# Chain fetch -- the pinned universe, batched.
# ============================================================================


def fetch_universe_option_chains(
    *,
    client: OptionsDataClient,
    symbols: Sequence[str],
    expiration_date_gte: date,
    expiration_date_lte: date,
    max_quote_age_seconds: float,
    strike_price_gte: float | None = None,
    strike_price_lte: float | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Fetches option chains for every symbol in `symbols`
    (independently of how that list was produced -- see
    fetch_pinned_universe_option_chains() for the .351-sourced
    version).

    A per-underlying OptionsMarketDataError (e.g. that underlying has no
    listed options, or a transient API failure) is caught and recorded
    in `fetch_errors`; it never loses every other underlying's
    already-fetched data. OptionsFeedNotEntitledError is the one
    exception NOT caught here -- an entitlement failure is an
    account-level condition true for every symbol, not a per-symbol
    data-quality problem, so it is deliberately allowed to propagate
    and abort the whole batch immediately rather than burning through
    the rest of the universe on a request that cannot succeed.
    """
    if not symbols:
        raise OptionsMarketDataError("EMPTY_SYMBOL_LIST")

    now_dt = now or datetime.now(timezone.utc)
    chains: dict[str, Any] = {}
    fetch_errors: dict[str, str] = {}

    for symbol in symbols:
        try:
            chains[symbol] = fetch_option_chain(
                symbol,
                client=client,
                expiration_date_gte=expiration_date_gte,
                expiration_date_lte=expiration_date_lte,
                max_quote_age_seconds=max_quote_age_seconds,
                strike_price_gte=strike_price_gte,
                strike_price_lte=strike_price_lte,
                now=now_dt,
            )
        except OptionsFeedNotEntitledError:
            raise
        except OptionsMarketDataError as exc:
            fetch_errors[symbol] = str(exc)

    return {
        "schema_version": SCHEMA_VERSION,
        "engine": ENGINE,
        "as_of": now_dt.isoformat(),
        "universe_size": len(symbols),
        "fetched_count": len(chains),
        "failed_count": len(fetch_errors),
        "chains": chains,
        "fetch_errors": fetch_errors,
    }


def fetch_pinned_universe_option_chains(
    *,
    client: OptionsDataClient,
    expiration_date_gte: date,
    expiration_date_lte: date,
    max_quote_age_seconds: float,
    strike_price_gte: float | None = None,
    strike_price_lte: float | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """fetch_universe_option_chains() over .351's pinned 27-symbol
    equity/ETF universe -- the convenience entry point most callers
    should use, per Martin's 2026-10-06 scoping answer (reuse the
    pinned universe rather than a separate options-specific list)."""
    universe_module = _load_pinned_universe_module()
    pinned = universe_module.load_pinned_universe()
    return fetch_universe_option_chains(
        client=client,
        symbols=pinned.symbols,
        expiration_date_gte=expiration_date_gte,
        expiration_date_lte=expiration_date_lte,
        max_quote_age_seconds=max_quote_age_seconds,
        strike_price_gte=strike_price_gte,
        strike_price_lte=strike_price_lte,
        now=now,
    )
