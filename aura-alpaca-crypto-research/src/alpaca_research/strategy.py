from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import pandas as pd


@dataclass(frozen=True)
class Signal:
    symbol: str
    timestamp: pd.Timestamp
    action: str  # HOLD, BUY, SELL
    score: float
    strategy_version: str


class Strategy(Protocol):
    version: str

    def evaluate(self, bars: pd.DataFrame, symbol: str) -> Signal:
        ...


@dataclass(frozen=True)
class FrozenStrategy:
    """Explicit placeholder until the competition strategy is copied in unchanged.

    This intentionally emits HOLD. It prevents accidental claims that the repository
    already contains a validated trading strategy.
    """

    version: str = "UNSET"

    def evaluate(self, bars: pd.DataFrame, symbol: str) -> Signal:
        timestamp = pd.Timestamp(bars.index[-1]) if not bars.empty else pd.Timestamp.utcnow()
        return Signal(symbol, timestamp, "HOLD", 0.0, self.version)
