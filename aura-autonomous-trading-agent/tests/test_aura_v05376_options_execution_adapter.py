#!/usr/bin/env python3
"""Tests for AURA v0.5.3.76 -- Options Multi-Leg Execution Adapter (O4).

No live Alpaca order is required anywhere in this file -- every network-
capable function is exercised against a fake test-double client, exactly
the discipline `.335`'s own test suite already established. The real
alpaca-py 0.44.0 SDK objects (OptionLegRequest, LimitOrderRequest, the
enums) ARE constructed for real in the pure-layer tests -- only the
network boundary (TradingClient) is faked.
"""
from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aura_v05376_options_execution_adapter as A  # noqa: E402


# --------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------- #

def _legs(
    direction_a="OPEN_LONG",
    direction_b="OPEN_SHORT",
    strike_a="150",
    strike_b="160",
    right_a="CALL",
    right_b="CALL",
    expiry_a=date(2026, 12, 18),
    expiry_b=date(2026, 12, 18),
    ratio_a=1,
    ratio_b=1,
    underlying_a="AAPL",
    underlying_b="AAPL",
):
    return [
        {
            "underlying_symbol": underlying_a, "strike": strike_a, "expiry": expiry_a,
            "right": right_a, "direction": direction_a, "ratio": ratio_a,
        },
        {
            "underlying_symbol": underlying_b, "strike": strike_b, "expiry": expiry_b,
            "right": right_b, "direction": direction_b, "ratio": ratio_b,
        },
    ]


def _spec(legs=None, **overrides):
    spec = {
        "legs": legs if legs is not None else _legs(),
        "qty": 1,
        "order_type": "LIMIT",
        "limit_price": "2.50",
        "time_in_force": "DAY",
        "client_order_id": "test-client-order-id",
    }
    spec.update(overrides)
    return spec


def _fake_leg_order(symbol, status, filled_qty, filled_avg_price=None):
    return SimpleNamespace(symbol=symbol, status=status, filled_qty=filled_qty, filled_avg_price=filled_avg_price)


class FakeClient:
    """Test double for alpaca.trading.client.TradingClient -- exposes
    exactly the two methods this adapter calls, nothing else."""

    def __init__(self, existing_order=None, submit_response=None, raise_on_lookup=False):
        self._existing_order = existing_order
        self._submit_response = submit_response
        self._raise_on_lookup = raise_on_lookup
        self.submit_calls = []

    def get_order_by_client_id(self, client_order_id):
        if self._raise_on_lookup:
            raise RuntimeError("NOT_FOUND")
        return self._existing_order

    def submit_order(self, order_data):
        self.submit_calls.append(order_data)
        return self._submit_response


# ======================================================================
# validate_vertical_spec() -- the vertical-shape gate
# ======================================================================

def test_valid_vertical_spec_round_trips():
    validated = A.validate_vertical_spec(_spec())
    assert validated["structure"]["order_class"] == "MLEG"
    assert validated["structure"]["leg_count"] == 2
    assert validated["limit_price"] == Decimal("2.50")
    assert validated["qty"] == 1


def test_not_a_dict_fails_closed():
    with pytest.raises(RuntimeError, match="EXECUTION_SPEC_NOT_OBJECT"):
        A.validate_vertical_spec("not a dict")


@pytest.mark.parametrize("leg_count", [0, 1, 3, 4])
def test_wrong_leg_count_fails_closed(leg_count):
    legs = _legs() * 2  # 4 legs available to slice from
    spec = _spec(legs=legs[:leg_count])
    with pytest.raises(RuntimeError, match="V1_SCOPE_REQUIRES_EXACTLY_2_LEGS"):
        A.validate_vertical_spec(spec)


def test_invalid_direction_fails_closed():
    legs = _legs(direction_a="SIDEWAYS")
    with pytest.raises(RuntimeError, match="INVALID_LEG_DIRECTION"):
        A.validate_vertical_spec(_spec(legs=legs))


def test_mismatched_underlyings_fails_closed_via_o1():
    legs = _legs(underlying_b="MSFT")
    with pytest.raises(RuntimeError, match="STRUCTURE_SPANS_MULTIPLE_UNDERLYINGS"):
        A.validate_vertical_spec(_spec(legs=legs))


def test_mixed_rights_rejected():
    legs = _legs(right_b="PUT")
    with pytest.raises(RuntimeError, match="NOT_A_VERTICAL_MIXED_RIGHTS"):
        A.validate_vertical_spec(_spec(legs=legs))


def test_mixed_expiries_rejected():
    legs = _legs(expiry_b=date(2027, 1, 15))
    with pytest.raises(RuntimeError, match="NOT_A_VERTICAL_MIXED_EXPIRIES"):
        A.validate_vertical_spec(_spec(legs=legs))


def test_identical_strikes_rejected():
    legs = _legs(strike_b="150")
    with pytest.raises(RuntimeError, match="NOT_A_VERTICAL_IDENTICAL_STRIKES"):
        A.validate_vertical_spec(_spec(legs=legs))


def test_non_1_to_1_ratio_rejected():
    legs = _legs(ratio_b=2)
    with pytest.raises(RuntimeError, match="V1_SCOPE_REQUIRES_1_TO_1_RATIO"):
        A.validate_vertical_spec(_spec(legs=legs))


def test_both_legs_long_rejected():
    legs = _legs(direction_b="OPEN_LONG")
    with pytest.raises(RuntimeError, match="NOT_A_VERTICAL_REQUIRES_ONE_LONG_ONE_SHORT_LEG"):
        A.validate_vertical_spec(_spec(legs=legs))


def test_both_legs_short_rejected():
    legs = _legs(direction_a="OPEN_SHORT")
    with pytest.raises(RuntimeError, match="NOT_A_VERTICAL_REQUIRES_ONE_LONG_ONE_SHORT_LEG"):
        A.validate_vertical_spec(_spec(legs=legs))


def test_mixed_open_close_rejected():
    legs = _legs(direction_a="OPEN_LONG", direction_b="CLOSE_SHORT")
    with pytest.raises(RuntimeError, match="MIXED_OPEN_CLOSE_DIRECTIONS_IN_ONE_MLEG_ORDER"):
        A.validate_vertical_spec(_spec(legs=legs))


def test_closing_a_vertical_is_accepted():
    legs = _legs(direction_a="CLOSE_LONG", direction_b="CLOSE_SHORT")
    validated = A.validate_vertical_spec(_spec(legs=legs))
    assert validated["directions"] == ["CLOSE_LONG", "CLOSE_SHORT"]


def test_market_order_type_rejected():
    with pytest.raises(RuntimeError, match="V1_SCOPE_LIMIT_ORDERS_ONLY"):
        A.validate_vertical_spec(_spec(order_type="MARKET"))


def test_missing_limit_price_fails_closed():
    with pytest.raises(RuntimeError, match="INVALID_LIMIT_PRICE"):
        A.validate_vertical_spec(_spec(limit_price=None))


def test_nan_limit_price_fails_closed():
    with pytest.raises(RuntimeError, match="INVALID_LIMIT_PRICE"):
        A.validate_vertical_spec(_spec(limit_price="nan"))


def test_negative_limit_price_allowed_as_net_credit():
    validated = A.validate_vertical_spec(_spec(limit_price="-1.75"))
    assert validated["limit_price"] == Decimal("-1.75")


def test_zero_limit_price_allowed():
    validated = A.validate_vertical_spec(_spec(limit_price="0"))
    assert validated["limit_price"] == Decimal("0")


@pytest.mark.parametrize("bad_qty", [0, -1, 1.5, "1", True])
def test_invalid_qty_fails_closed(bad_qty):
    with pytest.raises(RuntimeError, match="INVALID_QTY"):
        A.validate_vertical_spec(_spec(qty=bad_qty))


def test_missing_client_order_id_fails_closed():
    with pytest.raises(RuntimeError, match="MISSING_CLIENT_ORDER_ID"):
        A.validate_vertical_spec(_spec(client_order_id="   "))


def test_invalid_time_in_force_fails_closed():
    with pytest.raises(RuntimeError, match="INVALID_TIME_IN_FORCE"):
        A.validate_vertical_spec(_spec(time_in_force="FOK"))


# ======================================================================
# build_mleg_order_request_spec() -- pure, deterministic, JSON-safe
# ======================================================================

def test_build_order_request_spec_is_json_serializable_and_deterministic():
    import json

    order_spec_1 = A.build_mleg_order_request_spec(_spec())
    order_spec_1.pop("built_at")
    order_spec_2 = A.build_mleg_order_request_spec(_spec())
    order_spec_2.pop("built_at")

    json.dumps(order_spec_1)  # must not raise
    assert order_spec_1 == order_spec_2


def test_build_order_request_spec_leg_fields():
    order_spec = A.build_mleg_order_request_spec(_spec())
    assert order_spec["order_class"] == "MLEG"
    assert order_spec["underlying_symbol"] == "AAPL"
    leg_a, leg_b = order_spec["legs"]
    assert leg_a["occ_symbol"] == "AAPL261218C00150000"
    assert leg_a["side"] == "buy"
    assert leg_a["position_intent"] == "buy_to_open"
    assert leg_b["occ_symbol"] == "AAPL261218C00160000"
    assert leg_b["side"] == "sell"
    assert leg_b["position_intent"] == "sell_to_open"


def test_build_order_request_spec_propagates_validation_failure():
    with pytest.raises(RuntimeError, match="V1_SCOPE_LIMIT_ORDERS_ONLY"):
        A.build_mleg_order_request_spec(_spec(order_type="MARKET"))


# ======================================================================
# to_alpaca_mleg_order_request() -- real SDK object construction
# ======================================================================

def test_to_alpaca_order_request_builds_real_mleg_request():
    from alpaca.trading.enums import OrderClass, OrderSide, OrderType, PositionIntent, TimeInForce
    from alpaca.trading.requests import LimitOrderRequest

    order_spec = A.build_mleg_order_request_spec(_spec())
    request = A.to_alpaca_mleg_order_request(order_spec)

    assert isinstance(request, LimitOrderRequest)
    assert request.order_class == OrderClass.MLEG
    assert request.type == OrderType.LIMIT
    assert request.time_in_force == TimeInForce.DAY
    assert request.limit_price == Decimal("2.50")
    assert request.qty == 1
    assert request.client_order_id == "test-client-order-id"
    # Confirmed by direct SDK introspection: no top-level symbol/side for
    # an MLEG order.
    assert request.symbol is None
    assert request.side is None
    assert len(request.legs) == 2
    assert request.legs[0].side == OrderSide.BUY
    assert request.legs[0].position_intent == PositionIntent.BUY_TO_OPEN
    assert request.legs[1].side == OrderSide.SELL
    assert request.legs[1].position_intent == PositionIntent.SELL_TO_OPEN


def test_to_alpaca_order_request_rejects_unknown_time_in_force():
    order_spec = A.build_mleg_order_request_spec(_spec())
    order_spec["time_in_force"] = "FOK"
    with pytest.raises(RuntimeError, match="INVALID_TIME_IN_FORCE"):
        A.to_alpaca_mleg_order_request(order_spec)


# ======================================================================
# classify_multileg_fill_status() -- the loud divergence-detection core
# ======================================================================

_EXPECTED_LEGS = [
    {"occ_symbol": "AAPL261218C00150000", "ratio_qty": 1},
    {"occ_symbol": "AAPL261218C00160000", "ratio_qty": 1},
]


def test_consistent_filled_classification():
    order = SimpleNamespace(status="filled", legs=[
        _fake_leg_order("AAPL261218C00150000", "filled", "1", "2.50"),
        _fake_leg_order("AAPL261218C00160000", "filled", "1", "0.30"),
    ])
    result = A.classify_multileg_fill_status(order, expected_legs=_EXPECTED_LEGS)
    assert result["classification"] == "CONSISTENT"
    assert result["status"] == "filled"
    assert result["requires_human_attention"] is False


def test_consistent_new_classification_without_expected_legs():
    order = SimpleNamespace(status="new", legs=[
        _fake_leg_order("AAPL261218C00150000", "new", "0"),
        _fake_leg_order("AAPL261218C00160000", "new", "0"),
    ])
    result = A.classify_multileg_fill_status(order)
    assert result["classification"] == "CONSISTENT"
    assert result["requires_human_attention"] is False


def test_missing_leg_data_is_loud_not_assumed_fine():
    order = SimpleNamespace(status="filled", legs=None)
    result = A.classify_multileg_fill_status(order, expected_legs=_EXPECTED_LEGS)
    assert result["classification"] == "MISSING_LEG_DATA"
    assert result["requires_human_attention"] is True


def test_empty_leg_list_is_also_missing_leg_data():
    order = SimpleNamespace(status="filled", legs=[])
    result = A.classify_multileg_fill_status(order, expected_legs=_EXPECTED_LEGS)
    assert result["classification"] == "MISSING_LEG_DATA"


def test_leg_count_mismatch_detected():
    order = SimpleNamespace(status="filled", legs=[
        _fake_leg_order("AAPL261218C00150000", "filled", "1", "2.50"),
    ])
    result = A.classify_multileg_fill_status(order, expected_legs=_EXPECTED_LEGS)
    assert result["classification"] == "LEG_COUNT_MISMATCH"
    assert result["requires_human_attention"] is True


def test_leg_status_divergence_never_folded_into_partially_filled():
    order = SimpleNamespace(status="partially_filled", legs=[
        _fake_leg_order("AAPL261218C00150000", "filled", "1", "2.50"),
        _fake_leg_order("AAPL261218C00160000", "new", "0"),
    ])
    result = A.classify_multileg_fill_status(order, expected_legs=_EXPECTED_LEGS)
    assert result["classification"] == "LEG_DIVERGENCE_DETECTED"
    assert result["requires_human_attention"] is True
    assert "status" not in result  # never silently assigns a merged status


def test_parent_leg_status_mismatch_detected():
    order = SimpleNamespace(status="new", legs=[
        _fake_leg_order("AAPL261218C00150000", "filled", "1", "2.50"),
        _fake_leg_order("AAPL261218C00160000", "filled", "1", "0.30"),
    ])
    result = A.classify_multileg_fill_status(order, expected_legs=_EXPECTED_LEGS)
    assert result["classification"] == "PARENT_LEG_STATUS_MISMATCH"
    assert result["requires_human_attention"] is True


def test_leg_fill_ratio_divergence_detected_even_when_statuses_match():
    order = SimpleNamespace(status="partially_filled", legs=[
        _fake_leg_order("AAPL261218C00150000", "partially_filled", "1", "2.50"),
        _fake_leg_order("AAPL261218C00160000", "partially_filled", "0.5", "0.30"),
    ])
    result = A.classify_multileg_fill_status(order, expected_legs=_EXPECTED_LEGS)
    assert result["classification"] == "LEG_FILL_RATIO_DIVERGENCE"
    assert result["requires_human_attention"] is True


def test_leg_fill_ratio_consistent_when_truly_proportional():
    order = SimpleNamespace(status="partially_filled", legs=[
        _fake_leg_order("AAPL261218C00150000", "partially_filled", "0.5", "2.50"),
        _fake_leg_order("AAPL261218C00160000", "partially_filled", "0.5", "0.30"),
    ])
    result = A.classify_multileg_fill_status(order, expected_legs=_EXPECTED_LEGS)
    assert result["classification"] == "CONSISTENT"
    assert result["status"] == "partially_filled"


def test_rejected_consistently_is_not_treated_as_divergence():
    order = SimpleNamespace(status="rejected", legs=[
        _fake_leg_order("AAPL261218C00150000", "rejected", "0"),
        _fake_leg_order("AAPL261218C00160000", "rejected", "0"),
    ])
    result = A.classify_multileg_fill_status(order, expected_legs=_EXPECTED_LEGS)
    assert result["classification"] == "CONSISTENT"
    assert result["status"] == "rejected"
    assert result["requires_human_attention"] is False


def test_classify_never_raises_on_malformed_order():
    order = SimpleNamespace()  # no status, no legs attribute at all
    result = A.classify_multileg_fill_status(order)
    assert result["classification"] == "MISSING_LEG_DATA"


# ======================================================================
# submit() -- the only network-capable new-order path
# ======================================================================

def test_submit_idempotency_short_circuits_on_existing_order():
    existing = SimpleNamespace(id="broker-123", status="filled", legs=[
        _fake_leg_order("AAPL261218C00150000", "filled", "1", "2.50"),
        _fake_leg_order("AAPL261218C00160000", "filled", "1", "0.30"),
    ])
    client = FakeClient(existing_order=existing)
    order_spec = A.build_mleg_order_request_spec(_spec())

    result = A.submit(order_spec, client)

    assert result["status"] == "ALREADY_EXISTS"
    assert result["broker_order_id"] == "broker-123"
    assert result["fill_classification"]["classification"] == "CONSISTENT"
    assert client.submit_calls == []  # never actually submitted again


def test_submit_submits_when_no_existing_order():
    new_order = SimpleNamespace(id="broker-456", status="new", legs=[
        _fake_leg_order("AAPL261218C00150000", "new", "0"),
        _fake_leg_order("AAPL261218C00160000", "new", "0"),
    ])
    client = FakeClient(existing_order=None, submit_response=new_order, raise_on_lookup=False)
    order_spec = A.build_mleg_order_request_spec(_spec())

    result = A.submit(order_spec, client)

    assert result["status"] == "SUBMITTED"
    assert result["broker_order_id"] == "broker-456"
    assert result["paper"] is True
    assert result["live"] is False
    assert len(client.submit_calls) == 1
    assert result["fill_classification"]["classification"] == "CONSISTENT"


def test_submit_submits_when_lookup_raises():
    new_order = SimpleNamespace(id="broker-789", status="filled", legs=[
        _fake_leg_order("AAPL261218C00150000", "filled", "1", "2.50"),
        _fake_leg_order("AAPL261218C00160000", "filled", "1", "0.30"),
    ])
    client = FakeClient(submit_response=new_order, raise_on_lookup=True)
    order_spec = A.build_mleg_order_request_spec(_spec())

    result = A.submit(order_spec, client)
    assert result["status"] == "SUBMITTED"


def test_submit_surfaces_leg_divergence_without_remediation():
    diverged_order = SimpleNamespace(id="broker-999", status="partially_filled", legs=[
        _fake_leg_order("AAPL261218C00150000", "filled", "1", "2.50"),
        _fake_leg_order("AAPL261218C00160000", "new", "0"),
    ])
    client = FakeClient(submit_response=diverged_order, raise_on_lookup=True)
    order_spec = A.build_mleg_order_request_spec(_spec())

    result = A.submit(order_spec, client)

    assert result["status"] == "SUBMITTED"  # submission itself succeeded
    assert result["fill_classification"]["classification"] == "LEG_DIVERGENCE_DETECTED"
    assert result["fill_classification"]["requires_human_attention"] is True
    # No cancel/replace/unwind call exists on FakeClient at all -- the
    # adapter has no mechanism to attempt remediation even if it wanted to.
    assert not hasattr(client, "cancel_order")


def test_submit_passes_real_sdk_request_object_to_client():
    from alpaca.trading.requests import LimitOrderRequest

    captured = {}

    class CapturingClient(FakeClient):
        def submit_order(self, order_data):
            captured["order_data"] = order_data
            return SimpleNamespace(id="broker-1", status="new", legs=[
                _fake_leg_order("AAPL261218C00150000", "new", "0"),
                _fake_leg_order("AAPL261218C00160000", "new", "0"),
            ])

    client = CapturingClient(existing_order=None, raise_on_lookup=False)
    order_spec = A.build_mleg_order_request_spec(_spec())
    A.submit(order_spec, client)

    assert isinstance(captured["order_data"], LimitOrderRequest)


# ======================================================================
# fetch_order_status() -- the read-only polling path
# ======================================================================

def test_fetch_order_status_polls_and_classifies():
    order = SimpleNamespace(id="broker-321", status="filled", legs=[
        _fake_leg_order("AAPL261218C00150000", "filled", "1", "2.50"),
        _fake_leg_order("AAPL261218C00160000", "filled", "1", "0.30"),
    ])
    client = FakeClient(existing_order=order)

    result = A.fetch_order_status(client, "some-client-order-id")

    assert result["broker_order_id"] == "broker-321"
    assert result["client_order_id"] == "some-client-order-id"
    assert result["fill_classification"]["classification"] == "CONSISTENT"


def test_fetch_order_status_surfaces_divergence_too():
    order = SimpleNamespace(id="broker-654", status="partially_filled", legs=[
        _fake_leg_order("AAPL261218C00150000", "filled", "1", "2.50"),
        _fake_leg_order("AAPL261218C00160000", "new", "0"),
    ])
    client = FakeClient(existing_order=order)

    result = A.fetch_order_status(client, "some-client-order-id")
    assert result["fill_classification"]["classification"] == "LEG_DIVERGENCE_DETECTED"


# ======================================================================
# Network-boundary disclosure -- same "real code, no live call" style
# test .335's own suite uses
# ======================================================================

def test_network_capable_functions_disclosure_is_complete():
    import ast

    source = Path(A.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    module_level_funcs = {
        node.name for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert A.NETWORK_CAPABLE_FUNCTIONS <= module_level_funcs

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name not in A.NETWORK_CAPABLE_FUNCTIONS:
            for inner in ast.walk(node):
                if isinstance(inner, ast.Name) and inner.id == "client":
                    pytest.fail(
                        f"{node.name} references `client` but is not in NETWORK_CAPABLE_FUNCTIONS"
                    )


def test_no_live_trading_client_constructed_outside_main():
    source = Path(A.__file__).read_text(encoding="utf-8")
    # TradingClient is only imported inside main() (lazy import), never at
    # module level -- confirms nothing can construct one as a side effect
    # of merely importing this module.
    assert "from alpaca.trading.client import TradingClient" not in source.split("def main()")[0]


def test_paper_only_env_var_name_is_distinct_from_siblings():
    assert A.OPTIONS_PAPER_ORDERS_ENV_VAR == "AURA_ALPACA_OPTIONS_PAPER_ORDERS_ENABLED"
    assert A.OPTIONS_PAPER_ORDERS_ENV_VAR != "AURA_ALPACA_EQUITY_PAPER_ORDERS_ENABLED"
    assert A.OPTIONS_PAPER_ORDERS_ENV_VAR != "AURA_ALPACA_PAPER_ORDERS_ENABLED"


# ======================================================================
# CLI main() -- validate-only path (no credentials/network required)
# ======================================================================

def test_main_validate_only_writes_validated_no_submission(tmp_path):
    import json

    input_spec = _spec()
    input_spec["legs"][0]["expiry"] = input_spec["legs"][0]["expiry"].isoformat()
    input_spec["legs"][1]["expiry"] = input_spec["legs"][1]["expiry"].isoformat()

    input_path = tmp_path / "spec.json"
    output_path = tmp_path / "out.json"
    input_path.write_text(json.dumps(input_spec), encoding="utf-8")

    argv = sys.argv
    sys.argv = ["aura_v05376", "--input", str(input_path), "--output", str(output_path)]
    try:
        exit_code = A.main()
    finally:
        sys.argv = argv

    assert exit_code == 0
    result = json.loads(output_path.read_text(encoding="utf-8"))
    assert result["status"] == "VALIDATED_NO_SUBMISSION"
    assert result["order_spec"]["order_class"] == "MLEG"


def test_main_submit_paper_without_env_var_fails_closed(tmp_path, monkeypatch):
    import json

    monkeypatch.delenv(A.OPTIONS_PAPER_ORDERS_ENV_VAR, raising=False)

    input_spec = _spec()
    input_spec["legs"][0]["expiry"] = input_spec["legs"][0]["expiry"].isoformat()
    input_spec["legs"][1]["expiry"] = input_spec["legs"][1]["expiry"].isoformat()

    input_path = tmp_path / "spec.json"
    output_path = tmp_path / "out.json"
    input_path.write_text(json.dumps(input_spec), encoding="utf-8")

    argv = sys.argv
    sys.argv = ["aura_v05376", "--input", str(input_path), "--output", str(output_path), "--submit-paper"]
    try:
        exit_code = A.main()
    finally:
        sys.argv = argv

    assert exit_code == 1
    result = json.loads(output_path.read_text(encoding="utf-8"))
    assert result["status"] == "FAIL_CLOSED"
    assert "OPTIONS_PAPER_ORDER_SUBMISSION_NOT_ENABLED" in result["error"]
