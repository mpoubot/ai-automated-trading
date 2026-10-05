#!/usr/bin/env python3
"""Unit tests for AURA v0.5.3.73 (Options Instrument & Metadata
Foundation, Phase O1).

Covers: OCC symbol build/parse round-trip (various roots, dates,
strikes, rights), option-contract metadata build + fail-closed
validation + fingerprint verification, option-structure build (1-4 legs)
+ fail-closed validation (leg count, side, ratio, cross-underlying,
duplicate legs) + order_class derivation (SIMPLE vs MLEG) + fingerprint
verification including per-leg contract re-verification.

No real credentials, no network call, no order submission anywhere in
this file.
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    mod = importlib.util.module_from_spec(spec)
    # Must register in sys.modules BEFORE exec: this module defines a
    # `@dataclass` under `from __future__ import annotations`, and
    # dataclasses resolves those string annotations via
    # sys.modules[cls.__module__] -- without this, exec_module raises
    # AttributeError on the class definition itself (CPython 3.13).
    # No other module in this repo uses @dataclass, so prior test files
    # (e.g. .334's) never needed this; harmless here since the name is
    # unique to this test run and isn't relied on for isolation.
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    except Exception:
        sys.modules.pop(name, None)
        raise
    return mod


META = _load("aura_v05373_options_instrument_metadata", "aura_v05373_options_instrument_metadata.py")


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
# OCC symbol -- build/parse round-trip
# ======================================================================= #

def test_occ_round_trip_simple_call() -> None:
    occ = META.build_occ_symbol("AAPL", date(2024, 1, 19), "CALL", Decimal("150"))
    expect("occ: exact expected string", occ == "AAPL240119C00150000")
    parsed = META.parse_occ_symbol(occ)
    expect("occ: round-trip underlying", parsed["underlying_symbol"] == "AAPL")
    expect("occ: round-trip expiry", parsed["expiry"] == date(2024, 1, 19))
    expect("occ: round-trip right", parsed["right"] == "CALL")
    expect("occ: round-trip strike", parsed["strike"] == Decimal("150"))


def test_occ_round_trip_put_with_cents_strike() -> None:
    occ = META.build_occ_symbol("TSLA", date(2026, 12, 18), "PUT", Decimal("182.50"))
    expect("occ: cents strike string", occ == "TSLA261218P00182500")
    parsed = META.parse_occ_symbol(occ)
    expect("occ: cents strike round-trip", parsed["strike"] == Decimal("182.50"))
    expect("occ: put right round-trip", parsed["right"] == "PUT")


def test_occ_round_trip_one_letter_root() -> None:
    occ = META.build_occ_symbol("F", date(2025, 6, 20), "CALL", Decimal("12"))
    expect("occ: 1-letter root in symbol", occ.startswith("F25"))
    parsed = META.parse_occ_symbol(occ)
    expect("occ: 1-letter root round-trip", parsed["underlying_symbol"] == "F")


def test_occ_round_trip_six_letter_root() -> None:
    occ = META.build_occ_symbol("ABCDEF", date(2025, 3, 21), "PUT", Decimal("5"))
    parsed = META.parse_occ_symbol(occ)
    expect("occ: 6-letter root round-trip", parsed["underlying_symbol"] == "ABCDEF")


def test_occ_round_trip_high_strike() -> None:
    occ = META.build_occ_symbol("NVDA", date(2027, 1, 15), "CALL", Decimal("999.75"))
    parsed = META.parse_occ_symbol(occ)
    expect("occ: high strike round-trip", parsed["strike"] == Decimal("999.75"))


def test_occ_parse_rejects_malformed_symbol() -> None:
    expect_fails("occ: too-short string", META.parse_occ_symbol, "AAPL")
    expect_fails("occ: bad right char", META.parse_occ_symbol, "AAPL240119X00150000")
    expect_fails("occ: non-digit strike", META.parse_occ_symbol, "AAPL240119CABCDEFGH")
    expect_fails("occ: lowercase root", META.parse_occ_symbol, "aapl240119C00150000")
    expect_fails("occ: invalid calendar date", META.parse_occ_symbol, "AAPL240230C00150000")
    expect_fails("occ: not a string", META.parse_occ_symbol, None)


def test_occ_build_rejects_invalid_inputs() -> None:
    expect_fails("occ build: bad underlying (digits)", META.build_occ_symbol, "AAP1", date(2024, 1, 19), "CALL", Decimal("150"))
    expect_fails("occ build: bad underlying (7 letters)", META.build_occ_symbol, "ABCDEFG", date(2024, 1, 19), "CALL", Decimal("150"))
    expect_fails("occ build: bad right", META.build_occ_symbol, "AAPL", date(2024, 1, 19), "CALLX", Decimal("150"))
    expect_fails("occ build: zero strike", META.build_occ_symbol, "AAPL", date(2024, 1, 19), "CALL", Decimal("0"))
    expect_fails("occ build: negative strike", META.build_occ_symbol, "AAPL", date(2024, 1, 19), "CALL", Decimal("-5"))
    expect_fails("occ build: strike finer than $0.001", META.build_occ_symbol, "AAPL", date(2024, 1, 19), "CALL", Decimal("150.0001"))
    expect_fails("occ build: expiry not a date", META.build_occ_symbol, "AAPL", "2024-01-19", "CALL", Decimal("150"))
    expect_fails("occ build: non-numeric strike", META.build_occ_symbol, "AAPL", date(2024, 1, 19), "CALL", "not-a-number")


# ======================================================================= #
# Option contract metadata
# ======================================================================= #

def test_contract_metadata_builds_with_default_multiplier() -> None:
    record = META.build_option_contract_metadata(
        underlying_symbol="AAPL", strike=Decimal("150"), expiry=date(2024, 1, 19), right="CALL",
    )
    expect("contract: multiplier defaults to 100", record["multiplier"] == 100)
    expect("contract: occ_symbol matches build_occ_symbol", record["occ_symbol"] == "AAPL240119C00150000")
    expect("contract: strike stored as string", record["strike"] == "150")
    expect("contract: expiry stored as ISO date", record["expiry"] == "2024-01-19")
    expect("contract: right stored verbatim", record["right"] == "CALL")
    expect("contract: has a fingerprint", isinstance(record["contract_fingerprint"], str) and len(record["contract_fingerprint"]) == 64)


def test_contract_metadata_accepts_explicit_multiplier() -> None:
    record = META.build_option_contract_metadata(
        underlying_symbol="SPX", strike=Decimal("4500"), expiry=date(2025, 6, 20), right="PUT", multiplier=10,
    )
    expect("contract: explicit multiplier honored", record["multiplier"] == 10)


def test_contract_metadata_rejects_invalid_fields() -> None:
    expect_fails("contract: invalid right", META.build_option_contract_metadata,
                 underlying_symbol="AAPL", strike=Decimal("150"), expiry=date(2024, 1, 19), right="CALLX")
    expect_fails("contract: invalid multiplier zero", META.build_option_contract_metadata,
                 underlying_symbol="AAPL", strike=Decimal("150"), expiry=date(2024, 1, 19), right="CALL", multiplier=0)
    expect_fails("contract: invalid multiplier float", META.build_option_contract_metadata,
                 underlying_symbol="AAPL", strike=Decimal("150"), expiry=date(2024, 1, 19), right="CALL", multiplier=100.5)
    expect_fails("contract: invalid multiplier bool", META.build_option_contract_metadata,
                 underlying_symbol="AAPL", strike=Decimal("150"), expiry=date(2024, 1, 19), right="CALL", multiplier=True)
    expect_fails("contract: invalid underlying", META.build_option_contract_metadata,
                 underlying_symbol="aapl", strike=Decimal("150"), expiry=date(2024, 1, 19), right="CALL")
    expect_fails("contract: invalid strike", META.build_option_contract_metadata,
                 underlying_symbol="AAPL", strike=Decimal("-1"), expiry=date(2024, 1, 19), right="CALL")


def test_contract_metadata_fingerprint_changes_with_content() -> None:
    a = META.build_option_contract_metadata(underlying_symbol="AAPL", strike=Decimal("150"), expiry=date(2024, 1, 19), right="CALL")
    b = META.build_option_contract_metadata(underlying_symbol="AAPL", strike=Decimal("155"), expiry=date(2024, 1, 19), right="CALL")
    expect("contract: different strike -> different fingerprint", a["contract_fingerprint"] != b["contract_fingerprint"])


def test_contract_metadata_verify_passes_on_untouched_record() -> None:
    record = META.build_option_contract_metadata(underlying_symbol="AAPL", strike=Decimal("150"), expiry=date(2024, 1, 19), right="CALL")
    ok, problems = META.verify_option_contract_metadata(record)
    expect("contract verify: untouched record passes", ok and problems == [])


def test_contract_metadata_verify_fails_on_tampered_fingerprint() -> None:
    record = META.build_option_contract_metadata(underlying_symbol="AAPL", strike=Decimal("150"), expiry=date(2024, 1, 19), right="CALL")
    record["contract_fingerprint"] = "0" * 64
    ok, problems = META.verify_option_contract_metadata(record)
    expect("contract verify: tampered fingerprint fails", not ok and "CONTRACT_FINGERPRINT_MISMATCH" in problems)


def test_contract_metadata_verify_fails_on_inconsistent_occ_symbol() -> None:
    record = META.build_option_contract_metadata(underlying_symbol="AAPL", strike=Decimal("150"), expiry=date(2024, 1, 19), right="CALL")
    # Tamper the OCC symbol directly without recomputing the fingerprint --
    # simulates a hand-constructed record, not one built by the builder.
    record["occ_symbol"] = "AAPL240119C00999000"
    record["contract_fingerprint"] = META.canonical_option_contract_fingerprint(record)
    ok, problems = META.verify_option_contract_metadata(record)
    expect("contract verify: inconsistent occ_symbol caught", not ok and "OCC_SYMBOL_INCONSISTENT_WITH_FIELDS" in problems)


def test_contract_metadata_verify_rejects_non_dict() -> None:
    ok, problems = META.verify_option_contract_metadata("not-a-dict")
    expect("contract verify: non-dict rejected", not ok and problems == ["NOT_A_DICT"])


# ======================================================================= #
# Option structure -- single-leg
# ======================================================================= #

def test_structure_single_leg_long_call_classifies_as_simple() -> None:
    leg = META.OptionLegInput(underlying_symbol="AAPL", strike=Decimal("150"), expiry=date(2024, 1, 19), right="CALL", side="BUY")
    record = META.build_option_structure([leg])
    expect("structure: single leg order_class SIMPLE", record["order_class"] == "SIMPLE")
    expect("structure: single leg leg_count 1", record["leg_count"] == 1)
    expect("structure: single leg underlying", record["underlying_symbol"] == "AAPL")
    expect("structure: single leg side preserved", record["legs"][0]["side"] == "BUY")
    expect("structure: single leg default ratio 1", record["legs"][0]["ratio"] == 1)


def test_structure_single_leg_covered_call_short_leg() -> None:
    """A covered call is, from this module's perspective, just a SELL
    CALL single-leg structure -- the "covered" part (an existing stock
    position collateralizing it) is explicitly NOT this module's concern,
    per the module docstring."""
    leg = META.OptionLegInput(underlying_symbol="PLTR", strike=Decimal("195"), expiry=date(2026, 9, 11), right="CALL", side="SELL")
    record = META.build_option_structure([leg])
    expect("covered call: order_class SIMPLE", record["order_class"] == "SIMPLE")
    expect("covered call: side SELL", record["legs"][0]["side"] == "SELL")


# ======================================================================= #
# Option structure -- multi-leg
# ======================================================================= #

def test_structure_two_leg_vertical_debit_spread_classifies_as_mleg() -> None:
    long_leg = META.OptionLegInput(underlying_symbol="PLTR", strike=Decimal("185"), expiry=date(2026, 9, 11), right="CALL", side="BUY")
    short_leg = META.OptionLegInput(underlying_symbol="PLTR", strike=Decimal("190"), expiry=date(2026, 9, 11), right="CALL", side="SELL")
    record = META.build_option_structure([long_leg, short_leg])
    expect("vertical: order_class MLEG", record["order_class"] == "MLEG")
    expect("vertical: leg_count 2", record["leg_count"] == 2)
    expect("vertical: leg order preserved (long first)", record["legs"][0]["side"] == "BUY" and record["legs"][1]["side"] == "SELL")


def test_structure_four_leg_iron_condor_classifies_as_mleg() -> None:
    legs = [
        META.OptionLegInput(underlying_symbol="SPY", strike=Decimal("400"), expiry=date(2026, 10, 16), right="PUT", side="BUY"),
        META.OptionLegInput(underlying_symbol="SPY", strike=Decimal("410"), expiry=date(2026, 10, 16), right="PUT", side="SELL"),
        META.OptionLegInput(underlying_symbol="SPY", strike=Decimal("450"), expiry=date(2026, 10, 16), right="CALL", side="SELL"),
        META.OptionLegInput(underlying_symbol="SPY", strike=Decimal("460"), expiry=date(2026, 10, 16), right="CALL", side="BUY"),
    ]
    record = META.build_option_structure(legs)
    expect("condor: order_class MLEG", record["order_class"] == "MLEG")
    expect("condor: leg_count 4", record["leg_count"] == 4)


def test_structure_straddle_same_strike_opposite_rights_allowed() -> None:
    """Same strike/expiry, opposite rights, same side -- a legitimate
    straddle, not a duplicate leg (duplicate means identical in every
    field, including right)."""
    legs = [
        META.OptionLegInput(underlying_symbol="TSLA", strike=Decimal("250"), expiry=date(2026, 11, 20), right="CALL", side="BUY"),
        META.OptionLegInput(underlying_symbol="TSLA", strike=Decimal("250"), expiry=date(2026, 11, 20), right="PUT", side="BUY"),
    ]
    record = META.build_option_structure(legs)
    expect("straddle: builds without error", record["leg_count"] == 2)


# ======================================================================= #
# Option structure -- fail-closed validation
# ======================================================================= #

def test_structure_rejects_zero_legs() -> None:
    expect_fails("structure: zero legs rejected", META.build_option_structure, [])


def test_structure_rejects_five_legs() -> None:
    legs = [
        META.OptionLegInput(underlying_symbol="SPY", strike=Decimal(str(400 + i)), expiry=date(2026, 10, 16), right="CALL", side="BUY")
        for i in range(5)
    ]
    expect_fails("structure: five legs rejected", META.build_option_structure, legs)


def test_structure_rejects_invalid_side() -> None:
    leg = META.OptionLegInput(underlying_symbol="AAPL", strike=Decimal("150"), expiry=date(2024, 1, 19), right="CALL", side="SHORT")
    expect_fails("structure: invalid side rejected", META.build_option_structure, [leg])


def test_structure_rejects_invalid_ratio() -> None:
    leg_zero = META.OptionLegInput(underlying_symbol="AAPL", strike=Decimal("150"), expiry=date(2024, 1, 19), right="CALL", side="BUY", ratio=0)
    expect_fails("structure: zero ratio rejected", META.build_option_structure, [leg_zero])
    leg_float = META.OptionLegInput(underlying_symbol="AAPL", strike=Decimal("150"), expiry=date(2024, 1, 19), right="CALL", side="BUY", ratio=1.5)
    expect_fails("structure: non-int ratio rejected", META.build_option_structure, [leg_float])


def test_structure_rejects_cross_underlying_legs() -> None:
    legs = [
        META.OptionLegInput(underlying_symbol="AAPL", strike=Decimal("150"), expiry=date(2024, 1, 19), right="CALL", side="BUY"),
        META.OptionLegInput(underlying_symbol="MSFT", strike=Decimal("300"), expiry=date(2024, 1, 19), right="CALL", side="SELL"),
    ]
    expect_fails("structure: cross-underlying legs rejected", META.build_option_structure, legs)


def test_structure_rejects_duplicate_identical_legs() -> None:
    legs = [
        META.OptionLegInput(underlying_symbol="AAPL", strike=Decimal("150"), expiry=date(2024, 1, 19), right="CALL", side="BUY"),
        META.OptionLegInput(underlying_symbol="AAPL", strike=Decimal("150"), expiry=date(2024, 1, 19), right="CALL", side="BUY"),
    ]
    expect_fails("structure: duplicate identical legs rejected", META.build_option_structure, legs)


def test_structure_rejects_a_leg_with_invalid_contract_fields() -> None:
    bad_leg = META.OptionLegInput(underlying_symbol="aapl", strike=Decimal("150"), expiry=date(2024, 1, 19), right="CALL", side="BUY")
    expect_fails("structure: invalid leg contract field rejected", META.build_option_structure, [bad_leg])


def test_structure_rejects_non_leg_input_type() -> None:
    expect_fails("structure: plain dict instead of OptionLegInput rejected", META.build_option_structure,
                 [{"underlying_symbol": "AAPL", "strike": "150", "expiry": date(2024, 1, 19), "right": "CALL", "side": "BUY"}])


# ======================================================================= #
# Option structure -- fingerprint / verification
# ======================================================================= #

def test_structure_fingerprint_changes_with_leg_content() -> None:
    legs_a = [META.OptionLegInput(underlying_symbol="AAPL", strike=Decimal("150"), expiry=date(2024, 1, 19), right="CALL", side="BUY")]
    legs_b = [META.OptionLegInput(underlying_symbol="AAPL", strike=Decimal("155"), expiry=date(2024, 1, 19), right="CALL", side="BUY")]
    a = META.build_option_structure(legs_a)
    b = META.build_option_structure(legs_b)
    expect("structure: different leg -> different fingerprint", a["structure_fingerprint"] != b["structure_fingerprint"])


def test_structure_verify_passes_on_untouched_record() -> None:
    legs = [
        META.OptionLegInput(underlying_symbol="PLTR", strike=Decimal("185"), expiry=date(2026, 9, 11), right="CALL", side="BUY"),
        META.OptionLegInput(underlying_symbol="PLTR", strike=Decimal("190"), expiry=date(2026, 9, 11), right="CALL", side="SELL"),
    ]
    record = META.build_option_structure(legs)
    ok, problems = META.verify_option_structure(record)
    expect("structure verify: untouched record passes", ok and problems == [])


def test_structure_verify_fails_on_tampered_structure_fingerprint() -> None:
    leg = META.OptionLegInput(underlying_symbol="AAPL", strike=Decimal("150"), expiry=date(2024, 1, 19), right="CALL", side="BUY")
    record = META.build_option_structure([leg])
    record["structure_fingerprint"] = "0" * 64
    ok, problems = META.verify_option_structure(record)
    expect("structure verify: tampered fingerprint fails", not ok and "STRUCTURE_FINGERPRINT_MISMATCH" in problems)


def test_structure_verify_fails_when_a_leg_contract_is_tampered() -> None:
    """Proves verify_option_structure() actually re-checks each leg's own
    contract, not just the structure-level fingerprint -- a structure
    whose outer fingerprint still matches content, but whose embedded
    leg contract was tampered, must still fail."""
    leg = META.OptionLegInput(underlying_symbol="AAPL", strike=Decimal("150"), expiry=date(2024, 1, 19), right="CALL", side="BUY")
    record = META.build_option_structure([leg])
    record["legs"][0]["contract"]["contract_fingerprint"] = "0" * 64
    # Recompute the outer structure fingerprint so ONLY the inner leg
    # contract fingerprint is inconsistent -- isolating what's being tested.
    record["structure_fingerprint"] = META.canonical_option_structure_fingerprint(record)
    ok, problems = META.verify_option_structure(record)
    expect("structure verify: tampered leg contract caught", not ok)
    expect("structure verify: problem attributes to the right leg", any("LEG_0_" in p for p in problems))


def test_structure_verify_rejects_non_dict() -> None:
    ok, problems = META.verify_option_structure("not-a-dict")
    expect("structure verify: non-dict rejected", not ok and problems == ["NOT_A_DICT"])


def test_structure_verify_rejects_missing_legs() -> None:
    leg = META.OptionLegInput(underlying_symbol="AAPL", strike=Decimal("150"), expiry=date(2024, 1, 19), right="CALL", side="BUY")
    record = META.build_option_structure([leg])
    record["legs"] = []
    record["structure_fingerprint"] = META.canonical_option_structure_fingerprint(record)
    ok, problems = META.verify_option_structure(record)
    expect("structure verify: empty legs list caught", not ok and "NO_LEGS" in problems)


# ======================================================================= #
# classify_order_class -- pure, never raises
# ======================================================================= #

def test_classify_order_class_boundaries() -> None:
    expect("classify: 1 leg SIMPLE", META.classify_order_class(1) == "SIMPLE")
    expect("classify: 2 legs MLEG", META.classify_order_class(2) == "MLEG")
    expect("classify: 4 legs MLEG", META.classify_order_class(4) == "MLEG")
    expect("classify: 0 legs invalid", META.classify_order_class(0) == "INVALID_LEG_COUNT")
    expect("classify: 5 legs invalid", META.classify_order_class(5) == "INVALID_LEG_COUNT")
    expect("classify: never raises on nonsense input", META.classify_order_class(-1) == "INVALID_LEG_COUNT")
