#!/usr/bin/env python3
"""
AURA v0.5.4 -- Cost model (isolated, swappable)

`cost_pct = 0.001` (0.10%) is Martin's EXPLICITLY_SELECTED ROUND-TRIP cost
assumption for .54's baseline. Confirmed round-trip convention (not a
per-side/one-way cost), matching this repository's existing evidence:
  - `aura_exit_policy_backtest.py:75-76`: "A flat round-trip cost
    (--cost-bps, default 10bps, same convention as v2) is subtracted
    from every trade's gross return."
  - `aura_exit_policy_backtest.py:343`: `net_return_pct = gross_return_pct
    - cost_pct` -- ONE subtraction per trade, not applied twice (once for
    entry, once for exit).
  - `aura_regime_backtest_v2.py:47-48,152`: `DEFAULT_COST_BPS = 10.0`,
    documented the same way.

`0.10%` is explicitly documented (per `AURA_v0.53_ATR_Exit_Cost_Sizing_
Decision_Support_2026-09-15.md`, Section C/D) as a CONSERVATIVE
EQUITY-RESEARCH ASSUMPTION carried over from this repo's crypto-track
default, NOT a recovered broker fee schedule and NOT derived from any
actual Alpaca commission/spread/slippage data for equities. Martin's own
.54 spec: "record explicitly that this is a conservative equity-research
assumption... keep isolated/swappable for a future commission+spread+
slippage model." This module is that isolation point: everything else in
.54 calls `CostModel.apply(gross_return_frac)`, never a bare subtraction
inline, so a future, more granular cost model (separate commission +
spread + slippage terms) can replace `FlatRoundTripCostModel` without
touching the exit engine, sizing, or backtest runner.

Unit convention: fractions throughout (0.001 == 0.10%), matching
`aura_v054_exit_engine.py`'s convention -- NOT the percentage-point
convention `aura_exit_policy_backtest.py` uses internally.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

VERSION = "AURA v0.5.4"

# EXPLICITLY_SELECTED -- Martin's .54 spec, 2026-09-15. Confirmed round-trip
# per repository evidence above. NOT a recovered broker fee schedule.
COST_PCT_ROUND_TRIP = 0.001


class CostModelError(Exception):
    pass


@runtime_checkable
class CostModel(Protocol):
    """The one seam for transaction-cost assumptions. A future model
    (separate commission/spread/slippage terms, possibly asymmetric
    entry vs. exit) implements this same `apply` signature and can be
    swapped into `aura_v054_backtest.py` without any other .54 module
    changing.
    """

    def apply(self, gross_return_frac: float) -> float:
        ...


@dataclass(frozen=True, slots=True)
class FlatRoundTripCostModel:
    """.54's baseline cost model: a single flat round-trip fraction
    subtracted once per trade. `cost_pct` defaults to Martin's
    EXPLICITLY_SELECTED 0.001 (0.10%) but is never hardcoded inline
    elsewhere -- every consumer takes a `CostModel` instance.
    """

    cost_pct: float = COST_PCT_ROUND_TRIP

    def __post_init__(self) -> None:
        if self.cost_pct < 0:
            raise CostModelError(f"INVALID_COST_PCT:{self.cost_pct!r}:must be >= 0")

    def apply(self, gross_return_frac: float) -> float:
        return gross_return_frac - self.cost_pct
