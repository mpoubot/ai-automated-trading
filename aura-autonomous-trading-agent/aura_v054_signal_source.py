#!/usr/bin/env python3
"""
AURA v0.5.4 -- Entry-signal source (pluggable interface into the backtest runner)

Two implementations:

1. `AuraFrozenDecisionEngineSignalSource` -- the REAL integration: wires
   `.51`'s `build_technical_regime_from_bars`, `.52`'s `build_short_
   technical_regime_from_bars`, and `.50`'s `decide()` (via
   `aura_v054_llm_stub.NeutralDeterministicLLMClient`, Martin's approved
   neutral-stub resolution to the determinism/AI-proposal conflict)
   together exactly as frozen. Uses the SAME parameter values already
   frozen and approved in this project
   (`AURA_v0.53_Frozen_Candidate_Freeze_Record_2026-09-15.md`,
   `tests/test_aura_v05350_decision_engine.py`'s `DECIDE_KWARGS`,
   `tests/test_aura_v05351_...py`'s `TEST_PARAMS`,
   `tests/test_aura_v05352_...py`'s `TEST_PARAMS`) -- not re-derived,
   not re-selected here.

   *** CRITICAL, PROVEN CONSEQUENCE OF THE FROZEN PARAMETERS ***
   `DECIDE_KWARGS` freezes `technical_weight=0.0` and
   `short_technical_weight=0.0` (already flagged in
   `AURA_v0.53_Frozen_Candidate_Freeze_Record_2026-09-15.md`'s internal-
   consistency check as ".51/.52 currently mathematically inert in the
   decision score"). This module supplies NO `sentiment_regime` or
   `wave_result` (`.47`/`.48`'s evidence-construction pipeline is out of
   scope for .54 -- Martin's spec covers ATR/exit/sizing/portfolio-risk/
   cost/data, not a `.46`-`.49` sentiment/wave rebuild), so
   `sentiment_usable`/`wave_usable` are always `False` too. Reading
   `compute_base_rank_score` (`aura_v05350_decision_engine.py:599-637`)
   directly: with every usable-weighted term either zero-weighted or
   zero-usable, `base_rank_score` is MATHEMATICALLY, PROVABLY always
   `0.0` for every symbol, every bar -- `direction` is always
   `"NO_DIRECTIONAL_EVIDENCE"`, `outcome` is always `"ABSTAIN"`. This is
   proven directly by `tests/test_aura_v054_signal_source.py` (not
   merely asserted here).

   In other words: run exactly as currently frozen, .54's real decision-
   engine signal source produces ZERO trade decisions, by construction --
   not a bug in this integration, but a faithful, now-fully-confirmed
   consequence of the frozen `.50`/`.51`/`.52` parameter set. This is
   surfaced prominently in the .54 documentation and final report as a
   finding, not silently worked around.

2. `SyntheticTestFixtureSignalSource` -- a deliberately simple,
   deterministic, NON-AI, NON-.50 rule (a fixed lookback-momentum
   crossover) used ONLY to drive the sizing/exit/portfolio-risk/cost
   pipeline end-to-end in `aura_v054_backtest.py`'s pipeline-validation
   mode, so that machinery can be exercised without depending on the
   (currently silent) real decision engine. THIS IS NOT A TRADING
   STRATEGY, NOT `.50`/`.51`/`.52`, AND MUST NEVER BE PRESENTED AS ONE OR
   AS EVIDENCE OF ANY EDGE. Every result it touches is tagged
   `SIGNAL_SOURCE_LABEL = "SYNTHETIC_TEST_FIXTURE_RULE"`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

import pandas as pd

import aura_v05349_ai_proposal_pipeline as PROPOSAL
import aura_v05350_decision_engine as ENGINE
import aura_v05351_live_alpaca_equity_signal_source as M51
import aura_v05352_stock_etf_short_side_signal as M52
from aura_v054_llm_stub import NeutralDeterministicLLMClient

VERSION = "AURA v0.5.4"

UNIVERSE_VERSION = "S&P100_FROZEN_2026-08-26_CORRECTED"  # aura_v0481_universe_integrity.py's UNIVERSE_VERSION, reused verbatim

# Frozen exactly as approved -- tests/test_aura_v05350_decision_engine.py's DECIDE_KWARGS,
# minus proposal_module/llm_client/now (supplied per-call by this module).
FROZEN_DECIDE_KWARGS: dict[str, Any] = dict(
    sentiment_weight=1.0,
    wave_weight=1.0,
    technical_weight=0.0,
    short_technical_weight=0.0,
    decision_threshold=0.1,
    ai_penalty_per_concern=0.2,
    critic_penalty_per_issue=0.15,
)

# Frozen exactly as approved -- tests/test_aura_v05351_live_alpaca_equity_signal_source.py's TEST_PARAMS.
FROZEN_TECHNICAL_PARAMS = M51.TechnicalScoringParams(
    min_bars_required=55,
    max_bar_age_seconds=3 * 86400.0,
    ema3_cross_ema8_points=15.0,
    ema3_above_ema8_points=5.0,
    ema8_above_ema21_points=15.0,
    ema21_above_ema50_points=10.0,
    macd_bullish_points=10.0,
    rsi_constructive_low=50.0,
    rsi_constructive_high=70.0,
    rsi_constructive_points=10.0,
    rsi_extended_threshold=75.0,
    rsi_extended_penalty_points=15.0,
    rel_volume_threshold=1.20,
    rel_volume_points=15.0,
    price_acceleration_points=5.0,
    persistence_lookback_bars=3,
    persistence_min_bars_for_bonus=3,
    persistence_points=5.0,
    whipsaw_prior_move_threshold=0.01,
    whipsaw_now_move_threshold=0.005,
    whipsaw_penalty_points=20.0,
    score_floor=0.0,
    score_ceiling=100.0,
    early_status_max_persistence=1,
    confirmed_score_threshold=70.0,
    confirmed_min_persistence=2,
    confirming_score_threshold=45.0,
)

# Frozen exactly as approved -- tests/test_aura_v05352_stock_etf_short_side_signal.py's TEST_PARAMS.
FROZEN_SHORT_TECHNICAL_PARAMS = M52.ShortTechnicalScoringParams(
    min_bars_required=55,
    max_bar_age_seconds=3 * 86400.0,
    ema3_cross_below_ema8_points=15.0,
    ema3_below_ema8_points=5.0,
    ema8_below_ema21_points=15.0,
    ema21_below_ema50_points=10.0,
    macd_bearish_points=10.0,
    rsi_weak_low=30.0,
    rsi_weak_high=50.0,
    rsi_weak_points=10.0,
    rsi_oversold_threshold=25.0,
    rsi_oversold_penalty_points=15.0,
    rel_volume_threshold=1.20,
    rel_volume_points=15.0,
    price_deceleration_points=5.0,
    persistence_lookback_bars=3,
    persistence_min_bars_for_bonus=3,
    persistence_points=5.0,
    squeeze_prior_move_threshold=0.01,
    squeeze_now_move_threshold=0.005,
    squeeze_penalty_points=20.0,
    score_floor=0.0,
    score_ceiling=100.0,
    early_status_max_persistence=1,
    confirmed_score_threshold=70.0,
    confirmed_min_persistence=2,
    confirming_score_threshold=45.0,
)


class SignalSourceError(Exception):
    pass


@runtime_checkable
class SignalSource(Protocol):
    SIGNAL_SOURCE_LABEL: str

    def decide_for_symbol(self, symbol: str, bars_up_to_now: pd.DataFrame, *, now: datetime) -> Any:
        ...


@dataclass
class AuraFrozenDecisionEngineSignalSource:
    """The real `.50`/`.51`/`.52` integration, frozen parameters, neutral
    deterministic LLM stub. See module docstring: under the currently
    frozen weights, this always returns an `ABSTAIN` `TradingDecision` --
    documented, proven by test, not a defect of this wiring.
    """

    SIGNAL_SOURCE_LABEL: str = "AURA_FROZEN_DECISION_ENGINE_050_051_052"

    def __post_init__(self) -> None:
        self._llm_client = NeutralDeterministicLLMClient()

    def decide_for_symbol(self, symbol: str, bars_up_to_now: pd.DataFrame, *, now: datetime):
        bars_indexed = bars_up_to_now.set_index("timestamp")[["open", "high", "low", "close", "volume"]]

        technical_regime = M51.build_technical_regime_from_bars(
            symbol, bars_indexed, params=FROZEN_TECHNICAL_PARAMS, universe_version=UNIVERSE_VERSION, now=now
        )
        short_technical_regime = M52.build_short_technical_regime_from_bars(
            symbol, bars_indexed, params=FROZEN_SHORT_TECHNICAL_PARAMS, universe_version=UNIVERSE_VERSION, now=now
        )
        evidence = ENGINE.build_candidate_evidence(
            symbol,
            technical_regime=technical_regime,
            short_technical_regime=short_technical_regime,
            now=now,
        )
        decision = ENGINE.decide(
            evidence,
            **FROZEN_DECIDE_KWARGS,
            proposal_module=PROPOSAL,
            llm_client=self._llm_client,
            now=now,
        )
        return decision


@dataclass
class SyntheticTestFixtureSignalSource:
    """NOT A STRATEGY. A fixed, deterministic 20-day-momentum rule used
    ONLY to exercise the sizing/exit/portfolio-risk/cost pipeline
    end-to-end against `SyntheticBarsProvider` data, since the real
    `AuraFrozenDecisionEngineSignalSource` deterministically never
    trades under the currently frozen weights (see above) and there is
    no real market data in this environment to drive a real backtest.

    Rule (arbitrary, disclosed, not researched or validated): LONG if
    today's close > close 20 trading days ago; otherwise no signal. This
    exists solely so a trade actually opens in the pipeline-validation
    run -- it carries no claim about edge, and results produced through
    it must never be reported as `.50`/`.51`/`.52` performance or as any
    kind of real trading-strategy result.
    """

    SIGNAL_SOURCE_LABEL: str = "SYNTHETIC_TEST_FIXTURE_RULE"
    lookback_bars: int = 20

    def decide_for_symbol(self, symbol: str, bars_up_to_now: pd.DataFrame, *, now: datetime) -> dict[str, Any]:
        if len(bars_up_to_now) <= self.lookback_bars:
            return {"outcome": "ABSTAIN", "direction": "NO_DIRECTIONAL_EVIDENCE"}
        closes = bars_up_to_now["close"].to_numpy()
        if closes[-1] > closes[-1 - self.lookback_bars]:
            return {"outcome": "DECIDE_LONG", "direction": "LONG_LEANING"}
        return {"outcome": "ABSTAIN", "direction": "NO_DIRECTIONAL_EVIDENCE"}
