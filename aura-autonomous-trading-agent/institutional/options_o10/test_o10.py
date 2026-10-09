#!/usr/bin/env python3
"""Tests for O10 (`signal_mapping.py`, `execution_constraints.py`,
`position_sizing.py`), using `chain_data_feeds.SyntheticFixtureOptionsChain`.

Two tests (`test_credit_spec_passes_real_376_vertical_validation`,
`test_debit_spec_structurally_validates_but_flags_custom_exit_required`)
import and call the REAL staged `.376`
(`aura_v05376_options_execution_adapter.py`) `validate_vertical_spec()`
-- genuine interop proof, not a mock.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chain_data_feeds import SyntheticFixtureOptionsChain
from execution_constraints import evaluate_execution_constraints
from position_sizing import check_aggregate_options_exposure, compute_spread_quantity
from signal_mapping import StructureProposal, load_execution_adapter_module, map_decision_to_structure
from strategy_config import O10StrategyConfig

NOW = datetime(2026, 1, 2, 15, 0, 0)


@dataclass(frozen=True, slots=True)
class FakeTradingDecision:
    """Duck-typed stand-in for `.350.TradingDecision` -- only the
    attributes `signal_mapping.py` actually reads are present."""

    symbol: str
    direction: str
    outcome: str
    final_rank_score: float
    decision_threshold: float
    candidate_id: str = "TEST-CANDIDATE-1"


def make_chain(**overrides: Any) -> SyntheticFixtureOptionsChain:
    params: dict[str, Any] = {"spot_price": 100.0, "iv_guess": 0.30}
    params.update(overrides)
    return SyntheticFixtureOptionsChain(**params)


def make_config(**overrides: Any) -> O10StrategyConfig:
    return O10StrategyConfig(**overrides)


# ============================================================================
# signal_mapping.map_decision_to_structure
# ============================================================================


def test_bull_put_spread_for_moderate_confidence_long():
    decision = FakeTradingDecision(
        symbol="AAPL", direction="LONG_LEANING", outcome="DECIDE_LONG",
        final_rank_score=1.2, decision_threshold=1.0,
    )
    proposal = map_decision_to_structure(
        decision, chain=make_chain(), underlying_price=100.0, underlying_atr=2.0,
        now=NOW, config=make_config(),
    )
    assert proposal is not None
    assert proposal.structure_name == "BULL_PUT_SPREAD"
    assert proposal.is_debit is False
    assert proposal.requires_custom_exit_logic is False
    assert proposal.orders_enabled is True
    assert Decimal(proposal.spec["limit_price"]) < 0  # net credit, `.376` sign convention
    assert proposal.spec["legs"][0]["right"] == "PUT"
    assert proposal.spec["legs"][0]["direction"] == "OPEN_SHORT"
    assert proposal.spec["legs"][1]["direction"] == "OPEN_LONG"
    assert proposal.spec["qty"] == 1  # documented placeholder -- position_sizing's job to replace


def test_bull_call_spread_for_high_confidence_long_is_flagged_debit():
    decision = FakeTradingDecision(
        symbol="AAPL", direction="LONG_LEANING", outcome="DECIDE_LONG",
        final_rank_score=2.5, decision_threshold=1.0,
    )
    proposal = map_decision_to_structure(
        decision, chain=make_chain(), underlying_price=100.0, underlying_atr=2.0,
        now=NOW, config=make_config(),
    )
    assert proposal is not None
    assert proposal.structure_name == "BULL_CALL_SPREAD"
    assert proposal.is_debit is True
    assert proposal.requires_custom_exit_logic is True
    assert proposal.orders_enabled is False  # hard intercept, Martin's 2026-10-09 directive
    assert Decimal(proposal.spec["limit_price"]) > 0  # net DEBIT
    assert any(".380" in r and "cannot manage" in r for r in proposal.rationale)


def test_bear_call_spread_at_both_confidence_levels_with_different_aggressiveness():
    moderate = FakeTradingDecision(
        symbol="AAPL", direction="SHORT_LEANING", outcome="DECIDE_SHORT",
        final_rank_score=-1.2, decision_threshold=1.0,
    )
    high = FakeTradingDecision(
        symbol="AAPL", direction="SHORT_LEANING", outcome="DECIDE_SHORT",
        final_rank_score=-2.5, decision_threshold=1.0,
    )
    cfg = make_config()
    moderate_proposal = map_decision_to_structure(
        moderate, chain=make_chain(), underlying_price=100.0, underlying_atr=2.0, now=NOW, config=cfg,
    )
    high_proposal = map_decision_to_structure(
        high, chain=make_chain(), underlying_price=100.0, underlying_atr=2.0, now=NOW, config=cfg,
    )
    assert moderate_proposal is not None and high_proposal is not None
    assert moderate_proposal.structure_name == "BEAR_CALL_SPREAD"
    assert high_proposal.structure_name == "BEAR_CALL_SPREAD"
    assert moderate_proposal.is_debit is False and high_proposal.is_debit is False
    assert moderate_proposal.orders_enabled is True and high_proposal.orders_enabled is True
    assert Decimal(moderate_proposal.spec["limit_price"]) < 0
    assert Decimal(high_proposal.spec["limit_price"]) < 0

    moderate_short_strike = float(moderate_proposal.spec["legs"][0]["strike"])
    high_short_strike = float(high_proposal.spec["legs"][0]["strike"])
    # High confidence targets target_short_leg_delta_max (0.30) instead of
    # target_short_leg_delta_central (0.20) -- a higher-delta call strike
    # sits CLOSER to the money (lower strike), per the mapping table.
    assert high_short_strike < moderate_short_strike


def test_no_trade_and_abstain_return_none():
    no_trade = FakeTradingDecision(
        symbol="AAPL", direction="LONG_LEANING", outcome="NO_TRADE",
        final_rank_score=0.1, decision_threshold=1.0,
    )
    abstain = FakeTradingDecision(
        symbol="AAPL", direction="NO_DIRECTIONAL_EVIDENCE", outcome="ABSTAIN",
        final_rank_score=0.0, decision_threshold=1.0,
    )
    for decision in (no_trade, abstain):
        proposal = map_decision_to_structure(
            decision, chain=make_chain(), underlying_price=100.0, underlying_atr=2.0,
            now=NOW, config=make_config(),
        )
        assert proposal is None


def test_missing_delta_fails_closed_to_none_not_a_guess():
    decision = FakeTradingDecision(
        symbol="AAPL", direction="LONG_LEANING", outcome="DECIDE_LONG",
        final_rank_score=1.2, decision_threshold=1.0,
    )
    no_delta_chain = make_chain(include_delta=False)
    proposal = map_decision_to_structure(
        decision, chain=no_delta_chain, underlying_price=100.0, underlying_atr=2.0,
        now=NOW, config=make_config(),
    )
    assert proposal is None


def test_empty_chain_returns_none():
    class EmptyChain:
        DATA_SOURCE_LABEL = "EMPTY_TEST_FIXTURE"
        IS_REAL_MARKET_DATA = False

        def fetch_chain(self, underlying_symbol, *, dte_min, dte_max, now):
            return []

    decision = FakeTradingDecision(
        symbol="AAPL", direction="LONG_LEANING", outcome="DECIDE_LONG",
        final_rank_score=1.2, decision_threshold=1.0,
    )
    proposal = map_decision_to_structure(
        decision, chain=EmptyChain(), underlying_price=100.0, underlying_atr=2.0,
        now=NOW, config=make_config(),
    )
    assert proposal is None


# ============================================================================
# Real .376 interop -- genuine import + call, not mocked.
# ============================================================================


def test_credit_spec_passes_real_376_vertical_validation():
    adapter = load_execution_adapter_module()
    cases = (
        ("DECIDE_LONG", 1.2, "LONG_LEANING", "BULL_PUT_SPREAD"),
        ("DECIDE_SHORT", -1.2, "SHORT_LEANING", "BEAR_CALL_SPREAD"),
    )
    for outcome, score, direction, expected_name in cases:
        decision = FakeTradingDecision(
            symbol="MSFT", direction=direction, outcome=outcome,
            final_rank_score=score, decision_threshold=1.0,
        )
        proposal = map_decision_to_structure(
            decision, chain=make_chain(spot_price=300.0), underlying_price=300.0, underlying_atr=6.0,
            now=NOW, config=make_config(),
        )
        assert proposal is not None
        assert proposal.structure_name == expected_name
        validated = adapter.validate_vertical_spec(proposal.spec)  # raises RuntimeError on any violation
        assert validated["structure"]["leg_count"] == 2
        assert validated["structure"]["order_class"] == "MLEG"


def test_debit_spec_structurally_validates_but_flags_custom_exit_required():
    adapter = load_execution_adapter_module()
    decision = FakeTradingDecision(
        symbol="MSFT", direction="LONG_LEANING", outcome="DECIDE_LONG",
        final_rank_score=2.5, decision_threshold=1.0,
    )
    proposal = map_decision_to_structure(
        decision, chain=make_chain(spot_price=300.0), underlying_price=300.0, underlying_atr=6.0,
        now=NOW, config=make_config(),
    )
    assert proposal is not None
    assert proposal.structure_name == "BULL_CALL_SPREAD"

    # `.376.validate_vertical_spec()` only checks vertical SHAPE (2 legs,
    # same right/expiry, different strikes, one long + one short, 1:1
    # ratio) -- it has no concept of credit vs debit, so this is expected
    # to pass even though the position is a debit `.380` cannot exit:
    validated = adapter.validate_vertical_spec(proposal.spec)
    assert validated["structure"]["leg_count"] == 2

    # This is the real safety distinction, and it does NOT live in
    # `.376`'s spec-shape validity -- it lives here:
    assert proposal.requires_custom_exit_logic is True
    assert proposal.is_debit is True
    assert proposal.orders_enabled is False


# ============================================================================
# execution_constraints.evaluate_execution_constraints
# ============================================================================


class _FixedChain:
    DATA_SOURCE_LABEL = "TEST_FIXED_CHAIN"
    IS_REAL_MARKET_DATA = False

    def __init__(self, contracts: list[dict[str, Any]]):
        self._contracts = contracts

    def fetch_chain(self, underlying_symbol, *, dte_min, dte_max, now):
        return self._contracts


def _leg(direction: str, strike: float, expiry, right: str = "PUT") -> dict[str, Any]:
    return {
        "direction": direction, "underlying_symbol": "AAPL", "strike": str(strike),
        "expiry": expiry, "right": right, "ratio": 1, "multiplier": 100,
    }


def _proposal_with_legs(expiry, *, orders_enabled: bool = True) -> StructureProposal:
    spec = {
        "legs": [_leg("OPEN_SHORT", 100.0, expiry), _leg("OPEN_LONG", 95.0, expiry)],
        "order_type": "LIMIT", "limit_price": "-0.50", "qty": 1,
        "client_order_id": "TEST-CO-CONSTRAINTS", "time_in_force": "DAY",
    }
    return StructureProposal(
        structure_name="BULL_PUT_SPREAD", legs=[], spec=spec, rationale=(),
        is_debit=False, requires_custom_exit_logic=False, orders_enabled=orders_enabled,
    )


def _contract(strike, expiry, *, bid, ask, oi, vol, right="PUT"):
    return {
        "underlying_symbol": "AAPL", "expiry": expiry, "strike": strike, "right": right,
        "bid": bid, "ask": ask, "open_interest": oi, "volume": vol,
        "delta": -0.20 if right == "PUT" else 0.20,
    }


def test_execution_constraints_blocks_on_thin_open_interest():
    expiry = NOW.date() + timedelta(days=35)
    proposal = _proposal_with_legs(expiry)
    contracts = [
        _contract(100.0, expiry, bid=9.9, ask=10.1, oi=50, vol=50),  # short leg: OI below min(100)
        _contract(95.0, expiry, bid=4.9, ask=5.1, oi=500, vol=50),  # long leg: fine
    ]
    verdict = evaluate_execution_constraints(proposal, chain=_FixedChain(contracts), config=make_config())
    assert verdict.allowed is False
    assert any("OPEN_INTEREST_TOO_THIN" in r for r in verdict.reasons)
    assert not any("PRIOR_DAY_VOLUME_TOO_THIN" in r for r in verdict.reasons)
    assert not any("BID_ASK_SPREAD_TOO_WIDE" in r for r in verdict.reasons)


def test_execution_constraints_blocks_on_low_prior_day_volume():
    expiry = NOW.date() + timedelta(days=35)
    proposal = _proposal_with_legs(expiry)
    contracts = [
        _contract(100.0, expiry, bid=9.9, ask=10.1, oi=500, vol=5),  # short leg: volume below min(10)
        _contract(95.0, expiry, bid=4.9, ask=5.1, oi=500, vol=50),
    ]
    verdict = evaluate_execution_constraints(proposal, chain=_FixedChain(contracts), config=make_config())
    assert verdict.allowed is False
    assert any("PRIOR_DAY_VOLUME_TOO_THIN" in r for r in verdict.reasons)
    assert not any("OPEN_INTEREST_TOO_THIN" in r for r in verdict.reasons)
    assert not any("BID_ASK_SPREAD_TOO_WIDE" in r for r in verdict.reasons)


def test_execution_constraints_blocks_on_wide_spread_short_leg_tighter_bound():
    expiry = NOW.date() + timedelta(days=35)
    proposal = _proposal_with_legs(expiry)
    # Short leg spread = 0.8/10.0 = 8% of mid: exceeds the SHORT-leg bound
    # (5%) but would pass the generic long-leg bound (10%) -- this is the
    # case that specifically proves the tighter short-leg bound is the one
    # actually enforced, not the looser generic one.
    contracts = [
        _contract(100.0, expiry, bid=9.6, ask=10.4, oi=500, vol=50),  # short leg: 8% spread
        _contract(95.0, expiry, bid=4.9, ask=5.1, oi=500, vol=50),    # long leg: 2% spread, fine
    ]
    verdict = evaluate_execution_constraints(proposal, chain=_FixedChain(contracts), config=make_config())
    assert verdict.allowed is False
    assert any("BID_ASK_SPREAD_TOO_WIDE" in r for r in verdict.reasons)
    assert not any("OPEN_INTEREST_TOO_THIN" in r for r in verdict.reasons)
    assert not any("PRIOR_DAY_VOLUME_TOO_THIN" in r for r in verdict.reasons)


def test_execution_constraints_same_wide_spread_on_long_leg_passes_looser_bound():
    expiry = NOW.date() + timedelta(days=35)
    proposal = _proposal_with_legs(expiry)
    # Same 8% spread, but now on the LONG leg -- within the 10% long-leg
    # bound, so this must NOT block, demonstrating the bound really is
    # leg-role-dependent rather than a single flat number.
    contracts = [
        _contract(100.0, expiry, bid=9.9, ask=10.1, oi=500, vol=50),  # short leg: 2%, fine
        _contract(95.0, expiry, bid=4.8, ask=5.2, oi=500, vol=50),    # long leg: 0.4/5.0 = 8%, within 10% bound
    ]
    verdict = evaluate_execution_constraints(proposal, chain=_FixedChain(contracts), config=make_config())
    assert verdict.allowed is True
    assert verdict.reasons == ()


def test_execution_constraints_all_clear_baseline():
    expiry = NOW.date() + timedelta(days=35)
    proposal = _proposal_with_legs(expiry)
    contracts = [
        _contract(100.0, expiry, bid=9.9, ask=10.1, oi=500, vol=50),
        _contract(95.0, expiry, bid=4.9, ask=5.1, oi=500, vol=50),
    ]
    verdict = evaluate_execution_constraints(proposal, chain=_FixedChain(contracts), config=make_config())
    assert verdict.allowed is True
    assert verdict.reasons == ()


def test_execution_constraints_blocks_unconditionally_when_orders_disabled():
    # Hard intercept, Martin's 2026-10-09 directive: even a proposal with
    # perfect liquidity on every leg must be blocked when
    # orders_enabled=False (e.g. a bull call spread flagged
    # requires_custom_exit_logic=True) -- no liquidity number can
    # override this.
    expiry = NOW.date() + timedelta(days=35)
    proposal = _proposal_with_legs(expiry, orders_enabled=False)
    contracts = [
        _contract(100.0, expiry, bid=9.9, ask=10.1, oi=500, vol=50),
        _contract(95.0, expiry, bid=4.9, ask=5.1, oi=500, vol=50),
    ]
    verdict = evaluate_execution_constraints(proposal, chain=_FixedChain(contracts), config=make_config())
    assert verdict.allowed is False
    assert any("ORDERS_DISABLED" in r for r in verdict.reasons)
    assert verdict.evidence["orders_enabled"] is False
    # Liquidity checks still ran and found nothing else wrong -- this is
    # the ONLY reason, proving the intercept is independent of liquidity.
    assert len(verdict.reasons) == 1


def test_execution_constraints_blocks_on_disabled_orders_even_with_bad_liquidity_too():
    # Both problems present at once -- every reason is still collected,
    # matching this module's "never short-circuit" style.
    expiry = NOW.date() + timedelta(days=35)
    proposal = _proposal_with_legs(expiry, orders_enabled=False)
    contracts = [
        _contract(100.0, expiry, bid=9.9, ask=10.1, oi=50, vol=50),  # also thin OI
        _contract(95.0, expiry, bid=4.9, ask=5.1, oi=500, vol=50),
    ]
    verdict = evaluate_execution_constraints(proposal, chain=_FixedChain(contracts), config=make_config())
    assert verdict.allowed is False
    assert any("ORDERS_DISABLED" in r for r in verdict.reasons)
    assert any("OPEN_INTEREST_TOO_THIN" in r for r in verdict.reasons)


# ============================================================================
# position_sizing
# ============================================================================


def _sizing_proposal(limit_price: str, strikes=(100.0, 95.0), is_debit: bool = False) -> StructureProposal:
    expiry = NOW.date() + timedelta(days=35)
    short_direction = "OPEN_LONG" if is_debit else "OPEN_SHORT"
    long_direction = "OPEN_SHORT" if is_debit else "OPEN_LONG"
    legs = [
        _leg(short_direction, strikes[0], expiry),
        _leg(long_direction, strikes[1], expiry),
    ]
    spec = {
        "legs": legs, "order_type": "LIMIT", "limit_price": limit_price, "qty": 1,
        "client_order_id": "TEST-CO-SIZING", "time_in_force": "DAY",
    }
    return StructureProposal(
        structure_name="TEST", legs=[], spec=spec, rationale=(),
        is_debit=is_debit, requires_custom_exit_logic=is_debit, orders_enabled=not is_debit,
    )


def test_position_sizing_zero_when_risk_per_contract_exceeds_budget():
    # width=5, credit=1.00 -> max_loss_per_contract = (5-1)*100 = $400.
    # 1% of a $1,000 account = $10 budget -- one contract already exceeds it.
    proposal = _sizing_proposal(limit_price="-1.00")
    qty = compute_spread_quantity(proposal, account_equity_usd=1_000.0, config=make_config())
    assert qty == 0


def test_position_sizing_positive_integer_when_budget_allows():
    proposal = _sizing_proposal(limit_price="-1.00")
    qty = compute_spread_quantity(proposal, account_equity_usd=1_000_000.0, config=make_config())
    assert isinstance(qty, int)
    assert qty == 25  # budget=$10,000 / $400 per contract


def test_position_sizing_debit_structure_uses_debit_paid_as_max_loss():
    # Debit structure: max loss per contract = debit paid * 100 = $2.00*100=$200.
    proposal = _sizing_proposal(limit_price="2.00", is_debit=True)
    qty = compute_spread_quantity(proposal, account_equity_usd=1_000_000.0, config=make_config())
    assert qty == 50  # budget=$10,000 / $200 per contract


def test_aggregate_options_exposure_cap():
    cfg = make_config()
    # 9% of equity -- within the 10% cap.
    assert check_aggregate_options_exposure(
        8_000.0, 1_000.0, account_equity_usd=100_000.0, config=cfg,
    ) is True
    # 10.5% of equity -- breaches the 10% cap.
    assert check_aggregate_options_exposure(
        9_500.0, 1_000.0, account_equity_usd=100_000.0, config=cfg,
    ) is False
