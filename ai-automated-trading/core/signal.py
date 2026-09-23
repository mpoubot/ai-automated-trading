"""
core/signal.py

Minimal Signal / PositionIntent types.

A Strategy emits a Signal (a raw directional opinion at one bar). The
backtest engine turns an *accepted* Signal into a PositionIntent once
risk/portfolio sizing has approved and sized it. Keeping these as two
distinct types -- rather than one blob -- keeps the signal-generation vs.
decision/sizing boundary explicit even at Phase 5's minimal scope, matching
the target architecture's Signal Engine / Decision Engine separation
(TARGET_ARCHITECTURE_SPECIFICATION, Phase 3).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Optional

Direction = Literal["long", "short"]


@dataclass(frozen=True)
class Signal:
    """A strategy's raw directional opinion at one bar. Not yet sized, not
    yet risk-checked -- that happens downstream in the backtest engine."""
    timestamp: datetime
    direction: Direction
    reason: str
    atr_at_signal: Optional[float] = None
    confidence: float = 1.0  # reserved for future use; Phase 5 strategies use 1.0


@dataclass(frozen=True)
class PositionIntent:
    """A Signal that has passed risk/portfolio sizing and is ready for the
    backtest engine (or, in a later phase, a real execution engine) to act
    on."""
    timestamp: datetime
    direction: Direction
    entry_price: float
    stop_price: float
    stop_distance: float
    quantity: float
    position_size_usdt: float
    leverage: float
    risk_amount_usdt: float
    reason: str
