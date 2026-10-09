#!/usr/bin/env python3
"""AURA O10 -- Pre-Authorization Liquidity / Spread-Width / OI Gate.

WHAT THIS MODULE IS, AND EXACTLY WHERE IT SITS IN THE PIPELINE
------------------------------------------------------------------------
`aura_v05378_options_execution_authorization.py` (O5) is CONFIRMED to
perform ZERO liquidity, spread-width, or open-interest checks --
`.378.NETWORK_CAPABLE_FUNCTIONS` is an empty frozenset and its own
module docstring explicitly defers any "is this contract still
listed/quotable" check to a later phase. Nothing upstream of `.378`
(`.373`/`.376`) checks this either -- they validate STRUCTURAL
coherence (same underlying, valid strikes, vertical shape) only, never
market quality.

This module is that missing check -- a NEW pre-authorization gate, not
a modification to `.378` (which this module never imports, calls, or
alters) and not a replacement for anything `.378` already does. It is
meant to run in exactly this position in the pipeline:

    signal_mapping.map_decision_to_structure()   <- produces a
            |                                       StructureProposal
            v
    execution_constraints.evaluate_execution_constraints()   <- THIS
            |                                       MODULE. Runs here.
            v
    position_sizing.compute_spread_quantity()    <- sets the real qty
            |                                       on the spec
            v
    aura_v05378_options_execution_authorization.authorize()   <- .378,
                                                      UNCHANGED, called
                                                      by a future
                                                      orchestrator (O9)
                                                      -- never from
                                                      inside this file.

A caller that skips this module and sends a proposal straight to
`.378.authorize()` will get no liquidity protection at all -- `.378`
will happily authorize a spread on a strike with zero open interest
and a 400%-of-mid bid/ask spread, because it was never designed to
check that. This module's entire purpose is to be the thing a caller
runs in between, every time, before authorization.

WHAT IS CHECKED, PER LEG
------------------------------------------------------------------------
For each leg in the proposal, this module looks up that leg's CURRENT
quote from the supplied `chain` (by underlying/expiry/strike/right --
never trusting a quote embedded in the proposal itself, since the
proposal may have been built minutes or hours earlier) and blocks if:

  - `open_interest < config.min_open_interest`
  - `volume < config.min_prior_day_volume` (prior trading day's volume,
    per `chain_data_feeds.py`'s documented contract shape)
  - the leg's own bid/ask spread, as a percentage of its own mid price,
    exceeds `config.max_bid_ask_spread_pct_of_mid` -- EXCEPT for the
    leg whose opening side is SELL (the short/written leg), which uses
    the tighter `config.max_bid_ask_spread_pct_of_mid_short_leg` bound
    instead (it is the leg most likely to be assigned, so AURA holds it
    to a stricter liquidity bar than the long/protective leg).

Every leg is evaluated independently and every failing check is
collected -- this module never short-circuits on the first failure, so
a caller sees every reason a proposal was blocked, not just the first
one encountered (same "always evaluate everything, for audit" style
`.380`'s trigger evaluation uses).

No network call anywhere in this file -- `chain` is a caller-supplied
`OptionsChainProvider`; this module only reads its `fetch_chain()`
return value.

`orders_enabled` -- HARD, UNCONDITIONAL INTERCEPT (2026-10-09)
------------------------------------------------------------------------
Per Martin's explicit directive: before any liquidity/spread/OI check
below runs, this module also checks `proposal.orders_enabled` (set by
`signal_mapping.py`, always `False` for the bull call spread and any
other structure flagged `requires_custom_exit_logic=True`). If it is
`False`, this is added to `reasons` unconditionally -- live order
placement stays restricted to net-credit spreads until custom exit
logic for debit structures is built and verified, REGARDLESS of how
good the proposal's liquidity otherwise looks. This check is never
skipped and never overridable by `config` -- it is not a liquidity
threshold, it is a categorical restriction on which structures may be
authorized for live trading at all. Per this module's own "always
evaluate everything" style, the per-leg liquidity checks below still
run and their own reasons (if any) are still collected even when this
check alone is already enough to block -- a caller sees every reason a
proposal is blocked, never just the first one found.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from chain_data_feeds import OptionsChainProvider
from signal_mapping import StructureProposal
from strategy_config import O10StrategyConfig

VERSION = "AURA_O10 v1"
ENGINE = "OPTIONS_EXECUTION_CONSTRAINTS"

NETWORK_CAPABLE_FUNCTIONS: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class ExecutionConstraintVerdict:
    allowed: bool
    reasons: tuple[str, ...]
    evidence: dict[str, Any]


def _find_quote(
    contracts: list[dict[str, Any]], *, expiry: date, strike: Any, right: str,
) -> dict[str, Any] | None:
    target_strike = float(strike)
    for record in contracts:
        if (
            record.get("expiry") == expiry
            and record.get("right") == right
            and abs(float(record.get("strike", float("nan"))) - target_strike) < 1e-6
        ):
            return record
    return None


def _spread_pct_of_mid(bid: Decimal, ask: Decimal) -> Decimal | None:
    mid = (bid + ask) / Decimal("2")
    if mid <= 0:
        return None
    return (ask - bid) / mid


def evaluate_execution_constraints(
    proposal: StructureProposal, *, chain: OptionsChainProvider, config: O10StrategyConfig,
) -> ExecutionConstraintVerdict:
    """Evaluates every leg of `proposal.spec["legs"]` against `config`'s
    liquidity/spread-width/OI bounds, using a FRESH `chain.fetch_chain()`
    lookup (never the stale quotes a proposal may have been built from
    minutes or hours earlier). Returns an `ExecutionConstraintVerdict`
    that is never used to mutate `proposal` -- this module makes a
    recommendation, it does not alter the spec or call `.378` itself.
    """
    legs = proposal.spec.get("legs", [])
    if not legs:
        return ExecutionConstraintVerdict(
            allowed=False, reasons=("NO_LEGS_IN_PROPOSAL",), evidence={},
        )

    underlying_symbol = legs[0].get("underlying_symbol")
    # DTE window wide enough to be certain of catching every leg's own
    # expiry regardless of where it falls inside the strategy's own
    # target window -- a generous, fixed re-fetch window, not reusing
    # `config.target_dte_min/max` (a leg's expiry may now be fewer DTE
    # than when the proposal was first built, due to time having passed).
    now = datetime.now()
    widest_expiry = max((leg.get("expiry") for leg in legs if isinstance(leg.get("expiry"), date)), default=None)
    dte_max = (widest_expiry - now.date()).days + 2 if widest_expiry is not None else 400
    contracts = chain.fetch_chain(underlying_symbol, dte_min=0, dte_max=max(dte_max, 1), now=now)

    reasons: list[str] = []
    evidence: dict[str, Any] = {"legs": [], "orders_enabled": proposal.orders_enabled}

    if not proposal.orders_enabled:
        reasons.append(
            "ORDERS_DISABLED:proposal.orders_enabled=False (requires_custom_exit_logic=True -- "
            ".380 cannot exit a net-debit vertical; live order placement is restricted to "
            "net-credit spreads until custom exit logic exists, per Martin's 2026-10-09 directive)"
        )

    for leg in legs:
        expiry = leg.get("expiry")
        strike = leg.get("strike")
        right = leg.get("right")
        direction = leg.get("direction")
        leg_label = f"{right}@{strike}/{expiry}"
        is_short_leg = direction in ("OPEN_SHORT", "CLOSE_SHORT")

        quote = _find_quote(contracts, expiry=expiry, strike=strike, right=right)
        if quote is None:
            reasons.append(f"NO_CURRENT_QUOTE_FOR_LEG:{leg_label}")
            evidence["legs"].append({"leg": leg_label, "quote_found": False})
            continue

        leg_evidence: dict[str, Any] = {
            "leg": leg_label,
            "quote_found": True,
            "is_short_leg": is_short_leg,
            "open_interest": quote.get("open_interest"),
            "volume": quote.get("volume"),
            "bid": quote.get("bid"),
            "ask": quote.get("ask"),
        }

        open_interest = quote.get("open_interest")
        if not isinstance(open_interest, int) or open_interest < config.min_open_interest:
            reasons.append(
                f"OPEN_INTEREST_TOO_THIN:{leg_label}:open_interest={open_interest} < "
                f"min_open_interest={config.min_open_interest}"
            )

        volume = quote.get("volume")
        if not isinstance(volume, int) or volume < config.min_prior_day_volume:
            reasons.append(
                f"PRIOR_DAY_VOLUME_TOO_THIN:{leg_label}:volume={volume} < "
                f"min_prior_day_volume={config.min_prior_day_volume}"
            )

        try:
            bid = Decimal(str(quote.get("bid")))
            ask = Decimal(str(quote.get("ask")))
        except Exception:
            reasons.append(f"INVALID_QUOTE:{leg_label}")
            evidence["legs"].append(leg_evidence)
            continue

        spread_pct = _spread_pct_of_mid(bid, ask)
        bound = (
            Decimal(str(config.max_bid_ask_spread_pct_of_mid_short_leg))
            if is_short_leg
            else Decimal(str(config.max_bid_ask_spread_pct_of_mid))
        )
        leg_evidence["spread_pct_of_mid"] = str(spread_pct) if spread_pct is not None else None
        leg_evidence["spread_pct_bound"] = str(bound)

        if spread_pct is None or spread_pct > bound:
            reasons.append(
                f"BID_ASK_SPREAD_TOO_WIDE:{leg_label}:spread_pct_of_mid="
                f"{spread_pct if spread_pct is not None else 'UNDEFINED'} > bound={bound}"
                f"{'(short-leg bound)' if is_short_leg else '(long-leg bound)'}"
            )

        evidence["legs"].append(leg_evidence)

    return ExecutionConstraintVerdict(allowed=(len(reasons) == 0), reasons=tuple(reasons), evidence=evidence)
