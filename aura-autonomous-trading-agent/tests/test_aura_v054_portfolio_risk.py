"""AURA v0.5.4 tests -- Categories H, I, J: portfolio risk controls."""

from __future__ import annotations

import pytest

import aura_v054_portfolio_risk as RISK


def _pos(symbol, market_value, planned_risk_dollars, entry_bar_index=0):
    return RISK.PlannedPositionRisk(
        symbol=symbol, entry_bar_index=entry_bar_index, quantity=1, entry_price=market_value,
        planned_stop_distance=1.0, planned_risk_dollars=planned_risk_dollars, market_value=market_value,
    )


def test_default_limits_match_frozen_baseline():
    limits = RISK.PortfolioRiskLimits()
    assert limits.max_positions == 10
    assert limits.max_gross_exposure == 1.00
    assert limits.max_portfolio_risk == 0.02


def test_no_leverage_enforced_at_construction():
    with pytest.raises(RISK.PortfolioRiskError):
        RISK.PortfolioRiskLimits(max_gross_exposure=1.5)


def test_category_h_max_positions_limit():
    limits = RISK.PortfolioRiskLimits(max_positions=10, max_gross_exposure=1.0, max_portfolio_risk=1.0)
    ledger = RISK.PlannedRiskLedger()
    equity = 1_000_000.0
    for i in range(10):
        symbol = f"SYM{i}"
        verdict = RISK.evaluate_candidate_position(
            ledger=ledger, limits=limits, equity=equity, candidate_market_value=1.0, candidate_planned_risk_dollars=1.0,
        )
        assert verdict.allowed, verdict.reasons
        ledger.open_position(_pos(symbol, 1.0, 1.0))
    # 11th position must be rejected on the position-count limit.
    verdict = RISK.evaluate_candidate_position(
        ledger=ledger, limits=limits, equity=equity, candidate_market_value=1.0, candidate_planned_risk_dollars=1.0,
    )
    assert not verdict.allowed
    assert any(r.startswith("MAX_POSITIONS_EXCEEDED") for r in verdict.reasons)
    assert ledger.open_count == 10


def test_category_i_max_gross_exposure_limit():
    limits = RISK.PortfolioRiskLimits(max_positions=100, max_gross_exposure=1.00, max_portfolio_risk=1.0)
    ledger = RISK.PlannedRiskLedger()
    equity = 100_000.0
    ledger.open_position(_pos("A", 90_000.0, 1.0))
    # Adding another 20,000 would push gross exposure to 110% > 100%.
    verdict = RISK.evaluate_candidate_position(
        ledger=ledger, limits=limits, equity=equity, candidate_market_value=20_000.0, candidate_planned_risk_dollars=1.0,
    )
    assert not verdict.allowed
    assert any(r.startswith("MAX_GROSS_EXPOSURE_EXCEEDED") for r in verdict.reasons)
    assert verdict.projected_gross_exposure == pytest.approx(1.10)


def test_category_j_max_portfolio_risk_limit():
    limits = RISK.PortfolioRiskLimits(max_positions=100, max_gross_exposure=1.0, max_portfolio_risk=0.02)
    ledger = RISK.PlannedRiskLedger()
    equity = 100_000.0
    ledger.open_position(_pos("A", 1.0, 1_500.0))  # 1.5% already at risk
    # Adding 600 more dollars of risk pushes total to 2.1% > 2%.
    verdict = RISK.evaluate_candidate_position(
        ledger=ledger, limits=limits, equity=equity, candidate_market_value=1.0, candidate_planned_risk_dollars=600.0,
    )
    assert not verdict.allowed
    assert any(r.startswith("MAX_PORTFOLIO_RISK_EXCEEDED") for r in verdict.reasons)
    assert verdict.projected_portfolio_risk == pytest.approx(0.021)


def test_all_three_limits_can_pass_simultaneously():
    limits = RISK.PortfolioRiskLimits()
    ledger = RISK.PlannedRiskLedger()
    equity = 100_000.0
    verdict = RISK.evaluate_candidate_position(
        ledger=ledger, limits=limits, equity=equity, candidate_market_value=5_000.0, candidate_planned_risk_dollars=500.0,
    )
    assert verdict.allowed
    assert verdict.reasons == ()


def test_ledger_rejects_duplicate_open_and_close_of_unopened():
    ledger = RISK.PlannedRiskLedger()
    ledger.open_position(_pos("A", 1.0, 1.0))
    with pytest.raises(RISK.PortfolioRiskError):
        ledger.open_position(_pos("A", 2.0, 2.0))
    ledger.close_position("A")
    with pytest.raises(RISK.PortfolioRiskError):
        ledger.close_position("A")
