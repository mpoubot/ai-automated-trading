#!/usr/bin/env python3
"""Unit + compatibility tests for AURA v0.5.3.34 (Asset / Instrument
Metadata).

Covers, in order, every item from Martin's explicit test list for this
milestone: stock, ETF, crypto, futures, long capability, short capability,
unsupported direction, unsupported asset/venue combination, shortability,
malformed metadata, deterministic representation/fingerprinting, fail-
closed behaviour -- plus a dedicated .33 compatibility regression and a
"future support" verification test (Stocks/ETFs/Crypto/Futures LONG+SHORT
representability).

No real credentials, no network call, no order submission anywhere in
this file.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


META = _load("aura_v05334_asset_instrument_metadata", "aura_v05334_asset_instrument_metadata.py")
CANON = _load("aura_v05333_canonical_execution_specification", "aura_v05333_canonical_execution_specification.py")


def expect(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)
    print(f"PASS: {name}")


def expect_fails(name: str, fn, *args, **kwargs) -> None:
    try:
        fn(*args, **kwargs)
    except RuntimeError:
        print(f"PASS: {name}")
        return
    raise AssertionError(f"{name} (expected RuntimeError, none raised)")


# ======================================================================= #
# Stock
# ======================================================================= #

def test_stock_metadata_builds_and_classifies_correctly() -> None:
    record = META.build_instrument_metadata(
        instrument_type="STOCK", contract_class=None, symbol="AAPL", venue="ALPACA",
    )
    expect("stock: instrument_type STOCK", record["instrument_type"] == "STOCK")
    expect("stock: contract_class None", record["contract_class"] is None)
    expect("stock: long_capable True", record["long_capable"] is True)
    expect("stock: short_mechanism REQUIRES_BORROW", record["short_mechanism"] == "REQUIRES_BORROW")
    expect("stock: trading_calendar EXCHANGE_HOURS", record["trading_calendar"] == "EXCHANGE_HOURS")
    expect("stock: default shortability_status UNKNOWN, never SHORTABLE", record["shortability_status"] == "UNKNOWN")
    expect("stock: canonical_asset_class maps to .33's STOCK", record["canonical_asset_class"] == "STOCK")


def test_stock_invalid_ticker_shape_fails_closed() -> None:
    expect_fails("stock: lowercase ticker rejected", META.build_instrument_metadata,
                 instrument_type="STOCK", contract_class=None, symbol="aapl", venue="ALPACA")
    expect_fails("stock: crypto-pair-shaped symbol rejected for STOCK", META.build_instrument_metadata,
                 instrument_type="STOCK", contract_class=None, symbol="BTC/USD", venue="ALPACA")


# ======================================================================= #
# ETF
# ======================================================================= #

def test_etf_metadata_builds_and_classifies_correctly() -> None:
    record = META.build_instrument_metadata(
        instrument_type="ETF", contract_class=None, symbol="SPY", venue="ALPACA",
        shortability_status="SHORTABLE",
    )
    expect("etf: instrument_type ETF", record["instrument_type"] == "ETF")
    expect("etf: short_mechanism REQUIRES_BORROW", record["short_mechanism"] == "REQUIRES_BORROW")
    expect("etf: explicit SHORTABLE preserved, not overwritten", record["shortability_status"] == "SHORTABLE")
    expect("etf: canonical_asset_class maps to .33's ETF", record["canonical_asset_class"] == "ETF")


# ======================================================================= #
# Crypto (both contract classes)
# ======================================================================= #

def test_crypto_spot_metadata() -> None:
    record = META.build_instrument_metadata(
        instrument_type="CRYPTO", contract_class="SPOT", symbol="BTC/USD", venue="ALPACA",
    )
    expect("crypto-spot: short_mechanism REQUIRES_BORROW (no margin on Alpaca spot crypto)",
           record["short_mechanism"] == "REQUIRES_BORROW")
    expect("crypto-spot: trading_calendar CONTINUOUS_24_7", record["trading_calendar"] == "CONTINUOUS_24_7")
    expect("crypto-spot: canonical_asset_class maps to .33's CRYPTO_SPOT", record["canonical_asset_class"] == "CRYPTO_SPOT")


def test_crypto_perpetual_swap_metadata() -> None:
    record = META.build_instrument_metadata(
        instrument_type="CRYPTO", contract_class="PERPETUAL_SWAP", symbol="BTC/USDT:USDT", venue="MEXC",
    )
    expect("crypto-swap: short_mechanism NATIVE", record["short_mechanism"] == "NATIVE")
    expect("crypto-swap: trading_calendar CONTINUOUS_24_7", record["trading_calendar"] == "CONTINUOUS_24_7")
    expect("crypto-swap: canonical_asset_class maps to .33's CRYPTO_FUTURES",
           record["canonical_asset_class"] == "CRYPTO_FUTURES")
    # A NATIVE-short instrument has no borrow concept -- supplying a
    # SHORTABLE/NOT_SHORTABLE claim for one is itself a malformed input.
    expect_fails("crypto-swap: shortability_status not applicable to a NATIVE-short instrument",
                 META.build_instrument_metadata, instrument_type="CRYPTO", contract_class="PERPETUAL_SWAP",
                 symbol="BTC/USDT:USDT", venue="MEXC", shortability_status="SHORTABLE")


# ======================================================================= #
# Futures (forward-looking scaffolding, no venue yet)
# ======================================================================= #

def test_futures_structural_capability_is_modeled_even_though_no_venue_exists_yet() -> None:
    capability = META.STRUCTURAL_CAPABILITY[("FUTURES", "DATED_FUTURE")]
    expect("futures: long_capable True", capability["long_capable"] is True)
    expect("futures: short_mechanism NATIVE (futures are natively short-capable)", capability["short_mechanism"] == "NATIVE")
    expect("futures: classify_direction_capability(OPEN_SHORT) is NATIVE",
           META.classify_direction_capability("FUTURES", "DATED_FUTURE", "OPEN_SHORT") == META.NATIVE)


def test_futures_has_no_compatible_venue_today_and_fails_closed_honestly() -> None:
    expect("futures: VENUE_COMPATIBILITY is empty (no venue integrates dated futures in this repo)",
           META.VENUE_COMPATIBILITY[("FUTURES", "DATED_FUTURE")] == frozenset())
    expect_fails("futures: building metadata against any real venue fails closed (none compatible)",
                 META.build_instrument_metadata, instrument_type="FUTURES", contract_class="DATED_FUTURE",
                 symbol="CL", venue="MEXC")
    expect("futures: to_canonical_asset_class returns None, not a fabricated .33 mapping",
           META.to_canonical_asset_class("FUTURES", "DATED_FUTURE") is None)


# ======================================================================= #
# Long capability
# ======================================================================= #

def test_long_capability_is_universally_native() -> None:
    for instrument_type, contract_class in META.STRUCTURAL_CAPABILITY:
        cap = META.classify_direction_capability(instrument_type, contract_class, "OPEN_LONG")
        expect(f"long-capability[{instrument_type}/{contract_class}]: OPEN_LONG is NATIVE", cap == META.NATIVE)
        cap_close = META.classify_direction_capability(instrument_type, contract_class, "CLOSE_LONG")
        expect(f"long-capability[{instrument_type}/{contract_class}]: CLOSE_LONG is NATIVE", cap_close == META.NATIVE)


# ======================================================================= #
# Short capability -- the core of this milestone
# ======================================================================= #

def test_short_capability_native_for_futures_and_perpetual_swap() -> None:
    expect("short-native: CRYPTO/PERPETUAL_SWAP OPEN_SHORT is NATIVE",
           META.classify_direction_capability("CRYPTO", "PERPETUAL_SWAP", "OPEN_SHORT") == META.NATIVE)
    expect("short-native: FUTURES/DATED_FUTURE OPEN_SHORT is NATIVE",
           META.classify_direction_capability("FUTURES", "DATED_FUTURE", "OPEN_SHORT") == META.NATIVE)


def test_short_capability_requires_borrow_for_stock_etf_crypto_spot() -> None:
    for instrument_type, contract_class in (("STOCK", None), ("ETF", None), ("CRYPTO", "SPOT")):
        default = META.classify_direction_capability(instrument_type, contract_class, "OPEN_SHORT")
        expect(f"short-borrow[{instrument_type}]: unspecified shortability classifies BORROW_UNKNOWN, never assumed shortable",
               default == META.BORROW_UNKNOWN)
        confirmed = META.classify_direction_capability(instrument_type, contract_class, "OPEN_SHORT", shortability_status="SHORTABLE")
        expect(f"short-borrow[{instrument_type}]: explicit SHORTABLE classifies BORROW_CONFIRMED",
               confirmed == META.BORROW_CONFIRMED)
        denied = META.classify_direction_capability(instrument_type, contract_class, "OPEN_SHORT", shortability_status="NOT_SHORTABLE")
        expect(f"short-borrow[{instrument_type}]: explicit NOT_SHORTABLE classifies BORROW_DENIED",
               denied == META.BORROW_DENIED)


def test_shortability_never_defaults_to_shortable_anywhere() -> None:
    """Structural guarantee: DEFAULT_SHORTABILITY_STATUS itself is UNKNOWN,
    not SHORTABLE -- checked directly, not just via one code path."""
    expect("shortability-default: module-level default is UNKNOWN", META.DEFAULT_SHORTABILITY_STATUS == "UNKNOWN")
    record = META.build_instrument_metadata(instrument_type="STOCK", contract_class=None, symbol="TSLA", venue="ALPACA")
    expect("shortability-default: an unspecified build defaults to UNKNOWN, not SHORTABLE",
           record["shortability_status"] == "UNKNOWN")


# ======================================================================= #
# Unsupported direction
# ======================================================================= #

def test_unsupported_direction_classifies_not_applicable_never_raises() -> None:
    expect("unsupported-direction: garbage direction is NOT_APPLICABLE",
           META.classify_direction_capability("STOCK", None, "SIDEWAYS") == META.NOT_APPLICABLE)
    expect("unsupported-direction: lowercase direction is NOT_APPLICABLE",
           META.classify_direction_capability("STOCK", None, "open_long") == META.NOT_APPLICABLE)


# ======================================================================= #
# Unsupported asset/venue combination
# ======================================================================= #

def test_unsupported_asset_venue_combination_fails_closed() -> None:
    expect_fails("unsupported-combo: MEXC + STOCK", META.build_instrument_metadata,
                 instrument_type="STOCK", contract_class=None, symbol="AAPL", venue="MEXC")
    expect_fails("unsupported-combo: ALPACA + CRYPTO/PERPETUAL_SWAP", META.build_instrument_metadata,
                 instrument_type="CRYPTO", contract_class="PERPETUAL_SWAP", symbol="BTC/USDT:USDT", venue="ALPACA")
    expect_fails("unsupported-combo: invalid instrument_type", META.build_instrument_metadata,
                 instrument_type="OPTIONS", contract_class=None, symbol="AAPL", venue="ALPACA")
    expect_fails("unsupported-combo: STOCK with a contract_class set (must be None)", META.build_instrument_metadata,
                 instrument_type="STOCK", contract_class="SPOT", symbol="AAPL", venue="ALPACA")
    expect("unsupported-combo: classify_direction_capability on an invalid combo is NOT_APPLICABLE, never raises",
           META.classify_direction_capability("NONSENSE", "NOWHERE", "OPEN_LONG") == META.NOT_APPLICABLE)


# ======================================================================= #
# Shortability shape validation
# ======================================================================= #

def test_shortability_status_shape_validated() -> None:
    expect_fails("shortability-shape: invalid shortability_status string rejected", META.build_instrument_metadata,
                 instrument_type="STOCK", contract_class=None, symbol="AAPL", venue="ALPACA",
                 shortability_status="PROBABLY")


# ======================================================================= #
# Malformed metadata / fail-closed
# ======================================================================= #

def test_malformed_metadata_fails_closed() -> None:
    expect_fails("malformed: missing/None instrument_type", META.build_instrument_metadata,
                 instrument_type=None, contract_class=None, symbol="AAPL", venue="ALPACA")
    expect_fails("malformed: missing/None venue", META.build_instrument_metadata,
                 instrument_type="STOCK", contract_class=None, symbol="AAPL", venue=None)
    expect_fails("malformed: non-string symbol", META.build_instrument_metadata,
                 instrument_type="STOCK", contract_class=None, symbol=12345, venue="ALPACA")
    expect_fails("malformed: negative tick_size", META.build_instrument_metadata,
                 instrument_type="STOCK", contract_class=None, symbol="AAPL", venue="ALPACA", tick_size="-0.01")
    expect_fails("malformed: zero min_quantity", META.build_instrument_metadata,
                 instrument_type="STOCK", contract_class=None, symbol="AAPL", venue="ALPACA", min_quantity="0")
    expect_fails("malformed: non-numeric tick_size", META.build_instrument_metadata,
                 instrument_type="STOCK", contract_class=None, symbol="AAPL", venue="ALPACA", tick_size="not-a-number")


def test_optional_fields_absent_are_represented_honestly_as_none() -> None:
    record = META.build_instrument_metadata(instrument_type="ETF", contract_class=None, symbol="QQQ", venue="ALPACA")
    expect("optional-fields: tick_size None when not supplied (not fabricated)", record["tick_size"] is None)
    expect("optional-fields: min_quantity None when not supplied (not fabricated)", record["min_quantity"] is None)


# ======================================================================= #
# Deterministic fingerprinting
# ======================================================================= #

def test_deterministic_fingerprinting() -> None:
    kwargs = dict(instrument_type="STOCK", contract_class=None, symbol="MSFT", venue="ALPACA", shortability_status="SHORTABLE")
    record1 = META.build_instrument_metadata(**kwargs)
    record2 = META.build_instrument_metadata(**kwargs)
    expect("fingerprint: two builds of identical content are fingerprint-equal",
           record1["metadata_fingerprint"] == record2["metadata_fingerprint"])

    different = META.build_instrument_metadata(**dict(kwargs, symbol="NVDA"))
    expect("fingerprint: a materially different record has a different fingerprint",
           different["metadata_fingerprint"] != record1["metadata_fingerprint"])

    ok, errors = META.verify_instrument_metadata(record1)
    expect("fingerprint: self-verification succeeds on an untampered record", ok and not errors)

    tampered = dict(record1)
    tampered["shortability_status"] = "NOT_SHORTABLE"
    ok2, errors2 = META.verify_instrument_metadata(tampered)
    expect("fingerprint: self-verification catches tampering", not ok2 and "METADATA_FINGERPRINT_MISMATCH" in errors2)


# ======================================================================= #
# Compatibility with .33
# ======================================================================= #

def test_compatible_with_canonical_execution_specification_33() -> None:
    for asset_class in CANON.ASSET_CLASSES:
        combo = META.from_canonical_asset_class(asset_class)
        if combo is None:
            # Only true today for nothing -- .33 has exactly 4 asset
            # classes and all 4 are represented in this module's bridge.
            raise AssertionError(f".33 asset_class {asset_class!r} has no .34 mapping")
        instrument_type, contract_class = combo
        expect(f".33-compat[{asset_class}]: round-trips back to the same .33 asset_class",
               META.to_canonical_asset_class(instrument_type, contract_class) == asset_class)

    # Symbol shapes must agree: .34's patterns for the four .33-representable
    # combinations must be identical to .33's own SYMBOL_PATTERNS, since
    # both were built from the same two real adapter regexes.
    for asset_class, symbol_examples in (
        ("CRYPTO_FUTURES", ["BTC/USDT:USDT", "ETH/USDT:USDT"]),
        ("CRYPTO_SPOT", ["BTC/USD", "ETH/USD"]),
        ("STOCK", ["AAPL", "MSFT"]),
        ("ETF", ["SPY", "QQQ"]),
    ):
        instrument_type, contract_class = META.from_canonical_asset_class(asset_class)
        meta_pattern = META.SYMBOL_PATTERNS[(instrument_type, contract_class)]
        canon_pattern = CANON.SYMBOL_PATTERNS[asset_class]
        for symbol in symbol_examples:
            expect(f".33-compat[{asset_class}]: symbol {symbol!r} agrees between .34 and .33 patterns",
                   bool(meta_pattern.match(symbol)) == bool(canon_pattern.match(symbol)) is True)


# ======================================================================= #
# Future-support verification: Stocks/ETFs/Crypto/Futures LONG+SHORT
# representability, per Martin's explicit closing checklist
# ======================================================================= #

def test_stocks_etfs_crypto_futures_long_and_short_are_all_representable() -> None:
    checks = [
        ("STOCK", None, "Stocks"),
        ("ETF", None, "ETFs"),
        ("CRYPTO", "SPOT", "Crypto (spot)"),
        ("CRYPTO", "PERPETUAL_SWAP", "Crypto (perpetual/futures)"),
        ("FUTURES", "DATED_FUTURE", "Futures"),
    ]
    for instrument_type, contract_class, label in checks:
        long_cap = META.classify_direction_capability(instrument_type, contract_class, "OPEN_LONG")
        short_cap = META.classify_direction_capability(instrument_type, contract_class, "OPEN_SHORT")
        expect(f"future-support[{label}]: LONG is representable (NATIVE)", long_cap == META.NATIVE)
        expect(f"future-support[{label}]: SHORT is representable (NATIVE or a defined borrow state, never NOT_APPLICABLE)",
               short_cap in (META.NATIVE, META.BORROW_CONFIRMED, META.BORROW_DENIED, META.BORROW_UNKNOWN))


if __name__ == "__main__":
    tests = [obj for name, obj in list(globals().items()) if name.startswith("test_")]
    for t in tests:
        t()
    print(f"AURA v0.5.3.34 UNIT TEST CONTRACT: ALL {len(tests)} PASS")
