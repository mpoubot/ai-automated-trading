#!/usr/bin/env python3
"""
AURA v0.5.3.33 -- Canonical Execution Specification / Intent Producer

Martin's explicit framing for this milestone: NOT another MEXC-specific
order builder. This module is the canonical boundary between AURA's
decision/research layer and execution -- venue-agnostic, asset-class-aware,
direction-aware from the start, so a future .33-Supervisor/orchestrator (or
any other consumer) has one schema to validate against regardless of
whether the ultimate venue is MEXC futures or Alpaca.

============================================================================
A. THE CANONICAL EXECUTION-SPEC CONTRACT (per the pre-code verification
   pass required before writing this module)
============================================================================

Grounded directly in `AURA_implementation_spec_execution_authority_and_
mexc_adapter_2026-09-09.md` Sec.1.1-1.3 (the already-governance-approved
`ExecutionIntent` / `origin_reference` data model) -- this module is the
schema-validated realization of that model's producer side, generalized
beyond the MEXC-only wording that document was written in. Every field
below is mapped to one of: (a) that governance document's ExecutionIntent/
origin_reference fields, (b) an existing adapter's actual validate_spec()
requirement (.22 or .27), or (c) Martin's own explicit field list for this
milestone. No field exists "because it sounds useful" -- see the per-field
provenance comments in CanonicalExecutionSpecification's docstring below.

This is also, structurally, the Decision-Seam Validation Rule's boundary
(per the governance doc, added 2026-09-09 after the DELTAX V2 typed-
boundary audit): the exact seam where a value starts driving an execution
decision must itself carry validation, not rely on an upstream check one
hop earlier. This module IS that seam for the execution-spec boundary --
build_canonical_execution_specification() is a real schema/type/enum gate,
not a passthrough.

============================================================================
B. WHICH EXISTING MODULE(S) CURRENTLY PRODUCE ANYTHING SIMILAR
============================================================================

- v0.5.3.23 (aura_v05323_execution_specification_builder.py) produces the
  ALPACA-track wire-format spec directly (exchange="ALPACA", account_mode
  ="PAPER", side/quantity/order_type/time_in_force/client_order_id). It is
  venue-specific (hardwired to .22's exact expected shape), has no
  asset_class field (implicitly crypto -- see finding D below), no
  direction enum beyond BUY/SELL, no leverage/reduce_only concept (correct,
  since .22 has none either), and both its direction allowlists
  (VALIDATED_LONG_ENTRY_REGIME_LABELS / VALIDATED_SHORT_ENTRY_REGIME_LABELS)
  are empty by default -- confirmed by direct read this session, it never
  actually produces a live-reachable spec in current configuration.
- v0.5.3.27 (aura_v05327_mexc_execution_adapter.py)'s validate_spec()
  defines the MEXC-track wire format directly (exchange="MEXC",
  account_mode="LIVE", market_type="swap", side/reduce_only/quantity/
  leverage/client_order_id/expires_at). It is venue-specific by design and
  correctly has no notion of asset class other than MEXC swap.
- v0.5.3.29's DIRECTIONS = {"OPEN_LONG","OPEN_SHORT","CLOSE_LONG",
  "CLOSE_SHORT"} is the one already-canonical, already direction-complete
  enum in the repo -- confirmed by direct read this session -- and is
  reused here as-is (see item F).

Neither .23 nor .27 is a canonical, venue-agnostic schema. Nothing in the
repo currently expresses "one decision, several possible venues."

============================================================================
C. WHAT'S MISSING (closed by this module)
============================================================================

1. A single schema capable of representing a decision BEFORE a venue is
   chosen, so the decision/research layer never has to know MEXC's or
   Alpaca's wire format.
2. An explicit, honest capability classification (SUPPORTED_BY_SCHEMA /
   SUPPORTED_BY_ADAPTER / NOT_YET_IMPLEMENTED) per (asset_class, venue,
   direction) combination -- see finding D and classify_support() below.
3. Deterministic fingerprinting and provenance fields (decision_id,
   strategy_id/version, signal_timestamp, specification_timestamp,
   source_kind, evidence_hash) at the point a decision becomes an
   execution artifact -- these did not exist as named fields anywhere
   upstream of .27/.23 before this module.
4. Explicit `expires_at` support at the spec-producer layer -- closing the
   staleness gap the governance doc's canonical-architecture note flagged
   directly: "neither v05323_execution_specification_builder.py nor the
   .22 adapter stamps or checks a timestamp/TTL on an execution spec."

============================================================================
D. HOW CRYPTO FUTURES / STOCKS / ETFS / LONG / SHORT ACTUALLY FIT TODAY
   (verified against the real repository, not assumed)
============================================================================

*** IMPORTANT FINDING, reported prominently because Martin's instruction
    says this distinction matters: ***

v0.5.3.22 (the module referred to throughout this project as "the Alpaca
adapter") is NOT a stock or ETF adapter. Its own SUPPORTED_SYMBOL_PATTERN
is `^[A-Z0-9]{2,10}/USD$` -- a slash-delimited, USD-quoted PAIR shape (e.g.
"BTC/USD"), which is Alpaca's CRYPTO trading notation. A real equity or ETF
ticker (e.g. "AAPL", "SPY") does NOT match this pattern and would be
rejected by .22's own validate_spec() with UNSUPPORTED_SYMBOL. This is
confirmed directly from the adapter's source, not inferred. So despite
being colloquially called "the stocks track" by module numbering
proximity, the entire .12->.19->.22->.23->.26 chain is Alpaca-paper CRYPTO
(BTC/USD, ETH/USD today), not equities or ETFs.

Consequence: there is currently NO execution adapter anywhere in this
repository that accepts a real stock or ETF ticker. This module's schema
CAN represent asset_class=STOCK / asset_class=ETF (Martin's explicit
"valid LONG stock spec" / "valid LONG ETF spec" test requirements are
satisfied at the SCHEMA level -- see the test file), but the venue
translator for those combinations fails closed with
ASSET_CLASS_NOT_YET_IMPLEMENTED_FOR_VENUE, because no adapter exists to
target. This is NOT invented support -- it is exactly Martin's requested
SUPPORTED_BY_SCHEMA / NOT_YET_IMPLEMENTED distinction, made explicit rather
than silently assumed.

Full classification (see classify_support(), same table, machine-checked):

| asset_class    | venue  | direction            | classification       |
|----------------|--------|-----------------------|-----------------------|
| CRYPTO_FUTURES | MEXC   | OPEN_LONG/CLOSE_LONG  | SUPPORTED_BY_ADAPTER |
| CRYPTO_FUTURES | MEXC   | OPEN_SHORT/CLOSE_SHORT| SUPPORTED_BY_ADAPTER |
| CRYPTO_SPOT    | ALPACA | OPEN_LONG/CLOSE_LONG  | SUPPORTED_BY_ADAPTER |
| CRYPTO_SPOT    | ALPACA | OPEN_SHORT/CLOSE_SHORT| SUPPORTED_BY_SCHEMA  |
| STOCK          | ALPACA | any                   | NOT_YET_IMPLEMENTED  |
| ETF            | ALPACA | any                   | NOT_YET_IMPLEMENTED  |
| (any other asset_class/venue combination not listed above)  | NOT_YET_IMPLEMENTED |

CRYPTO_SPOT/ALPACA OPEN_SHORT/CLOSE_SHORT is SUPPORTED_BY_SCHEMA, not
SUPPORTED_BY_ADAPTER: .22's SIDE_MAP structurally accepts SELL, but Alpaca
crypto is spot with no margin/borrow/reduce_only concept in this adapter --
there is no way to structurally distinguish "sell to open a short" from
"sell to close a long" (both translate to the identical SELL wire order),
and no evidence anywhere in this repo that Alpaca's paper-crypto endpoint
would honor a naked short. This is the asymmetric case Martin's instruction
anticipated: real, schema-representable, but not provably adapter-backed.

Crypto futures (MEXC) is the only combination proven end-to-end through a
real, tested adapter for BOTH directions, because .27+.29's reduce_only/
side pairing gives a structural (not merely permitted) way to distinguish
all four of OPEN_LONG/OPEN_SHORT/CLOSE_LONG/CLOSE_SHORT.

============================================================================
E. WHICH EXISTING MODULES MUST REMAIN UNTOUCHED
============================================================================

.17, .19, .22, .23, .26, .27, .28, .29, .30, .31, .32 are all read-only
inputs to this milestone's design and are NOT modified by this file. This
module is purely additive: a new schema/producer plus pure translator
functions that call .27.validate_spec() / .22.validate_spec() (dynamically
imported, unmodified) to PROVE wire-format compatibility in tests, never to
submit anything and never to alter those modules' behavior.

============================================================================
F. ONE CANONICAL PRODUCER, NOT SEPARATE VENUE-SPECIFIC PRODUCERS
============================================================================

Design chosen: one canonical schema + one canonical constructor
(build_canonical_execution_specification()) + explicit, pure per-venue
translator functions (to_mexc_execution_spec(), to_alpaca_execution_spec())
that each produce exactly the wire shape the real venue adapter already
expects. This satisfies Martin's requirement to determine the abstraction
from evidence rather than assume it: research/decision code should only
ever need to construct ONE object; venue selection happens at the
translation boundary, and each translator's output is proven compatible
with the real adapter's own validate_spec() by direct regression test
(never by re-implementing that adapter's validation logic here).

Direction is v0.5.3.29's own DIRECTIONS enum -- "the canonical enum already
established by the repo" -- reused verbatim rather than inventing a new
one, per the explicit instruction to look for an existing canonical enum
first.

Authorization self-attestation fields required by the venue wire formats
(.27's kill_switch/execution_authorized/live_execution_authorized; .22's
kill_switch/execution_authorized/paper_execution_authorized/live_execution)
are NOT canonical-layer fields -- they do not exist on
CanonicalExecutionSpecification at all. Each translator accepts them ONLY
as explicit keyword-only overrides, defaulted to the fully-blocked/safe
state (kill_switch=True, every *_authorized=False). A caller must
deliberately flip all three away from their safe defaults to produce a
wire spec that could even structurally pass the venue adapter's own
validate_spec() gate -- and even then this module performs no submission
of any kind; it only ever returns a dict. This is the concrete mechanism
by which "do not connect this to live execution" is enforced structurally,
not just by convention.

============================================================================
G. FILENAME / MODULE NUMBER
============================================================================

aura_v05333_canonical_execution_specification.py -- confirmed free
(`ls aura_v0533*.py aura_v0534*.py` returned nothing before this file was
written). NOTE, disclosed per established precedent: the uploaded roadmap
document's own milestone table labels ".33 = Execution Safety/Supervisor."
This module is NOT that -- it is the Execution Specification / Intent
Producer milestone Martin explicitly requested by name in his most recent
instruction, using the next free sequential module number, exactly as
".31"/"the .33.1 mismatch" was handled previously (flag plainly, don't
block on it). Recommend the roadmap table's own numbering be reconciled by
Martin when Execution Safety/Supervisor is actually built (likely .34).

============================================================================
Safety
============================================================================

No real credentials, no network call, no order submission anywhere in this
file. No orchestrator. Not wired into .31's Authorization flow. Not wired
into live execution. This module is a pure producer: given decision-layer
inputs, it returns a validated dict (or raises/fails closed on malformed
input) -- nothing here ever calls a venue's API.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

VERSION = "AURA v0.5.3.33"
ENGINE = "CANONICAL_EXECUTION_SPECIFICATION"
SCHEMA_VERSION = "1.0"

# ============================================================================
# Canonical enums
# ============================================================================

# Reused verbatim from v0.5.3.29 (MEXC Intent Ledger) -- "the canonical
# enum already established by the repo" per the explicit instruction to
# look for one before inventing a new one.
DIRECTIONS = {"OPEN_LONG", "OPEN_SHORT", "CLOSE_LONG", "CLOSE_SHORT"}

# Per Martin's explicit asset-class list for this milestone (Crypto futures
# on MEXC, Stocks/ETFs on Alpaca), plus CRYPTO_SPOT to honestly name what
# the existing .12-.26 "Alpaca track" actually is (see finding D) rather
# than misrepresent it as STOCK.
ASSET_CLASSES = {"CRYPTO_FUTURES", "CRYPTO_SPOT", "STOCK", "ETF"}

VENUES = {"MEXC", "ALPACA"}

ORDER_TYPES = {"MARKET", "LIMIT"}

TIME_IN_FORCE_VALUES = {"GTC", "IOC", "DAY"}

SOURCE_KINDS = {"DETERMINISTIC_SIGNAL", "AI_PROPOSAL", "HUMAN_OVERRIDE"}

# venue -> set of asset classes that venue can even be paired with at the
# SCHEMA level (before adapter-existence is considered at all).
VENUE_ASSET_CLASS_COMPATIBILITY: dict[str, set[str]] = {
    "MEXC": {"CRYPTO_FUTURES"},
    "ALPACA": {"CRYPTO_SPOT", "STOCK", "ETF"},
}

# Symbol shape per asset class. MEXC/CRYPTO_SPOT patterns are copied
# verbatim from the real adapters that already enforce them (.27, .22) --
# not re-derived -- so a canonical spec that passes THIS check is already
# known to be symbol-format-compatible with the real adapter. STOCK/ETF has
# no adapter today (finding D), so this is a standard US-market ticker
# shape, schema-level only, explicitly documented as such.
SYMBOL_PATTERNS: dict[str, re.Pattern[str]] = {
    "CRYPTO_FUTURES": re.compile(r"^[A-Z0-9]{2,15}/USDT:USDT$"),   # v0.5.3.27 SUPPORTED_SYMBOL_PATTERN
    "CRYPTO_SPOT": re.compile(r"^[A-Z0-9]{2,10}/USD$"),            # v0.5.3.22 SUPPORTED_SYMBOL_PATTERN
    "STOCK": re.compile(r"^[A-Z]{1,5}$"),                          # schema-level only -- no adapter yet
    "ETF": re.compile(r"^[A-Z]{1,5}$"),                            # schema-level only -- no adapter yet
}

CLIENT_ORDER_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,48}$")


def fail(message: str) -> None:
    raise RuntimeError(message)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _parse_iso8601(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        fail(f"INVALID_{field.upper()}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        fail(f"INVALID_{field.upper()}")
    if parsed.tzinfo is None:
        fail(f"INVALID_{field.upper()}:NAIVE_DATETIME")
    return parsed


def _decimal_positive(value: Any, field: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        fail(f"INVALID_{field.upper()}")
    if not number.is_finite() or number <= 0:
        fail(f"INVALID_{field.upper()}")
    return number


# ============================================================================
# Capability classification (Martin's explicit SUPPORTED_BY_SCHEMA /
# SUPPORTED_BY_ADAPTER / NOT_YET_IMPLEMENTED requirement -- see finding D)
# ============================================================================

SUPPORTED_BY_SCHEMA = "SUPPORTED_BY_SCHEMA"
SUPPORTED_BY_ADAPTER = "SUPPORTED_BY_ADAPTER"
NOT_YET_IMPLEMENTED = "NOT_YET_IMPLEMENTED"

_ADAPTER_PROVEN_DIRECTIONS: dict[tuple[str, str], set[str]] = {
    ("CRYPTO_FUTURES", "MEXC"): {"OPEN_LONG", "OPEN_SHORT", "CLOSE_LONG", "CLOSE_SHORT"},
    ("CRYPTO_SPOT", "ALPACA"): {"OPEN_LONG", "CLOSE_LONG"},
}


def classify_support(asset_class: str, venue: str, direction: str) -> str:
    """Honest capability classification. Never guesses; every branch is
    grounded in a verified fact cited in this module's docstring (finding
    D). Malformed asset_class/venue/direction still classify cleanly as
    NOT_YET_IMPLEMENTED rather than raising -- this function is a pure
    lookup, used for reporting/introspection, not a validation gate (that
    is build_canonical_execution_specification()'s job)."""
    if asset_class not in ASSET_CLASSES or venue not in VENUES or direction not in DIRECTIONS:
        return NOT_YET_IMPLEMENTED
    if venue not in VENUE_ASSET_CLASS_COMPATIBILITY or asset_class not in VENUE_ASSET_CLASS_COMPATIBILITY[venue]:
        return NOT_YET_IMPLEMENTED
    if asset_class in ("STOCK", "ETF"):
        # SCHEMA can represent it (build_canonical_execution_specification()
        # accepts it, checked above the caller); no adapter anywhere in the
        # repo accepts a plain equity/ETF ticker (finding D), so there is no
        # adapter-backed path for this combination at all today.
        return NOT_YET_IMPLEMENTED
    proven = _ADAPTER_PROVEN_DIRECTIONS.get((asset_class, venue), set())
    if direction in proven:
        return SUPPORTED_BY_ADAPTER
    return SUPPORTED_BY_SCHEMA


# ============================================================================
# Canonical constructor
# ============================================================================

def canonical_client_order_id(
    venue: str,
    asset_class: str,
    symbol: str,
    direction: str,
    decision_id: str,
    signal_timestamp: str,
) -> str:
    """Deterministic, reproducible client_order_id -- generalizes
    v0.5.3.23's build_client_order_id(symbol, safety_state_id,
    signal_state_hash) pattern (same "hash of stable-sorted basis dict"
    approach) beyond the Alpaca track. Never accepted as caller input on
    the canonical spec itself (see build_canonical_execution_specification)
    -- always computed here, from the same fields every time, so the same
    decision always yields the same id (replay-safe by construction) and
    a different decision can never collide onto the same id by chance.

    Output satisfies the INTERSECTION of v0.5.3.27's client_order_id regex
    ([A-Za-z0-9._-]+, <=48 chars) and v0.5.3.22's (non-empty, <=48 chars),
    so it passes through either translator unmodified."""
    basis = stable_json({
        "venue": venue,
        "asset_class": asset_class,
        "symbol": symbol,
        "direction": direction,
        "decision_id": decision_id,
        "signal_timestamp": signal_timestamp,
    })
    return f"AURA-{sha256_text(basis)[:40]}"


def canonical_spec_fingerprint(spec: dict[str, Any]) -> str:
    """Fingerprint over DECISION content only -- deliberately excludes
    spec_fingerprint itself (self-referential) and specification_timestamp
    (wall-clock build time, not decision content). Two independent builds
    of the identical decision (same decision_id/symbol/direction/quantity/
    etc.), even microseconds apart, must fingerprint identically -- that is
    what "deterministic fingerprinting" means for a caller that wants to
    recognize "the same intent," not two different ones, and it is also
    what makes client_order_id (built from the same excluded-timestamp
    basis) and spec_fingerprint agree on what counts as "the same spec.\""""
    canonical = {k: v for k, v in spec.items() if k not in ("spec_fingerprint", "specification_timestamp")}
    return sha256_text(stable_json(canonical))


def build_canonical_execution_specification(
    *,
    asset_class: str,
    venue: str,
    direction: str,
    symbol: str,
    quantity: Any,
    decision_id: str,
    strategy_id: str,
    strategy_version: str,
    signal_timestamp: str,
    source_kind: str = "DETERMINISTIC_SIGNAL",
    order_type: str = "MARKET",
    limit_price: Any = None,
    leverage: Any = None,
    time_in_force: str | None = None,
    expires_at: str | None = None,
    evidence_hash: str | None = None,
    reference_price: Any = None,
) -> dict[str, Any]:
    """The canonical, venue-agnostic Execution Specification. Every branch
    below fails closed via fail() (RuntimeError) on the first malformed or
    incompatible field -- this is the Decision-Seam Validation Rule
    boundary (governance doc Sec.1.4): nothing partially-valid is ever
    returned.

    Field provenance (per verification item A -- nothing added merely
    because it sounded useful):
      asset_class, venue, direction, symbol, quantity, order_type,
        leverage, time_in_force  -> existing adapter requirements (.22, .27)
        generalized to be asked once, venue-agnostically
      decision_id, source_kind, evidence_hash                 -> governance
        doc Sec.1.2 origin_reference (source_id/source_kind/evidence_hash)
      strategy_id, strategy_version                            -> governance
        doc Sec.1.2 origin_reference.source_version, made explicit as two
        fields (identity + version) since AURA's own strategy modules are
        versioned by filename today (.13/.14/.15), not by a single string
      signal_timestamp                                          -> distinct
        from specification_timestamp per governance doc's ExecutionIntent
        created_at vs. the upstream Signal Decision's own timing
      specification_timestamp                                   -> governance
        doc Sec.1.1 ExecutionIntent.created_at
      expires_at                                                 -> governance
        doc Sec.1.1 ExecutionIntent.expires_at / Sec.2.3 TTL, and the
        explicitly flagged existing gap ("neither .23 nor .22 stamps or
        checks a timestamp/TTL on an execution spec")
      client_order_id (computed, not accepted as input)          -> governance
        doc Sec.1.1 + .23's build_client_order_id() precedent, generalized
      spec_fingerprint                                           -> governance
        doc Sec.2.2 spec_fingerprint / Day 2B canonical-JSON SHA-256 pattern,
        adopted as-is
    """
    if asset_class not in ASSET_CLASSES:
        fail(f"INVALID_ASSET_CLASS:{asset_class!r}")
    if venue not in VENUES:
        fail(f"INVALID_VENUE:{venue!r}")
    if direction not in DIRECTIONS:
        fail(f"INVALID_DIRECTION:{direction!r}")
    if venue not in VENUE_ASSET_CLASS_COMPATIBILITY or asset_class not in VENUE_ASSET_CLASS_COMPATIBILITY[venue]:
        fail(f"INCOMPATIBLE_VENUE_ASSET_CLASS:{venue}/{asset_class}")

    symbol_pattern = SYMBOL_PATTERNS[asset_class]
    if not isinstance(symbol, str) or not symbol_pattern.match(symbol):
        fail(f"INVALID_SYMBOL_FOR_ASSET_CLASS:{symbol!r}")

    quantity_decimal = _decimal_positive(quantity, "quantity")

    if order_type not in ORDER_TYPES:
        fail(f"INVALID_ORDER_TYPE:{order_type!r}")
    limit_price_decimal: Decimal | None = None
    if order_type == "LIMIT":
        limit_price_decimal = _decimal_positive(limit_price, "limit_price")
    elif limit_price is not None:
        fail("LIMIT_PRICE_SET_WITHOUT_LIMIT_ORDER_TYPE")

    # reference_price (added v0.5.3.40): the price the decision/sizing
    # calculation was actually made against -- caller-supplied, exactly
    # like quantity, never computed or fetched here (this module performs
    # no financial computation and makes no network call, by design --
    # see module docstring). For a LIMIT order this MUST equal
    # limit_price when both are given (a limit order's own committed
    # price IS its reference price -- Martin's explicit instruction);
    # divergence is a spec-construction error, not a runtime revalidation
    # concern, so it fails closed here rather than being silently
    # resolved one way or the other. For a MARKET order, no such
    # structural anchor exists -- reference_price is optional at this
    # layer; v0.5.3.40's revalidation gate (not this module) is what
    # fails closed (NO_REFERENCE_PRICE_AVAILABLE) if it's absent when a
    # live-price drift check is attempted. This module's job stops at
    # validating and fingerprinting whatever it's given, exactly the same
    # boundary quantity already draws.
    reference_price_decimal: Decimal | None = None
    if reference_price is not None:
        reference_price_decimal = _decimal_positive(reference_price, "reference_price")
        if (
            order_type == "LIMIT"
            and limit_price_decimal is not None
            and reference_price_decimal != limit_price_decimal
        ):
            fail("LIMIT_REFERENCE_PRICE_MISMATCH")

    # Futures-only fields (leverage) must not be imposed on non-futures
    # asset classes -- explicit test requirement.
    is_futures = asset_class == "CRYPTO_FUTURES"
    is_entry = direction in ("OPEN_LONG", "OPEN_SHORT")
    leverage_int: int | None = None
    if is_futures:
        if is_entry:
            if not isinstance(leverage, int) or isinstance(leverage, bool) or leverage < 1:
                fail("MISSING_OR_INVALID_LEVERAGE_FOR_FUTURES_ENTRY")
            leverage_int = leverage
        elif leverage is not None:
            fail("LEVERAGE_NOT_ALLOWED_ON_CLOSE_DIRECTION")
    elif leverage is not None:
        fail(f"INCOMPATIBLE_FUTURES_FIELD_LEVERAGE_ON_NON_FUTURES:{asset_class}")

    reduce_only = (direction in ("CLOSE_LONG", "CLOSE_SHORT")) if is_futures else None

    if time_in_force is not None:
        if time_in_force not in TIME_IN_FORCE_VALUES:
            fail(f"INVALID_TIME_IN_FORCE:{time_in_force!r}")
        if venue == "MEXC":
            fail("TIME_IN_FORCE_NOT_APPLICABLE_TO_MEXC")

    if not isinstance(decision_id, str) or not decision_id.strip():
        fail("MISSING_DECISION_ID")
    if not isinstance(strategy_id, str) or not strategy_id.strip():
        fail("MISSING_STRATEGY_ID")
    if not isinstance(strategy_version, str) or not strategy_version.strip():
        fail("MISSING_STRATEGY_VERSION")
    if source_kind not in SOURCE_KINDS:
        fail(f"INVALID_SOURCE_KIND:{source_kind!r}")

    _parse_iso8601(signal_timestamp, "signal_timestamp")  # validated, format-only check
    specification_timestamp = now()
    spec_dt = _parse_iso8601(specification_timestamp, "specification_timestamp")

    expires_at_out: str | None = None
    if expires_at is not None:
        expires_dt = _parse_iso8601(expires_at, "expires_at")
        if expires_dt <= spec_dt:
            fail("INVALID_EXPIRY:NOT_AFTER_SPECIFICATION_TIMESTAMP")
        expires_at_out = expires_at

    if evidence_hash is not None and (not isinstance(evidence_hash, str) or not evidence_hash.strip()):
        fail("INVALID_EVIDENCE_HASH")

    client_order_id = canonical_client_order_id(
        venue=venue, asset_class=asset_class, symbol=symbol, direction=direction,
        decision_id=decision_id, signal_timestamp=signal_timestamp,
    )
    if not CLIENT_ORDER_ID_RE.match(client_order_id):  # pragma: no cover - structural guarantee
        fail("CLIENT_ORDER_ID_GENERATION_FAILED")

    spec: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "engine": ENGINE,
        "agent_version": VERSION,
        "asset_class": asset_class,
        "venue": venue,
        "direction": direction,
        "symbol": symbol,
        "quantity": str(quantity_decimal),
        "order_type": order_type,
        "limit_price": str(limit_price_decimal) if limit_price_decimal is not None else None,
        "reference_price": str(reference_price_decimal) if reference_price_decimal is not None else None,
        "leverage": leverage_int,
        "reduce_only": reduce_only,
        "time_in_force": time_in_force,
        "client_order_id": client_order_id,
        "decision_id": decision_id,
        "strategy_id": strategy_id,
        "strategy_version": strategy_version,
        "source_kind": source_kind,
        "evidence_hash": evidence_hash,
        "signal_timestamp": signal_timestamp,
        "specification_timestamp": specification_timestamp,
        "expires_at": expires_at_out,
        "support_classification": classify_support(asset_class, venue, direction),
        "spec_fingerprint": None,
    }
    spec["spec_fingerprint"] = canonical_spec_fingerprint(spec)
    return spec


def verify_canonical_specification(spec: dict[str, Any]) -> tuple[bool, list[str]]:
    """Self-consistency check, mirroring the repo-wide convention
    (.29's verify_intent(), .31's verify_safety_state(), .32's
    verify_claim()): recomputes spec_fingerprint from the record's own
    content and compares."""
    if not isinstance(spec, dict):
        return False, ["NOT_A_DICT"]
    expected = canonical_spec_fingerprint(spec)
    if spec.get("spec_fingerprint") != expected:
        return False, ["SPEC_FINGERPRINT_MISMATCH"]
    return True, []


# ============================================================================
# Venue translators
# ============================================================================
# Pure functions: canonical dict in, venue wire-format dict out. Never call
# a network API, never import an adapter's submit()/main(), only ever
# constructed so that the corresponding adapter's own validate_spec() can
# be called against the output in tests -- proof of compatibility, not
# reimplementation of that adapter's logic.

_MEXC_DIRECTION_TO_SIDE_REDUCE: dict[str, tuple[str, bool]] = {
    "OPEN_LONG": ("BUY", False),
    "CLOSE_LONG": ("SELL", True),
    "OPEN_SHORT": ("SELL", False),
    "CLOSE_SHORT": ("BUY", True),
}

_ALPACA_DIRECTION_TO_SIDE: dict[str, str] = {
    "OPEN_LONG": "BUY",
    "CLOSE_LONG": "SELL",
    "OPEN_SHORT": "SELL",
    "CLOSE_SHORT": "BUY",
}


def to_mexc_execution_spec(
    spec: dict[str, Any],
    *,
    kill_switch: bool = True,
    execution_authorized: bool = False,
    live_execution_authorized: bool = False,
) -> dict[str, Any]:
    """Translates a canonical CRYPTO_FUTURES/MEXC spec into exactly
    v0.5.3.27's expected wire shape. Authorization self-attestation fields
    default to the fully-blocked state (see module docstring item F) --
    a caller must deliberately override all three to produce a spec that
    could even structurally pass .27.validate_spec(). This function never
    submits anything; it returns a dict.

    Raises (fails closed) if the canonical spec's asset_class/venue is not
    CRYPTO_FUTURES/MEXC, or if order_type is not MARKET (MEXC's adapter
    supports MARKET only -- LIMIT is explicitly reserved, not buildable
    yet, per the governance-approved implementation spec Sec.1.1)."""
    if spec.get("asset_class") != "CRYPTO_FUTURES" or spec.get("venue") != "MEXC":
        fail(f"ASSET_CLASS_VENUE_NOT_MEXC_FUTURES:{spec.get('asset_class')}/{spec.get('venue')}")
    if spec.get("order_type") != "MARKET":
        fail(f"UNSUPPORTED_ORDER_TYPE_FOR_MEXC:{spec.get('order_type')}")

    direction = spec.get("direction")
    if direction not in _MEXC_DIRECTION_TO_SIDE_REDUCE:
        fail(f"INVALID_DIRECTION_FOR_MEXC_TRANSLATION:{direction!r}")
    side, reduce_only = _MEXC_DIRECTION_TO_SIDE_REDUCE[direction]

    wire: dict[str, Any] = {
        "execution_spec_version": "1.0",
        "exchange": "MEXC",
        "account_mode": "LIVE",
        "market_type": "swap",
        "kill_switch": kill_switch,
        "execution_authorized": execution_authorized,
        "live_execution_authorized": live_execution_authorized,
        "symbol": spec["symbol"],
        "side": side,
        "order_type": "MARKET",
        "reduce_only": reduce_only,
        "quantity": spec["quantity"],
        "client_order_id": spec["client_order_id"],
        # Added v0.5.3.40: .31 only ever sees this wire dict, never the
        # canonical spec, so reference_price must be carried across the
        # translation explicitly or v0.5.3.40's revalidation gate would
        # have nothing to check drift against on the MEXC path. May be
        # None (see build_canonical_execution_specification's docstring
        # note above) -- carried through unchanged either way, never
        # defaulted here.
        "reference_price": spec.get("reference_price"),
    }
    if not reduce_only:
        wire["leverage"] = spec.get("leverage")
    if spec.get("expires_at"):
        wire["expires_at"] = spec["expires_at"]
    return wire


def to_alpaca_execution_spec(
    spec: dict[str, Any],
    *,
    kill_switch: bool = True,
    execution_authorized: bool = False,
    paper_execution_authorized: bool = False,
) -> dict[str, Any]:
    """Translates a canonical CRYPTO_SPOT/ALPACA spec into exactly
    v0.5.3.22's expected wire shape. Fails closed
    (ASSET_CLASS_NOT_YET_IMPLEMENTED_FOR_VENUE) for STOCK/ETF, since no
    adapter in this repository accepts a plain equity/ETF ticker today
    (finding D) -- this is the explicit, honest NOT_YET_IMPLEMENTED
    boundary, not a symbol-regex accident.

    Authorization self-attestation fields default to the fully-blocked
    state, same discipline as to_mexc_execution_spec(). live_execution is
    always False (v0.5.3.22 has no live endpoint at all)."""
    asset_class = spec.get("asset_class")
    if spec.get("venue") != "ALPACA":
        fail(f"VENUE_NOT_ALPACA:{spec.get('venue')}")
    if asset_class in ("STOCK", "ETF"):
        fail(f"ASSET_CLASS_NOT_YET_IMPLEMENTED_FOR_VENUE:{asset_class}/ALPACA")
    if asset_class != "CRYPTO_SPOT":
        fail(f"UNSUPPORTED_ASSET_CLASS_FOR_ALPACA_TRANSLATION:{asset_class}")
    if spec.get("order_type") not in ORDER_TYPES:
        fail(f"INVALID_ORDER_TYPE:{spec.get('order_type')}")

    direction = spec.get("direction")
    if direction not in _ALPACA_DIRECTION_TO_SIDE:
        fail(f"INVALID_DIRECTION_FOR_ALPACA_TRANSLATION:{direction!r}")
    side = _ALPACA_DIRECTION_TO_SIDE[direction]

    tif = spec.get("time_in_force") or "GTC"
    if tif not in TIME_IN_FORCE_VALUES:
        fail(f"INVALID_TIME_IN_FORCE:{tif!r}")

    wire: dict[str, Any] = {
        "execution_spec_version": "1.0",
        "exchange": "ALPACA",
        "account_mode": "PAPER",
        "live_execution": False,
        "kill_switch": kill_switch,
        "execution_authorized": execution_authorized,
        "paper_execution_authorized": paper_execution_authorized,
        "symbol": spec["symbol"],
        "side": side,
        "quantity": spec["quantity"],
        "order_type": spec["order_type"],
        "time_in_force": tif,
        "client_order_id": spec["client_order_id"],
    }
    if spec.get("order_type") == "LIMIT" and spec.get("limit_price") is not None:
        wire["limit_price"] = spec["limit_price"]
    return wire
