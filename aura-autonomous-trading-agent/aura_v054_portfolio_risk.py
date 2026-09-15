#!/usr/bin/env python3
"""
AURA v0.5.4 -- Portfolio risk controls + planned-risk persistence

Two responsibilities, kept in one small module because they are two
halves of the same fix:

1. `PortfolioRiskLimits` -- the .54 baseline portfolio-level guardrails
   Martin explicitly selected (EXPLICITLY_SELECTED / BASELINE_RESEARCH_
   HYPOTHESIS, not optimized, not claimed best):
       max_positions       = 10
       max_gross_exposure   = 1.00   (100% of equity, no leverage)
       max_portfolio_risk   = 0.02   (2% of equity at risk, summed across
                                       all open positions' planned risk)
   These are DELIBERATELY NOT a reuse of `aura_v05344_portfolio_exposure_
   enforcement.py`'s `PortfolioLimits` (confirmed by direct read: its
   fields are `max_portfolio_heat_ratio` / `max_asset_concentration_
   ratio` / `max_net_exposure_ratio` / `mexc_leverage_cap` -- a different,
   crypto/MEXC-leverage-aware shape) -- `.54` is STOCK/ETF-only, no
   leverage, so a smaller, purpose-built limit set is the minimum clean
   extension rather than force-fitting a crypto-shaped dataclass.

2. `PlannedRiskLedger` -- the fix for the confirmed architectural gap in
   `aura_v05343_portfolio_exposure_observability.py:118,696`: "AURA does
   not persist a stop-loss/planned-risk distance for any already-open
   position" (`per_trade_risk` structurally `NOT_COMPUTABLE`). This
   ledger persists, per open position, exactly the numbers
   `aura_v054_position_sizing.SizingResult` already computed at entry
   (`planned_stop_distance`, `planned_risk_dollars`, `market_value`) so
   that portfolio-level risk/exposure IS computable at every later bar,
   not just at the moment of entry. This is a minimal, .54-scoped
   mechanism -- it does not modify or attempt to fix `.44`'s own
   MEXC-facing observability module.
"""

from __future__ import annotations

from dataclasses import dataclass, field

VERSION = "AURA v0.5.4"

# EXPLICITLY_SELECTED / BASELINE_RESEARCH_HYPOTHESIS -- Martin's .54 spec, 2026-09-15.
MAX_POSITIONS = 10
MAX_GROSS_EXPOSURE = 1.00  # fraction of equity; no leverage
MAX_PORTFOLIO_RISK = 0.02  # fraction of equity; sum of all open positions' planned risk


class PortfolioRiskError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class PortfolioRiskLimits:
    max_positions: int = MAX_POSITIONS
    max_gross_exposure: float = MAX_GROSS_EXPOSURE
    max_portfolio_risk: float = MAX_PORTFOLIO_RISK

    def __post_init__(self) -> None:
        if self.max_positions <= 0:
            raise PortfolioRiskError("INVALID_MAX_POSITIONS:must be > 0")
        if self.max_gross_exposure <= 0:
            raise PortfolioRiskError("INVALID_MAX_GROSS_EXPOSURE:must be > 0")
        if self.max_gross_exposure > 1.0:
            # No leverage, per Martin's explicit spec -- gross exposure
            # cannot exceed 100% of equity.
            raise PortfolioRiskError("INVALID_MAX_GROSS_EXPOSURE:must be <= 1.0 (no leverage)")
        if self.max_portfolio_risk <= 0:
            raise PortfolioRiskError("INVALID_MAX_PORTFOLIO_RISK:must be > 0")


@dataclass(frozen=True, slots=True)
class PlannedPositionRisk:
    """One open position's persisted, at-entry-computed risk state --
    exactly what `aura_v05343` found could not be reconstructed later."""

    symbol: str
    entry_bar_index: int
    quantity: int
    entry_price: float
    planned_stop_distance: float
    planned_risk_dollars: float
    market_value: float


@dataclass
class PlannedRiskLedger:
    """Tracks currently-open positions' planned risk. Deterministic,
    in-memory, append/remove only -- no hidden state, no I/O."""

    positions: dict[str, PlannedPositionRisk] = field(default_factory=dict)

    def open_position(self, risk: PlannedPositionRisk) -> None:
        if risk.symbol in self.positions:
            raise PortfolioRiskError(f"ALREADY_OPEN:{risk.symbol}:cannot open a second position for the same symbol")
        self.positions[risk.symbol] = risk

    def close_position(self, symbol: str) -> None:
        if symbol not in self.positions:
            raise PortfolioRiskError(f"NOT_OPEN:{symbol}:cannot close a position that is not open")
        del self.positions[symbol]

    @property
    def open_count(self) -> int:
        return len(self.positions)

    def total_market_value(self) -> float:
        return sum(p.market_value for p in self.positions.values())

    def total_planned_risk_dollars(self) -> float:
        return sum(p.planned_risk_dollars for p in self.positions.values())


@dataclass(frozen=True, slots=True)
class EnforcementVerdict:
    allowed: bool
    reasons: tuple[str, ...]
    projected_open_count: int
    projected_gross_exposure: float
    projected_portfolio_risk: float


def evaluate_candidate_position(
    *,
    ledger: PlannedRiskLedger,
    limits: PortfolioRiskLimits,
    equity: float,
    candidate_market_value: float,
    candidate_planned_risk_dollars: float,
) -> EnforcementVerdict:
    """Deterministic pre-trade check: would opening this candidate
    position, on top of what `ledger` already has open, breach any of
    `.54`'s three baseline portfolio limits? Every breach is reported
    (not just the first), so a rejected candidate's log line is fully
    explanatory. Never mutates `ledger` -- the caller opens the position
    (via `ledger.open_position`) only after this returns `allowed=True`.
    """
    if equity <= 0:
        raise PortfolioRiskError(f"INVALID_EQUITY:{equity!r}:must be > 0")
    if candidate_market_value < 0:
        raise PortfolioRiskError("INVALID_CANDIDATE_MARKET_VALUE:must be >= 0")
    if candidate_planned_risk_dollars < 0:
        raise PortfolioRiskError("INVALID_CANDIDATE_PLANNED_RISK_DOLLARS:must be >= 0")

    projected_count = ledger.open_count + 1
    projected_gross_exposure = (ledger.total_market_value() + candidate_market_value) / equity
    projected_portfolio_risk = (ledger.total_planned_risk_dollars() + candidate_planned_risk_dollars) / equity

    reasons: list[str] = []
    if projected_count > limits.max_positions:
        reasons.append(f"MAX_POSITIONS_EXCEEDED:{projected_count}>{limits.max_positions}")
    if projected_gross_exposure > limits.max_gross_exposure:
        reasons.append(
            f"MAX_GROSS_EXPOSURE_EXCEEDED:{projected_gross_exposure:.6f}>{limits.max_gross_exposure:.6f}"
        )
    if projected_portfolio_risk > limits.max_portfolio_risk:
        reasons.append(
            f"MAX_PORTFOLIO_RISK_EXCEEDED:{projected_portfolio_risk:.6f}>{limits.max_portfolio_risk:.6f}"
        )

    return EnforcementVerdict(
        allowed=not reasons,
        reasons=tuple(reasons),
        projected_open_count=projected_count,
        projected_gross_exposure=projected_gross_exposure,
        projected_portfolio_risk=projected_portfolio_risk,
    )
