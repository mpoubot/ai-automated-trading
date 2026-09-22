"""
strategies/pipeline_fixture.py

PipelineValidationFixture: the synthetic, deterministic, explicitly-labelled
strategy used to prove the Phase 5 research pipeline works end-to-end. This
is Martin's explicit choice (clarifying-question answer, 2026-09-22):
"Use a simple, deterministic, explicitly labelled fixture ... Label every
result clearly: 'PIPELINE VALIDATION FIXTURE -- NOT A TRADING STRATEGY'. Do
not interpret its profitability or use it as evidence of trading edge."

This fixture carries NO trading logic and NO market view: it enters long,
deterministically, every `interval_bars` bars (never short -- direction is
not the point here), purely to exercise every stage of the pipeline
(canonical instrument -> strategy interface -> signal -> backtest ->
position sizing -> costs -> funding -> risk -> portfolio -> metrics ->
reproducible results). Its `is_research_fixture = True` flag makes this
structural (core/strategy_base.py, core/results.py), not just a comment --
any downstream consumer can and must check this flag before treating a
result as strategy evidence.

AURA v0.4.x (or any other real strategy) is explicitly NOT ported here --
that is later, real-strategy validation work, out of Phase 5 scope.
"""
from __future__ import annotations

from typing import Optional

import pandas as pd

from core.instrument import Instrument
from core.signal import Signal
from core.strategy_base import PIPELINE_FIXTURE_LABEL, Strategy


class PipelineValidationFixture(Strategy):
    strategy_id = "pipeline_fixture_v1"
    version = "1.0.0"
    is_research_fixture = True

    def __init__(self, interval_bars: int = 48, atr_warmup_period: int = 14):
        if interval_bars <= 0:
            raise ValueError(f"interval_bars must be > 0, got {interval_bars}")
        self.interval_bars = interval_bars
        self.atr_warmup_period = atr_warmup_period

    def required_warmup_bars(self) -> int:
        # A few extra bars beyond the ATR warmup period so 'atr' is never NaN
        # at the first bar this strategy is asked to evaluate.
        return self.atr_warmup_period + 5

    def evaluate(self, window: pd.DataFrame, instrument: Instrument) -> Optional[Signal]:
        bar_index = len(window) - 1  # index of the current (last) bar within window
        if bar_index % self.interval_bars != 0:
            return None

        row = window.iloc[-1]
        atr_val = row.get("atr")
        if atr_val is None or pd.isna(atr_val):
            return None

        return Signal(
            timestamp=row["timestamp"],
            direction="long",
            reason=(
                f"{PIPELINE_FIXTURE_LABEL}: deterministic long entry every "
                f"{self.interval_bars} bars (bar_index={bar_index}); no market "
                f"view, no trading logic -- exercises the pipeline only."
            ),
            atr_at_signal=float(atr_val),
            confidence=1.0,
        )
