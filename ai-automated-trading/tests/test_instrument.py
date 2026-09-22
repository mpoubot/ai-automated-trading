"""
tests/test_instrument.py

Canonical instrument handling: symbol mapping (ccxt-style <-> native MEXC),
idempotency, canonical_id stability, and rejection of unsupported venues.
"""
from core.instrument import Instrument, to_native_mexc_symbol


def test_to_native_mexc_symbol_from_ccxt_style():
    assert to_native_mexc_symbol("BTC/USDT:USDT") == "BTC_USDT"
    assert to_native_mexc_symbol("SUI/USDT:USDT") == "SUI_USDT"


def test_to_native_mexc_symbol_idempotent():
    assert to_native_mexc_symbol("BTC_USDT") == "BTC_USDT"
    assert to_native_mexc_symbol(to_native_mexc_symbol("BTC/USDT:USDT")) == "BTC_USDT"


def test_to_native_mexc_symbol_rejects_malformed():
    # A string containing "/" (so it's treated as ccxt-style, not an
    # already-native passthrough) but missing a base or quote leg.
    import pytest
    with pytest.raises(ValueError):
        to_native_mexc_symbol("/USDT:USDT")
    with pytest.raises(ValueError):
        to_native_mexc_symbol("BTC/:USDT")


def test_to_native_mexc_symbol_passthrough_does_not_validate_native_shape():
    # Documented behavior: a string with no "/" is treated as already-native
    # and passed through unchanged (matches mexc_native.py's convention) --
    # this function does not additionally validate native-format shape.
    assert to_native_mexc_symbol("NOTASYMBOL") == "NOTASYMBOL"


def test_instrument_mexc_swap_constructor():
    inst = Instrument.mexc_swap("btc", "usdt")
    assert inst.venue == "MEXC"
    assert inst.base == "BTC"
    assert inst.quote == "USDT"
    assert inst.market_type == "swap"
    assert inst.native_symbol == "BTC_USDT"


def test_instrument_from_native_mexc():
    inst = Instrument.from_native_mexc("ETH_USDT")
    assert inst == Instrument.mexc_swap("ETH", "USDT")
    assert inst.native_symbol == "ETH_USDT"


def test_canonical_id_is_stable_and_venue_qualified():
    inst = Instrument.mexc_swap("BTC")
    assert inst.canonical_id == "MEXC:BTC_USDT:swap"
    # Same logical instrument built two different ways -> identical canonical_id.
    assert Instrument.from_native_mexc("BTC_USDT").canonical_id == inst.canonical_id


def test_instrument_rejects_unsupported_venue():
    import pytest
    with pytest.raises(ValueError):
        Instrument(venue="ALPACA", base="AAPL", quote="USD", market_type="spot")


def test_instrument_is_frozen_and_hashable():
    inst = Instrument.mexc_swap("BTC")
    assert hash(inst) is not None
    # frozen dataclass -> attempting to mutate raises
    import pytest
    import dataclasses
    with pytest.raises(dataclasses.FrozenInstanceError):
        inst.base = "ETH"
