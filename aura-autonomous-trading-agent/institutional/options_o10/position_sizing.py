#!/usr/bin/env python3
"""AURA O10 -- Position Sizing (per-trade and aggregate-dollar risk caps).

WHAT THIS MODULE IS, AND IS DELIBERATELY NOT
------------------------------------------------------------------------
Two small, pure, stateless risk-sizing checks that are O10's OWN
responsibility before a proposal is even sized for authorization:

  1. `compute_spread_quantity()` -- how many contracts of a given
     defined-risk spread can be opened without that ONE spread's own
     max loss exceeding `config.max_loss_per_spread_pct_equity` of
     account equity.
  2. `check_aggregate_options_exposure()` -- whether adding one more
     position's max-loss dollar figure to the sum of every OTHER
     already-open options position's max-loss dollar figure would
     breach `config.max_aggregate_options_loss_pct_equity` of account
     equity.

Both are simple dollar-sum arithmetic. The FULL cross-portfolio Greeks
aggregation (net delta/theta/vega exposure across every open options
position, correlated-underlying concentration, etc.) is explicitly a
SEPARATE module another agent is building -- this module does not
attempt any part of that. It owns exactly the two checks named above,
nothing more.

MAX LOSS PER CONTRACT, FOR A DEFINED-RISK VERTICAL
------------------------------------------------------------------------
For a credit spread (short leg sold, long leg bought, strikes `width`
apart), the defined max loss per contract is `(width - credit_received)
* multiplier` -- the worst case is the underlying moving fully through
both strikes, at which point the spread's intrinsic value converges to
`width`, and the trader keeps only the credit collected against that.
For a debit spread (the bull call spread path), the defined max loss
per contract is simply the debit paid (`* multiplier`) -- the worst
case is the entire premium paid being lost. Both figures are computed
here directly from `proposal.spec["limit_price"]` (the net credit/debit,
signed exactly as `.376` defines it: negative=credit, positive=debit)
and the two legs' strikes -- never re-fetching a quote itself.

No network call anywhere in this file.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from strategy_config import O10StrategyConfig

VERSION = "AURA_O10 v1"
ENGINE = "OPTIONS_POSITION_SIZING"

NETWORK_CAPABLE_FUNCTIONS: frozenset[str] = frozenset()

CONTRACT_MULTIPLIER = Decimal("100")  # standard US equity option contract multiplier


def _spread_width_dollars(proposal: Any) -> Decimal:
    legs = proposal.spec["legs"]
    strikes = [Decimal(str(leg["strike"])) for leg in legs]
    return abs(strikes[0] - strikes[1])


def max_loss_per_contract_usd(proposal: Any) -> Decimal:
    """Pure. Defined max loss per ONE contract of `proposal`'s spread,
    in dollars (already multiplied by `CONTRACT_MULTIPLIER`). See
    module docstring for the credit-vs-debit formula."""
    net_price = Decimal(str(proposal.spec["limit_price"]))  # negative=credit, positive=debit, `.376` convention
    width = _spread_width_dollars(proposal)

    if proposal.is_debit:
        # Worst case: the entire debit paid is lost.
        per_share_loss = net_price
    else:
        # net_price is negative (a credit); credit_received = -net_price.
        credit_received = -net_price
        per_share_loss = width - credit_received

    per_share_loss = max(per_share_loss, Decimal("0"))
    return per_share_loss * CONTRACT_MULTIPLIER


def compute_spread_quantity(
    proposal: Any, *, account_equity_usd: float, config: O10StrategyConfig,
) -> int:
    """Pure. Returns the largest non-negative integer `qty` such that

        max_loss_per_contract_usd(proposal) * qty
            <= config.max_loss_per_spread_pct_equity * account_equity_usd

    Returns `0` when even a single contract's defined max loss already
    exceeds the per-spread risk budget (too small an account, or too
    wide/expensive a spread, to justify this trade at all under this
    cap) -- `0` is a documented "don't trade this" signal, never raised
    as an error.
    """
    if account_equity_usd <= 0:
        return 0

    per_contract_loss = max_loss_per_contract_usd(proposal)
    if per_contract_loss <= 0:
        # A defined max loss of zero (or negative, which should not
        # happen for a well-formed vertical) carries no risk-based
        # sizing signal either way -- fail closed to 0 rather than
        # guess an arbitrarily large quantity for a free-looking trade.
        return 0

    budget_usd = Decimal(str(config.max_loss_per_spread_pct_equity)) * Decimal(str(account_equity_usd))
    if budget_usd <= 0:
        return 0

    qty = int(budget_usd // per_contract_loss)  # both operands positive here -- floor division == floor
    return max(qty, 0)


def check_aggregate_options_exposure(
    open_positions_max_loss_sum_usd: float,
    new_position_max_loss_usd: float,
    *,
    account_equity_usd: float,
    config: O10StrategyConfig,
) -> bool:
    """Pure. Returns `True` when adding `new_position_max_loss_usd` to
    the sum of every already-open options position's own defined max
    loss (`open_positions_max_loss_sum_usd`) would stay AT OR BELOW
    `config.max_aggregate_options_loss_pct_equity` of
    `account_equity_usd` -- i.e. `True` means "allowed to proceed",
    `False` means the aggregate cap would be breached.

    This is the per-trade/aggregate-DOLLAR check only -- it has no
    concept of which positions are open, their Greeks, or any
    correlation between underlyings; see module docstring."""
    if account_equity_usd <= 0:
        return False
    if open_positions_max_loss_sum_usd < 0 or new_position_max_loss_usd < 0:
        return False

    cap_usd = config.max_aggregate_options_loss_pct_equity * account_equity_usd
    total_usd = open_positions_max_loss_sum_usd + new_position_max_loss_usd
    return total_usd <= cap_usd
