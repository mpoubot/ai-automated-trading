#!/usr/bin/env python3
"""
AURA v0.5.4 -- ATR risk-based position sizing

Deliberately NOT a reuse of `aura_v05325_position_sizing.py`: that module
is the OLD fixed-10%-of-equity / 5-position-cap live-Alpaca-account sizer
(confirmed by direct read of its module docstring -- "Fixed fraction of
LIVE Alpaca paper account equity... 10% of equity per position, max 5
concurrent positions ... both values Martin's own explicit choice").
Martin's .54 spec explicitly says: "Do NOT use the old fixed-10% model."
This module computes size from RISK, not from a flat equity fraction:

    risk_capital   = equity * risk_fraction_per_trade
    quantity       = floor(risk_capital / planned_stop_distance)

`risk_fraction_per_trade = 0.005` (0.5% of equity) is Martin's
EXPLICITLY_SELECTED / BASELINE_RESEARCH_HYPOTHESIS value -- not
optimized, not claimed best.

`planned_stop_distance` MUST be supplied by the caller, explicitly
computed (in .54's case, `aura_v054_exit_engine.initial_stop_distance_
long`) BEFORE this function is called -- this module never invents,
defaults, or estimates a distance on its own. This directly addresses the
architectural gap found in `aura_v05343_portfolio_exposure_observability.
py:118,696` ("AURA does not persist a stop-loss/planned-risk distance for
any already-open position", `per_trade_risk` structurally
`NOT_COMPUTABLE`): `.54` fixes this by making the planned distance a
REQUIRED, explicit input to sizing, and by persisting it per-position
(see `aura_v054_portfolio_risk.py`'s `PlannedRiskLedger`) rather than
leaving it uncomputable after entry.

Fails safe (raises, never returns a fabricated/zero/negative quantity)
when `planned_stop_distance` is missing, non-positive, or NaN, or when
`equity`/`entry_price` are non-positive.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

VERSION = "AURA v0.5.4"

# EXPLICITLY_SELECTED / BASELINE_RESEARCH_HYPOTHESIS -- Martin's .54 spec, 2026-09-15.
RISK_FRACTION_PER_TRADE = 0.005  # 0.5% of equity risked per trade


class PositionSizingError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class SizingResult:
    quantity: int
    risk_capital: float
    planned_stop_distance: float
    planned_risk_dollars: float  # == quantity * planned_stop_distance (<= risk_capital, since quantity floors)
    market_value: float  # == quantity * entry_price
    risk_fraction_per_trade: float


def _validate_inputs(equity: float, entry_price: float, planned_stop_distance: float | None, risk_fraction_per_trade: float) -> None:
    if equity is None or equity <= 0:
        raise PositionSizingError(f"INVALID_EQUITY:{equity!r}:must be > 0")
    if entry_price is None or entry_price <= 0:
        raise PositionSizingError(f"INVALID_ENTRY_PRICE:{entry_price!r}:must be > 0")
    if risk_fraction_per_trade is None or risk_fraction_per_trade <= 0:
        raise PositionSizingError(f"INVALID_RISK_FRACTION_PER_TRADE:{risk_fraction_per_trade!r}:must be > 0")
    if planned_stop_distance is None:
        raise PositionSizingError("MISSING_PLANNED_STOP_DISTANCE:sizing requires an explicit, pre-computed stop distance")
    if isinstance(planned_stop_distance, float) and math.isnan(planned_stop_distance):
        raise PositionSizingError("INVALID_PLANNED_STOP_DISTANCE:NaN")
    if planned_stop_distance <= 0:
        raise PositionSizingError(f"INVALID_PLANNED_STOP_DISTANCE:{planned_stop_distance!r}:must be > 0")


def size_position_by_atr_risk(
    *,
    equity: float,
    entry_price: float,
    planned_stop_distance: float,
    risk_fraction_per_trade: float = RISK_FRACTION_PER_TRADE,
) -> SizingResult:
    """Deterministic, whole-share ATR-risk-based sizing. `quantity` is
    floored (never rounds up, never risks more than `risk_capital`).
    A `planned_stop_distance` so large that even one share exceeds the
    risk budget returns `quantity = 0` (REJECTED_INSUFFICIENT_RISK_
    BUDGET-equivalent; not an error -- a symbol can legitimately be
    un-sizeable under the frozen risk budget) rather than raising, since
    zero-size is itself a valid, auditable sizing decision distinct from
    a missing/invalid input.
    """
    _validate_inputs(equity, entry_price, planned_stop_distance, risk_fraction_per_trade)

    risk_capital = equity * risk_fraction_per_trade
    quantity = int(math.floor(risk_capital / planned_stop_distance))
    if quantity < 0:
        quantity = 0

    planned_risk_dollars = quantity * planned_stop_distance
    market_value = quantity * entry_price

    return SizingResult(
        quantity=quantity,
        risk_capital=risk_capital,
        planned_stop_distance=planned_stop_distance,
        planned_risk_dollars=planned_risk_dollars,
        market_value=market_value,
        risk_fraction_per_trade=risk_fraction_per_trade,
    )
