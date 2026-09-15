"""AURA v0.5.4 tests -- Categories G, O: ATR risk-based position sizing."""

from __future__ import annotations

import math

import pytest

import aura_v054_position_sizing as SIZING


def test_category_g_half_percent_risk_sizing_basic():
    result = SIZING.size_position_by_atr_risk(
        equity=100_000.0, entry_price=50.0, planned_stop_distance=10.0,
    )
    assert SIZING.RISK_FRACTION_PER_TRADE == 0.005
    assert result.risk_capital == pytest.approx(500.0)  # 0.5% of 100,000
    assert result.quantity == 50  # floor(500 / 10)
    assert result.planned_risk_dollars == pytest.approx(500.0)
    assert result.market_value == pytest.approx(2500.0)


def test_sizing_floors_and_never_exceeds_risk_budget():
    result = SIZING.size_position_by_atr_risk(equity=10_000.0, entry_price=20.0, planned_stop_distance=3.0)
    risk_capital = 10_000.0 * 0.005  # 50.0
    assert result.quantity == int(risk_capital // 3.0)
    assert result.planned_risk_dollars <= risk_capital


def test_sizing_returns_zero_quantity_when_distance_exceeds_risk_budget():
    result = SIZING.size_position_by_atr_risk(equity=1_000.0, entry_price=50.0, planned_stop_distance=1_000_000.0)
    assert result.quantity == 0
    assert result.market_value == 0.0


def test_category_o_fails_safe_on_missing_or_invalid_stop_distance():
    with pytest.raises(SIZING.PositionSizingError):
        SIZING.size_position_by_atr_risk(equity=100_000.0, entry_price=50.0, planned_stop_distance=None)
    with pytest.raises(SIZING.PositionSizingError):
        SIZING.size_position_by_atr_risk(equity=100_000.0, entry_price=50.0, planned_stop_distance=0.0)
    with pytest.raises(SIZING.PositionSizingError):
        SIZING.size_position_by_atr_risk(equity=100_000.0, entry_price=50.0, planned_stop_distance=-5.0)
    with pytest.raises(SIZING.PositionSizingError):
        SIZING.size_position_by_atr_risk(equity=100_000.0, entry_price=50.0, planned_stop_distance=float("nan"))


def test_category_o_fails_safe_on_invalid_equity_or_price():
    with pytest.raises(SIZING.PositionSizingError):
        SIZING.size_position_by_atr_risk(equity=0.0, entry_price=50.0, planned_stop_distance=1.0)
    with pytest.raises(SIZING.PositionSizingError):
        SIZING.size_position_by_atr_risk(equity=100_000.0, entry_price=0.0, planned_stop_distance=1.0)
    with pytest.raises(SIZING.PositionSizingError):
        SIZING.size_position_by_atr_risk(equity=100_000.0, entry_price=50.0, planned_stop_distance=1.0, risk_fraction_per_trade=0.0)


def test_sizing_is_deterministic():
    r1 = SIZING.size_position_by_atr_risk(equity=55_000.0, entry_price=33.0, planned_stop_distance=4.5)
    r2 = SIZING.size_position_by_atr_risk(equity=55_000.0, entry_price=33.0, planned_stop_distance=4.5)
    assert r1 == r2
