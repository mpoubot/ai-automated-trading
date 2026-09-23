"""
core/strategy_base.py

Strategy interface for the Phase 5 research core.

This is the structural fix for the one concrete defect found during the
"inspect before coding" review of mexc_bot: backtester.py::simulate_symbol()
line 218 calls `strat.evaluate_signal(window)` as a hard, direct import --
the backtest engine and the strategy are not actually separable there. This
module makes the strategy a pluggable, swappable interface instead: the
backtest engine (core/backtest_engine.py) depends only on this ABC, never on
any strategy's internals.

The `is_research_fixture` flag makes the REAL STRATEGY vs. RESEARCH FIXTURE
distinction structural, not just a comment: any code consuming a Strategy
(the results writer, the experiment runner, a future dashboard) can check
this flag and refuse to present fixture output as trading-strategy
performance -- per the Phase 5 command's explicit requirement to never
report fixture performance as strategy performance.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

import pandas as pd

from core.instrument import Instrument
from core.signal import Signal

PIPELINE_FIXTURE_LABEL = "PIPELINE VALIDATION FIXTURE — NOT A TRADING STRATEGY"


class Strategy(ABC):
    """
    Pluggable strategy interface.

    strategy_id / version / is_research_fixture are class- or instance-level
    attributes a concrete strategy must set.
    """

    strategy_id: str
    version: str
    is_research_fixture: bool = False  # MUST be True for any non-trading fixture

    @abstractmethod
    def required_warmup_bars(self) -> int:
        """Minimum number of prior bars this strategy needs before it can
        evaluate a signal (indicator warmup period, etc.). The backtest
        engine skips bars before this count is available."""
        raise NotImplementedError

    @abstractmethod
    def evaluate(self, window: pd.DataFrame, instrument: Instrument) -> Optional[Signal]:
        """
        `window` is all bars up to and including the current bar
        (df.iloc[:i+1]) -- matches mexc_bot's existing convention so ported
        exit-management mechanics stay self-consistent. A strategy MUST NOT
        look beyond the last row of `window` (no lookahead). Returns None
        for "no signal at this bar."
        """
        raise NotImplementedError

    def label(self) -> str:
        return PIPELINE_FIXTURE_LABEL if self.is_research_fixture else f"{self.strategy_id} v{self.version}"
