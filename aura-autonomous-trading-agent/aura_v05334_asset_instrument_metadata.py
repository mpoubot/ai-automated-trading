#!/usr/bin/env python3
"""
AURA v0.5.3.34 -- Asset / Instrument Metadata

Milestone 1 of the approved Alpaca-equity execution floor (per Martin's
2026-09-11 GO message and the capability audit that preceded it:
`AURA_capability_audit_and_next_phase_roadmap_2026-09-11.md`). Approved
sequence: .34 Asset/Instrument Metadata -> .35 Alpaca Equity/ETF Execution
Adapter -> .36 Alpaca-Native Execution Authorization -> .37 Alpaca
Replay-Protected Consumption -> .38 Common Execution Supervisor. This
module is .34 ONLY. Per explicit instruction: do not build the equity
adapter yet, do not build an orchestrator, do not enable paper or live
execution, do not modify the MEXC spine or .19/.22.

============================================================================
1. What this module is
============================================================================

The canonical, venue-agnostic knowledge needed to safely classify an
instrument BEFORE any execution spec is built for it: what kind of thing
is it (STOCK / ETF / CRYPTO / FUTURES), what direction capabilities does
that kind of thing structurally have (can you go long, can you go short,
and if short, does that require a broker to lend you the instrument or is
it a native capability of the contract itself), and which venues in this
repository actually support it today.

This is explicitly a SCHEMA/CLASSIFICATION layer, not a live data-fetch
layer and not an adapter. It never calls a broker API, never submits an
order, and never modifies any existing module. Per-symbol facts that can
only come from a real source (shortability, tick size, minimum quantity)
are modeled as fields this module VALIDATES THE SHAPE OF when supplied,
never as values this module invents. A caller (the future .35 adapter,
most naturally) is responsible for actually sourcing those facts, e.g.
from Alpaca's Assets API -- fetching them is explicitly out of scope here,
matching the instruction not to build the adapter yet.

============================================================================
2. Item 1 -- inspection of existing symbol/instrument/asset metadata
   handling in this repository (done before writing any code below)
============================================================================

- `.27` (aura_v05327_mexc_execution_adapter.py) and `.22`
  (aura_v05322_alpaca_paper_execution_adapter.py) each hardcode their own
  narrow SUPPORTED_SYMBOL_PATTERN -- a symbol SHAPE check only, with no
  concept of instrument type, direction capability, or shortability at
  all. Reused verbatim below (see SYMBOL_PATTERNS) rather than re-derived,
  matching this module's own "verify from the code" discipline.
- `.33` (aura_v05333_canonical_execution_specification.py) already
  generalizes those two patterns plus two new schema-only STOCK/ETF
  patterns into its own SYMBOL_PATTERNS dict, and already has an
  ASSET_CLASSES/VENUE_ASSET_CLASS_COMPATIBILITY pair. This module's own
  taxonomy is designed to be a strict superset of `.33`'s (see
  `to_canonical_asset_class()` below and its dedicated compatibility
  test) -- it does not compete with or replace `.33`'s vocabulary, it
  classifies instruments a level below it.
- `.21` (aura_v05321_alpaca_market_data.py)'s `discover_crypto_universe()`
  is the ONLY place in this repository that calls Alpaca's live Assets
  API today. Directly re-read this session: it reads exactly two fields
  off each returned asset -- `tradable` (bool) and `symbol` (str) -- then
  filters to USD-pair-shaped symbols via its own `_USD_PAIR_RE`. It never
  reads `shortable`, `easy_to_borrow`, `marginable`, or `fractionable`.
  This confirms, precisely, what the capability audit already found by
  repo-wide grep: **no shortability, margin, or borrow data is read,
  stored, or acted on anywhere in this codebase.** This module defines the
  shape that data would need to take if/when something starts sourcing
  it; it does not source it itself.
- `.25` (aura_v05325_position_sizing.py) reads live Alpaca PAPER account
  equity and open positions (read-only) but has no per-symbol instrument
  classification of its own -- it consumes whatever symbol `.23` already
  decided to size, with no independent metadata layer beneath it either.
- No other module in the repository (grepped this session, same query the
  audit used: `shortable|easy_to_borrow|margin|short_sell|AssetClass`)
  contains any instrument-classification or shortability logic outside
  this new file and `.33`.

============================================================================
3. Taxonomy (item 2 -- minimum canonical metadata to distinguish STOCK,
   ETF, CRYPTO, FUTURES)
============================================================================

Two axes, deliberately kept separate:

  `instrument_type` -- Martin's explicit four-category axis: STOCK, ETF,
  CRYPTO, FUTURES. This is "what kind of thing is this."

  `contract_class` -- only meaningful for CRYPTO and FUTURES (None for
  STOCK/ETF, which are plain cash equities): SPOT, PERPETUAL_SWAP, or
  DATED_FUTURE. This is "what contractual shape does the exposure take."

Why split these: `.33` already has CRYPTO_FUTURES (MEXC perpetual swap)
and CRYPTO_SPOT (Alpaca spot pair) as two DIFFERENT asset classes with
different direction/leverage semantics, even though both are "crypto."
Collapsing "crypto" into one instrument_type would lose that distinction;
keeping contract_class separate preserves it while still answering
Martin's four-category question directly (`instrument_type` alone answers
it). FUTURES is deliberately modeled as its own instrument_type, distinct
from CRYPTO+PERPETUAL_SWAP (which is MEXC's actual crypto product) --
this is forward-looking scaffolding for a non-crypto dated-future
instrument (e.g. an index or commodity future) that does not exist
anywhere in this repository today. No venue is mapped to it (see
VENUE_COMPATIBILITY below) -- it is NOT_YET_IMPLEMENTED end-to-end, by
honest classification, not by omission. This is a disclosed design
choice, not a discovered fact -- flagged as such rather than presented as
verified.

The mapping to `.33`'s existing ASSET_CLASSES is exact and tested:

    (STOCK, None)                  -> "STOCK"
    (ETF, None)                    -> "ETF"
    (CRYPTO, "SPOT")               -> "CRYPTO_SPOT"
    (CRYPTO, "PERPETUAL_SWAP")     -> "CRYPTO_FUTURES"
    (FUTURES, "DATED_FUTURE")      -> not yet representable in `.33`
                                       (honest NOT_YET_IMPLEMENTED, not a
                                       fabricated mapping)

============================================================================
4. LONG/SHORT capability modeling (items 3-5)
============================================================================

`long_capable` is structurally True for every instrument_type this module
knows about -- going long is always coherent. Modeled explicitly anyway
(never assumed silently) per the instruction to explicitly model it.

`short_mechanism` is the important distinction, and it is NOT a bare bool:

  - "NATIVE" -- shorting is a structural capability of the contract
    itself, requiring no borrow (MEXC-style perpetual swaps, and dated
    futures generally: selling a contract you don't "own" is what a
    futures contract IS). Matches `.27`/`.29`'s existing reduce_only/side
    model exactly -- OPEN_SHORT is just as native there as OPEN_LONG.
  - "REQUIRES_BORROW" -- shorting requires the broker to actually lend you
    the instrument (equities, ETFs, and spot crypto margin shorting).
    This is conditional, per-symbol, and NEVER assumed available.

Per instruction 5 ("do not assume that every stock or ETF is shortable")
and instruction 6 ("do not fabricate market metadata"): this module has
**no hardcoded per-symbol shortability data anywhere**, for any symbol,
real or example. `shortability_status` is always an explicit input,
defaulting to `"UNKNOWN"` when not supplied -- never defaulting to
`"SHORTABLE"`. `classify_direction_capability()` below treats `"UNKNOWN"`
as fail-closed: it returns `"BORROW_UNKNOWN"`, a distinct value from both
"confirmed shortable" and "confirmed not shortable," so a caller can never
mistake absence-of-information for permission.

============================================================================
5. Venue separation (item 7)
============================================================================

`STRUCTURAL_CAPABILITY` (instrument_type/contract_class -> long/short
mechanism/trading_calendar) contains NOTHING venue-specific -- it would be
true of these instrument shapes on any venue. `VENUE_COMPATIBILITY`
(instrument_type/contract_class -> which venues in *this repository*
actually integrate it) is a separate table entirely, so adding a venue
later never touches the structural section, and reasoning about "what IS
this instrument" never requires knowing "who can trade it."

============================================================================
6. Fail-closed behaviour (item 8)
============================================================================

Every builder/validator here raises via fail() (RuntimeError) on the
first malformed, missing, or incompatible field -- no partially-valid
metadata record is ever returned, matching every other module in this
chain (`.27`, `.31`, `.32`, `.33`). classify_direction_capability() is a
pure lookup (like `.33`'s classify_support()) and never raises -- it
returns an honest classification, including for nonsense input.

============================================================================
7. Compatibility with .33 (item 9)
============================================================================

`to_canonical_asset_class()` / `from_canonical_asset_class()` are the
tested bridge. A dedicated regression test round-trips all four of
`.33`'s real ASSET_CLASSES values through this module and confirms exact
equality, and cross-checks this module's own SYMBOL_PATTERNS against
`.33`'s (both were built from the same two real adapter regexes, so they
must agree). This module does not read or import `.33` for its own
logic (no coupling), only in the compatibility test.

============================================================================
8. What this deliberately does NOT do
============================================================================

No adapter. No order construction. No Alpaca or MEXC API call of any
kind (unlike `.21`, `.25`, or the future `.35`). No orchestrator. No
research/intelligence content (news, sentiment, Elliott Wave, sector
rotation) -- those remain upstream of `.33`, per the audit's own §7
finding, and are untouched here. No modification to any existing file --
this module is purely additive, exactly like `.33` was.

No real credentials, no network call, anywhere in this file.
"""
from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any

VERSION = "AURA v0.5.3.34"
ENGINE = "ASSET_INSTRUMENT_METADATA"
SCHEMA_VERSION = "1.0"

# ============================================================================
# Canonical enums
# ============================================================================

INSTRUMENT_TYPES = {"STOCK", "ETF", "CRYPTO", "FUTURES"}

CONTRACT_CLASSES = {"SPOT", "PERPETUAL_SWAP", "DATED_FUTURE"}

# Reused verbatim from v0.5.3.29/.33 -- "the canonical enum already
# established by the repo," not re-derived.
DIRECTIONS = {"OPEN_LONG", "OPEN_SHORT", "CLOSE_LONG", "CLOSE_SHORT"}

SHORTABILITY_STATUSES = {"SHORTABLE", "NOT_SHORTABLE", "UNKNOWN"}
DEFAULT_SHORTABILITY_STATUS = "UNKNOWN"

TRADING_CALENDARS = {"CONTINUOUS_24_7", "EXCHANGE_HOURS"}

VENUES = {"MEXC", "ALPACA"}

SHORT_MECHANISMS = {"NATIVE", "REQUIRES_BORROW", "NOT_APPLICABLE"}

# Direction-capability classification values (distinct from .33's
# SUPPORTED_BY_SCHEMA/SUPPORTED_BY_ADAPTER/NOT_YET_IMPLEMENTED, which is an
# execution-adapter-readiness classification -- this one is a structural/
# capability classification, one layer below).
NATIVE = "NATIVE"
BORROW_CONFIRMED = "BORROW_CONFIRMED"
BORROW_DENIED = "BORROW_DENIED"
BORROW_UNKNOWN = "BORROW_UNKNOWN"
NOT_APPLICABLE = "NOT_APPLICABLE"

# ============================================================================
# Structural capability -- venue-agnostic, per instruction 7. Nothing in
# this table mentions a venue.
# ============================================================================

STRUCTURAL_CAPABILITY: dict[tuple[str, str | None], dict[str, Any]] = {
    ("STOCK", None): {"long_capable": True, "short_mechanism": "REQUIRES_BORROW", "trading_calendar": "EXCHANGE_HOURS"},
    ("ETF", None): {"long_capable": True, "short_mechanism": "REQUIRES_BORROW", "trading_calendar": "EXCHANGE_HOURS"},
    ("CRYPTO", "SPOT"): {"long_capable": True, "short_mechanism": "REQUIRES_BORROW", "trading_calendar": "CONTINUOUS_24_7"},
    ("CRYPTO", "PERPETUAL_SWAP"): {"long_capable": True, "short_mechanism": "NATIVE", "trading_calendar": "CONTINUOUS_24_7"},
    ("FUTURES", "DATED_FUTURE"): {"long_capable": True, "short_mechanism": "NATIVE", "trading_calendar": "EXCHANGE_HOURS"},
}

# ============================================================================
# Venue compatibility -- kept in its own table, deliberately separate from
# STRUCTURAL_CAPABILITY (instruction 7). Reflects ONLY what this repository
# actually integrates today (verified against .27/.22's real symbol
# patterns), never an aspiration.
# ============================================================================

VENUE_COMPATIBILITY: dict[tuple[str, str | None], frozenset[str]] = {
    ("STOCK", None): frozenset({"ALPACA"}),
    ("ETF", None): frozenset({"ALPACA"}),
    ("CRYPTO", "SPOT"): frozenset({"ALPACA"}),
    ("CRYPTO", "PERPETUAL_SWAP"): frozenset({"MEXC"}),
    ("FUTURES", "DATED_FUTURE"): frozenset(),  # NOT_YET_IMPLEMENTED, honestly -- no venue integrates this today
}

# Symbol shape per (instrument_type, contract_class) -- CRYPTO/PERPETUAL_SWAP
# and CRYPTO/SPOT copied verbatim from .27/.22's own real regexes (not
# re-derived); STOCK/ETF are the same schema-level ticker shape .33 uses.
# FUTURES/DATED_FUTURE has no proven wire format anywhere in this repo, so
# no pattern is offered for it -- validate_instrument_metadata() fails
# closed for it rather than guessing a shape.
SYMBOL_PATTERNS: dict[tuple[str, str | None], re.Pattern[str]] = {
    ("STOCK", None): re.compile(r"^[A-Z]{1,5}$"),
    ("ETF", None): re.compile(r"^[A-Z]{1,5}$"),
    ("CRYPTO", "SPOT"): re.compile(r"^[A-Z0-9]{2,10}/USD$"),                 # v0.5.3.22 SUPPORTED_SYMBOL_PATTERN
    ("CRYPTO", "PERPETUAL_SWAP"): re.compile(r"^[A-Z0-9]{2,15}/USDT:USDT$"),  # v0.5.3.27 SUPPORTED_SYMBOL_PATTERN
}


def fail(message: str) -> None:
    raise RuntimeError(message)


def stable_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _decimal_positive_or_none(value: Any, field: str) -> Decimal | None:
    if value is None:
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        fail(f"INVALID_{field.upper()}")
    if not number.is_finite() or number <= 0:
        fail(f"INVALID_{field.upper()}")
    return number


# ============================================================================
# Canonical <-> .33 asset-class bridge (item 9)
# ============================================================================

_TO_CANONICAL_ASSET_CLASS: dict[tuple[str, str | None], str] = {
    ("STOCK", None): "STOCK",
    ("ETF", None): "ETF",
    ("CRYPTO", "SPOT"): "CRYPTO_SPOT",
    ("CRYPTO", "PERPETUAL_SWAP"): "CRYPTO_FUTURES",
}
_FROM_CANONICAL_ASSET_CLASS: dict[str, tuple[str, str | None]] = {
    v: k for k, v in _TO_CANONICAL_ASSET_CLASS.items()
}


def to_canonical_asset_class(instrument_type: str, contract_class: str | None) -> str | None:
    """Maps (instrument_type, contract_class) to one of .33's ASSET_CLASSES
    strings. Returns None -- never a fabricated guess -- for a combination
    .33 cannot yet represent (currently only FUTURES/DATED_FUTURE)."""
    return _TO_CANONICAL_ASSET_CLASS.get((instrument_type, contract_class))


def from_canonical_asset_class(asset_class: str) -> tuple[str, str | None] | None:
    return _FROM_CANONICAL_ASSET_CLASS.get(asset_class)


# ============================================================================
# Direction-capability classification -- pure lookup, never raises, mirrors
# .33's classify_support() shape.
# ============================================================================

def classify_direction_capability(
    instrument_type: str,
    contract_class: str | None,
    direction: str,
    shortability_status: str = DEFAULT_SHORTABILITY_STATUS,
) -> str:
    """Honest, structural classification of whether a given direction is
    even a coherent capability for this instrument -- NOT whether an
    adapter exists to submit it (that remains .33's SUPPORTED_BY_SCHEMA /
    SUPPORTED_BY_ADAPTER / NOT_YET_IMPLEMENTED, a different question one
    layer up). Never fabricates SHORTABLE: an unsupplied or unrecognized
    shortability_status is treated as UNKNOWN, which always classifies as
    BORROW_UNKNOWN for a REQUIRES_BORROW instrument -- fail-closed by
    construction, not by caller discipline."""
    if direction not in DIRECTIONS:
        return NOT_APPLICABLE
    capability = STRUCTURAL_CAPABILITY.get((instrument_type, contract_class))
    if capability is None:
        return NOT_APPLICABLE

    if direction in ("OPEN_LONG", "CLOSE_LONG"):
        return NATIVE if capability["long_capable"] else NOT_APPLICABLE

    # OPEN_SHORT / CLOSE_SHORT
    mechanism = capability["short_mechanism"]
    if mechanism == "NATIVE":
        return NATIVE
    if mechanism == "REQUIRES_BORROW":
        status = shortability_status if shortability_status in SHORTABILITY_STATUSES else DEFAULT_SHORTABILITY_STATUS
        if status == "SHORTABLE":
            return BORROW_CONFIRMED
        if status == "NOT_SHORTABLE":
            return BORROW_DENIED
        return BORROW_UNKNOWN
    return NOT_APPLICABLE


# ============================================================================
# Instrument metadata record -- builder + validator + fingerprint
# ============================================================================

def build_instrument_metadata(
    *,
    instrument_type: str,
    contract_class: str | None,
    symbol: str,
    venue: str,
    shortability_status: str = DEFAULT_SHORTABILITY_STATUS,
    tick_size: Any = None,
    min_quantity: Any = None,
) -> dict[str, Any]:
    """Fail-closed builder for one canonical InstrumentMetadata record.
    tick_size/min_quantity/shortability_status are exactly what the caller
    supplies -- never fabricated, never defaulted to a permissive value.
    Absence is represented honestly (None / "UNKNOWN"), not guessed."""
    if instrument_type not in INSTRUMENT_TYPES:
        fail(f"INVALID_INSTRUMENT_TYPE:{instrument_type!r}")
    if contract_class is not None and contract_class not in CONTRACT_CLASSES:
        fail(f"INVALID_CONTRACT_CLASS:{contract_class!r}")

    capability = STRUCTURAL_CAPABILITY.get((instrument_type, contract_class))
    if capability is None:
        fail(f"INVALID_INSTRUMENT_CONTRACT_COMBINATION:{instrument_type}/{contract_class}")

    if venue not in VENUES:
        fail(f"INVALID_VENUE:{venue!r}")
    compatible_venues = VENUE_COMPATIBILITY.get((instrument_type, contract_class), frozenset())
    if venue not in compatible_venues:
        fail(f"INCOMPATIBLE_VENUE_FOR_INSTRUMENT:{venue}/{instrument_type}/{contract_class}")

    symbol_pattern = SYMBOL_PATTERNS.get((instrument_type, contract_class))
    if symbol_pattern is None:
        fail(f"NO_PROVEN_SYMBOL_SHAPE_FOR_COMBINATION:{instrument_type}/{contract_class}")
    if not isinstance(symbol, str) or not symbol_pattern.match(symbol):
        fail(f"INVALID_SYMBOL_FOR_INSTRUMENT:{symbol!r}")

    if shortability_status not in SHORTABILITY_STATUSES:
        fail(f"INVALID_SHORTABILITY_STATUS:{shortability_status!r}")
    if capability["short_mechanism"] != "REQUIRES_BORROW" and shortability_status != DEFAULT_SHORTABILITY_STATUS:
        # A NATIVE-short instrument (futures/perpetual swap) has no borrow
        # concept at all -- accepting a SHORTABLE/NOT_SHORTABLE claim for
        # one would be recording a fact that doesn't apply, silently
        # implying a borrow check happened when none is even meaningful.
        fail("SHORTABILITY_STATUS_NOT_APPLICABLE_TO_NATIVE_SHORT_INSTRUMENT")

    tick_size_decimal = _decimal_positive_or_none(tick_size, "tick_size")
    min_quantity_decimal = _decimal_positive_or_none(min_quantity, "min_quantity")

    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "engine": ENGINE,
        "agent_version": VERSION,
        "instrument_type": instrument_type,
        "contract_class": contract_class,
        "symbol": symbol,
        "venue": venue,
        "long_capable": capability["long_capable"],
        "short_mechanism": capability["short_mechanism"],
        "trading_calendar": capability["trading_calendar"],
        "shortability_status": shortability_status,
        "tick_size": str(tick_size_decimal) if tick_size_decimal is not None else None,
        "min_quantity": str(min_quantity_decimal) if min_quantity_decimal is not None else None,
        "canonical_asset_class": to_canonical_asset_class(instrument_type, contract_class),
        "metadata_fingerprint": None,
    }
    record["metadata_fingerprint"] = canonical_instrument_metadata_fingerprint(record)
    return record


def canonical_instrument_metadata_fingerprint(record: dict[str, Any]) -> str:
    canonical = {k: v for k, v in record.items() if k != "metadata_fingerprint"}
    return sha256_text(stable_json(canonical))


def verify_instrument_metadata(record: dict[str, Any]) -> tuple[bool, list[str]]:
    """Self-consistency check, mirroring .29/.31/.32/.33's own
    verify_*() convention: recomputes the fingerprint from the record's
    own content and compares."""
    if not isinstance(record, dict):
        return False, ["NOT_A_DICT"]
    expected = canonical_instrument_metadata_fingerprint(record)
    if record.get("metadata_fingerprint") != expected:
        return False, ["METADATA_FINGERPRINT_MISMATCH"]
    return True, []
