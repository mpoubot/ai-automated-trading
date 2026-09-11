#!/usr/bin/env python3
"""Unit + compatibility tests for AURA v0.5.3.35 (Alpaca Equity / ETF
Execution Adapter).

Covers, in order, every item from Martin's explicit .35 test list: STOCK
LONG OPEN/CLOSE, STOCK SHORT OPEN/CLOSE, ETF LONG OPEN/CLOSE, ETF SHORT
OPEN/CLOSE, shortable=confirmed/denied/unknown, easy_to_borrow constraints,
non-tradable asset, invalid symbol, unsupported asset class, invalid venue,
invalid direction, malformed .33 execution specification, BUY/SELL versus
position-intent distinction, duplicate/ambiguous position intent, quantity
validation, fractional quantity where supported/unsupported, deterministic
order construction, no accidental live submission, fail-closed behaviour --
plus a dedicated .33 -> .34 -> .35 compatibility-chain test and CLI-level
PAPER/LIVE boundary tests.

All Alpaca API responses used here are mocked/fake (plain dicts or tiny
fake classes standing in for `Asset` / `TradingClient`). No live Alpaca
order is submitted or required to prove this adapter works, per Martin's
explicit instruction.
"""
from __future__ import annotations

import importlib.util
import inspect
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


ADAPTER = _load("aura_v05335_alpaca_equity_execution_adapter", "aura_v05335_alpaca_equity_execution_adapter.py")
CANON = _load("aura_v05333_canonical_execution_specification", "aura_v05333_canonical_execution_specification.py")
META = _load("aura_v05334_asset_instrument_metadata", "aura_v05334_asset_instrument_metadata.py")

from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest  # noqa: E402


def expect(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)
    print(f"PASS: {name}")


def expect_fails(name: str, fn, *args, **kwargs) -> None:
    try:
        fn(*args, **kwargs)
    except RuntimeError as exc:
        print(f"PASS: {name} ({exc})")
        return
    raise AssertionError(f"{name} (expected RuntimeError, none raised)")


# ======================================================================= #
# Fixtures / builders
# ======================================================================= #

SIGNAL_TS = "2026-09-11T12:00:00+00:00"


def make_spec(
    *,
    asset_class: str = "STOCK",
    direction: str = "OPEN_LONG",
    symbol: str = "AAPL",
    quantity="10",
    order_type: str = "MARKET",
    limit_price=None,
    venue: str = "ALPACA",
    decision_id: str = "DEC-1",
    signal_timestamp: str = SIGNAL_TS,
) -> dict:
    return CANON.build_canonical_execution_specification(
        asset_class=asset_class,
        venue=venue,
        direction=direction,
        symbol=symbol,
        quantity=quantity,
        decision_id=decision_id,
        strategy_id="STRAT-1",
        strategy_version="v1.0",
        signal_timestamp=signal_timestamp,
        order_type=order_type,
        limit_price=limit_price,
    )


def make_asset(
    *,
    symbol: str = "AAPL",
    tradable=True,
    shortable=True,
    easy_to_borrow=True,
    fractionable=True,
    asset_class: str = "us_equity",
    exchange: str = "NASDAQ",
    status: str = "active",
) -> dict:
    return {
        "symbol": symbol,
        "asset_class": asset_class,
        "exchange": exchange,
        "status": status,
        "tradable": tradable,
        "shortable": shortable,
        "easy_to_borrow": easy_to_borrow,
        "fractionable": fractionable,
        "marginable": True,
        "min_order_size": None,
        "min_trade_increment": None,
        "price_increment": None,
    }


def tamper(spec: dict, **overrides) -> dict:
    """Mutates a copy of a legitimately-built spec and recomputes its
    fingerprint, so verify_canonical_specification() still passes its own
    self-consistency check -- used to reach .35's OWN field checks (venue,
    direction, symbol) independently of .33's construction-time guards,
    which would otherwise refuse to build an invalid spec in the first
    place."""
    tampered = dict(spec)
    tampered.update(overrides)
    tampered["spec_fingerprint"] = CANON.canonical_spec_fingerprint(tampered)
    return tampered


def break_fingerprint(spec: dict) -> dict:
    tampered = dict(spec)
    tampered["spec_fingerprint"] = "0" * 64
    return tampered


# ======================================================================= #
# STOCK LONG / SHORT OPEN / CLOSE
# ======================================================================= #

def test_stock_long_open() -> None:
    spec = make_spec(asset_class="STOCK", direction="OPEN_LONG", symbol="AAPL")
    asset = make_asset(symbol="AAPL")
    order_spec = ADAPTER.build_order_request_spec(spec, asset)
    expect("stock long open: side BUY", order_spec["side"] == "buy")
    expect("stock long open: position_intent BUY_TO_OPEN", order_spec["position_intent"] == "buy_to_open")
    expect("stock long open: direction preserved", order_spec["direction"] == "OPEN_LONG")


def test_stock_long_close() -> None:
    spec = make_spec(asset_class="STOCK", direction="CLOSE_LONG", symbol="AAPL")
    # Deliberately NOT shortable -- closing a long must never require a
    # shortability check at all.
    asset = make_asset(symbol="AAPL", shortable=False, easy_to_borrow=False)
    order_spec = ADAPTER.build_order_request_spec(spec, asset)
    expect("stock long close: side SELL", order_spec["side"] == "sell")
    expect("stock long close: position_intent SELL_TO_CLOSE", order_spec["position_intent"] == "sell_to_close")


def test_stock_short_open_confirmed_shortable() -> None:
    spec = make_spec(asset_class="STOCK", direction="OPEN_SHORT", symbol="TSLA")
    asset = make_asset(symbol="TSLA", shortable=True, easy_to_borrow=True)
    order_spec = ADAPTER.build_order_request_spec(spec, asset)
    expect("stock short open: side SELL", order_spec["side"] == "sell")
    expect("stock short open: position_intent SELL_TO_OPEN", order_spec["position_intent"] == "sell_to_open")
    expect("stock short open: shortability SHORTABLE", order_spec["shortability_status"] == "SHORTABLE")
    expect("stock short open: capability BORROW_CONFIRMED", order_spec["direction_capability"] == "BORROW_CONFIRMED")


def test_stock_short_close_does_not_require_shortability_confirmation() -> None:
    """Closing an existing short (buying back) must be allowed even when
    the asset is NOT currently shortable -- shortability only gates
    OPENING a new short, never closing one."""
    spec = make_spec(asset_class="STOCK", direction="CLOSE_SHORT", symbol="TSLA")
    asset = make_asset(symbol="TSLA", shortable=False, easy_to_borrow=False)
    order_spec = ADAPTER.build_order_request_spec(spec, asset)
    expect("stock short close: side BUY", order_spec["side"] == "buy")
    expect("stock short close: position_intent BUY_TO_CLOSE", order_spec["position_intent"] == "buy_to_close")
    expect("stock short close: allowed despite NOT_SHORTABLE asset", order_spec["shortability_status"] == "NOT_SHORTABLE")


# ======================================================================= #
# ETF LONG / SHORT OPEN / CLOSE
# ======================================================================= #

def test_etf_long_open() -> None:
    spec = make_spec(asset_class="ETF", direction="OPEN_LONG", symbol="SPY")
    asset = make_asset(symbol="SPY")
    order_spec = ADAPTER.build_order_request_spec(spec, asset)
    expect("etf long open: side BUY", order_spec["side"] == "buy")
    expect("etf long open: position_intent BUY_TO_OPEN", order_spec["position_intent"] == "buy_to_open")


def test_etf_long_close() -> None:
    spec = make_spec(asset_class="ETF", direction="CLOSE_LONG", symbol="SPY")
    asset = make_asset(symbol="SPY", shortable=False, easy_to_borrow=False)
    order_spec = ADAPTER.build_order_request_spec(spec, asset)
    expect("etf long close: side SELL", order_spec["side"] == "sell")
    expect("etf long close: position_intent SELL_TO_CLOSE", order_spec["position_intent"] == "sell_to_close")


def test_etf_short_open_confirmed_shortable() -> None:
    spec = make_spec(asset_class="ETF", direction="OPEN_SHORT", symbol="IWM")
    asset = make_asset(symbol="IWM", shortable=True, easy_to_borrow=True)
    order_spec = ADAPTER.build_order_request_spec(spec, asset)
    expect("etf short open: side SELL", order_spec["side"] == "sell")
    expect("etf short open: position_intent SELL_TO_OPEN", order_spec["position_intent"] == "sell_to_open")


def test_etf_short_close() -> None:
    spec = make_spec(asset_class="ETF", direction="CLOSE_SHORT", symbol="IWM")
    asset = make_asset(symbol="IWM", shortable=True, easy_to_borrow=True)
    order_spec = ADAPTER.build_order_request_spec(spec, asset)
    expect("etf short close: side BUY", order_spec["side"] == "buy")
    expect("etf short close: position_intent BUY_TO_CLOSE", order_spec["position_intent"] == "buy_to_close")


# ======================================================================= #
# Shortability: confirmed / denied / unknown, easy_to_borrow constraints
# ======================================================================= #

def test_shortable_denied_blocks_open_short() -> None:
    spec = make_spec(asset_class="STOCK", direction="OPEN_SHORT", symbol="GME")
    asset = make_asset(symbol="GME", shortable=False, easy_to_borrow=False)
    expect_fails("shortable=denied blocks OPEN_SHORT", ADAPTER.build_order_request_spec, spec, asset)


def test_shortable_unknown_blocks_open_short() -> None:
    spec = make_spec(asset_class="STOCK", direction="OPEN_SHORT", symbol="GME")
    asset = make_asset(symbol="GME", shortable=None, easy_to_borrow=None)
    expect_fails("shortable=unknown (None) blocks OPEN_SHORT", ADAPTER.build_order_request_spec, spec, asset)


def test_easy_to_borrow_false_treated_as_unknown_blocks_open_short() -> None:
    """shortable=True but easy_to_borrow=False (hard-to-borrow) must be
    treated as UNKNOWN, not SHORTABLE -- the conservative reading (module
    docstring §3)."""
    spec = make_spec(asset_class="STOCK", direction="OPEN_SHORT", symbol="AMC")
    asset = make_asset(symbol="AMC", shortable=True, easy_to_borrow=False)
    expect(
        "hard-to-borrow classifies as UNKNOWN",
        ADAPTER.alpaca_asset_to_shortability_status(True, False) == "UNKNOWN",
    )
    expect_fails("hard-to-borrow (easy_to_borrow=False) blocks OPEN_SHORT", ADAPTER.build_order_request_spec, spec, asset)


def test_shortable_confirmed_requires_both_flags_true() -> None:
    expect("both True -> SHORTABLE", ADAPTER.alpaca_asset_to_shortability_status(True, True) == "SHORTABLE")
    expect("shortable False -> NOT_SHORTABLE regardless of etb",
           ADAPTER.alpaca_asset_to_shortability_status(False, True) == "NOT_SHORTABLE")
    expect("shortable None -> UNKNOWN", ADAPTER.alpaca_asset_to_shortability_status(None, True) == "UNKNOWN")


# ======================================================================= #
# Tradability / symbol / asset-class / venue / direction validation
# ======================================================================= #

def test_non_tradable_asset_rejected() -> None:
    spec = make_spec(asset_class="STOCK", direction="OPEN_LONG", symbol="XYZ")
    asset = make_asset(symbol="XYZ", tradable=False)
    expect_fails("non-tradable asset rejected", ADAPTER.build_order_request_spec, spec, asset)


def test_symbol_mismatch_between_spec_and_asset_rejected() -> None:
    spec = make_spec(asset_class="STOCK", direction="OPEN_LONG", symbol="AAPL")
    asset = make_asset(symbol="MSFT")
    expect_fails("spec/asset symbol mismatch rejected", ADAPTER.build_order_request_spec, spec, asset)


def test_tampered_empty_symbol_rejected() -> None:
    spec = make_spec(asset_class="STOCK", direction="OPEN_LONG", symbol="AAPL")
    tampered = tamper(spec, symbol="")
    asset = make_asset(symbol="")
    expect_fails("empty symbol rejected (MISSING_SYMBOL)", ADAPTER.build_order_request_spec, tampered, asset)


def test_invalid_symbol_shape_rejected_by_metadata_layer() -> None:
    """A symbol that passes .35's own presence/match check but violates
    .34's STOCK ticker shape (1-5 uppercase letters) must still fail --
    proves the .34 metadata layer is genuinely consulted, not bypassed."""
    spec = make_spec(asset_class="STOCK", direction="OPEN_LONG", symbol="AAPL")
    tampered = tamper(spec, symbol="AAPLXX")
    asset = make_asset(symbol="AAPLXX")
    expect_fails("oversized ticker rejected by .34 symbol-shape check", ADAPTER.build_order_request_spec, tampered, asset)


def test_unsupported_asset_class_for_equity_adapter() -> None:
    spec = make_spec(asset_class="CRYPTO_SPOT", direction="OPEN_LONG", symbol="BTC/USD")
    asset = make_asset(symbol="BTC/USD")
    expect_fails("CRYPTO_SPOT rejected by equity adapter", ADAPTER.build_order_request_spec, spec, asset)


def test_invalid_venue_rejected() -> None:
    spec = make_spec(asset_class="STOCK", direction="OPEN_LONG", symbol="AAPL")
    tampered = tamper(spec, venue="MEXC")
    asset = make_asset(symbol="AAPL")
    expect_fails("venue != ALPACA rejected", ADAPTER.build_order_request_spec, tampered, asset)


def test_invalid_direction_rejected() -> None:
    spec = make_spec(asset_class="STOCK", direction="OPEN_LONG", symbol="AAPL")
    tampered = tamper(spec, direction="SIDEWAYS")
    asset = make_asset(symbol="AAPL")
    expect_fails("invalid direction rejected", ADAPTER.build_order_request_spec, tampered, asset)


def test_malformed_canonical_spec_rejected() -> None:
    spec = make_spec(asset_class="STOCK", direction="OPEN_LONG", symbol="AAPL")
    broken = break_fingerprint(spec)
    asset = make_asset(symbol="AAPL")
    expect_fails("tampered/malformed .33 spec rejected", ADAPTER.build_order_request_spec, broken, asset)


def test_not_a_dict_spec_rejected() -> None:
    expect_fails("non-dict spec rejected", ADAPTER.validate_equity_spec, "not a dict", make_asset())


def test_invalid_order_type_rejected() -> None:
    spec = make_spec(asset_class="STOCK", direction="OPEN_LONG", symbol="AAPL")
    tampered = tamper(spec, order_type="STOP")
    asset = make_asset(symbol="AAPL")
    expect_fails("invalid order_type rejected", ADAPTER.build_order_request_spec, tampered, asset)


# ======================================================================= #
# LIMIT order path
# ======================================================================= #

def test_limit_order_requires_limit_price() -> None:
    spec = make_spec(asset_class="STOCK", direction="OPEN_LONG", symbol="AAPL", order_type="LIMIT", limit_price="150.00")
    tampered = tamper(spec, limit_price=None)
    asset = make_asset(symbol="AAPL")
    expect_fails("LIMIT order without limit_price rejected", ADAPTER.build_order_request_spec, tampered, asset)


def test_limit_order_builds_correctly() -> None:
    spec = make_spec(asset_class="STOCK", direction="OPEN_LONG", symbol="AAPL", order_type="LIMIT", limit_price="150.00")
    asset = make_asset(symbol="AAPL")
    order_spec = ADAPTER.build_order_request_spec(spec, asset)
    expect("limit order: order_type LIMIT", order_spec["order_type"] == "LIMIT")
    expect("limit order: limit_price present", order_spec["limit_price"] == "150.00")
    request = ADAPTER.to_alpaca_order_request(order_spec)
    expect("limit order: real LimitOrderRequest built", isinstance(request, LimitOrderRequest))


def test_to_alpaca_order_request_market_and_limit_objects() -> None:
    spec = make_spec(asset_class="STOCK", direction="OPEN_LONG", symbol="AAPL")
    asset = make_asset(symbol="AAPL")
    order_spec = ADAPTER.build_order_request_spec(spec, asset)
    request = ADAPTER.to_alpaca_order_request(order_spec)
    expect("market order: real MarketOrderRequest built", isinstance(request, MarketOrderRequest))
    expect("market order: side matches", request.side.value == "buy")
    expect("market order: position_intent matches", request.position_intent.value == "buy_to_open")
    expect("market order: client_order_id passed through", request.client_order_id == order_spec["client_order_id"])


# ======================================================================= #
# BUY/SELL vs position-intent distinction, duplicate/ambiguous mapping
# ======================================================================= #

def test_buy_sell_vs_position_intent_distinction() -> None:
    """The critical position-semantics requirement: CLOSE_LONG and
    OPEN_SHORT both have side SELL, but must never be conflated -- their
    position_intent values must differ. Likewise OPEN_LONG and CLOSE_SHORT
    both have side BUY but different position_intent."""
    close_long_side, close_long_intent = ADAPTER.DIRECTION_TO_SIDE_POSITION_INTENT_STR["CLOSE_LONG"]
    open_short_side, open_short_intent = ADAPTER.DIRECTION_TO_SIDE_POSITION_INTENT_STR["OPEN_SHORT"]
    expect("CLOSE_LONG and OPEN_SHORT share side SELL", close_long_side == open_short_side == "sell")
    expect("CLOSE_LONG and OPEN_SHORT have DIFFERENT position_intent", close_long_intent != open_short_intent)

    open_long_side, open_long_intent = ADAPTER.DIRECTION_TO_SIDE_POSITION_INTENT_STR["OPEN_LONG"]
    close_short_side, close_short_intent = ADAPTER.DIRECTION_TO_SIDE_POSITION_INTENT_STR["CLOSE_SHORT"]
    expect("OPEN_LONG and CLOSE_SHORT share side BUY", open_long_side == close_short_side == "buy")
    expect("OPEN_LONG and CLOSE_SHORT have DIFFERENT position_intent", open_long_intent != close_short_intent)


def test_no_duplicate_or_ambiguous_position_intents() -> None:
    """All four directions must map to four DISTINCT (side, position_intent)
    pairs -- no two directions are ever indistinguishable at the wire
    level."""
    pairs = list(ADAPTER.DIRECTION_TO_SIDE_POSITION_INTENT_STR.values())
    expect("exactly 4 directions mapped", len(pairs) == 4)
    expect("all 4 (side, position_intent) pairs are unique", len(set(pairs)) == 4)
    intents = [intent for _side, intent in pairs]
    expect("all 4 position_intent values are unique", len(set(intents)) == 4)


# ======================================================================= #
# Quantity validation
# ======================================================================= #

def test_quantity_zero_rejected() -> None:
    spec = make_spec(asset_class="STOCK", direction="OPEN_LONG", symbol="AAPL")
    tampered = tamper(spec, quantity="0")
    asset = make_asset(symbol="AAPL")
    expect_fails("zero quantity rejected", ADAPTER.build_order_request_spec, tampered, asset)


def test_quantity_negative_rejected() -> None:
    spec = make_spec(asset_class="STOCK", direction="OPEN_LONG", symbol="AAPL")
    tampered = tamper(spec, quantity="-5")
    asset = make_asset(symbol="AAPL")
    expect_fails("negative quantity rejected", ADAPTER.build_order_request_spec, tampered, asset)


def test_quantity_non_numeric_rejected() -> None:
    spec = make_spec(asset_class="STOCK", direction="OPEN_LONG", symbol="AAPL")
    tampered = tamper(spec, quantity="not-a-number")
    asset = make_asset(symbol="AAPL")
    expect_fails("non-numeric quantity rejected", ADAPTER.build_order_request_spec, tampered, asset)


def test_fractional_quantity_supported_when_fractionable() -> None:
    spec = make_spec(asset_class="STOCK", direction="OPEN_LONG", symbol="AAPL", quantity="1.5")
    asset = make_asset(symbol="AAPL", fractionable=True)
    order_spec = ADAPTER.build_order_request_spec(spec, asset)
    expect("fractional quantity accepted when fractionable", order_spec["quantity"] == "1.5")


def test_fractional_quantity_rejected_when_not_fractionable() -> None:
    spec = make_spec(asset_class="STOCK", direction="OPEN_LONG", symbol="AAPL", quantity="1.5")
    asset = make_asset(symbol="AAPL", fractionable=False)
    expect_fails("fractional quantity rejected when NOT fractionable", ADAPTER.build_order_request_spec, spec, asset)


def test_whole_quantity_allowed_when_not_fractionable() -> None:
    spec = make_spec(asset_class="STOCK", direction="OPEN_LONG", symbol="AAPL", quantity="3")
    asset = make_asset(symbol="AAPL", fractionable=False)
    order_spec = ADAPTER.build_order_request_spec(spec, asset)
    expect("whole quantity fine even when not fractionable", order_spec["quantity"] == "3")


# ======================================================================= #
# Deterministic order construction
# ======================================================================= #

def test_deterministic_order_construction() -> None:
    spec = make_spec(asset_class="STOCK", direction="OPEN_LONG", symbol="AAPL", decision_id="SAME-DECISION")
    asset = make_asset(symbol="AAPL")
    first = ADAPTER.build_order_request_spec(spec, asset)
    second = ADAPTER.build_order_request_spec(spec, asset)
    first_stable = {k: v for k, v in first.items() if k != "built_at"}
    second_stable = {k: v for k, v in second.items() if k != "built_at"}
    expect("identical inputs -> identical order_spec (excluding build timestamp)", first_stable == second_stable)


# ======================================================================= #
# No accidental live submission -- structural check
# ======================================================================= #

def test_network_capable_functions_disclosure_is_accurate() -> None:
    expect(
        "NETWORK_CAPABLE_FUNCTIONS is exactly {fetch_asset_metadata, submit, main}",
        ADAPTER.NETWORK_CAPABLE_FUNCTIONS == frozenset({"fetch_asset_metadata", "submit", "main"}),
    )
    for name in ("fail", "now", "alpaca_asset_to_shortability_status", "validate_equity_spec",
                 "build_order_request_spec", "to_alpaca_order_request",
                 "_decimal", "_is_fractional"):
        fn = getattr(ADAPTER, name)
        source = inspect.getsource(fn)
        expect(f"{name}(): no reference to a trading client object", "client." not in source)
        expect(f"{name}(): never constructs TradingClient(", "TradingClient(" not in source)


def test_pure_functions_do_not_accept_a_client_argument() -> None:
    for name in ("validate_equity_spec", "build_order_request_spec", "to_alpaca_order_request"):
        params = list(inspect.signature(getattr(ADAPTER, name)).parameters)
        expect(f"{name}() has no client-shaped parameter", "client" not in params)


# ======================================================================= #
# fetch_asset_metadata() -- read-only network call
# ======================================================================= #

class _FakeEnumField:
    def __init__(self, value):
        self.value = value


class _FakeAsset:
    def __init__(self, **kwargs):
        self.symbol = kwargs.get("symbol")
        self.asset_class = _FakeEnumField(kwargs.get("asset_class", "us_equity"))
        self.exchange = _FakeEnumField(kwargs.get("exchange", "NASDAQ"))
        self.status = _FakeEnumField(kwargs.get("status", "active"))
        self.tradable = kwargs.get("tradable", True)
        self.shortable = kwargs.get("shortable", True)
        self.easy_to_borrow = kwargs.get("easy_to_borrow", True)
        self.fractionable = kwargs.get("fractionable", True)
        self.marginable = kwargs.get("marginable", True)
        self.min_order_size = kwargs.get("min_order_size")
        self.min_trade_increment = kwargs.get("min_trade_increment")
        self.price_increment = kwargs.get("price_increment")


class _FakeReadOnlyClient:
    def __init__(self, asset: _FakeAsset):
        self._asset = asset
        self.calls = []

    def get_asset(self, symbol):
        self.calls.append(symbol)
        return self._asset


def test_fetch_asset_metadata_extracts_expected_fields() -> None:
    fake_asset = _FakeAsset(symbol="AAPL", tradable=True, shortable=True, easy_to_borrow=True, fractionable=True)
    client = _FakeReadOnlyClient(fake_asset)
    result = ADAPTER.fetch_asset_metadata(client, "AAPL")
    expect("fetch_asset_metadata: symbol", result["symbol"] == "AAPL")
    expect("fetch_asset_metadata: asset_class unwrapped from enum", result["asset_class"] == "us_equity")
    expect("fetch_asset_metadata: exchange unwrapped from enum", result["exchange"] == "NASDAQ")
    expect("fetch_asset_metadata: tradable", result["tradable"] is True)
    expect("fetch_asset_metadata: shortable", result["shortable"] is True)
    expect("fetch_asset_metadata: easy_to_borrow", result["easy_to_borrow"] is True)
    expect("fetch_asset_metadata: only one call made", client.calls == ["AAPL"])


# ======================================================================= #
# submit() -- idempotency + new-order paths (fake client, no network)
# ======================================================================= #

class _FakeOrder:
    def __init__(self, order_id, status):
        self.id = order_id
        self.status = status


class _FakeSubmittingClient:
    def __init__(self, existing_order=None, not_found_exc=Exception):
        self._existing = existing_order
        self._not_found_exc = not_found_exc
        self.submit_calls = []

    def get_order_by_client_id(self, client_order_id):
        if self._existing is None:
            raise self._not_found_exc("order not found")
        return self._existing

    def submit_order(self, order_data):
        self.submit_calls.append(order_data)
        return _FakeOrder(order_id="broker-123", status="new")


def test_submit_idempotent_when_order_already_exists() -> None:
    spec = make_spec(asset_class="STOCK", direction="OPEN_LONG", symbol="AAPL")
    asset = make_asset(symbol="AAPL")
    order_spec = ADAPTER.build_order_request_spec(spec, asset)
    existing = _FakeOrder(order_id="already-there", status="filled")
    client = _FakeSubmittingClient(existing_order=existing)
    result = ADAPTER.submit(order_spec, client)
    expect("idempotent submit: status ALREADY_EXISTS", result["status"] == "ALREADY_EXISTS")
    expect("idempotent submit: no new submit_order call made", client.submit_calls == [])
    expect("idempotent submit: paper True, live False", result["paper"] is True and result["live"] is False)


def test_submit_submits_new_order() -> None:
    spec = make_spec(asset_class="STOCK", direction="OPEN_LONG", symbol="AAPL")
    asset = make_asset(symbol="AAPL")
    order_spec = ADAPTER.build_order_request_spec(spec, asset)
    client = _FakeSubmittingClient(existing_order=None)
    result = ADAPTER.submit(order_spec, client)
    expect("new submit: status SUBMITTED", result["status"] == "SUBMITTED")
    expect("new submit: exactly one submit_order call", len(client.submit_calls) == 1)
    expect("new submit: broker_order_id present", result["broker_order_id"] == "broker-123")
    expect("new submit: paper True, live False", result["paper"] is True and result["live"] is False)


# ======================================================================= #
# .33 -> .34 -> .35 compatibility chain
# ======================================================================= #

def test_compatibility_chain_33_34_35() -> None:
    spec = CANON.build_canonical_execution_specification(
        asset_class="STOCK", venue="ALPACA", direction="OPEN_SHORT", symbol="TSLA",
        quantity="5", decision_id="CHAIN-1", strategy_id="STRAT-CHAIN", strategy_version="v1",
        signal_timestamp=SIGNAL_TS, order_type="MARKET",
    )
    ok, errors = CANON.verify_canonical_specification(spec)
    expect(".33 spec self-verifies", ok and errors == [])

    asset = make_asset(symbol="TSLA", shortable=True, easy_to_borrow=True)
    validated = ADAPTER.validate_equity_spec(spec, asset)
    expect(".34 metadata rebuilt fresh inside .35", validated["instrument_metadata"]["instrument_type"] == "STOCK")
    ok2, errors2 = META.verify_instrument_metadata(validated["instrument_metadata"])
    expect(".34 metadata self-verifies", ok2 and errors2 == [])

    order_spec = ADAPTER.build_order_request_spec(spec, asset)
    request = ADAPTER.to_alpaca_order_request(order_spec)
    expect("chain produces real MarketOrderRequest", isinstance(request, MarketOrderRequest))
    expect("chain: side SELL for OPEN_SHORT", request.side.value == "sell")
    expect("chain: position_intent SELL_TO_OPEN for OPEN_SHORT", request.position_intent.value == "sell_to_open")


# ======================================================================= #
# CLI / main() -- PAPER/LIVE boundary
# ======================================================================= #

def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, default=str), encoding="utf-8")


def test_main_cli_validates_without_submission_by_default(tmp_path, monkeypatch) -> None:
    spec = make_spec(asset_class="STOCK", direction="OPEN_LONG", symbol="AAPL")
    asset = make_asset(symbol="AAPL")
    input_path = tmp_path / "spec.json"
    asset_path = tmp_path / "asset.json"
    output_path = tmp_path / "out.json"
    _write_json(input_path, spec)
    _write_json(asset_path, asset)

    monkeypatch.setattr(
        "sys.argv",
        ["prog", "--input", str(input_path), "--asset-input", str(asset_path), "--output", str(output_path)],
    )
    rc = ADAPTER.main()
    result = json.loads(output_path.read_text(encoding="utf-8"))
    expect("default CLI run: exit 0", rc == 0)
    expect("default CLI run: VALIDATED_NO_SUBMISSION", result["status"] == "VALIDATED_NO_SUBMISSION")
    expect("default CLI run: no broker_order_id present", "broker_order_id" not in result)


def test_main_cli_blocks_submission_without_env_var(tmp_path, monkeypatch) -> None:
    spec = make_spec(asset_class="STOCK", direction="OPEN_LONG", symbol="AAPL")
    asset = make_asset(symbol="AAPL")
    input_path = tmp_path / "spec.json"
    asset_path = tmp_path / "asset.json"
    output_path = tmp_path / "out.json"
    _write_json(input_path, spec)
    _write_json(asset_path, asset)

    monkeypatch.delenv(ADAPTER.EQUITY_PAPER_ORDERS_ENV_VAR, raising=False)
    monkeypatch.setattr(
        "sys.argv",
        ["prog", "--input", str(input_path), "--asset-input", str(asset_path),
         "--output", str(output_path), "--submit-paper"],
    )
    rc = ADAPTER.main()
    result = json.loads(output_path.read_text(encoding="utf-8"))
    expect("submission blocked without env var: exit 1", rc == 1)
    expect("submission blocked without env var: FAIL_CLOSED", result["status"] == "FAIL_CLOSED")
    expect("submission blocked without env var: names the env var", ADAPTER.EQUITY_PAPER_ORDERS_ENV_VAR in result["error"])


def test_main_cli_submits_with_both_switches_enabled_using_fake_client(tmp_path, monkeypatch) -> None:
    spec = make_spec(asset_class="STOCK", direction="OPEN_LONG", symbol="AAPL")
    asset = make_asset(symbol="AAPL")
    input_path = tmp_path / "spec.json"
    asset_path = tmp_path / "asset.json"
    output_path = tmp_path / "out.json"
    _write_json(input_path, spec)
    _write_json(asset_path, asset)

    monkeypatch.setenv(ADAPTER.EQUITY_PAPER_ORDERS_ENV_VAR, "true")
    monkeypatch.setenv("ALPACA_PAPER_API_KEY", "fake-key")
    monkeypatch.setenv("ALPACA_PAPER_SECRET_KEY", "fake-secret")

    captured_paper_flag = {}

    def _fake_trading_client(key, secret, paper=False):
        captured_paper_flag["paper"] = paper
        return _FakeSubmittingClient(existing_order=None)

    monkeypatch.setattr(ADAPTER, "TradingClient", _fake_trading_client)
    monkeypatch.setattr(
        "sys.argv",
        ["prog", "--input", str(input_path), "--asset-input", str(asset_path),
         "--output", str(output_path), "--submit-paper"],
    )
    rc = ADAPTER.main()
    result = json.loads(output_path.read_text(encoding="utf-8"))
    expect("enabled submission: exit 0", rc == 0)
    expect("enabled submission: status SUBMITTED", result["status"] == "SUBMITTED")
    expect("enabled submission: TradingClient constructed with paper=True", captured_paper_flag["paper"] is True)
    expect("enabled submission: paper True, live False in result", result["paper"] is True and result["live"] is False)


def test_main_cli_fetch_live_asset_metadata_missing_credentials_fails_closed(tmp_path, monkeypatch) -> None:
    spec = make_spec(asset_class="STOCK", direction="OPEN_LONG", symbol="AAPL")
    input_path = tmp_path / "spec.json"
    output_path = tmp_path / "out.json"
    _write_json(input_path, spec)

    monkeypatch.delenv("ALPACA_PAPER_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_PAPER_SECRET_KEY", raising=False)
    monkeypatch.setattr(
        "sys.argv",
        ["prog", "--input", str(input_path), "--fetch-live-asset-metadata", "--output", str(output_path)],
    )
    rc = ADAPTER.main()
    result = json.loads(output_path.read_text(encoding="utf-8"))
    expect("missing credentials for live fetch: exit 1", rc == 1)
    expect("missing credentials for live fetch: FAIL_CLOSED", result["status"] == "FAIL_CLOSED")


def test_main_cli_requires_asset_input_or_fetch_flag(tmp_path, monkeypatch) -> None:
    spec = make_spec(asset_class="STOCK", direction="OPEN_LONG", symbol="AAPL")
    input_path = tmp_path / "spec.json"
    output_path = tmp_path / "out.json"
    _write_json(input_path, spec)

    monkeypatch.setattr("sys.argv", ["prog", "--input", str(input_path), "--output", str(output_path)])
    rc = ADAPTER.main()
    result = json.loads(output_path.read_text(encoding="utf-8"))
    expect("neither --asset-input nor --fetch-live-asset-metadata: exit 1", rc == 1)
    expect("neither flag supplied: FAIL_CLOSED", result["status"] == "FAIL_CLOSED")
