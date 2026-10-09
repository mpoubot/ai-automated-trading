#!/usr/bin/env python3
"""AURA O10 -- Strategy Configuration (defined-risk directional spreads).

Every parameter below is a concrete, institutional-style proposed
default -- never `None` -- per explicit instruction: O10 must hand
every downstream consumer (signal mapping, execution constraints,
position sizing) a complete, usable config out of the box, with each
choice's rationale disclosed in a one-line comment rather than buried
in a separate document. Every default is a PROPOSAL Martin should
review against his own risk appetite and broker-specific realities
before any real capital follows it -- none of these numbers are
regulatory minimums or derived from AURA's own live trading history
(AURA has no realized-options-trading track record yet).

RELATIONSHIP TO `.380`'s OWN EXIT-ENGINE DEFAULTS -- READ BEFORE CHANGING
------------------------------------------------------------------------
`profit_target_pct_of_credit` (0.50) and `stop_loss_multiple_of_credit`
(2.00) are chosen to MATCH `aura_v05380_options_exit_engine.py`'s own
`DEFAULT_EXIT_CONFIG` exactly (`take_profit_credit_pct=0.50`,
`stop_loss_credit_pct=2.00`) -- these two numbers are meant to be the
SAME policy expressed in two places (O10's strategy-level statement of
intent, `.380`'s actual enforced trigger), not two independently-tuned
figures that happen to coincide. If one changes, the other should be
re-reviewed for whether it should change too.

`dte_exit_threshold` here (21) is DELIBERATELY DIFFERENT from `.380`'s
own `dte_exit_threshold` (3), and this is NOT a drift bug:

  - `.380`'s 3-DTE trigger is its own hard, final, "close no matter
    what" exit -- evaluated against an already-open position, every
    cycle, with priority over everything except KILL_SWITCH/TAKE_
    PROFIT/STOP_LOSS.
  - THIS module's 21-DTE figure is a much-earlier, strategy-level
    "stop opening fresh risk in this name / start paying closer
    attention" signal that O10 or a future orchestrator can act on
    BEFORE `.380`'s own trigger would ever fire -- e.g. by declining
    to open a brand-new spread in a name already inside 21 DTE, since
    a theta-decay credit strategy opened too close to its own exit
    window captures little of its intended premium decay.

  These two numbers are NOT meant to conflict: O10's 21-DTE fires
  first, chronologically, as the more conservative of the two. `.380`
  is never modified by, or even imported by, this module.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class O10StrategyConfig:
    # ---- Expiry targeting -----------------------------------------
    target_dte_min: int = 30  # classic "30-45 DTE" theta-decay sweet spot (DELTAX reference pattern)
    target_dte_max: int = 45  # upper bound of the same window -- avoids excessive gamma risk near expiry

    # ---- Short-leg delta targeting (credit spreads) ----------------
    target_short_leg_delta_min: float = 0.16  # ~1 standard deviation OTM lower bound, classic credit-spread range
    target_short_leg_delta_max: float = 0.30  # upper bound of the same range -- more premium, more assignment risk
    target_short_leg_delta_central: float = 0.20  # the single "moderate confidence" target within [min, max]

    # ---- Long-leg (protective) strike width -------------------------
    long_leg_width_atr_multiple: float = 1.5  # width scales with realized volatility (ATR), 1x-2x range midpoint
    long_leg_width_min_dollars: float = 5.0  # floor so a near-zero-ATR name still gets a meaningfully defined risk

    # ---- Exit policy (mirrors `.380`'s own defaults -- see module docstring) ----
    profit_target_pct_of_credit: float = 0.50  # close at 50% of max credit captured -- matches `.380` exactly
    stop_loss_multiple_of_credit: float = 2.00  # close if cost-to-close reaches 2x credit received -- matches `.380`
    dte_exit_threshold: int = 21  # EARLIER/more conservative than `.380`'s own 3-DTE hard exit -- see module docstring

    # ---- Portfolio-level risk caps ----------------------------------
    max_loss_per_spread_pct_equity: float = 0.01  # cap any single spread's defined max loss at 1% of account equity
    max_aggregate_options_loss_pct_equity: float = 0.10  # cap ALL open options positions' summed max loss at 10%

    # ---- Pre-authorization liquidity gate (see execution_constraints.py) ----
    max_bid_ask_spread_pct_of_mid: float = 0.10  # reject a leg whose quoted spread exceeds 10% of its own mid price
    max_bid_ask_spread_pct_of_mid_short_leg: float = 0.05  # tighter bound for the short leg -- it is the one assigned
    min_open_interest: int = 100  # below this, a fill at a fair price is unlikely; avoid illiquid strikes entirely
    min_prior_day_volume: int = 10  # a near-zero-volume contract's quoted OI may be stale/unreliable

    # ---- Confidence-tier thresholds (multiples of `.350`'s decision_threshold) ----
    moderate_confidence_score_multiple: float = 1.0  # the baseline DECIDE_LONG/DECIDE_SHORT threshold itself
    high_confidence_score_multiple: float = 2.0  # 2x the baseline threshold -- the "go debit / go aggressive" cutoff
