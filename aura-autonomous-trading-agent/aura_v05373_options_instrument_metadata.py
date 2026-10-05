#!/usr/bin/env python3
"""
AURA v0.5.3.73 -- Options Instrument & Metadata Foundation (Phase O1)

What this module is
------------------------------------------------------------------------
Phase O1 of the options build (`AURA_Options_ETF_Expansion_Scoping_2026-
09-24.md`, see its 2026-10-05 re-scope addendum for the renumbering that
put this module at `.373`). Pattern-analogue of `.334`
(`aura_v05334_asset_instrument_metadata.py`): a SCHEMA/CLASSIFICATION
layer only. It never calls a broker API, never submits an order, never
computes a price or a Greek, and never modifies any existing module. It
answers exactly two questions: "what IS this options contract" (a typed
record: underlying, strike, expiry, right, multiplier, OCC symbol) and
"what IS this options order shape" (an ordered list of 1-4 such
contracts, each with a side and a ratio -- a STRUCTURE, not a scalar
quantity like a stock position).

Everything downstream -- market data (O2), Greeks (O3), the execution
adapter (O4), authorization/replay (O5), exits (O6), cost model (O7),
risk (O8), the supervisor (O9) -- builds on the two record types defined
here. Nothing here decides whether a structure is a "good" structure
(that is O10's job, strategy research, explicitly out of scope for a
metadata layer) -- this module only decides whether a structure is a
*coherent* one: same underlying across every leg, 1-4 legs, valid
sides/ratios, no duplicate legs.

OCC symbol parsing/construction -- ONE canonical implementation
------------------------------------------------------------------------
The 2026-09-29 DELTAX_v2 pattern-reference audit flagged that DELTAX's
own OCC parser is re-implemented three near-identical times across its
codebase (spread builder, exit builder, event helpers) rather than
shared -- "a minor smell... AURA should design one canonical parser, not
copy the duplication." This module is that one canonical parser:
`parse_occ_symbol()` / `build_occ_symbol()`, both pure, both tested
round-trip against each other, used everywhere downstream that needs an
OCC symbol rather than re-implemented per caller.

Format (confirmed against Alpaca's actual contract-symbol convention,
not the old paper-only fixed-width-with-space-padding OCC spec):

    ROOT (1-6 uppercase letters) + YYMMDD (6 digits) + C|P (1 char)
    + 8-digit strike, strike-in-dollars * 1000, zero-padded

    e.g. "AAPL240119C00150000" -> AAPL, 2024-01-19, CALL, strike 150.00

Century is assumed 20xx (YY -> 2000+YY) -- a disclosed assumption, not a
fabricated one: OCC's current 6-digit-date convention postdates 2010,
and nothing in this repository or Alpaca's API will produce a contract
expiring before 2000 or after 2099.

Strike precision: OCC's 8-digit field represents dollars*1000, so the
finest strike this format can express is $0.001. `build_occ_symbol()`
fails closed (never rounds silently) if a caller supplies a strike with
finer resolution than that -- real listed strikes are never that
granular (increments are $0.50/$1/$5 in practice), so this is a
format-correctness check, not a restrictive one.

Single-leg vs multi-leg -- Alpaca's own vocabulary, not an invented one
------------------------------------------------------------------------
`order_class` ("SIMPLE" for 1 leg, "MLEG" for 2-4 legs) is derived from
leg count, never caller-supplied, and deliberately mirrors Alpaca's own
`order_class: simple | mleg` rather than inventing an options-strategy
taxonomy (vertical spread, iron condor, etc.). Classifying a structure's
*strategy name* from its legs is a judgment call that belongs to O10
(strategy research) or a human reading the structure, not to a metadata
layer that must stay objectively verifiable. Per Martin's 2026-10-05
decision, both single-leg (long calls/puts, covered calls, cash-secured
puts) and multi-leg (verticals, iron condors) are in scope -- this
module treats a 1-leg "structure" and a 4-leg one as the same kind of
object, just validated against a different leg-count bound.

What "covered" means, and why it's explicitly NOT modeled here
------------------------------------------------------------------------
A covered call is a single SELL CALL options leg *plus* an existing
stock position outside the options leg entirely -- the "covered" part is
a portfolio-level relationship (does the account hold >= 100 shares per
contract to collateralize the short call), not a property of the
options structure itself. Modeling that relationship belongs to O4
(execution) or O8 (risk), which can see the account's actual equity
holdings; O1 has no concept of an account and must not pretend to.

Fail-closed behaviour, matching `.334`/`.29`/`.31`/`.32`/`.33`
------------------------------------------------------------------------
Every builder raises via fail() (RuntimeError) on the first malformed,
missing, or incoherent field -- no partially-valid record is ever
returned. Classification functions (`classify_order_class()`) are pure
lookups and never raise. verify_*() functions recompute a fingerprint
from a record's own content and compare, the same self-consistency
convention `.334` uses.

What this deliberately does NOT do
------------------------------------------------------------------------
No market data fetch, no Greeks, no pricing, no order submission, no
account/position awareness, no strategy classification, no modification
to any existing file. No real credentials, no network call, anywhere in
this file.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

VERSION = "AURA v0.5.3.73"
ENGINE = "OPTIONS_INSTRUMENT_METADATA"
SCHEMA_VERSION = "1.0"

# ============================================================================
# Canonical enums
# ============================================================================

RIGHTS = {"CALL", "PUT"}
_RIGHT_TO_OCC_CHAR = {"CALL": "C", "PUT": "P"}
_OCC_CHAR_TO_RIGHT = {v: k for k, v in _RIGHT_TO_OCC_CHAR.items()}

SIDES = {"BUY", "SELL"}

ORDER_CLASSES = {"SIMPLE", "MLEG"}

DEFAULT_MULTIPLIER = 100

MIN_LEGS = 1
MAX_LEGS = 4  # Alpaca's multi-leg order API ceiling, per the O4 scoping note

# OCC root: 1-6 uppercase letters. Real strike field: 8 digits (dollars *
# 1000). Date field: 6 digits (YYMMDD). Right: single C or P.
_OCC_SYMBOL_RE = re.compile(r"^([A-Z]{1,6})(\d{2})(\d{2})(\d{2})([CP])(\d{8})$")

_OCC_CENTURY = 2000  # disclosed assumption -- see module docstring
_STRIKE_SCALE = Decimal(1000)


def fail(message: str) -> None:
    raise RuntimeError(message)


def stable_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _underlying_symbol_or_fail(symbol: Any) -> str:
    if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z]{1,6}", symbol):
        fail(f"INVALID_UNDERLYING_SYMBOL:{symbol!r}")
    return symbol


def _strike_decimal_or_fail(strike: Any) -> Decimal:
    try:
        value = Decimal(str(strike))
    except (InvalidOperation, TypeError, ValueError):
        fail(f"INVALID_STRIKE:{strike!r}")
    if not value.is_finite() or value <= 0:
        fail(f"INVALID_STRIKE:{strike!r}")
    scaled = value * _STRIKE_SCALE
    if scaled != scaled.to_integral_value():
        fail(f"STRIKE_FINER_THAN_OCC_PRECISION:{strike!r}")
    if scaled < 0 or scaled > Decimal(99999999):
        fail(f"STRIKE_OUT_OF_OCC_RANGE:{strike!r}")
    return value


def _expiry_date_or_fail(expiry: Any) -> date:
    if not isinstance(expiry, date):
        fail(f"INVALID_EXPIRY:{expiry!r}")
    if expiry.year < _OCC_CENTURY or expiry.year > _OCC_CENTURY + 99:
        fail(f"EXPIRY_OUT_OF_OCC_CENTURY_ASSUMPTION:{expiry!r}")
    return expiry


def _right_or_fail(right: Any) -> str:
    if right not in RIGHTS:
        fail(f"INVALID_RIGHT:{right!r}")
    return right


def _multiplier_or_fail(multiplier: Any) -> int:
    if not isinstance(multiplier, int) or isinstance(multiplier, bool) or multiplier <= 0:
        fail(f"INVALID_MULTIPLIER:{multiplier!r}")
    return multiplier


# ============================================================================
# OCC symbol -- the one canonical parser/builder (see module docstring)
# ============================================================================

def build_occ_symbol(underlying_symbol: str, expiry: date, right: str, strike: Any) -> str:
    """Pure. Fails closed on any malformed input rather than guessing a
    shape. Round-trips exactly with parse_occ_symbol()."""
    symbol = _underlying_symbol_or_fail(underlying_symbol)
    exp = _expiry_date_or_fail(expiry)
    r = _right_or_fail(right)
    strike_decimal = _strike_decimal_or_fail(strike)

    yy = exp.year - _OCC_CENTURY
    date_part = f"{yy:02d}{exp.month:02d}{exp.day:02d}"
    strike_part = f"{int(strike_decimal * _STRIKE_SCALE):08d}"
    return f"{symbol}{date_part}{_RIGHT_TO_OCC_CHAR[r]}{strike_part}"


def parse_occ_symbol(occ_symbol: str) -> dict[str, Any]:
    """Pure. Fails closed on anything not matching the exact OCC shape --
    never partially parses. Returns a plain dict (underlying_symbol,
    expiry: date, right, strike: Decimal) so it composes directly with
    build_option_contract_metadata() below."""
    if not isinstance(occ_symbol, str):
        fail(f"INVALID_OCC_SYMBOL:{occ_symbol!r}")
    match = _OCC_SYMBOL_RE.match(occ_symbol)
    if match is None:
        fail(f"MALFORMED_OCC_SYMBOL:{occ_symbol!r}")
    root, yy, mm, dd, right_char, strike_digits = match.groups()

    try:
        expiry = date(_OCC_CENTURY + int(yy), int(mm), int(dd))
    except ValueError:
        fail(f"INVALID_OCC_EXPIRY_DATE:{occ_symbol!r}")

    strike = Decimal(int(strike_digits)) / _STRIKE_SCALE

    return {
        "underlying_symbol": root,
        "expiry": expiry,
        "right": _OCC_CHAR_TO_RIGHT[right_char],
        "strike": strike,
    }


# ============================================================================
# Option contract metadata -- builder + validator + fingerprint
# ============================================================================

def build_option_contract_metadata(
    *,
    underlying_symbol: str,
    strike: Any,
    expiry: date,
    right: str,
    multiplier: int = DEFAULT_MULTIPLIER,
) -> dict[str, Any]:
    """Fail-closed builder for one canonical OptionContractMetadata
    record. multiplier is an explicit field (never silently assumed
    elsewhere), defaulting to 100 (the near-universal case) but always
    validated and always recorded, never hardcoded downstream."""
    symbol = _underlying_symbol_or_fail(underlying_symbol)
    strike_decimal = _strike_decimal_or_fail(strike)
    exp = _expiry_date_or_fail(expiry)
    r = _right_or_fail(right)
    mult = _multiplier_or_fail(multiplier)

    occ_symbol = build_occ_symbol(symbol, exp, r, strike_decimal)

    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "engine": ENGINE,
        "agent_version": VERSION,
        "underlying_symbol": symbol,
        "strike": str(strike_decimal),
        "expiry": exp.isoformat(),
        "right": r,
        "multiplier": mult,
        "occ_symbol": occ_symbol,
        "contract_fingerprint": None,
    }
    record["contract_fingerprint"] = canonical_option_contract_fingerprint(record)
    return record


def canonical_option_contract_fingerprint(record: dict[str, Any]) -> str:
    canonical = {k: v for k, v in record.items() if k != "contract_fingerprint"}
    return sha256_text(stable_json(canonical))


def verify_option_contract_metadata(record: dict[str, Any]) -> tuple[bool, list[str]]:
    """Self-consistency check, mirroring .334's verify_instrument_metadata()
    convention: recomputes the fingerprint from the record's own content
    and compares. Also re-derives the OCC symbol from the record's own
    fields and confirms it matches the stored one -- catching any record
    that was hand-constructed (not via the builder) with an inconsistent
    OCC symbol."""
    if not isinstance(record, dict):
        return False, ["NOT_A_DICT"]
    problems: list[str] = []

    expected_fingerprint = canonical_option_contract_fingerprint(record)
    if record.get("contract_fingerprint") != expected_fingerprint:
        problems.append("CONTRACT_FINGERPRINT_MISMATCH")

    try:
        expiry = date.fromisoformat(record["expiry"])
        re_derived_occ = build_occ_symbol(
            record["underlying_symbol"], expiry, record["right"], record["strike"],
        )
        if re_derived_occ != record.get("occ_symbol"):
            problems.append("OCC_SYMBOL_INCONSISTENT_WITH_FIELDS")
    except Exception:
        problems.append("UNPARSEABLE_RECORD_FIELDS")

    return (len(problems) == 0), problems


# ============================================================================
# Option structure -- an ordered list of 1-4 legs. A single-leg structure
# and a multi-leg one are the same kind of object (see module docstring).
# ============================================================================

@dataclass(frozen=True, slots=True)
class OptionLegInput:
    """Caller-supplied leg: a contract plus the order-level fields a
    contract alone doesn't carry (side, ratio). Deliberately a plain
    input type, not the built/fingerprinted contract record itself --
    build_option_structure() builds each leg's contract record
    internally so every leg in a structure goes through the exact same
    validation as a standalone contract."""

    underlying_symbol: str
    strike: Any
    expiry: date
    right: str
    side: str
    ratio: int = 1
    multiplier: int = DEFAULT_MULTIPLIER


def classify_order_class(leg_count: int) -> str:
    """Pure, never raises. Mirrors Alpaca's own order_class vocabulary
    (simple | mleg) rather than an invented strategy taxonomy -- see
    module docstring."""
    if leg_count == 1:
        return "SIMPLE"
    if 2 <= leg_count <= MAX_LEGS:
        return "MLEG"
    return "INVALID_LEG_COUNT"


def build_option_structure(legs: list[OptionLegInput]) -> dict[str, Any]:
    """Fail-closed builder for one canonical OptionStructure record.

    Validates (in order, failing on the first violation):
      - leg count within [MIN_LEGS, MAX_LEGS]
      - every leg's contract fields are individually valid (each leg goes
        through build_option_contract_metadata())
      - every leg's side is in SIDES, ratio is a positive int
      - every leg shares the same underlying_symbol -- Alpaca's mleg API
        requires this; a structure spanning two different underlyings is
        not a single order, it's two orders mis-described as one
      - no two legs are identical (same strike/expiry/right/side) -- a
        structure can legitimately hold two legs with the same strike
        and expiry but opposite rights (a straddle) or same right but
        opposite sides at different strikes (a vertical), but never two
        perfectly identical legs, which would just be a larger quantity
        of the same leg mis-described as two legs
    """
    if not isinstance(legs, list) or not (MIN_LEGS <= len(legs) <= MAX_LEGS):
        fail(f"INVALID_LEG_COUNT:{len(legs) if isinstance(legs, list) else legs!r}")

    built_legs: list[dict[str, Any]] = []
    underlyings: set[str] = set()
    seen_leg_keys: set[tuple[str, str, str, str]] = set()

    for index, leg in enumerate(legs):
        if not isinstance(leg, OptionLegInput):
            fail(f"INVALID_LEG_INPUT_TYPE:leg[{index}]={leg!r}")
        if leg.side not in SIDES:
            fail(f"INVALID_LEG_SIDE:leg[{index}]={leg.side!r}")
        if not isinstance(leg.ratio, int) or isinstance(leg.ratio, bool) or leg.ratio <= 0:
            fail(f"INVALID_LEG_RATIO:leg[{index}]={leg.ratio!r}")

        contract = build_option_contract_metadata(
            underlying_symbol=leg.underlying_symbol,
            strike=leg.strike,
            expiry=leg.expiry,
            right=leg.right,
            multiplier=leg.multiplier,
        )
        underlyings.add(contract["underlying_symbol"])

        leg_key = (contract["occ_symbol"], leg.side, str(leg.ratio), str(leg.multiplier))
        if leg_key in seen_leg_keys:
            fail(f"DUPLICATE_LEG:leg[{index}]={leg_key!r}")
        seen_leg_keys.add(leg_key)

        built_legs.append({
            "contract": contract,
            "side": leg.side,
            "ratio": leg.ratio,
        })

    if len(underlyings) != 1:
        fail(f"STRUCTURE_SPANS_MULTIPLE_UNDERLYINGS:{sorted(underlyings)!r}")

    order_class = classify_order_class(len(built_legs))
    if order_class == "INVALID_LEG_COUNT":
        fail(f"INVALID_LEG_COUNT:{len(built_legs)}")

    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "engine": ENGINE,
        "agent_version": VERSION,
        "underlying_symbol": next(iter(underlyings)),
        "order_class": order_class,
        "leg_count": len(built_legs),
        "legs": built_legs,
        "structure_fingerprint": None,
    }
    record["structure_fingerprint"] = canonical_option_structure_fingerprint(record)
    return record


def canonical_option_structure_fingerprint(record: dict[str, Any]) -> str:
    canonical = {k: v for k, v in record.items() if k != "structure_fingerprint"}
    return sha256_text(stable_json(canonical))


def verify_option_structure(record: dict[str, Any]) -> tuple[bool, list[str]]:
    """Self-consistency check: recomputes the structure fingerprint and
    also re-verifies every leg's own contract fingerprint (a structure
    record is only as trustworthy as its weakest leg)."""
    if not isinstance(record, dict):
        return False, ["NOT_A_DICT"]
    problems: list[str] = []

    expected_fingerprint = canonical_option_structure_fingerprint(record)
    if record.get("structure_fingerprint") != expected_fingerprint:
        problems.append("STRUCTURE_FINGERPRINT_MISMATCH")

    legs = record.get("legs")
    if not isinstance(legs, list) or not legs:
        problems.append("NO_LEGS")
    else:
        for index, leg in enumerate(legs):
            contract = leg.get("contract") if isinstance(leg, dict) else None
            if contract is None:
                problems.append(f"LEG_{index}_MISSING_CONTRACT")
                continue
            ok, leg_problems = verify_option_contract_metadata(contract)
            if not ok:
                problems.extend(f"LEG_{index}_{p}" for p in leg_problems)

    return (len(problems) == 0), problems
