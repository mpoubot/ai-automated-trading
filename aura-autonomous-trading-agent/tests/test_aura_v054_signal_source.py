"""AURA v0.5.4 tests -- entry-signal sources.

Proves, rather than merely asserting in prose, the critical finding
documented in `aura_v054_signal_source.py`'s module docstring: under the
currently frozen `.50` weights (`technical_weight=0.0`,
`short_technical_weight=0.0`) with no sentiment/wave evidence source
wired, the real frozen decision-engine signal source ALWAYS returns
ABSTAIN / NO_DIRECTIONAL_EVIDENCE, for any technical regime.
"""

from __future__ import annotations

from datetime import datetime, timezone

import aura_v054_data_interface as DATA
import aura_v054_signal_source as SIG


def test_frozen_decision_engine_always_abstains_under_current_weights():
    provider = DATA.SyntheticBarsProvider(n_days=120)
    bars = provider.get_daily_bars("AAPL")
    src = SIG.AuraFrozenDecisionEngineSignalSource()
    now = bars["timestamp"].iloc[-1].to_pydatetime()

    decision = src.decide_for_symbol("AAPL", bars, now=now)

    assert decision.direction == "NO_DIRECTIONAL_EVIDENCE"
    assert decision.outcome == "ABSTAIN"
    assert decision.base_rank_score == 0.0
    assert decision.final_rank_score == 0.0


def test_frozen_decision_engine_abstains_across_many_symbols_and_bars():
    provider = DATA.SyntheticBarsProvider(n_days=150)
    for symbol in ("AAPL", "MSFT", "GOOGL", "AMZN"):
        bars = provider.get_daily_bars(symbol)
        src = SIG.AuraFrozenDecisionEngineSignalSource()
        for i in (60, 90, 120, 149):
            slice_df = bars.iloc[: i + 1]
            now = slice_df["timestamp"].iloc[-1].to_pydatetime()
            decision = src.decide_for_symbol(symbol, slice_df, now=now)
            assert decision.outcome == "ABSTAIN", f"{symbol}@{i} unexpectedly produced {decision.outcome}"


def test_frozen_decision_engine_is_deterministic():
    provider = DATA.SyntheticBarsProvider(n_days=120)
    bars = provider.get_daily_bars("AAPL")
    now = bars["timestamp"].iloc[-1].to_pydatetime()
    d1 = SIG.AuraFrozenDecisionEngineSignalSource().decide_for_symbol("AAPL", bars, now=now)
    d2 = SIG.AuraFrozenDecisionEngineSignalSource().decide_for_symbol("AAPL", bars, now=now)
    assert d1.direction == d2.direction
    assert d1.outcome == d2.outcome
    assert d1.final_rank_score == d2.final_rank_score
    assert d1.decision_hash == d2.decision_hash


def test_synthetic_test_fixture_source_is_labeled_not_a_strategy():
    src = SIG.SyntheticTestFixtureSignalSource()
    assert src.SIGNAL_SOURCE_LABEL == "SYNTHETIC_TEST_FIXTURE_RULE"


def test_synthetic_test_fixture_source_produces_long_decisions_sometimes():
    provider = DATA.SyntheticBarsProvider(n_days=150)
    bars = provider.get_daily_bars("AAPL")
    src = SIG.SyntheticTestFixtureSignalSource()
    outcomes = set()
    for i in range(30, 150):
        slice_df = bars.iloc[: i + 1]
        decision = src.decide_for_symbol("AAPL", slice_df, now=slice_df["timestamp"].iloc[-1])
        outcomes.add(decision["outcome"])
    # A momentum rule against an oscillating synthetic series must
    # produce at least one LONG signal across 120 evaluation points.
    assert "DECIDE_LONG" in outcomes
