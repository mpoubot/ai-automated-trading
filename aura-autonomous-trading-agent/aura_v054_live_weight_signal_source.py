#!/usr/bin/env python3
"""
AURA v0.5.4 -- Live-weight variant signal source for `.054`'s backtest/
permutation-test harness (NEW, 2026-10-08)

Built per Martin's explicit direction (AskUserQuestion, 2026-10-08) after
two corrections this same session:

  1. `.054`'s `FROZEN_DECIDE_KWARGS` (`technical_weight=0.0`/
     `short_technical_weight=0.0`) is NOT the live decision-engine config.
     It is a DELIBERATELY frozen, intentionally-inert research/backtest
     baseline -- confirmed directly from `aura_v05362_live_evidence_
     orchestrator.py`'s own module docstring: "Does not modify `.53`/
     `.55`/`.054` in any way... kept separate from `.054`'s deliberately-
     frozen `FROZEN_DECIDE_KWARGS` so that module's own contract and test
     suite (`tests/test_aura_v054_signal_source.py`, which PROVES the
     frozen config always ABSTAINs) are completely undisturbed." Editing
     `FROZEN_DECIDE_KWARGS` would break that test and undo a deliberate
     design decision -- not fix a gap. `aura_v054_signal_source.py` is
     therefore left completely untouched by this module.

  2. The ACTUAL live/paper-trading weights live separately, in
     `aura_v05362_live_evidence_orchestrator.py`'s `LIVE_EVIDENCE_DECIDE_
     KWARGS` (`technical_weight=1.0`, `short_technical_weight=1.0`,
     confirmed by Martin via `AskUserQuestion`, 2026-09-24/25).

THIS MODULE DOES NOT MODIFY `aura_v054_signal_source.py` IN ANY WAY -- it
is a new, separate, parallel signal source, the same "build a variant,
don't edit the frozen one" pattern the 2026-10-03 sector-rotation-weight
sweep already used for the exact same reason.

WHAT'S REUSED VERBATIM FROM THE REAL LIVE PATH -- CONFIRMED, NOT GUESSED
---------------------------------------------------------------------------
  - `FROZEN_TECHNICAL_PARAMS` / `FROZEN_SHORT_TECHNICAL_PARAMS` (`.51`'s/
    `.52`'s own 27-field scoring parameters): imported directly from
    `aura_v054_signal_source.py`, never redefined here. Confirmed these
    are the SAME params the real live path uses too --
    `aura_v05356_stage3_live_equity_cli.py` imports `signal_source_
    module.FROZEN_TECHNICAL_PARAMS`/`FROZEN_SHORT_TECHNICAL_PARAMS`
    directly from `aura_v054_signal_source.py` (confirmed at that file's
    own lines ~718-719, ~812-813) -- only the DECISION WEIGHTS differ
    between live and `.054`, never the technical scoring itself.
  - `UNIVERSE_VERSION`: imported directly from `aura_v054_signal_source.py`.
  - `NeutralDeterministicLLMClient`: the same neutral stub `.054` and the
    live path both use, for the same determinism reason.
  - `.50`/`.51`/`.52` wiring (`build_technical_regime_from_bars` ->
    `build_short_technical_regime_from_bars` -> `build_candidate_evidence`
    -> `decide`): structurally identical to `aura_v054_signal_source.
    AuraFrozenDecisionEngineSignalSource.decide_for_symbol` -- the ONLY
    difference is the `DECIDE_KWARGS` dict passed to `decide()`.

DECIDE_KWARGS weight values below are copied verbatim from `aura_v05362_
live_evidence_orchestrator.py`'s `LIVE_EVIDENCE_DECIDE_KWARGS` AS THAT
FILE CURRENTLY READS (`sector_rotation_weight=0.0`) -- the already-
approved `sector_rotation_weight=0.1` change sits on the unmerged
`feature/sector-rotation-weight-0.1` branch. If/when that branch merges,
update `LIVE_WEIGHT_DECIDE_KWARGS` here to match (and, per the limitation
below, actually wiring sector-rotation EVIDENCE into this module's
`decide_for_symbol` is a separate, larger task -- the weight alone does
nothing without it).

DISCLOSED LIMITATION -- SENTIMENT/WAVE/SECTOR-ROTATION WEIGHTS ARE SET
BUT INERT IN THIS BACKTEST CONTEXT, EXACTLY LIKE `.054`'S OWN FROZEN
SIGNAL SOURCE
---------------------------------------------------------------------------
Setting `sentiment_weight`/`wave_weight`/`sector_rotation_weight` to their
live values does NOT mean this backtest actually uses sentiment, wave, or
sector-rotation evidence. Exactly like `aura_v054_signal_source.py`'s own
`AuraFrozenDecisionEngineSignalSource`, THIS module supplies no
`sentiment_regime`, `wave_result`, or `sector_rotation_regime` either:

  - No historical news/sentiment dataset exists anywhere in this project
    (confirmed, `aura_v054_data_interface.py`'s own module docstring: no
    general internet access, no real credentials, no pinned dataset at
    `.54` implementation time) -- `.47`/`.48`'s evidence-construction
    pipeline was never rebuilt for historical/backtest use, live-only.
  - Real sector-rotation evidence (`aura_v05359_sector_rotation.py`'s
    `analyze_sector_rotation`) needs a whole separate benchmark/safe-
    haven/sector-universe bars fetch plus several required (no-default)
    parameters (`benchmark_symbol`, `lookback_bars`, `top_n`,
    `defensive_ma_period`, `score_scale`) -- a materially bigger, SEPARATE
    wiring job, not done here, not silently faked.

So this module's decision score, in practice, is driven ENTIRELY by real
`technical_regime` (`.51`, LONG-only, added when usable) and real
`short_technical_regime` (`.52`, SHORT-only, subtracted when usable) --
the same inputs `.054`'s own frozen signal source already builds on every
call, just no longer weighted at zero. This is the one part of the real
live decision logic that CAN be honestly exercised against historical
bars alone with no further build-out, and it is exactly what differs
between this module and `.054`'s frozen one.

NOT A CLAIM OF FULL LIVE PARITY. A backtest driven by this signal source
is materially closer to the real live decision logic than `.054`'s own
frozen `0.0`/`0.0` config, but it is still not a complete replica --
sentiment/wave/sector-rotation never fire here, same as `.054`. Label any
results accordingly (`SIGNAL_SOURCE_LABEL` below makes this explicit).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import pandas as pd

import aura_v05349_ai_proposal_pipeline as PROPOSAL
import aura_v05350_decision_engine as ENGINE
import aura_v05351_live_alpaca_equity_signal_source as M51
import aura_v05352_stock_etf_short_side_signal as M52
from aura_v054_llm_stub import NeutralDeterministicLLMClient
from aura_v054_signal_source import (
    FROZEN_SHORT_TECHNICAL_PARAMS,
    FROZEN_TECHNICAL_PARAMS,
    UNIVERSE_VERSION,
)

VERSION = "AURA v0.5.4"

# Copied verbatim from aura_v05362_live_evidence_orchestrator.py's
# LIVE_EVIDENCE_DECIDE_KWARGS, as that file reads on the branch this was
# written against (2026-10-08). sector_rotation_weight=0.0 because the
# approved 0.1 change is on the unmerged feature/sector-rotation-weight-0.1
# branch -- update this dict if/when that merges (see module docstring).
LIVE_WEIGHT_DECIDE_KWARGS: dict[str, Any] = dict(
    sentiment_weight=1.0,
    wave_weight=1.0,
    technical_weight=1.0,
    short_technical_weight=1.0,
    sector_rotation_weight=0.0,
    decision_threshold=0.1,
    ai_penalty_per_concern=0.2,
    critic_penalty_per_issue=0.15,
)


@dataclass
class LiveWeightDecisionEngineSignalSource:
    """Same real `.50`/`.51`/`.52` wiring as `aura_v054_signal_source.
    AuraFrozenDecisionEngineSignalSource`, with the live-confirmed
    decision weights (`LIVE_WEIGHT_DECIDE_KWARGS`) instead of `.054`'s
    deliberately-frozen `0.0`/`0.0`. See module docstring for exactly
    what is, and is not, representative of the real live path.
    """

    SIGNAL_SOURCE_LABEL: str = "AURA_LIVE_WEIGHT_050_051_052"

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
            **LIVE_WEIGHT_DECIDE_KWARGS,
            proposal_module=PROPOSAL,
            llm_client=self._llm_client,
            now=now,
        )
        return decision
