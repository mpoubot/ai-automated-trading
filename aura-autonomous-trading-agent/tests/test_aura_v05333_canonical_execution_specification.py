#!/usr/bin/env python3
"""Unit + compatibility tests for AURA v0.5.3.33 (Canonical Execution
Specification / Intent Producer).

Covers, in order, every item from Martin's explicit test list for this
milestone:
  - valid LONG / SHORT crypto-futures spec
  - valid LONG / SHORT stock spec (schema-level; SHORT is representable,
    translation to a venue is NOT_YET_IMPLEMENTED -- see finding D)
  - valid LONG / SHORT ETF spec (same)
  - invalid/missing asset class
  - invalid/missing venue
  - invalid direction
  - incompatible venue/asset-class combination
  - incompatible futures-only fields on stocks/ETFs
  - expiry handling
  - deterministic fingerprinting
  - provenance preservation
  - no live submission
  - malformed specifications fail closed
  - regression: the existing .27 (MEXC) and .22 (Alpaca) adapters' own
    validate_spec() accept this module's translated output unmodified --
    proof of wire-format compatibility, not a reimplementation.

No real credentials, no network call, no order submission anywhere in this
file.
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


CANON = _load("aura_v05333_canonical_execution_specification", "aura_v05333_canonical_execution_specification.py")
MEXC_ADAPTER = _load("aura_v05327_mexc_execution_adapter", "aura_v05327_mexc_execution_adapter.py")
ALPACA_ADAPTER = _load("aura_v05322_alpaca_paper_execution_adapter", "aura_v05322_alpaca_paper_execution_adapter.py")


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


BASE_MEXC_LONG = dict(
    asset_class="CRYPTO_FUTURES", venue="MEXC", direction="OPEN_LONG",
    symbol="BTC/USDT:USDT", quantity="0.01", decision_id="dec-001",
    strategy_id="AURA_SIGNAL", strategy_version="v0.5.3.13",
    signal_timestamp="2026-09-10T00:00:00+00:00", leverage=5,
)

BASE_ALPACA_SPOT_LONG = dict(
    asset_class="CRYPTO_SPOT", venue="ALPACA", direction="OPEN_LONG",
    symbol="BTC/USD", quantity="0.01", decision_id="dec-100",
    strategy_id="AURA_SIGNAL", strategy_version="v0.5.3.19",
    signal_timestamp="2026-09-10T00:00:00+00:00",
)

BASE_STOCK_LONG = dict(
    asset_class="STOCK", venue="ALPACA", direction="OPEN_LONG",
    symbol="AAPL", quantity="10", decision_id="dec-200",
    strategy_id="AURA_SIGNAL", strategy_version="v0.1",
    signal_timestamp="2026-09-10T00:00:00+00:00",
)

BASE_ETF_LONG = dict(
    asset_class="ETF", venue="ALPACA", direction="OPEN_LONG",
    symbol="SPY", quantity="5", decision_id="dec-300",
    strategy_id="AURA_SIGNAL", strategy_version="v0.1",
    signal_timestamp="2026-09-10T00:00:00+00:00",
)


# ======================================================================= #
# Valid specs -- one per Martin's explicit list
# ======================================================================= #

def test_valid_long_crypto_futures_spec() -> None:
    spec = CANON.build_canonical_execution_specification(**BASE_MEXC_LONG)
    expect("long-futures: direction OPEN_LONG", spec["direction"] == "OPEN_LONG")
    expect("long-futures: reduce_only False on entry", spec["reduce_only"] is False)
    expect("long-futures: leverage carried", spec["leverage"] == 5)
    expect("long-futures: classified SUPPORTED_BY_ADAPTER", spec["support_classification"] == "SUPPORTED_BY_ADAPTER")


def test_valid_short_crypto_futures_spec() -> None:
    open_short = dict(BASE_MEXC_LONG, direction="OPEN_SHORT", decision_id="dec-002")
    spec = CANON.build_canonical_execution_specification(**open_short)
    expect("short-futures: direction OPEN_SHORT", spec["direction"] == "OPEN_SHORT")
    expect("short-futures: reduce_only False on entry", spec["reduce_only"] is False)
    expect("short-futures: classified SUPPORTED_BY_ADAPTER", spec["support_classification"] == "SUPPORTED_BY_ADAPTER")

    close_short = dict(BASE_MEXC_LONG, direction="CLOSE_SHORT", decision_id="dec-003", leverage=None)
    spec2 = CANON.build_canonical_execution_specification(**close_short)
    expect("close-short: reduce_only True on close", spec2["reduce_only"] is True)
    expect("close-short: leverage is None on close (not required)", spec2["leverage"] is None)


def test_valid_long_stock_spec() -> None:
    spec = CANON.build_canonical_execution_specification(**BASE_STOCK_LONG)
    expect("long-stock: schema accepts asset_class STOCK", spec["asset_class"] == "STOCK")
    expect("long-stock: no leverage/reduce_only imposed", spec["leverage"] is None and spec["reduce_only"] is None)
    expect("long-stock: classified NOT_YET_IMPLEMENTED (no adapter accepts equity tickers)",
           spec["support_classification"] == "NOT_YET_IMPLEMENTED")


def test_valid_short_stock_spec_where_supported_by_the_existing_model() -> None:
    spec = CANON.build_canonical_execution_specification(**dict(BASE_STOCK_LONG, direction="OPEN_SHORT", decision_id="dec-201"))
    expect("short-stock: schema accepts OPEN_SHORT direction for STOCK", spec["direction"] == "OPEN_SHORT")
    expect("short-stock: classified NOT_YET_IMPLEMENTED, honestly -- no equity adapter exists at all",
           spec["support_classification"] == "NOT_YET_IMPLEMENTED")


def test_valid_long_etf_spec() -> None:
    spec = CANON.build_canonical_execution_specification(**BASE_ETF_LONG)
    expect("long-etf: schema accepts asset_class ETF", spec["asset_class"] == "ETF")
    expect("long-etf: classified NOT_YET_IMPLEMENTED (no adapter accepts ETF tickers)",
           spec["support_classification"] == "NOT_YET_IMPLEMENTED")


def test_valid_short_etf_spec_where_supported_by_the_existing_model() -> None:
    spec = CANON.build_canonical_execution_specification(**dict(BASE_ETF_LONG, direction="OPEN_SHORT", decision_id="dec-301"))
    expect("short-etf: schema accepts OPEN_SHORT direction for ETF", spec["direction"] == "OPEN_SHORT")
    expect("short-etf: classified NOT_YET_IMPLEMENTED", spec["support_classification"] == "NOT_YET_IMPLEMENTED")


# ======================================================================= #
# Invalid / missing asset class, venue, direction
# ======================================================================= #

def test_invalid_missing_asset_class() -> None:
    expect_fails("invalid-asset-class: unknown string",
                 CANON.build_canonical_execution_specification, **dict(BASE_MEXC_LONG, asset_class="OPTIONS"))
    expect_fails("missing-asset-class: None",
                 CANON.build_canonical_execution_specification, **dict(BASE_MEXC_LONG, asset_class=None))


def test_invalid_missing_venue() -> None:
    expect_fails("invalid-venue: unknown string",
                 CANON.build_canonical_execution_specification, **dict(BASE_MEXC_LONG, venue="BINANCE"))
    expect_fails("missing-venue: None",
                 CANON.build_canonical_execution_specification, **dict(BASE_MEXC_LONG, venue=None))


def test_invalid_direction() -> None:
    expect_fails("invalid-direction: unknown string",
                 CANON.build_canonical_execution_specification, **dict(BASE_MEXC_LONG, direction="SIDEWAYS"))
    expect_fails("invalid-direction: lowercase not accepted",
                 CANON.build_canonical_execution_specification, **dict(BASE_MEXC_LONG, direction="open_long"))


# ======================================================================= #
# Incompatible venue/asset-class combination
# ======================================================================= #

def test_incompatible_venue_asset_class_combination() -> None:
    expect_fails("incompatible: MEXC + STOCK",
                 CANON.build_canonical_execution_specification, **dict(BASE_STOCK_LONG, venue="MEXC"))
    expect_fails("incompatible: ALPACA + CRYPTO_FUTURES",
                 CANON.build_canonical_execution_specification, **dict(BASE_MEXC_LONG, venue="ALPACA"))


# ======================================================================= #
# Incompatible futures-only fields on stocks/ETFs
# ======================================================================= #

def test_incompatible_futures_only_fields_on_stocks_and_etfs() -> None:
    expect_fails("futures-field-on-stock: leverage set on STOCK",
                 CANON.build_canonical_execution_specification, **dict(BASE_STOCK_LONG, leverage=3))
    expect_fails("futures-field-on-etf: leverage set on ETF",
                 CANON.build_canonical_execution_specification, **dict(BASE_ETF_LONG, leverage=3))
    expect_fails("futures-field-on-crypto-spot: leverage set on CRYPTO_SPOT",
                 CANON.build_canonical_execution_specification, **dict(BASE_ALPACA_SPOT_LONG, leverage=3))


def test_futures_entry_requires_leverage() -> None:
    missing_leverage = dict(BASE_MEXC_LONG)
    missing_leverage.pop("leverage")
    expect_fails("futures-entry-missing-leverage: OPEN_LONG without leverage",
                 CANON.build_canonical_execution_specification, **missing_leverage)


def test_futures_close_rejects_leverage() -> None:
    expect_fails("futures-close-leverage-not-allowed: CLOSE_LONG with leverage set",
                 CANON.build_canonical_execution_specification,
                 **dict(BASE_MEXC_LONG, direction="CLOSE_LONG", leverage=5))


# ======================================================================= #
# Expiry handling
# ======================================================================= #

def test_expiry_handling() -> None:
    spec = CANON.build_canonical_execution_specification(**dict(BASE_MEXC_LONG, expires_at="2099-01-01T00:00:00+00:00"))
    expect("expiry: valid future expiry carried through", spec["expires_at"] == "2099-01-01T00:00:00+00:00")

    spec_no_expiry = CANON.build_canonical_execution_specification(**BASE_MEXC_LONG)
    expect("expiry: optional, None when omitted", spec_no_expiry["expires_at"] is None)

    expect_fails("expiry: rejects an expiry in the past relative to specification_timestamp",
                 CANON.build_canonical_execution_specification,
                 **dict(BASE_MEXC_LONG, expires_at="2020-01-01T00:00:00+00:00"))

    expect_fails("expiry: rejects a malformed expiry string",
                 CANON.build_canonical_execution_specification,
                 **dict(BASE_MEXC_LONG, expires_at="not-a-date"))


# ======================================================================= #
# Deterministic fingerprinting
# ======================================================================= #

def test_deterministic_fingerprinting() -> None:
    spec1 = CANON.build_canonical_execution_specification(**BASE_MEXC_LONG)
    spec2 = CANON.build_canonical_execution_specification(**BASE_MEXC_LONG)
    expect("fingerprint: two builds of the identical decision content are byte-for-byte fingerprint-equal",
           spec1["spec_fingerprint"] == spec2["spec_fingerprint"])
    expect("fingerprint: client_order_id is also deterministic (same decision content -> same id)",
           spec1["client_order_id"] == spec2["client_order_id"])

    different = dict(BASE_MEXC_LONG, quantity="0.02")
    spec3 = CANON.build_canonical_execution_specification(**different)
    expect("fingerprint: a materially different spec has a different fingerprint",
           spec3["spec_fingerprint"] != spec1["spec_fingerprint"])

    ok, errors = CANON.verify_canonical_specification(spec1)
    expect("fingerprint: self-verification succeeds on an untampered record", ok and not errors)

    tampered = dict(spec1)
    tampered["quantity"] = "999"
    ok2, errors2 = CANON.verify_canonical_specification(tampered)
    expect("fingerprint: self-verification catches tampering", not ok2 and "SPEC_FINGERPRINT_MISMATCH" in errors2)


# ======================================================================= #
# Provenance preservation
# ======================================================================= #

def test_provenance_preservation() -> None:
    spec = CANON.build_canonical_execution_specification(
        **BASE_MEXC_LONG, source_kind="AI_PROPOSAL", evidence_hash="sha256:evidence-abc",
    )
    expect("provenance: decision_id preserved", spec["decision_id"] == "dec-001")
    expect("provenance: strategy_id preserved", spec["strategy_id"] == "AURA_SIGNAL")
    expect("provenance: strategy_version preserved", spec["strategy_version"] == "v0.5.3.13")
    expect("provenance: source_kind preserved", spec["source_kind"] == "AI_PROPOSAL")
    expect("provenance: evidence_hash preserved", spec["evidence_hash"] == "sha256:evidence-abc")
    expect("provenance: signal_timestamp preserved distinctly from specification_timestamp",
           spec["signal_timestamp"] == "2026-09-10T00:00:00+00:00" and spec["specification_timestamp"] != spec["signal_timestamp"])
    expect("provenance: default source_kind is DETERMINISTIC_SIGNAL when not overridden -- AI never silently becomes the default",
           CANON.build_canonical_execution_specification(**BASE_MEXC_LONG)["source_kind"] == "DETERMINISTIC_SIGNAL")


def test_invalid_source_kind_and_missing_provenance_fail_closed() -> None:
    expect_fails("provenance: invalid source_kind rejected",
                 CANON.build_canonical_execution_specification, **dict(BASE_MEXC_LONG, source_kind="RANDOM_GUESS"))
    missing_decision = dict(BASE_MEXC_LONG)
    missing_decision["decision_id"] = ""
    expect_fails("provenance: empty decision_id rejected", CANON.build_canonical_execution_specification, **missing_decision)
    missing_strategy = dict(BASE_MEXC_LONG)
    missing_strategy["strategy_id"] = ""
    expect_fails("provenance: empty strategy_id rejected", CANON.build_canonical_execution_specification, **missing_strategy)


# ======================================================================= #
# No live submission -- structural guarantee
# ======================================================================= #

def test_no_live_submission_capability_exists_in_this_module() -> None:
    """This module has no submit/execute/order-placement function and no
    network-capable broker-library import at all -- structural, not merely
    behavioral. Checked two ways: (1) no top-level name matches a
    submission-shaped verb; (2) no broker/network library is bound as a
    top-level import (an EXACT-name check, not substring, so this module's
    own intentionally-named to_alpaca_execution_spec()/to_mexc_execution_
    spec() translator functions -- which only ever return dicts -- are not
    mistaken for a broker import)."""
    submission_verbs = ("submit", "execute_order", "place_order")
    names = [n.lower() for n in dir(CANON)]
    verb_hits = [n for n in names if any(v in n for v in submission_verbs)]
    expect("no-submission: module exposes no submission-shaped function", verb_hits == [])

    forbidden_top_level_imports = {"requests", "ccxt", "alpaca", "urllib", "socket", "http"}
    import_hits = forbidden_top_level_imports & set(names)
    expect("no-submission: module has no broker/network library bound at top level", import_hits == set())


def test_translators_default_to_fully_blocked_authorization_state() -> None:
    spec = CANON.build_canonical_execution_specification(**BASE_MEXC_LONG)
    mexc_wire = CANON.to_mexc_execution_spec(spec)
    expect("no-submission: MEXC translator defaults kill_switch=True", mexc_wire["kill_switch"] is True)
    expect("no-submission: MEXC translator defaults execution_authorized=False", mexc_wire["execution_authorized"] is False)
    expect("no-submission: MEXC translator defaults live_execution_authorized=False", mexc_wire["live_execution_authorized"] is False)

    spot_spec = CANON.build_canonical_execution_specification(**BASE_ALPACA_SPOT_LONG)
    alpaca_wire = CANON.to_alpaca_execution_spec(spot_spec)
    expect("no-submission: Alpaca translator defaults kill_switch=True", alpaca_wire["kill_switch"] is True)
    expect("no-submission: Alpaca translator defaults execution_authorized=False", alpaca_wire["execution_authorized"] is False)
    expect("no-submission: Alpaca translator defaults paper_execution_authorized=False", alpaca_wire["paper_execution_authorized"] is False)
    expect("no-submission: Alpaca translator always sets live_execution=False", alpaca_wire["live_execution"] is False)

    # And the fully-blocked default is actually rejected by the real
    # adapters' own validate_spec() -- proving the default is genuinely
    # unsubmittable, not just labeled that way.
    try:
        MEXC_ADAPTER.validate_spec(mexc_wire)
        raised = False
    except RuntimeError:
        raised = True
    expect("no-submission: real .27 adapter rejects the fully-blocked default (kill_switch=True)", raised)

    try:
        ALPACA_ADAPTER.validate_spec(alpaca_wire)
        raised2 = False
    except RuntimeError:
        raised2 = True
    expect("no-submission: real .22 adapter rejects the fully-blocked default (kill_switch=True)", raised2)


# ======================================================================= #
# Malformed specifications fail closed
# ======================================================================= #

def test_malformed_specifications_fail_closed() -> None:
    expect_fails("malformed: non-numeric quantity", CANON.build_canonical_execution_specification,
                 **dict(BASE_MEXC_LONG, quantity="not-a-number"))
    expect_fails("malformed: zero quantity", CANON.build_canonical_execution_specification,
                 **dict(BASE_MEXC_LONG, quantity="0"))
    expect_fails("malformed: negative quantity", CANON.build_canonical_execution_specification,
                 **dict(BASE_MEXC_LONG, quantity="-1"))
    expect_fails("malformed: symbol shape wrong for asset class", CANON.build_canonical_execution_specification,
                 **dict(BASE_MEXC_LONG, symbol="BTCUSD"))
    expect_fails("malformed: invalid order_type", CANON.build_canonical_execution_specification,
                 **dict(BASE_MEXC_LONG, order_type="STOP"))
    expect_fails("malformed: LIMIT order_type without limit_price", CANON.build_canonical_execution_specification,
                 **dict(BASE_MEXC_LONG, order_type="LIMIT"))
    expect_fails("malformed: limit_price set without LIMIT order_type", CANON.build_canonical_execution_specification,
                 **dict(BASE_MEXC_LONG, limit_price="100"))
    expect_fails("malformed: bad signal_timestamp", CANON.build_canonical_execution_specification,
                 **dict(BASE_MEXC_LONG, signal_timestamp="yesterday"))
    expect_fails("malformed: naive (no timezone) signal_timestamp", CANON.build_canonical_execution_specification,
                 **dict(BASE_MEXC_LONG, signal_timestamp="2026-09-10T00:00:00"))
    expect_fails("malformed: time_in_force on MEXC (not applicable)", CANON.build_canonical_execution_specification,
                 **dict(BASE_MEXC_LONG, time_in_force="GTC"))
    expect_fails("malformed: invalid time_in_force value", CANON.build_canonical_execution_specification,
                 **dict(BASE_ALPACA_SPOT_LONG, time_in_force="FOK"))


# ======================================================================= #
# Regression: the real .27 / .22 adapters accept this module's translated
# output -- proof the existing .27-.32 / .22 chains remain compatible and
# untouched, not a claim taken on faith.
# ======================================================================= #

def test_mexc_translation_compatible_with_real_adapter_validate_spec() -> None:
    for direction, leverage in (("OPEN_LONG", 5), ("OPEN_SHORT", 5), ("CLOSE_LONG", None), ("CLOSE_SHORT", None)):
        kwargs = dict(BASE_MEXC_LONG, direction=direction, decision_id=f"dec-mexc-{direction}")
        if leverage is None:
            kwargs.pop("leverage")
        else:
            kwargs["leverage"] = leverage
        spec = CANON.build_canonical_execution_specification(**kwargs)
        wire = CANON.to_mexc_execution_spec(
            spec, kill_switch=False, execution_authorized=True, live_execution_authorized=True,
        )
        validated = MEXC_ADAPTER.validate_spec(wire)
        expect(f"mexc-compat[{direction}]: real .27 validate_spec() accepts the translated spec",
               validated["client_order_id"] == spec["client_order_id"])
        expect(f"mexc-compat[{direction}]: side/reduce_only round-trip matches expectation",
               (validated["side"], validated["reduce_only"]) == CANON._MEXC_DIRECTION_TO_SIDE_REDUCE[direction])


def test_mexc_translation_rejects_limit_order_type() -> None:
    spec = dict(CANON.build_canonical_execution_specification(**BASE_MEXC_LONG))
    spec["order_type"] = "LIMIT"  # simulate a mis-tagged canonical spec reaching the translator
    expect_fails("mexc-compat: translator refuses LIMIT (MEXC adapter is MARKET-only)",
                 CANON.to_mexc_execution_spec, spec)


def test_alpaca_translation_compatible_with_real_adapter_validate_spec() -> None:
    for direction in ("OPEN_LONG", "CLOSE_LONG"):
        spec = CANON.build_canonical_execution_specification(
            **dict(BASE_ALPACA_SPOT_LONG, direction=direction, decision_id=f"dec-alpaca-{direction}"),
        )
        wire = CANON.to_alpaca_execution_spec(
            spec, kill_switch=False, execution_authorized=True, paper_execution_authorized=True,
        )
        validated = ALPACA_ADAPTER.validate_spec(wire)
        expect(f"alpaca-compat[{direction}]: real .22 validate_spec() accepts the translated spec",
               validated["client_order_id"] == spec["client_order_id"])


def test_alpaca_translation_fails_closed_for_stock_and_etf() -> None:
    stock_spec = CANON.build_canonical_execution_specification(**BASE_STOCK_LONG)
    expect_fails("alpaca-compat: STOCK translation fails closed (NOT_YET_IMPLEMENTED, no adapter exists)",
                 CANON.to_alpaca_execution_spec, stock_spec, kill_switch=False,
                 execution_authorized=True, paper_execution_authorized=True)

    etf_spec = CANON.build_canonical_execution_specification(**BASE_ETF_LONG)
    expect_fails("alpaca-compat: ETF translation fails closed (NOT_YET_IMPLEMENTED, no adapter exists)",
                 CANON.to_alpaca_execution_spec, etf_spec, kill_switch=False,
                 execution_authorized=True, paper_execution_authorized=True)


def test_classify_support_matches_the_documented_table() -> None:
    expect("classify: MEXC crypto-futures long is SUPPORTED_BY_ADAPTER",
           CANON.classify_support("CRYPTO_FUTURES", "MEXC", "OPEN_LONG") == CANON.SUPPORTED_BY_ADAPTER)
    expect("classify: MEXC crypto-futures short is SUPPORTED_BY_ADAPTER",
           CANON.classify_support("CRYPTO_FUTURES", "MEXC", "OPEN_SHORT") == CANON.SUPPORTED_BY_ADAPTER)
    expect("classify: Alpaca crypto-spot long is SUPPORTED_BY_ADAPTER",
           CANON.classify_support("CRYPTO_SPOT", "ALPACA", "OPEN_LONG") == CANON.SUPPORTED_BY_ADAPTER)
    expect("classify: Alpaca crypto-spot short is SUPPORTED_BY_SCHEMA only (no reduce_only/margin concept)",
           CANON.classify_support("CRYPTO_SPOT", "ALPACA", "OPEN_SHORT") == CANON.SUPPORTED_BY_SCHEMA)
    expect("classify: Alpaca stock is NOT_YET_IMPLEMENTED",
           CANON.classify_support("STOCK", "ALPACA", "OPEN_LONG") == CANON.NOT_YET_IMPLEMENTED)
    expect("classify: Alpaca etf is NOT_YET_IMPLEMENTED",
           CANON.classify_support("ETF", "ALPACA", "OPEN_LONG") == CANON.NOT_YET_IMPLEMENTED)
    expect("classify: MEXC + STOCK (incompatible venue/asset-class) is NOT_YET_IMPLEMENTED",
           CANON.classify_support("STOCK", "MEXC", "OPEN_LONG") == CANON.NOT_YET_IMPLEMENTED)
    expect("classify: garbage input classifies cleanly as NOT_YET_IMPLEMENTED, never raises",
           CANON.classify_support("NONSENSE", "NOWHERE", "SIDEWAYS") == CANON.NOT_YET_IMPLEMENTED)


if __name__ == "__main__":
    tests = [obj for name, obj in list(globals().items()) if name.startswith("test_")]
    for t in tests:
        t()
    print(f"AURA v0.5.3.33 UNIT TEST CONTRACT: ALL {len(tests)} PASS")
