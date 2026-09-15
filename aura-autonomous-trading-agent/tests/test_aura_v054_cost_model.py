"""AURA v0.5.4 tests -- Category K: 0.10% round-trip cost model."""

from __future__ import annotations

import pytest

import aura_v054_cost_model as COST


def test_default_cost_pct_is_ten_basis_points_round_trip():
    assert COST.COST_PCT_ROUND_TRIP == 0.001
    model = COST.FlatRoundTripCostModel()
    assert model.cost_pct == 0.001


def test_apply_subtracts_cost_once():
    model = COST.FlatRoundTripCostModel(cost_pct=0.001)
    assert model.apply(0.05) == pytest.approx(0.049)
    assert model.apply(-0.02) == pytest.approx(-0.021)


def test_negative_cost_pct_rejected():
    with pytest.raises(COST.CostModelError):
        COST.FlatRoundTripCostModel(cost_pct=-0.001)


def test_cost_model_satisfies_protocol():
    model = COST.FlatRoundTripCostModel()
    assert isinstance(model, COST.CostModel)
