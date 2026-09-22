"""
tests/test_risk.py

Risk state handling: position sizing math (ATR-based stop, fixed risk-%,
leverage cap/scale-down), and the explicit 3-state RiskState machine
(ACTIVE / RESTRICTED / HALTED) -- specifically that RESTRICTED (daily loss)
and HALTED (max drawdown) are distinct and behave differently (RESTRICTED
clears on a new day; HALTED does not), which is the structural fix for the
old CircuitBreaker's single binary halted flag.

REMEDIATION (PHASE5_INDEPENDENT_AUDIT_2026-09-22.md finding #3, MEDIUM):
the original can_open_new_trades() checked daily-loss first and returned
immediately on a breach, so a SIMULTANEOUS daily-loss + max-drawdown breach
was reported (and persisted) as RESTRICTED instead of the more severe
HALTED, for up to a full trading day. core/risk.py now computes both
conditions unconditionally and applies explicit severity precedence
(HALTED > RESTRICTED > ACTIVE). `test_simultaneous_daily_loss_and_drawdown_breach_reports_halted`
below is the direct regression test for this fix, plus the isolated
daily-loss-only and max-drawdown-only cases and reset/recovery behavior are
covered, and `test_final_risk_state_is_persisted_in_result_record`
(tests/test_reproducibility.py) covers the new ResultRecord fields.
"""
import pytest

from core.risk import RiskConfig, RiskState, SimpleResearchRiskModel, calc_position_plan


def test_calc_position_plan_basic_long():
    cfg = RiskConfig(risk_per_trade_pct=0.02, atr_stop_mult=1.0, max_leverage=5.0, min_leverage=1.0)
    plan = calc_position_plan(entry_price=100.0, atr=2.0, side="long", equity_usdt=1000.0, cfg=cfg)
    assert plan is not None
    assert plan.stop_price == 98.0  # entry - atr*mult
    assert plan.risk_amount_usdt <= 20.0 + 1e-6  # ~2% of 1000
    assert plan.leverage <= cfg.max_leverage


def test_calc_position_plan_basic_short():
    cfg = RiskConfig()
    plan = calc_position_plan(entry_price=100.0, atr=2.0, side="short", equity_usdt=1000.0, cfg=cfg)
    assert plan is not None
    assert plan.stop_price == 102.0  # entry + atr*mult


def test_calc_position_plan_rejects_invalid_atr():
    cfg = RiskConfig()
    assert calc_position_plan(100.0, atr=0.0, side="long", equity_usdt=1000.0, cfg=cfg) is None
    assert calc_position_plan(100.0, atr=-1.0, side="long", equity_usdt=1000.0, cfg=cfg) is None
    assert calc_position_plan(100.0, atr=None, side="long", equity_usdt=1000.0, cfg=cfg) is None


def test_calc_position_plan_leverage_is_capped_not_exceeded():
    # Tiny stop distance relative to equity would imply huge leverage --
    # must be scaled down to max_leverage, never exceed it.
    cfg = RiskConfig(risk_per_trade_pct=0.5, atr_stop_mult=0.001, max_leverage=3.0, min_leverage=1.0)
    plan = calc_position_plan(entry_price=100.0, atr=1.0, side="long", equity_usdt=1000.0, cfg=cfg)
    assert plan is not None
    assert plan.leverage <= 3.0 + 1e-9
    assert plan.position_size_usdt <= 1000.0 * 3.0 + 1e-6


def test_risk_state_starts_active():
    model = SimpleResearchRiskModel(starting_equity=1000.0)
    can_trade, state, _reason = model.can_open_new_trades(1000.0)
    assert can_trade is True
    assert state == RiskState.ACTIVE


def test_daily_loss_limit_restricts_not_halts():
    cfg = RiskConfig(daily_loss_limit_pct=0.08, max_drawdown_limit_pct=0.90)
    model = SimpleResearchRiskModel(starting_equity=1000.0, cfg=cfg)
    model.update(1000.0, "2025-01-01")
    can_trade, state, _reason = model.can_open_new_trades(900.0)  # -10% intraday
    assert can_trade is False
    assert state == RiskState.RESTRICTED


def test_restricted_state_clears_on_new_day():
    cfg = RiskConfig(daily_loss_limit_pct=0.08, max_drawdown_limit_pct=0.90)
    model = SimpleResearchRiskModel(starting_equity=1000.0, cfg=cfg)
    model.update(1000.0, "2025-01-01")
    model.can_open_new_trades(900.0)  # triggers RESTRICTED
    assert model.state == RiskState.RESTRICTED

    model.update(900.0, "2025-01-02")  # new day
    assert model.state == RiskState.ACTIVE
    can_trade, state, _reason = model.can_open_new_trades(900.0)
    assert can_trade is True
    assert state == RiskState.ACTIVE


def test_max_drawdown_halts_and_does_not_clear_on_new_day():
    cfg = RiskConfig(daily_loss_limit_pct=0.90, max_drawdown_limit_pct=0.20)
    model = SimpleResearchRiskModel(starting_equity=1000.0, cfg=cfg)
    model.update(1000.0, "2025-01-01")
    can_trade, state, _reason = model.can_open_new_trades(750.0)  # -25% from peak
    assert can_trade is False
    assert state == RiskState.HALTED

    # A new day must NOT clear HALTED -- it is terminal for the run.
    model.update(750.0, "2025-01-02")
    assert model.state == RiskState.HALTED
    can_trade2, state2, _reason2 = model.can_open_new_trades(750.0)
    assert can_trade2 is False
    assert state2 == RiskState.HALTED


def test_simultaneous_daily_loss_and_drawdown_breach_reports_halted():
    """
    Direct regression test for PHASE5_INDEPENDENT_AUDIT_2026-09-22.md
    finding #3: a single equity value that breaches BOTH the daily-loss
    limit (8% default) and the max-drawdown limit (20% default) at once
    must report the more severe HALTED, never RESTRICTED, and must not
    allow new trades either way. A 25% same-day drop from the peak breaches
    both thresholds simultaneously in one can_open_new_trades() call.
    """
    cfg = RiskConfig(daily_loss_limit_pct=0.08, max_drawdown_limit_pct=0.20)
    model = SimpleResearchRiskModel(starting_equity=1000.0, cfg=cfg)
    model.update(1000.0, "2025-01-01")  # day_start_equity = peak_equity = 1000.0

    can_trade, state, reason = model.can_open_new_trades(750.0)  # -25% both ways
    assert can_trade is False
    assert state == RiskState.HALTED, (
        f"expected HALTED when daily-loss (25% >= 8%) and max-drawdown (25% >= 20%) "
        f"breach simultaneously, got {state} -- this is exactly the masking defect "
        f"the remediation fixes (daily-loss was checked first and returned early, "
        f"before drawdown was ever evaluated)."
    )
    assert "drawdown" in reason.lower() and "halted" in reason.lower()
    assert model.state == RiskState.HALTED  # persisted on the model itself

    # Must remain HALTED (terminal) even across a new day, unlike a
    # daily-loss-only RESTRICTED state, and must still refuse new trades.
    model.update(750.0, "2025-01-02")
    assert model.state == RiskState.HALTED
    can_trade2, state2, _reason2 = model.can_open_new_trades(750.0)
    assert can_trade2 is False
    assert state2 == RiskState.HALTED


def test_daily_loss_only_breach_without_drawdown_stays_restricted():
    """Isolation check: a daily-loss breach with NO simultaneous
    max-drawdown breach must still report RESTRICTED, not HALTED -- the
    severity-precedence fix must not over-trigger HALTED unconditionally."""
    cfg = RiskConfig(daily_loss_limit_pct=0.08, max_drawdown_limit_pct=0.90)
    model = SimpleResearchRiskModel(starting_equity=1000.0, cfg=cfg)
    model.update(1000.0, "2025-01-01")
    can_trade, state, _reason = model.can_open_new_trades(900.0)  # -10% daily, -10% drawdown (< 90% limit)
    assert can_trade is False
    assert state == RiskState.RESTRICTED


def test_max_drawdown_only_breach_without_daily_loss_halts():
    """Isolation check: a max-drawdown breach with NO simultaneous
    daily-loss breach (e.g. the loss accumulated over several days, so
    today's own drop is small) must still report HALTED."""
    cfg = RiskConfig(daily_loss_limit_pct=0.50, max_drawdown_limit_pct=0.20)
    model = SimpleResearchRiskModel(starting_equity=1000.0, cfg=cfg)
    model.update(1000.0, "2025-01-01")
    model.update(850.0, "2025-01-02")  # new day_start_equity = 850, peak stays 1000
    can_trade, state, _reason = model.can_open_new_trades(795.0)  # daily: (850-795)/850=6.5% (<50%); drawdown: (1000-795)/1000=20.5% (>=20%)
    assert can_trade is False
    assert state == RiskState.HALTED
