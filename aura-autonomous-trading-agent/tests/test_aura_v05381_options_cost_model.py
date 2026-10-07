#!/usr/bin/env python3
"""Tests for AURA v0.5.3.81 -- Options Cost Model (O7).

No live Alpaca call anywhere in this file.
"""
from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aura_v05381_options_cost_model as COST  # noqa: E402

SHORT_OCC = "SPY261106C00450000"
LONG_OCC = "SPY261106C00455000"

LEGS = [
    {"occ_symbol": SHORT_OCC, "side": "SELL"},
    {"occ_symbol": LONG_OCC, "side": "BUY"},
]

VALID_CONFIG = {
    "commission_per_contract": "0.00",
    "regulatory_fee_per_contract_buy": "0.02",
    "regulatory_fee_per_contract_sell": "0.07",
}


def record(bid: str, ask: str, status: str = "OK") -> dict:
    return {"data_status": status, "broker_latest_quote": {"bid_price": bid, "ask_price": ask}}


def quotes(short=("1.53", "1.57"), long=("0.04", "0.06"), short_status="OK", long_status="OK") -> dict:
    return {
        SHORT_OCC: record(*short, status=short_status),
        LONG_OCC: record(*long, status=long_status),
    }


# ======================================================================
# validate_cost_config() -- fails closed, no built-in defaults
# ======================================================================

def test_validate_cost_config_rejects_non_dict():
    with pytest.raises(RuntimeError, match="INVALID_COST_CONFIG"):
        COST.validate_cost_config("nope")


def test_validate_cost_config_rejects_missing_keys():
    with pytest.raises(RuntimeError, match="MISSING_REQUIRED_CONFIG_KEYS"):
        COST.validate_cost_config({"commission_per_contract": "0.00"})


def test_validate_cost_config_rejects_negative_rate():
    bad = dict(VALID_CONFIG, commission_per_contract="-0.01")
    with pytest.raises(RuntimeError, match="NEGATIVE_COST_RATE"):
        COST.validate_cost_config(bad)


def test_validate_cost_config_rejects_non_numeric_rate():
    bad = dict(VALID_CONFIG, commission_per_contract="not-a-number")
    with pytest.raises(RuntimeError, match="INVALID_DECIMAL"):
        COST.validate_cost_config(bad)


def test_validate_cost_config_happy_path_returns_decimals():
    rates = COST.validate_cost_config(VALID_CONFIG)
    assert rates["commission_per_contract"] == Decimal("0.00")
    assert rates["regulatory_fee_per_contract_buy"] == Decimal("0.02")
    assert rates["regulatory_fee_per_contract_sell"] == Decimal("0.07")


def test_module_has_no_default_cost_config_with_real_numbers():
    """Martin's confirmed scope (2026-10-07): no built-in fee defaults
    anywhere in this module."""
    assert not hasattr(COST, "DEFAULT_COST_CONFIG")


# ======================================================================
# leg_round_trip_spread_cost() -- full bid-ask width, direction-independent
# ======================================================================

def test_leg_spread_cost_is_full_width_times_multiplier_times_qty():
    r = COST.leg_round_trip_spread_cost(record("1.00", "1.10"), qty=3)
    assert r["status"] == "OK"
    assert r["spread_cost"] == Decimal("0.10") * 100 * 3  # (1.10-1.00)*100*3 = 30.00


def test_leg_spread_cost_fails_closed_on_missing_record():
    r = COST.leg_round_trip_spread_cost(None, qty=1)
    assert r["status"] == "MISSING_QUOTE_RECORD"
    assert r["spread_cost"] is None


def test_leg_spread_cost_fails_closed_on_non_ok_status():
    r = COST.leg_round_trip_spread_cost(record("1.00", "1.10", status="STALE_QUOTE"), qty=1)
    assert r["status"] == "STALE_QUOTE"
    assert r["spread_cost"] is None


def test_leg_spread_cost_fails_closed_on_crossed_quote():
    r = COST.leg_round_trip_spread_cost(record("1.10", "1.00"), qty=1)
    assert r["status"] == "INVALID_QUOTE"


def test_leg_spread_cost_rejects_invalid_qty():
    with pytest.raises(RuntimeError, match="INVALID_QTY"):
        COST.leg_round_trip_spread_cost(record("1.00", "1.10"), qty=0)
    with pytest.raises(RuntimeError, match="INVALID_QTY"):
        COST.leg_round_trip_spread_cost(record("1.00", "1.10"), qty=-1)


def test_leg_spread_cost_direction_independent():
    """Same quote, same qty -- cost must be identical whether this leg
    is conceptually a BUY or SELL leg, since `side` isn't even an input
    (see module docstring: round-trip spread cost = ask - bid, always)."""
    r1 = COST.leg_round_trip_spread_cost(record("1.00", "1.10"), qty=1)
    r2 = COST.leg_round_trip_spread_cost(record("1.00", "1.10"), qty=1)
    assert r1["spread_cost"] == r2["spread_cost"]


def test_leg_spread_cost_custom_multiplier():
    r = COST.leg_round_trip_spread_cost(record("1.00", "1.10"), qty=1, multiplier=Decimal("10"))
    assert r["spread_cost"] == Decimal("1.00")  # (1.10-1.00)*10*1


# ======================================================================
# leg_round_trip_fee_cost() -- buy+sell symmetry regardless of open side
# ======================================================================

def test_leg_fee_cost_same_for_buy_and_sell_opening_side():
    rates = COST.validate_cost_config(VALID_CONFIG)
    buy_cost = COST.leg_round_trip_fee_cost("BUY", qty=1, rates=rates)
    sell_cost = COST.leg_round_trip_fee_cost("SELL", qty=1, rates=rates)
    assert buy_cost == sell_cost  # round trip always incurs one of each regardless of direction


def test_leg_fee_cost_includes_commission_both_sides():
    rates = COST.validate_cost_config({
        "commission_per_contract": "0.65",
        "regulatory_fee_per_contract_buy": "0.00",
        "regulatory_fee_per_contract_sell": "0.00",
    })
    cost = COST.leg_round_trip_fee_cost("BUY", qty=1, rates=rates)
    assert cost == Decimal("1.30")  # 0.65 open + 0.65 close


def test_leg_fee_cost_scales_with_qty():
    rates = COST.validate_cost_config(VALID_CONFIG)
    cost_1 = COST.leg_round_trip_fee_cost("BUY", qty=1, rates=rates)
    cost_5 = COST.leg_round_trip_fee_cost("BUY", qty=5, rates=rates)
    assert cost_5 == cost_1 * 5


def test_leg_fee_cost_rejects_invalid_side():
    rates = COST.validate_cost_config(VALID_CONFIG)
    with pytest.raises(RuntimeError, match="INVALID_LEG_SIDE"):
        COST.leg_round_trip_fee_cost("SHORT", qty=1, rates=rates)


def test_leg_fee_cost_rejects_invalid_qty():
    rates = COST.validate_cost_config(VALID_CONFIG)
    with pytest.raises(RuntimeError, match="INVALID_QTY"):
        COST.leg_round_trip_fee_cost("BUY", qty=0, rates=rates)


# ======================================================================
# estimate_round_trip_cost() -- the one entry point
# ======================================================================

def test_estimate_round_trip_cost_happy_path():
    r = COST.estimate_round_trip_cost(LEGS, quotes(), qty=2, config=VALID_CONFIG)
    assert r["status"] == "OK"
    assert r["spread_cost"] == "12.00"  # (0.04*100*2)+(0.02*100*2) = 8+4
    assert r["fee_cost"] == "0.36"      # 2 legs * 2 qty * (0+0.02+0+0.07)
    assert r["total_cost"] == "12.36"


def test_estimate_round_trip_cost_single_leg():
    r = COST.estimate_round_trip_cost([LEGS[0]], {SHORT_OCC: record("1.53", "1.57")}, qty=1, config=VALID_CONFIG)
    assert r["status"] == "OK"
    assert len(r["legs"]) == 1


def test_estimate_round_trip_cost_rejects_empty_legs():
    with pytest.raises(RuntimeError, match="INVALID_LEGS"):
        COST.estimate_round_trip_cost([], quotes(), qty=1, config=VALID_CONFIG)


def test_estimate_round_trip_cost_rejects_non_list_legs():
    with pytest.raises(RuntimeError, match="INVALID_LEGS"):
        COST.estimate_round_trip_cost("nope", quotes(), qty=1, config=VALID_CONFIG)


def test_estimate_round_trip_cost_rejects_missing_occ_symbol():
    bad_legs = [{"side": "BUY"}]
    with pytest.raises(RuntimeError, match="MISSING_OCC_SYMBOL"):
        COST.estimate_round_trip_cost(bad_legs, quotes(), qty=1, config=VALID_CONFIG)


def test_estimate_round_trip_cost_rejects_invalid_side():
    bad_legs = [{"occ_symbol": SHORT_OCC, "side": "LONG"}]
    with pytest.raises(RuntimeError, match="INVALID_LEG_SIDE"):
        COST.estimate_round_trip_cost(bad_legs, quotes(), qty=1, config=VALID_CONFIG)


def test_estimate_round_trip_cost_rejects_invalid_config():
    with pytest.raises(RuntimeError, match="MISSING_REQUIRED_CONFIG_KEYS"):
        COST.estimate_round_trip_cost(LEGS, quotes(), qty=1, config={})


def test_estimate_round_trip_cost_fee_always_computable_even_with_bad_quote():
    """Martin's confirmed design: the two cost components fail
    independently -- a bad quote on one leg must never silently zero
    out or block the fee component, which doesn't depend on quotes."""
    bad = quotes(short_status="STALE_QUOTE")
    r = COST.estimate_round_trip_cost(LEGS, bad, qty=2, config=VALID_CONFIG)
    assert r["status"] == "QUOTE_NOT_USABLE"
    assert r["spread_cost"] is None
    assert r["total_cost"] is None
    assert r["fee_cost"] == "0.36"  # unaffected by the bad quote


def test_estimate_round_trip_cost_per_leg_breakdown_shape():
    r = COST.estimate_round_trip_cost(LEGS, quotes(), qty=1, config=VALID_CONFIG)
    assert len(r["legs"]) == 2
    occ_symbols = {leg["occ_symbol"] for leg in r["legs"]}
    assert occ_symbols == {SHORT_OCC, LONG_OCC}
    for leg in r["legs"]:
        assert leg["quote_status"] == "OK"
        assert leg["spread_cost"] is not None
        assert leg["fee_cost"] is not None


def test_estimate_round_trip_cost_missing_leg_quote_treated_as_unusable():
    r = COST.estimate_round_trip_cost(LEGS, {SHORT_OCC: record("1.53", "1.57")}, qty=1, config=VALID_CONFIG)
    assert r["status"] == "QUOTE_NOT_USABLE"
    missing_leg = next(leg for leg in r["legs"] if leg["occ_symbol"] == LONG_OCC)
    assert missing_leg["quote_status"] == "MISSING_QUOTE_RECORD"


def test_estimate_round_trip_cost_output_is_json_serializable():
    import json
    r = COST.estimate_round_trip_cost(LEGS, quotes(), qty=1, config=VALID_CONFIG)
    json.dumps(r)


# ======================================================================
# Structural / disclosure properties
# ======================================================================

def test_network_capable_functions_is_empty():
    assert COST.NETWORK_CAPABLE_FUNCTIONS == frozenset()


def test_module_has_no_live_credentials_or_network_imports():
    source = Path(COST.__file__).read_text(encoding="utf-8")
    for token in ("requests.", "urlopen", "TradingClient(", "OptionHistoricalDataClient(", "StockHistoricalDataClient("):
        assert token not in source, f"unexpected live/network construction token found: {token}"
