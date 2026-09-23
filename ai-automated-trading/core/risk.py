"""
core/risk.py

Minimal research-scope risk layer: explicit ACTIVE / RESTRICTED / HALTED
states, plus ATR-based position sizing.

Ported (re-implemented, not imported) from mexc_bot/core/risk_manager.py:
calc_position_plan() (ATR-based stop distance, fixed risk-% sizing, leverage
capped and scaled down rather than exceeded) and CircuitBreaker (peak-equity
/ day-start-equity tracking). The one structural change from CircuitBreaker
is the addition of a distinct RESTRICTED tier: the old code had a single
binary `halted` flag driven by BOTH the daily-loss limit and the
max-drawdown limit, conflating "stop opening new trades today" with "stop
trading entirely." Per the Phase 5 command ("must not blindly reproduce the
old permanent-circuit-breaker problem"), this module separates them:
  - daily loss limit hit   -> RESTRICTED (no new entries; existing
    positions still managed; clears automatically when a new trading day
    starts)
  - max drawdown limit hit -> HALTED (hard stop; does not auto-reset)

Simplifying research-only assumption (documented per the command's
requirement): there is no live "circuit breaker reset" workflow here --
HALTED is terminal for the run, matching the frozen, deterministic,
single-pass nature of a Phase 5 backtest. A real reset/recovery policy is
explicit future work (Phase 4's D3 decision), out of scope for Phase 5.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class RiskState(Enum):
    ACTIVE = "active"
    RESTRICTED = "restricted"
    HALTED = "halted"


@dataclass(frozen=True)
class PositionPlan:
    side: str
    entry_price: float
    stop_price: float
    stop_distance: float
    position_size_usdt: float
    quantity: float
    leverage: float
    risk_amount_usdt: float


@dataclass
class RiskConfig:
    risk_per_trade_pct: float = 0.02
    atr_stop_mult: float = 1.0
    max_leverage: float = 5.0
    min_leverage: float = 1.0
    daily_loss_limit_pct: float = 0.08     # -> RESTRICTED for the rest of that day
    max_drawdown_limit_pct: float = 0.20   # -> HALTED (terminal for the run)


def calc_position_plan(entry_price: float, atr: float, side: str,
                        equity_usdt: float, cfg: RiskConfig) -> Optional[PositionPlan]:
    """Same sizing math as mexc_bot/core/risk_manager.py::calc_position_plan,
    parameterized by an explicit RiskConfig instead of a module-level config
    import."""
    if atr is None or atr <= 0 or entry_price <= 0:
        return None

    stop_distance = atr * cfg.atr_stop_mult
    if stop_distance <= 0:
        return None

    risk_amount_usdt = equity_usdt * cfg.risk_per_trade_pct

    if side == "long":
        stop_price = entry_price - stop_distance
    elif side == "short":
        stop_price = entry_price + stop_distance
    else:
        return None

    quantity = risk_amount_usdt / stop_distance
    position_size_usdt = quantity * entry_price

    implied_leverage = position_size_usdt / equity_usdt if equity_usdt > 0 else 0
    leverage = max(cfg.min_leverage, min(implied_leverage, cfg.max_leverage))

    if implied_leverage > cfg.max_leverage:
        position_size_usdt = equity_usdt * cfg.max_leverage
        quantity = position_size_usdt / entry_price
        risk_amount_usdt = quantity * stop_distance

    return PositionPlan(
        side=side,
        entry_price=entry_price,
        stop_price=stop_price,
        stop_distance=stop_distance,
        position_size_usdt=round(position_size_usdt, 2),
        quantity=quantity,
        leverage=round(leverage, 2),
        risk_amount_usdt=round(risk_amount_usdt, 2),
    )


class SimpleResearchRiskModel:
    """Tracks equity peak / day-start equity for ONE symbol's allocated
    capital slice and exposes an explicit 3-state machine. See module
    docstring for why RESTRICTED and HALTED are kept distinct."""

    def __init__(self, starting_equity: float, cfg: Optional[RiskConfig] = None):
        self.cfg = cfg or RiskConfig()
        self.peak_equity = starting_equity
        self.day_start_equity = starting_equity
        self.current_day: Optional[str] = None
        self.state = RiskState.ACTIVE
        self.state_reason = "ok"

    def update(self, equity: float, today_key: str) -> None:
        if self.current_day != today_key:
            self.current_day = today_key
            self.day_start_equity = equity
            # A new day clears a RESTRICTED (daily-loss) state -- but never
            # a HALTED one, which is terminal for the run.
            if self.state == RiskState.RESTRICTED:
                self.state = RiskState.ACTIVE
                self.state_reason = "ok (new day, daily-loss restriction cleared)"

        if equity > self.peak_equity:
            self.peak_equity = equity

    def can_open_new_trades(self, equity: float) -> tuple[bool, RiskState, str]:
        """
        REMEDIATION (PHASE5_INDEPENDENT_AUDIT_2026-09-22.md finding #3,
        MEDIUM): the original version of this method checked the daily-loss
        condition first and returned immediately on a breach, before the
        max-drawdown condition was ever evaluated in that same call. When
        both were breached simultaneously, the more severe HALTED condition
        was masked and the state was reported (and persisted) as RESTRICTED
        for up to a full trading day -- reproduced directly: a scenario with
        a simultaneous 25% daily loss and 25% drawdown (both well past their
        default 8%/20% thresholds) reported RESTRICTED on the triggering
        call and kept reporting RESTRICTED on every subsequent same-day
        call, only correctly transitioning to HALTED after the next
        day-boundary update() call reset day_start_equity and let the
        drawdown check run unmasked.

        Fix: BOTH conditions are now computed unconditionally before any
        state is assigned, and severity precedence is applied explicitly --
        HALTED (terminal) always wins over RESTRICTED (same-day only) over
        ACTIVE. This cannot accidentally allow new trades: every branch
        below that finds a breach still returns False, and the terminal
        HALTED short-circuit is preserved as the very first check (once
        halted, always halted for the rest of the run, regardless of
        subsequent equity recovery).
        """
        if self.state == RiskState.HALTED:
            return False, self.state, self.state_reason

        daily_loss_pct = (self.day_start_equity - equity) / self.day_start_equity \
            if self.day_start_equity > 0 else 0
        drawdown_pct = (self.peak_equity - equity) / self.peak_equity \
            if self.peak_equity > 0 else 0

        daily_loss_breached = daily_loss_pct >= self.cfg.daily_loss_limit_pct
        drawdown_breached = drawdown_pct >= self.cfg.max_drawdown_limit_pct

        if drawdown_breached:
            # HALTED takes precedence over RESTRICTED even if both breached
            # in the same call -- this is the defect fix: previously the
            # daily-loss check (below) would have returned first and this
            # branch would never have run.
            self.state = RiskState.HALTED
            if daily_loss_breached:
                self.state_reason = (
                    f"Max drawdown limit hit ({drawdown_pct:.1%}) -- halted "
                    f"(daily loss limit also breached simultaneously at {daily_loss_pct:.1%}, "
                    f"HALTED takes precedence)"
                )
            else:
                self.state_reason = f"Max drawdown limit hit ({drawdown_pct:.1%}) -- halted"
            return False, self.state, self.state_reason

        if daily_loss_breached:
            self.state = RiskState.RESTRICTED
            self.state_reason = f"Daily loss limit hit ({daily_loss_pct:.1%}) -- no new entries today"
            return False, self.state, self.state_reason

        if self.state == RiskState.RESTRICTED:
            return False, self.state, self.state_reason

        return True, RiskState.ACTIVE, "ok"
