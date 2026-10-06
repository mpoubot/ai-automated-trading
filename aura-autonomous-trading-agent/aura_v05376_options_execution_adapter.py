#!/usr/bin/env python3
"""
AURA v0.5.3.76 -- Options Multi-Leg Execution Adapter (Phase O4)

What this module is
------------------------------------------------------------------------
Phase O4 of the options build (`AURA_Options_ETF_Expansion_Scoping_2026-
09-24.md`). Pattern-analogue of `.335` (the Alpaca equity/ETF execution
adapter): the same strict pure-construction / network-only-submission
layering, extended to Alpaca's multi-leg ("MLEG") options order type.
This is the first module in the options build that is allowed to submit
a real (paper) order to Alpaca.

Per Martin's 2026-10-06 design decisions (all confirmed via explicit
clarifying questions before any code was written):

  1. v1 SCOPE: 2-leg VERTICAL SPREADS ONLY. Alpaca's own MLEG order type
     supports 2-4 legs (iron condors, butterflies, ratio spreads), and
     `.373` (O1)'s `build_option_structure()` already validates the full
     1-4 leg range -- but this adapter deliberately narrows itself to
     exactly 2 legs, shaped as a true vertical (see "What counts as a
     vertical" below), for the first real submission path. Extending to
     3-4 leg structures is explicitly deferred to a later phase.
  2. LEG-DIVERGENCE DETECTION: any detected per-leg status or fill
     mismatch is surfaced as its OWN distinct, loud, explicit
     classification -- NEVER silently folded into ordinary
     FILLED/PARTIALLY_FILLED. This module only DETECTS divergence; it
     never attempts automated remediation (no leg-level cancel, no
     unwind, no retry) -- that is explicitly a human-in-the-loop /
     later-phase decision, consistent with the "freeze and let a human
     resolve" philosophy already planned for O4B (assignment/exercise
     reconciliation, not yet built).
  3. ORDER TYPE: LIMIT only. No MARKET support for multi-leg orders --
     Alpaca's signed net-price convention (see below) only has clean,
     unambiguous meaning for a limit order; a market multi-leg order
     would have no net-price field to validate against at all.

============================================================================
0. Pre-code inspection (done before writing anything below, per explicit
   instruction) -- alpaca-py 0.44.0, the version pinned in
   requirements.txt, directly imported and introspected this session,
   not assumed:
============================================================================

- `alpaca.trading.enums.OrderClass` is REAL: `SIMPLE`, `MLEG`, `BRACKET`,
  `OCO`, `OTO`. Multi-leg orders use `MLEG`.
- `alpaca.trading.enums.PositionIntent` is REAL: `BUY_TO_OPEN`,
  `BUY_TO_CLOSE`, `SELL_TO_OPEN`, `SELL_TO_CLOSE` -- the exact same enum
  `.335` already uses for single-leg equity orders, reused here per leg.
- `alpaca.trading.requests.OptionLegRequest` (pydantic model) fields:
  `symbol` (required str -- the OCC symbol), `ratio_qty` (required
  float/int), `side` (optional `OrderSide`), `position_intent` (optional
  `PositionIntent`). Confirmed by direct construction.
- Multi-leg orders are built via the SAME `LimitOrderRequest` class
  single-leg orders use -- there is NO separate MLeg request class.
  Relevant fields for an MLEG order: `order_class=OrderClass.MLEG`,
  `legs=[OptionLegRequest, ...]`, `limit_price`, `qty` (the PARENT /
  spread-unit quantity, not a per-leg quantity), `type` (required
  `OrderType`), `time_in_force` (required), `client_order_id`.
  Top-level `symbol`/`side` are OMITTED entirely for MLEG orders --
  confirmed by direct construction: `LimitOrderRequest(order_class=
  OrderClass.MLEG, qty=1, type=OrderType.LIMIT, time_in_force=
  TimeInForce.DAY, limit_price=-2.50, legs=[...], client_order_id=...)`
  constructs cleanly with no top-level symbol field at all.
- `alpaca.trading.enums.OrderStatus` is a SINGLE SHARED enum for both
  simple and multi-leg orders (`NEW`, `PARTIALLY_FILLED`, `FILLED`,
  `DONE_FOR_DAY`, `CANCELED`, `EXPIRED`, `REPLACED`, `PENDING_CANCEL`,
  `PENDING_REPLACE`, `PENDING_REVIEW`, `ACCEPTED`, `PENDING_NEW`,
  `ACCEPTED_FOR_BIDDING`, `STOPPED`, `REJECTED`, `SUSPENDED`,
  `CALCULATED`, `HELD`).
- **CRITICAL FINDING**: `alpaca.trading.models.Order.model_fields`
  includes `legs: Optional[List[Order]]` -- each leg of a submitted MLEG
  order comes back as its OWN full, independent nested `Order` object,
  each with its OWN `status` / `filled_qty` / `filled_avg_price`. This
  means per-leg fill DIVERGENCE is structurally observable through the
  real API/SDK, despite Alpaca's own documentation describing MLEG
  fills as atomic ("fill together or not at all"). This module takes
  that documentation claim as a design goal, not a guarantee, and
  inspects `order.legs` directly rather than trusting the parent
  `status` alone -- see `classify_multileg_fill_status()` below.
- The SDK does NOT enforce a leg-count cap client-side (a 2-leg and an
  explicit 4-leg `OptionLegRequest` list both constructed without error
  in direct testing) -- the 2-4 leg range (and this module's own
  narrower 2-leg v1 scope) are business rules this module self-enforces,
  not something the SDK will catch for us.
- Limit-price sign convention (Alpaca's own docs, fetched directly):
  NEGATIVE `limit_price` = net CREDIT: POSITIVE = net DEBIT. This
  adapter passes the caller-supplied signed price straight through
  (structurally validated as a finite number) -- it does not second-
  guess the sign against current market pricing, which is a pre-trade
  sizing/pricing decision (O2/O3's domain), not an execution-adapter
  decision.
- Time-in-force for MLEG orders specifically: only `TimeInForce.DAY` was
  exercised in direct SDK construction this session. `GTC`/`IOC` are
  carried through this module using the same `TIF_MAP` `.335` already
  uses, by analogy -- but whether Alpaca's broker-side actually accepts
  a non-DAY TIF for a real MLEG order is UNVERIFIED (this sandbox has
  no live network access to confirm against a real account), per this
  project's CLAIMED/PROVEN/UNVERIFIED discipline. Flagged here, not
  silently assumed.

============================================================================
1. Layering -- same strict separation `.335` established, with one extra
   stage for leg-divergence classification
============================================================================

    option structure (.373, already built)
            |
            v
    validate_vertical_spec()         <- pure, no network call. Narrows
                                         .373's general 1-4 leg structure
                                         down to exactly a 2-leg vertical.
            |
            v
    build_mleg_order_request_spec()  <- pure, deterministic, JSON-
                                         serializable
            |
            v
    to_alpaca_mleg_order_request()   <- pure, builds the real SDK object
            |
            v
    submit()                          <- the ONLY function that touches
                                          the network for a new order;
                                          paper-only; gated by two
                                          independent switches, off by
                                          default. Classifies leg
                                          divergence on its own response.

`fetch_order_status()` is the other network-capable function -- a
READ-ONLY poll (`TradingClient.get_order_by_client_id()`), never a
submission call, so a caller can re-check a previously-submitted
multi-leg order's fill state later without resubmitting. See
`NETWORK_CAPABLE_FUNCTIONS` at the bottom of this file, and the
"real code, no live call" test that walks the module's own source to
confirm no other function references the trading client.

`classify_multileg_fill_status()` is pure (operates on an already-
fetched Order-like object, real or faked) and is reused identically by
`submit()` and `fetch_order_status()`, so the exact same divergence
logic applies whether the order was just submitted or is being polled
later.

============================================================================
2. What counts as a "vertical" -- a structural, machine-checked
   definition, not a vibe
============================================================================

`validate_vertical_spec()` requires, on top of everything `.373`'s
`build_option_structure()` already enforces (same underlying, no
duplicate legs, coherent contracts):

  - exactly 2 legs
  - both legs the SAME right (both CALL, or both PUT) -- a structure
    mixing rights is a straddle/strangle, not a vertical, and is out of
    v1 scope
  - both legs the SAME expiry -- a structure spanning two expiries is a
    diagonal/calendar spread, out of v1 scope
  - DIFFERENT strikes -- same strike + same right + opposite sides would
    be a net-zero, economically meaningless pair, not a real spread
  - a 1:1 ratio on both legs -- ratio spreads (e.g. 1x2) are out of v1
    scope, deferred with the leg-count extension
  - exactly one leg "long-direction" (`OPEN_LONG`/`CLOSE_LONG`) and
    exactly one "short-direction" (`OPEN_SHORT`/`CLOSE_SHORT`) -- a
    vertical always has one bought leg and one sold/written leg
  - both legs in the SAME opening/closing group -- both `OPEN_*` (a
    fresh spread) or both `CLOSE_*` (unwinding an existing spread).
    Opening one leg while closing the other in a single MLEG order is
    not a coherent single operation and is rejected rather than
    silently accepted.

Direction vocabulary reuses `.29`/`.33`/`.335`'s existing canonical
`OPEN_LONG` / `CLOSE_LONG` / `OPEN_SHORT` / `CLOSE_SHORT` labels,
applied per leg instead of per order -- the same
direction -> (side, position_intent) mapping `.335` already uses for
single-leg equity orders, just keyed and applied at leg granularity
here (`LEG_DIRECTION_TO_SIDE_POSITION_INTENT` below). This is a local,
independent definition (this module does not import `.335`), because
the two adapters are siblings extending the same vocabulary, not a
dependency relationship.

============================================================================
3. Leg-divergence classification -- the loud, never-silent detection
   design decision
============================================================================

`classify_multileg_fill_status(order, expected_legs=None)` returns one
of:

  - `CONSISTENT`        -- every leg reports the same status as every
                            other leg AND as the parent order. This is
                            the ONLY classification where
                            `requires_human_attention` is False.
  - `PARENT_LEG_STATUS_MISMATCH` -- all legs agree with each other, but
                            NOT with the parent order's own reported
                            status.
  - `LEG_DIVERGENCE_DETECTED`    -- the legs themselves disagree with
                            each other.
  - `LEG_FILL_RATIO_DIVERGENCE`  -- all legs report the same status
                            (e.g. all `PARTIALLY_FILLED`), but when
                            `expected_legs` (the submitted ratio_qty per
                            leg) is supplied, the observed filled
                            quantities are not proportional across legs
                            -- a subtler form of divergence that a bare
                            status-string comparison would miss.
  - `MISSING_LEG_DATA`   -- `order.legs` is absent/empty for what should
                            be a 2-leg MLEG order. Treated as its own
                            loud failure to classify, never assumed to
                            mean "fine."
  - `LEG_COUNT_MISMATCH` -- the broker returned a different number of
                            legs than was submitted.

None of these is ever merged into a plain `FILLED`/`PARTIALLY_FILLED`
answer. This module raises the flag; it never decides what to DO about
it -- no leg-level cancel, no unwind, no retry, anywhere in this file.

============================================================================
4. What this module deliberately does NOT do
============================================================================

No modification to `.333`, `.335`, or `.373` -- all read-only/imported-
as-vocabulary inputs, verified untouched after this file was written.
No authorization or replay protection (O5's job). No automated
remediation of a detected divergence (explicitly out of scope -- see
§3). No assignment/exercise reconciliation (O4B's job, not yet built).
No pre-trade pricing/sizing/Greeks sanity check against O2/O3 (this
adapter trusts the caller's `limit_price` structurally, not
economically -- that judgment belongs upstream). No live execution --
`TradingClient(..., paper=True)` is mandatory in `main()`, exactly like
`.22`/`.335`'s own invariant. Real submission additionally requires
BOTH `--submit-paper` on the CLI AND
`AURA_ALPACA_OPTIONS_PAPER_ORDERS_ENABLED=true` in the environment -- a
name deliberately distinct from `.335`'s
`AURA_ALPACA_EQUITY_PAPER_ORDERS_ENABLED` and `.22`/`.26`'s
`AURA_ALPACA_PAPER_ORDERS_ENABLED`, so enabling any one of the three can
never accidentally enable either of the others. No real MEXC
credentials anywhere in this file (out of scope entirely).
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from alpaca.trading.enums import OrderClass, OrderSide, OrderType, PositionIntent, TimeInForce
from alpaca.trading.requests import LimitOrderRequest, OptionLegRequest

VERSION = "AURA v0.5.3.76"
ENGINE = "OPTIONS_MULTILEG_EXECUTION_ADAPTER"
SCHEMA_VERSION = "1.0"

ROOT = Path(__file__).resolve().parent

VERTICAL_LEG_COUNT = 2  # v1 scope -- see module docstring §0/§2


def fail(message: str) -> None:
    raise RuntimeError(message)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


# --------------------------------------------------------------------- #
# Dynamic import of .373 -- same _load_*_module() try-import-first
# pattern already established by .30/.31/.335 (read directly from their
# source before writing this file). .373 is used strictly as a
# data/vocabulary source and is never modified.
# --------------------------------------------------------------------- #

def _load_options_metadata_module():
    try:
        import aura_v05373_options_instrument_metadata as mod
        return mod
    except ImportError:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "aura_v05373_options_instrument_metadata",
            ROOT / "aura_v05373_options_instrument_metadata.py",
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return mod


# --------------------------------------------------------------------- #
# Direction -> (side, position_intent) -- per LEG, not per order. See
# module docstring §2. Defined against the real SDK enums first (same
# convention `.335` uses for its own, per-order DIRECTION_TO_SIDE_
# POSITION_INTENT), then mirrored to plain lowercase strings (each
# enum's own `.value`, e.g. OrderSide.BUY.value == "buy") so the
# JSON-serializable order_spec dict never has to guess the wire-format
# casing -- it just reuses whatever the SDK itself considers canonical.
# --------------------------------------------------------------------- #

_LEG_DIRECTION_TO_SIDE_POSITION_INTENT_ENUM: dict[str, tuple[OrderSide, PositionIntent]] = {
    "OPEN_LONG": (OrderSide.BUY, PositionIntent.BUY_TO_OPEN),
    "CLOSE_LONG": (OrderSide.SELL, PositionIntent.SELL_TO_CLOSE),
    "OPEN_SHORT": (OrderSide.SELL, PositionIntent.SELL_TO_OPEN),
    "CLOSE_SHORT": (OrderSide.BUY, PositionIntent.BUY_TO_CLOSE),
}

LEG_DIRECTION_TO_SIDE_POSITION_INTENT: dict[str, tuple[str, str]] = {
    direction: (side.value, intent.value)
    for direction, (side, intent) in _LEG_DIRECTION_TO_SIDE_POSITION_INTENT_ENUM.items()
}

LONG_DIRECTIONS = {"OPEN_LONG", "CLOSE_LONG"}
SHORT_DIRECTIONS = {"OPEN_SHORT", "CLOSE_SHORT"}
OPENING_DIRECTIONS = {"OPEN_LONG", "OPEN_SHORT"}
CLOSING_DIRECTIONS = {"CLOSE_LONG", "CLOSE_SHORT"}

TIF_MAP = {"GTC": TimeInForce.GTC, "IOC": TimeInForce.IOC, "DAY": TimeInForce.DAY}


def _signed_decimal(value: Any, field: str) -> Decimal:
    """Unlike .335's _decimal(), this allows negative (net credit), zero
    (even-money spread), and positive (net debit) values -- see module
    docstring §0 on the sign convention. Only rejects non-numeric or
    non-finite input."""
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        fail(f"INVALID_{field.upper()}")
    if not number.is_finite():
        fail(f"INVALID_{field.upper()}")
    return number


# --------------------------------------------------------------------- #
# PURE: validation. No network call. Fails closed via fail() on the
# first violation -- never returns a partially-valid result.
# --------------------------------------------------------------------- #

def validate_vertical_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """Validates an options execution spec for v1's 2-leg-vertical-only
    MLEG submission. Builds a fresh `.373` OptionStructure from the
    spec's own leg fields (never accepts a pre-built structure as
    trusted input -- same "always independently recompute" discipline
    `.335` uses for instrument metadata), then narrows it down to the
    vertical shape described in the module docstring §2. Returns a
    normalized dict on success; raises RuntimeError (via fail()) on the
    first violation."""
    meta = _load_options_metadata_module()

    if not isinstance(spec, dict):
        fail("EXECUTION_SPEC_NOT_OBJECT")

    raw_legs = spec.get("legs")
    if not isinstance(raw_legs, list) or len(raw_legs) != VERTICAL_LEG_COUNT:
        fail(
            "V1_SCOPE_REQUIRES_EXACTLY_"
            f"{VERTICAL_LEG_COUNT}_LEGS:{len(raw_legs) if isinstance(raw_legs, list) else raw_legs!r}"
        )

    directions: list[str] = []
    leg_inputs = []
    for index, raw_leg in enumerate(raw_legs):
        if not isinstance(raw_leg, dict):
            fail(f"INVALID_LEG_SPEC_TYPE:leg[{index}]={raw_leg!r}")
        direction = raw_leg.get("direction")
        if direction not in LEG_DIRECTION_TO_SIDE_POSITION_INTENT:
            fail(f"INVALID_LEG_DIRECTION:leg[{index}]={direction!r}")
        directions.append(direction)
        # `.373`'s own vocabulary is uppercase BUY/SELL (meta.SIDES),
        # independent of Alpaca's lowercase wire-format enum values --
        # use the SDK enum's `.name` here, never its `.value` (which is
        # reserved for the order_spec / SDK-reconstruction path below).
        side_enum, _ = _LEG_DIRECTION_TO_SIDE_POSITION_INTENT_ENUM[direction]
        leg_inputs.append(meta.OptionLegInput(
            underlying_symbol=raw_leg.get("underlying_symbol"),
            strike=raw_leg.get("strike"),
            expiry=raw_leg.get("expiry"),
            right=raw_leg.get("right"),
            side=side_enum.name,
            ratio=raw_leg.get("ratio", 1),
            multiplier=raw_leg.get("multiplier", meta.DEFAULT_MULTIPLIER),
        ))

    # Fails closed (via meta.fail -> RuntimeError) on: bad leg count,
    # malformed contract fields, mixed underlyings, duplicate legs.
    structure = meta.build_option_structure(leg_inputs)

    if structure["order_class"] != "MLEG" or structure["leg_count"] != VERTICAL_LEG_COUNT:
        fail(f"STRUCTURE_NOT_A_{VERTICAL_LEG_COUNT}_LEG_MLEG:{structure['order_class']}/{structure['leg_count']}")

    rights = {leg["contract"]["right"] for leg in structure["legs"]}
    if len(rights) != 1:
        fail(f"NOT_A_VERTICAL_MIXED_RIGHTS:{sorted(rights)!r}")

    expiries = {leg["contract"]["expiry"] for leg in structure["legs"]}
    if len(expiries) != 1:
        fail(f"NOT_A_VERTICAL_MIXED_EXPIRIES:{sorted(expiries)!r}")

    strikes = [leg["contract"]["strike"] for leg in structure["legs"]]
    if strikes[0] == strikes[1]:
        fail(f"NOT_A_VERTICAL_IDENTICAL_STRIKES:{strikes!r}")

    ratios = {leg["ratio"] for leg in structure["legs"]}
    if ratios != {1}:
        fail(f"V1_SCOPE_REQUIRES_1_TO_1_RATIO:{[leg['ratio'] for leg in structure['legs']]!r}")

    long_count = sum(1 for d in directions if d in LONG_DIRECTIONS)
    short_count = sum(1 for d in directions if d in SHORT_DIRECTIONS)
    if long_count != 1 or short_count != 1:
        fail(f"NOT_A_VERTICAL_REQUIRES_ONE_LONG_ONE_SHORT_LEG:{directions!r}")

    opening_flags = {d in OPENING_DIRECTIONS for d in directions}
    if len(opening_flags) != 1:
        fail(f"MIXED_OPEN_CLOSE_DIRECTIONS_IN_ONE_MLEG_ORDER:{directions!r}")

    order_type = spec.get("order_type")
    if order_type != "LIMIT":
        fail(f"V1_SCOPE_LIMIT_ORDERS_ONLY:{order_type!r}")

    limit_price = _signed_decimal(spec.get("limit_price"), "limit_price")

    qty = spec.get("qty")
    if not isinstance(qty, int) or isinstance(qty, bool) or qty <= 0:
        fail(f"INVALID_QTY:{qty!r}")

    client_order_id = spec.get("client_order_id")
    if not isinstance(client_order_id, str) or not client_order_id.strip():
        fail("MISSING_CLIENT_ORDER_ID")

    time_in_force = spec.get("time_in_force")
    if time_in_force not in TIF_MAP:
        fail(f"INVALID_TIME_IN_FORCE:{time_in_force!r}")

    return {
        "structure": structure,
        "directions": directions,
        "order_type": order_type,
        "limit_price": limit_price,
        "qty": qty,
        "client_order_id": client_order_id,
        "time_in_force": time_in_force,
    }


# --------------------------------------------------------------------- #
# PURE: deterministic order-request construction, separate from
# submission (same discipline `.335` uses). JSON-serializable -- no SDK
# object, no network call.
# --------------------------------------------------------------------- #

def build_mleg_order_request_spec(spec: dict[str, Any]) -> dict[str, Any]:
    validated = validate_vertical_spec(spec)
    structure = validated["structure"]
    directions = validated["directions"]

    legs_out = []
    for leg, direction in zip(structure["legs"], directions):
        side_str, position_intent_str = LEG_DIRECTION_TO_SIDE_POSITION_INTENT[direction]
        legs_out.append({
            "occ_symbol": leg["contract"]["occ_symbol"],
            "ratio_qty": leg["ratio"],
            "side": side_str,
            "position_intent": position_intent_str,
            "direction": direction,
            "contract_fingerprint": leg["contract"]["contract_fingerprint"],
        })

    return {
        "schema_version": SCHEMA_VERSION,
        "engine": ENGINE,
        "agent_version": VERSION,
        "order_class": "MLEG",
        "underlying_symbol": structure["underlying_symbol"],
        "qty": str(validated["qty"]),
        "order_type": validated["order_type"],
        "limit_price": str(validated["limit_price"]),
        "time_in_force": validated["time_in_force"],
        "client_order_id": validated["client_order_id"],
        "legs": legs_out,
        "structure_fingerprint": structure["structure_fingerprint"],
        "built_at": now(),
    }


def to_alpaca_mleg_order_request(order_spec: dict[str, Any]):
    """Pure: builds the real alpaca-py SDK request object from an
    already-validated, deterministic order_spec. Still no network call
    -- constructing a request object does not submit it. No top-level
    symbol/side -- confirmed omitted for MLEG orders, see module
    docstring §0."""
    tif = TIF_MAP.get(order_spec["time_in_force"])
    if tif is None:
        fail(f"INVALID_TIME_IN_FORCE:{order_spec['time_in_force']}")

    legs = [
        OptionLegRequest(
            symbol=leg["occ_symbol"],
            ratio_qty=leg["ratio_qty"],
            side=OrderSide(leg["side"]),
            position_intent=PositionIntent(leg["position_intent"]),
        )
        for leg in order_spec["legs"]
    ]

    limit_price = _signed_decimal(order_spec.get("limit_price"), "limit_price")

    return LimitOrderRequest(
        order_class=OrderClass.MLEG,
        qty=order_spec["qty"],
        type=OrderType.LIMIT,
        time_in_force=tif,
        limit_price=limit_price,
        legs=legs,
        client_order_id=order_spec["client_order_id"],
    )


# --------------------------------------------------------------------- #
# PURE: leg-divergence classification. See module docstring §3. Reused
# identically by submit() and fetch_order_status().
# --------------------------------------------------------------------- #

def classify_multileg_fill_status(
    order: Any,
    expected_legs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """`order` is any object exposing `.status` and `.legs` (a real
    alpaca-py `Order` in production; a faked test double in every test
    in this milestone -- no live Alpaca order required). `expected_legs`
    is the optional `order_spec["legs"]` list (occ_symbol + ratio_qty)
    from the ORIGINAL submission, used only to compute proportional-fill
    cross-checks (LEG_FILL_RATIO_DIVERGENCE) -- omit it to get status-
    only classification.

    Never raises on a malformed/absent order.legs -- that case is itself
    one of the classifications returned (MISSING_LEG_DATA), because the
    whole point of this function is to make absence of clean data loud,
    not to crash or silently assume the best."""
    parent_status = _enum_value(getattr(order, "status", None))
    legs_raw = getattr(order, "legs", None)

    result: dict[str, Any] = {
        "adapter_version": VERSION,
        "observed_at": now(),
        "parent_status": parent_status,
        "requires_human_attention": True,
    }

    if not legs_raw:
        result.update({"classification": "MISSING_LEG_DATA", "legs_detail": []})
        return result

    if expected_legs is not None and len(legs_raw) != len(expected_legs):
        result.update({
            "classification": "LEG_COUNT_MISMATCH",
            "expected_leg_count": len(expected_legs),
            "observed_leg_count": len(legs_raw),
            "legs_detail": [],
        })
        return result

    legs_detail = [
        {
            "symbol": getattr(leg, "symbol", None),
            "status": _enum_value(getattr(leg, "status", None)),
            "filled_qty": getattr(leg, "filled_qty", None),
            "filled_avg_price": getattr(leg, "filled_avg_price", None),
        }
        for leg in legs_raw
    ]
    result["legs_detail"] = legs_detail

    statuses = {d["status"] for d in legs_detail}

    if len(statuses) != 1:
        result.update({"classification": "LEG_DIVERGENCE_DETECTED"})
        return result

    (common_status,) = statuses
    if common_status != parent_status:
        result.update({
            "classification": "PARENT_LEG_STATUS_MISMATCH",
            "common_leg_status": common_status,
        })
        return result

    # OrderStatus's real SDK values are lowercase ("filled",
    # "partially_filled", ...), confirmed by direct enum inspection --
    # not the uppercase names a comment-skim might expect.
    if expected_legs is not None and common_status in ("filled", "partially_filled"):
        ratio_by_symbol = {e["occ_symbol"]: e["ratio_qty"] for e in expected_legs}
        fill_ratios: list[Decimal] = []
        for detail in legs_detail:
            expected_ratio = ratio_by_symbol.get(detail["symbol"])
            filled_qty = detail["filled_qty"]
            if expected_ratio is None or filled_qty is None:
                continue
            try:
                fill_ratios.append(Decimal(str(filled_qty)) / Decimal(str(expected_ratio)))
            except (InvalidOperation, TypeError, ValueError, ZeroDivisionError):
                continue
        if len(fill_ratios) == len(legs_detail) and len(fill_ratios) >= 2:
            spread = max(fill_ratios) - min(fill_ratios)
            if spread > Decimal("0.0001"):
                result.update({
                    "classification": "LEG_FILL_RATIO_DIVERGENCE",
                    "common_leg_status": common_status,
                    "fill_ratios": [str(r) for r in fill_ratios],
                })
                return result

    result.update({
        "classification": "CONSISTENT",
        "status": common_status,
        "requires_human_attention": False,
    })
    return result


# --------------------------------------------------------------------- #
# NETWORK-CAPABLE: the only new-order submission path. Paper-only,
# mandatory. Idempotency preflight, same pattern as .22/.335.
# --------------------------------------------------------------------- #

def submit(order_spec: dict[str, Any], client: Any) -> dict[str, Any]:
    """The ONLY function in this module that submits a new order.
    `client` must already be constructed with paper=True by the caller
    -- this function does not construct a TradingClient itself (main()
    does, hardcoding paper=True, mirroring .22/.335's own invariant
    exactly)."""
    try:
        existing = client.get_order_by_client_id(order_spec["client_order_id"])
    except Exception:
        existing = None

    if existing is not None:
        fill = classify_multileg_fill_status(existing, expected_legs=order_spec["legs"])
        return {
            "adapter_version": VERSION,
            "status": "ALREADY_EXISTS",
            "broker_order_id": str(getattr(existing, "id", None)),
            "client_order_id": order_spec["client_order_id"],
            "observed_at": now(),
            "paper": True,
            "live": False,
            "fill_classification": fill,
        }

    request = to_alpaca_mleg_order_request(order_spec)
    order = client.submit_order(order_data=request)
    fill = classify_multileg_fill_status(order, expected_legs=order_spec["legs"])
    return {
        "adapter_version": VERSION,
        "status": "SUBMITTED",
        "broker_order_id": str(getattr(order, "id", None)),
        "client_order_id": order_spec["client_order_id"],
        "observed_at": now(),
        "paper": True,
        "live": False,
        "fill_classification": fill,
    }


def fetch_order_status(client: Any, client_order_id: str) -> dict[str, Any]:
    """NETWORK-CAPABLE, READ-ONLY: wraps
    TradingClient.get_order_by_client_id() -- never a submission call.
    Lets a caller re-check a previously-submitted multi-leg order's fill
    state later without resubmitting, classified via the exact same
    classify_multileg_fill_status() logic submit() uses. `expected_legs`
    is intentionally omitted here (the caller would need to have kept
    the original order_spec around to supply it) -- status-only
    classification is still fully meaningful on its own."""
    order = client.get_order_by_client_id(client_order_id)
    fill = classify_multileg_fill_status(order)
    return {
        "adapter_version": VERSION,
        "broker_order_id": str(getattr(order, "id", None)),
        "client_order_id": client_order_id,
        "observed_at": now(),
        "fill_classification": fill,
    }


# --------------------------------------------------------------------- #
# CLI -- mirrors .335's exact validate-by-default posture and two-switch
# submission discipline, with a DISTINCT env var name (see module
# docstring §4).
# --------------------------------------------------------------------- #

OPTIONS_PAPER_ORDERS_ENV_VAR = "AURA_ALPACA_OPTIONS_PAPER_ORDERS_ENABLED"


def _parse_spec_legs_dates(spec: dict[str, Any]) -> dict[str, Any]:
    """JSON has no date type -- CLI input legs carry `expiry` as an ISO
    "YYYY-MM-DD" string; this converts it to a real date object before
    validation, the same way any CLI boundary in this repo turns wire
    JSON into typed Python values. Does not mutate the caller's dict."""
    legs = spec.get("legs")
    if not isinstance(legs, list):
        return spec
    new_legs = []
    for leg in legs:
        if isinstance(leg, dict) and isinstance(leg.get("expiry"), str):
            leg = {**leg, "expiry": date.fromisoformat(leg["expiry"])}
        new_legs.append(leg)
    return {**spec, "legs": new_legs}


def main() -> int:
    from alpaca.trading.client import TradingClient

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="Options execution spec JSON (2-leg vertical)")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--submit-paper", action="store_true",
                         help="Actually submit to Alpaca PAPER; otherwise validate/build only.")
    parser.add_argument("--poll-client-order-id",
                         help="Instead of submitting, poll an existing order's fill status by client_order_id.")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "adapter_version": VERSION,
        "observed_at": now(),
        "paper": True,
        "live": False,
        "status": "BLOCKED",
    }
    try:
        key = os.getenv("ALPACA_PAPER_API_KEY")
        secret = os.getenv("ALPACA_PAPER_SECRET_KEY")

        if args.poll_client_order_id:
            if not key or not secret:
                fail("MISSING_ALPACA_PAPER_CREDENTIALS")
            client = TradingClient(key, secret, paper=True)
            result.update({"status": "POLLED"})
            result.update(fetch_order_status(client, args.poll_client_order_id))
        else:
            raw_spec = json.loads(args.input.read_text(encoding="utf-8"))
            spec = _parse_spec_legs_dates(raw_spec)
            order_spec = build_mleg_order_request_spec(spec)
            result.update({"status": "VALIDATED", "order_spec": order_spec})

            if not args.submit_paper:
                result["status"] = "VALIDATED_NO_SUBMISSION"
            elif os.getenv(OPTIONS_PAPER_ORDERS_ENV_VAR, "false").lower() != "true":
                fail(f"OPTIONS_PAPER_ORDER_SUBMISSION_NOT_ENABLED:{OPTIONS_PAPER_ORDERS_ENV_VAR}")
            else:
                if not key or not secret:
                    fail("MISSING_ALPACA_PAPER_CREDENTIALS")
                client = TradingClient(key, secret, paper=True)
                result.update(submit(order_spec, client))

        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2, default=str) + "\n", encoding="utf-8")
        print(json.dumps(result, indent=2, default=str))
        return 0
    except Exception as exc:
        result.update({"status": "FAIL_CLOSED", "error": f"{type(exc).__name__}: {exc}"})
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2, default=str) + "\n", encoding="utf-8")
        print(f"FAIL-CLOSED: {result['error']}")
        return 1


# Disclosure list, per the requirement to confirm exactly which functions
# in this module are network-capable. Checked directly by a dedicated
# test that this list is complete (no other module-level function
# references `client.` or `TradingClient(`).
NETWORK_CAPABLE_FUNCTIONS = frozenset({"fetch_order_status", "submit", "main"})


if __name__ == "__main__":
    raise SystemExit(main())
