#!/usr/bin/env python3
"""AURA O10 -- Directional Decision -> Options Structure Mapping.

WHAT THIS MODULE IS
------------------------------------------------------------------------
The missing strategy-research layer between `.350` (the decision
engine -- produces a directional `TradingDecision`, with no concept of
options at all) and `.376`/`.378` (pure vertical-spread construction
and authorization, with no concept of WHICH structure to build or WHY).
`map_decision_to_structure()` is the one function that answers "given
this directional decision, which defined-risk options structure, at
which strikes/expiry, should be proposed" -- and nothing downstream of
it (O4/.376, O5/.378, O6/.380) makes that choice; they only validate
and execute a structure this module has already decided on.

CLAIMED -> DERIVED -> EFFECTIVE pipeline, explicitly tagged
------------------------------------------------------------------------
  - `decision: TradingDecision` (from `.350`) is DERIVED, not CLAIMED --
    it is this codebase's own deterministic scoring output, not a
    vendor-asserted signal. Duck-typed here (accepted as `Any`, read
    only via attribute access) rather than imported, per the explicit
    instruction not to import `.350` itself.
  - Chain quotes (`chain.fetch_chain()`'s bid/ask/open_interest/volume/
    delta) are OBSERVED -- read directly off whatever provider the
    caller supplies (real or `chain_data_feeds.SyntheticFixtureOptionsChain`
    in tests), never recomputed or second-guessed by this module.
  - The `StructureProposal` this module returns (structure name, legs,
    the built `.376`-shaped spec dict) is EFFECTIVE -- this module's own
    strategy decision, built from the DERIVED decision plus OBSERVED
    chain data, and nothing more.

THE MAPPING TABLE (Martin's confirmed v1 structure menu)
------------------------------------------------------------------------
  DECIDE_LONG,  moderate confidence -> BULL PUT SPREAD   (net credit)
  DECIDE_LONG,  high confidence     -> BULL CALL SPREAD  (net DEBIT --
                                        opt-in, flagged, see warning below)
  DECIDE_SHORT, moderate confidence -> BEAR CALL SPREAD  (net credit,
                                        short strike at delta_central)
  DECIDE_SHORT, high confidence     -> BEAR CALL SPREAD  (net credit,
                                        short strike moved closer to the
                                        money, at delta_max -- more
                                        premium, more assignment risk)

"Moderate" vs "high" confidence is purely `abs(final_rank_score)`
compared against `decision_threshold * high_confidence_score_multiple`
(both from `.350`'s `TradingDecision` / `O10StrategyConfig`) -- `.350`'s
own `outcome` field already guarantees `abs(final_rank_score) >=
decision_threshold` for any `DECIDE_LONG`/`DECIDE_SHORT`, so "moderate"
here means the baseline band between that floor and the high-confidence
cutoff. `NO_TRADE` and `ABSTAIN` (and any other/unrecognized outcome)
return `None` -- there is no structure to propose for a decision that
isn't a directional call in the first place.

THE BULL CALL SPREAD IS A DEBIT STRUCTURE -- `.380` CANNOT EXIT IT
------------------------------------------------------------------------
`aura_v05380_options_exit_engine.py`'s `evaluate_exit()` hard-fails
(`V1_SCOPE_REQUIRES_NET_CREDIT_SPREAD`) on any position whose
`entry_net_price >= 0`. A bull call spread, built here as a net DEBIT
by design (buy near-the-money, sell further OTM, same direction as the
credit spreads' width but opposite sign), is exactly such a position.
This module still builds it (Martin's confirmed choice: an opt-in, NOT
default, high-confidence-only path) -- but EVERY `StructureProposal`
for this path sets `is_debit=True`, `requires_custom_exit_logic=True`,
AND carries this exact warning as one of its `rationale` entries:

    "DEBIT STRUCTURE: the existing .380 exit engine cannot manage this
    position; a caller choosing this path must build separate
    debit-spread exit logic before using it live."

A caller that ignores `requires_custom_exit_logic` and feeds this
structure's resulting position into `.380` will get `.380`'s own loud,
documented failure -- this module does not try to prevent that failure
by silently refusing to build the structure; it tries to prevent it by
making the risk impossible to miss on the `StructureProposal` itself.

`orders_enabled` -- HARD, DETERMINISTIC LIVE-ORDER INTERCEPT (2026-10-09)
------------------------------------------------------------------------
Per Martin's explicit directive: every `StructureProposal` this module
returns now also carries `orders_enabled: bool`, computed as `not
requires_custom_exit_logic` -- i.e. `False` for the bull call spread
(and any future debit structure this menu might grow), `True` for every
net-credit structure. This is deliberately NOT a config-driven toggle;
it is derived directly from the one condition it exists to represent
(does `.380` know how to exit this position), so it can never be left
at the wrong value by a forgotten default. `execution_constraints.py`
enforces this flag as an unconditional pre-authorization block (see
that module's docstring) -- restricting live order placement strictly
to net-credit spreads until custom debit-spread exit logic is built and
verified. This module itself does not refuse to BUILD the debit
proposal (same reasoning as `requires_custom_exit_logic` above: make the
risk visible, don't hide the structure) -- `orders_enabled=False` is the
mechanism that stops it from being tradeable live.

STRIKE SELECTION -- DELTA-TARGETED, FAIL-CLOSED WHEN DELTA IS MISSING
------------------------------------------------------------------------
Per `chain_data_feeds.py`'s documented choice: if the chain's contracts
for the needed `(expiry, right)` group carry no usable `delta` at all,
this module does NOT fall back to a price-distance heuristic -- it logs
a clear reason via the module logger and returns `None`. Guessing a
strike from price alone would silently change the strategy's intended
risk profile (a delta-targeted short leg is the entire point of this
structure menu), which this module will not do without the caller
explicitly asking for a different heuristic (not offered in v1).

The "primary" leg (the one selected by delta) is the SHORT leg for
every credit structure, and the LONG (bought) leg for the bull call
spread, which is instead targeted near a flat 0.50-delta approximation
of "at the money" -- `O10StrategyConfig` has no dedicated at-the-money
delta field (its delta fields are explicitly SHORT-leg fields, per the
exact parameter list this module was built against), so 0.50 is used
directly here as a documented, local constant for that one path only.
The other ("protective"/"other") leg's strike is chosen by WIDTH from
the primary strike (`max(long_leg_width_atr_multiple * underlying_atr,
long_leg_width_min_dollars)`), rounded to the nearest strike the chain
actually lists.

WHAT THIS MODULE DELIBERATELY DOES NOT DO
------------------------------------------------------------------------
No quantity/sizing decision (`position_sizing.py`'s job -- every spec
this module builds carries a structurally-valid placeholder `qty=1`
that a caller MUST replace before authorization). No liquidity/spread-
width/OI gating (`execution_constraints.py`'s job, run AFTER this
module, BEFORE `.378.authorize()`). No network call anywhere in this
file -- `chain` is a caller-supplied `OptionsChainProvider`; this
module never constructs one itself. No modification to any existing
AURA file -- `.373`'s real `OptionLegInput`/`build_option_structure`
are imported and used exactly as published.
"""
from __future__ import annotations

import importlib.util
import logging
import os
import sys
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from chain_data_feeds import OptionsChainProvider
from strategy_config import O10StrategyConfig

logger = logging.getLogger(__name__)

VERSION = "AURA_O10 v1"
ENGINE = "OPTIONS_SIGNAL_MAPPING"
STRATEGY_VERSION = "O10_v1"

# At-the-money approximation used ONLY for the bull-call-spread (debit)
# path's long (bought) leg -- see module docstring. Not a config field:
# `O10StrategyConfig`'s delta fields are explicitly SHORT-leg fields.
_BULL_CALL_LONG_LEG_TARGET_DELTA = 0.50

_STAGED_REFERENCE_DIR = Path(
    "/mnt/user-data/uploads/AI automated trading/aura-autonomous-trading-agent"
)


def _load_staged_module(module_name: str, filename: str) -> Any:
    """Imports a REAL staged AURA module (never reimplemented locally),
    mirroring the `_load_*_module()` try-import-then-file-path pattern
    every other module in this build already uses. Tries a plain
    import first (in case the module is already on `sys.path`), then
    falls back to loading it directly from the staged reference
    checkout by file path. Never modifies the source file it loads."""
    if module_name in sys.modules:
        return sys.modules[module_name]
    try:
        return importlib.import_module(module_name)
    except ImportError:
        pass

    candidates = [Path(__file__).resolve().parent / filename]
    _env_core_dir = os.environ.get("AURA_CORE_DIR")
    if _env_core_dir:
        candidates.append(Path(_env_core_dir) / filename)
    candidates.append(_STAGED_REFERENCE_DIR / filename)
    for path in candidates:
        if path.is_file():
            spec = importlib.util.spec_from_file_location(module_name, path)
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
            return module
    raise RuntimeError(
        f"MISSING_DEPENDENCY:{filename} not found on sys.path, next to this "
        f"file, via the AURA_CORE_DIR environment variable"
        + (f" ({_env_core_dir!r})" if _env_core_dir else " (not set)")
        + f", or in the staged reference directory {_STAGED_REFERENCE_DIR!s}"
    )


def load_options_metadata_module() -> Any:
    """The real, unmodified `.373` module -- `OptionLegInput`,
    `build_option_structure`, etc."""
    return _load_staged_module(
        "aura_v05373_options_instrument_metadata",
        "aura_v05373_options_instrument_metadata.py",
    )


def load_execution_adapter_module() -> Any:
    """The real, unmodified `.376` module -- used by `test_o10.py` for
    real interop validation (`validate_vertical_spec`). Not needed by
    `map_decision_to_structure()` itself, which builds `.376`'s spec
    SHAPE directly without calling into `.376`."""
    return _load_staged_module(
        "aura_v05376_options_execution_adapter",
        "aura_v05376_options_execution_adapter.py",
    )


DIRECTIONAL_OUTCOMES = {"DECIDE_LONG", "DECIDE_SHORT"}


@dataclass(frozen=True, slots=True)
class StructureProposal:
    """EFFECTIVE output of this module (see module docstring's
    CLAIMED/DERIVED/EFFECTIVE pipeline)."""

    structure_name: str  # "BULL_PUT_SPREAD" | "BULL_CALL_SPREAD" | "BEAR_CALL_SPREAD"
    legs: list[Any]  # the two `.373.OptionLegInput` instances, in the same order as spec["legs"]
    spec: dict[str, Any]  # `.376`-shaped vertical spec (qty is a PLACEHOLDER -- see module docstring)
    rationale: tuple[str, ...]
    is_debit: bool
    requires_custom_exit_logic: bool
    # Hard, deterministic live-order intercept (Martin's explicit
    # directive, 2026-10-09) -- always `not requires_custom_exit_logic`.
    # See module docstring. Enforced by execution_constraints.py.
    orders_enabled: bool = True


# ============================================================================
# Pure selection helpers
# ============================================================================


def _select_expiry(contracts: list[dict[str, Any]], dte_min: int, dte_max: int, now: datetime) -> date | None:
    as_of = now.date()
    mid_dte = (dte_min + dte_max) / 2.0
    candidates: set[date] = set()
    for record in contracts:
        expiry = record.get("expiry")
        if not isinstance(expiry, date):
            continue
        dte = (expiry - as_of).days
        if dte_min <= dte <= dte_max:
            candidates.add(expiry)
    if not candidates:
        return None
    return min(candidates, key=lambda e: abs((e - as_of).days - mid_dte))


def _group(contracts: list[dict[str, Any]], expiry: date, right: str) -> list[dict[str, Any]]:
    return [c for c in contracts if c.get("expiry") == expiry and c.get("right") == right]


def _select_by_delta(group: list[dict[str, Any]], right: str, target_abs_delta: float) -> dict[str, Any] | None:
    """Fails closed (returns `None`) when no contract in `group` has a
    usable delta -- see module docstring. Never guesses from price."""
    usable = [c for c in group if isinstance(c.get("delta"), (int, float))]
    if not usable:
        return None
    signed_target = target_abs_delta if right == "CALL" else -target_abs_delta
    return min(usable, key=lambda c: abs(float(c["delta"]) - signed_target))


def _select_nearest_strike(
    group: list[dict[str, Any]], target_strike: float, *, exclude_strike: float,
) -> dict[str, Any] | None:
    pool = [c for c in group if c.get("strike") != exclude_strike]
    if not pool:
        return None
    return min(pool, key=lambda c: abs(float(c["strike"]) - target_strike))


def _mid_price(contract: dict[str, Any]) -> Decimal:
    bid = Decimal(str(contract["bid"]))
    ask = Decimal(str(contract["ask"]))
    return (bid + ask) / Decimal("2")


# ============================================================================
# Structure-specific parameters -- see module docstring for the
# "primary leg chosen by delta, other leg chosen by width" framework
# this table drives.
# ============================================================================

_STRUCTURE_PARAMS: dict[str, dict[str, Any]] = {
    "BULL_PUT_SPREAD": {
        "right": "PUT",
        "primary_side": "SELL",
        "other_side": "BUY",
        "width_sign": -1,  # protective leg strike = primary_strike - width (further OTM, below)
    },
    "BEAR_CALL_SPREAD": {
        "right": "CALL",
        "primary_side": "SELL",
        "other_side": "BUY",
        "width_sign": +1,  # protective leg strike = primary_strike + width (further OTM, above)
    },
    "BULL_CALL_SPREAD": {
        "right": "CALL",
        "primary_side": "BUY",
        "other_side": "SELL",
        "width_sign": +1,  # short leg struck above the near-the-money long leg
    },
}

_SIDE_TO_OPEN_DIRECTION = {"BUY": "OPEN_LONG", "SELL": "OPEN_SHORT"}


def map_decision_to_structure(
    decision: Any,
    *,
    chain: OptionsChainProvider,
    underlying_price: float,
    underlying_atr: float,
    now: datetime,
    config: O10StrategyConfig,
) -> StructureProposal | None:
    """The one entry point. See module docstring.

    `decision`: duck-typed `.350.TradingDecision` -- reads `.symbol`,
    `.direction`, `.outcome`, `.final_rank_score`, `.decision_threshold`,
    and (optionally, for provenance only) `.candidate_id`.

    Returns `None` for `NO_TRADE`/`ABSTAIN`/any non-directional outcome,
    or when no suitable chain data is found (empty chain, no expiry in
    the target DTE window, or no usable delta for the leg that needs
    one -- each case logged with a specific reason via the module
    logger before returning).
    """
    outcome = getattr(decision, "outcome", None)
    if outcome not in DIRECTIONAL_OUTCOMES:
        logger.info("O10: outcome=%r is not directional -- no structure proposed", outcome)
        return None

    symbol = getattr(decision, "symbol", None)
    if not isinstance(symbol, str) or not symbol:
        fail_reason = f"INVALID_DECISION_SYMBOL:{symbol!r}"
        logger.warning("O10: %s", fail_reason)
        return None

    final_score = float(getattr(decision, "final_rank_score"))
    threshold = float(getattr(decision, "decision_threshold"))
    high_cutoff = threshold * config.high_confidence_score_multiple
    is_high_confidence = abs(final_score) >= high_cutoff

    if outcome == "DECIDE_LONG":
        if not is_high_confidence:
            structure_name, primary_target_delta = "BULL_PUT_SPREAD", config.target_short_leg_delta_central
        else:
            structure_name, primary_target_delta = "BULL_CALL_SPREAD", _BULL_CALL_LONG_LEG_TARGET_DELTA
    else:  # DECIDE_SHORT
        structure_name = "BEAR_CALL_SPREAD"
        primary_target_delta = (
            config.target_short_leg_delta_central if not is_high_confidence else config.target_short_leg_delta_max
        )

    params = _STRUCTURE_PARAMS[structure_name]
    right = params["right"]
    is_debit = structure_name == "BULL_CALL_SPREAD"
    requires_custom_exit_logic = is_debit

    contracts = chain.fetch_chain(symbol, dte_min=config.target_dte_min, dte_max=config.target_dte_max, now=now)
    if not contracts:
        logger.warning("O10: chain.fetch_chain(%r) returned no contracts -- no structure proposed", symbol)
        return None

    expiry = _select_expiry(contracts, config.target_dte_min, config.target_dte_max, now)
    if expiry is None:
        logger.warning(
            "O10: no expiry for %r falls within the [%d, %d] DTE window -- no structure proposed",
            symbol, config.target_dte_min, config.target_dte_max,
        )
        return None
    dte_days = (expiry - now.date()).days

    group = _group(contracts, expiry, right)
    if not group:
        logger.warning(
            "O10: no %s contracts for %r at expiry=%s -- no structure proposed", right, symbol, expiry.isoformat(),
        )
        return None

    primary_contract = _select_by_delta(group, right, primary_target_delta)
    if primary_contract is None:
        logger.warning(
            "O10: no usable delta data for %r %s contracts at expiry=%s -- failing closed per documented "
            "policy (chain_data_feeds.py), NOT falling back to a price-only strike heuristic",
            symbol, right, expiry.isoformat(),
        )
        return None

    primary_strike = float(primary_contract["strike"])
    width = max(config.long_leg_width_atr_multiple * underlying_atr, config.long_leg_width_min_dollars)
    other_target_strike = primary_strike + params["width_sign"] * width

    other_contract = _select_nearest_strike(group, other_target_strike, exclude_strike=primary_strike)
    if other_contract is None:
        logger.warning(
            "O10: no distinct %s strike available near target=%.2f for %r at expiry=%s -- no structure proposed",
            right, other_target_strike, symbol, expiry.isoformat(),
        )
        return None

    meta = load_options_metadata_module()

    primary_leg_input = meta.OptionLegInput(
        underlying_symbol=symbol,
        strike=str(primary_strike),
        expiry=expiry,
        right=right,
        side=params["primary_side"],
    )
    other_leg_input = meta.OptionLegInput(
        underlying_symbol=symbol,
        strike=str(other_contract["strike"]),
        expiry=expiry,
        right=right,
        side=params["other_side"],
    )

    try:
        structure = meta.build_option_structure([primary_leg_input, other_leg_input])
    except RuntimeError as exc:
        logger.warning(
            "O10: candidate %s structure for %r failed .373's own coherence validation (%s) -- no structure "
            "proposed", structure_name, symbol, exc,
        )
        return None

    primary_mid = _mid_price(primary_contract)
    other_mid = _mid_price(other_contract)
    net_price = (primary_mid if params["primary_side"] == "BUY" else -primary_mid) + (
        other_mid if params["other_side"] == "BUY" else -other_mid
    )

    decision_id = getattr(decision, "candidate_id", None) or getattr(decision, "decision_hash", None)
    client_order_id = f"O10-{structure_name}-{uuid.uuid4().hex[:16]}"
    expires_at = now + timedelta(minutes=5)  # conservative proposal-freshness bound -- see module docstring

    legs_spec = [
        {
            "direction": _SIDE_TO_OPEN_DIRECTION[params["primary_side"]],
            "underlying_symbol": symbol,
            "strike": str(primary_strike),
            "expiry": expiry,
            "right": right,
            "ratio": 1,
            "multiplier": meta.DEFAULT_MULTIPLIER,
        },
        {
            "direction": _SIDE_TO_OPEN_DIRECTION[params["other_side"]],
            "underlying_symbol": symbol,
            "strike": str(other_contract["strike"]),
            "expiry": expiry,
            "right": right,
            "ratio": 1,
            "multiplier": meta.DEFAULT_MULTIPLIER,
        },
    ]

    spec: dict[str, Any] = {
        "legs": legs_spec,
        "order_type": "LIMIT",
        "limit_price": str(net_price),
        "qty": 1,  # PLACEHOLDER -- caller MUST replace via position_sizing.compute_spread_quantity()
        "client_order_id": client_order_id,
        "time_in_force": "DAY",
        "expires_at": expires_at.isoformat(),
        "decision_id": decision_id,
        "strategy_id": structure_name,
        "strategy_version": STRATEGY_VERSION,
        "source_kind": "DETERMINISTIC_SIGNAL",
    }

    confidence_label = "HIGH" if is_high_confidence else "MODERATE"
    rationale = [
        f"decision outcome={outcome} direction={getattr(decision, 'direction', None)} "
        f"confidence={confidence_label} (abs(final_rank_score)={abs(final_score):.4f}, "
        f"decision_threshold={threshold:.4f}, high_confidence_cutoff={high_cutoff:.4f})",
        f"selected expiry={expiry.isoformat()} dte={dte_days} (target window "
        f"[{config.target_dte_min}, {config.target_dte_max}], midpoint-nearest)",
        f"primary ({params['primary_side']}) leg strike={primary_strike} selected by delta targeting "
        f"(target_abs_delta={primary_target_delta}, observed_delta={primary_contract['delta']})",
        f"other ({params['other_side']}) leg strike={other_contract['strike']} selected by width="
        f"{width:.2f} (max(long_leg_width_atr_multiple*ATR, long_leg_width_min_dollars)) from the primary strike, "
        f"rounded to the nearest listed strike",
        f"estimated net {'DEBIT' if is_debit else 'credit'} at mid prices: {net_price}",
        f"structure_fingerprint={structure['structure_fingerprint']} (.373 coherence-validated)",
    ]
    if is_debit:
        rationale.append(
            "DEBIT STRUCTURE: the existing .380 exit engine cannot manage this position; a caller choosing "
            "this path must build separate debit-spread exit logic before using it live."
        )

    return StructureProposal(
        structure_name=structure_name,
        legs=[primary_leg_input, other_leg_input],
        spec=spec,
        rationale=tuple(rationale),
        is_debit=is_debit,
        requires_custom_exit_logic=requires_custom_exit_logic,
        orders_enabled=not requires_custom_exit_logic,
    )
