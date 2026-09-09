#!/usr/bin/env python3
"""
AURA v0.5.3.23 — Execution Specification Builder

LOCKED ARCHITECTURE
--------------------
Consumes the canonical AURA v0.5.3.13 Signal Decision output and the
canonical AURA v0.5.3.19 Execution Safety output produced by the SAME
pipeline cycle. Produces, for each required symbol, either:

    - a complete Execution Specification matching the schema required by
      AURA v0.5.3.22 (aura_v05322_alpaca_paper_execution_adapter.py), or
    - an explicit BLOCKED record naming the reason.

This is the missing bridge between the risk-authorized chain and the
Alpaca PAPER adapter. Before this module existed, nothing in the AURA
v0.5.3 chain ever produced `side`, `quantity`, `order_type`, or
`client_order_id` — v0.5.3.22 could only be exercised with hand-written
test fixtures, never with real upstream output. See v0.5.3.21's own
docstring, which anticipated exactly this module and warned that the
runtime supervisor "must never invent those fields."

This layer deliberately does NOT:
    - fetch market data;
    - recalculate a signal or a risk decision;
    - treat any v0.5.3.13 regime label as an implicit trade direction;
    - invent, estimate, or default a position size;
    - authorize execution itself — it can only forward an authorization
      that v0.5.3.19 already granted, and v0.5.3.19 is presently
      research-only and hard-codes execution_authorized = False on every
      run, so this module currently cannot produce a live-reachable spec
      no matter what direction/sizing inputs it is given. That boundary
      is intentional and is not loosened here.

DIRECTION POLICY — explicit, allowlist-based, closed by default
-----------------------------------------------------------------
A symbol's v0.5.3.13 `regime_state` label produces:
    side = "BUY"  only if that exact label is a member of
            VALIDATED_LONG_ENTRY_REGIME_LABELS
    side = "SELL" only if that exact label is a member of
            VALIDATED_SHORT_ENTRY_REGIME_LABELS

Both allowlists are EMPTY by default.

In particular: "BEAR x LOW ATR x POSITIVE bar-2" — the only frozen
candidate v0.5.3.13 currently evaluates — is NOT a member of either
set. The research record for this candidate (the frozen v0.4.8
long-only EMA3/EMA8 + MACD + relative-volume hypothesis is a separate,
unrelated research track) does not establish a validated executable
direction for the BEAR regime label. A BEAR regime label therefore
produces BLOCKED / NO_VALIDATED_EXECUTION_DIRECTION here — never SELL,
never BUY. This module will never map BEAR -> SELL.

Populating either allowlist is a deliberate, reviewed decision by
whoever owns the trading strategy research. It is never inferred by
this module and must never be inferred by an AI agent acting on its
behalf.

QUANTITY POLICY
-----------------
No file anywhere in the v0.5.3.12-.20 chain computes a position size
(v0.5.3.15's own docstring says so explicitly). Quantity is therefore
NEVER computed or defaulted here either. It must be supplied explicitly
via --sizing-config, a small JSON object of
    {"BTC/USD": <positive number>, "ETH/USD": <positive number>}
A missing, non-numeric, or non-positive entry for a symbol that would
otherwise be eligible to execute is a fail-closed condition
(MISSING_QUANTITY_SOURCE), never a fabricated default.

Default inputs:
    regime_output/signal_decision/signal_decision.json   (v0.5.3.13)
    regime_output/execution_safety/execution_safety.json (v0.5.3.19)

Default output:
    regime_output/execution_specification/execution_specification.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

VERSION = "AURA v0.5.3.23"

EXPECTED_SIGNAL_VERSION = "AURA v0.5.3.13"
EXPECTED_SIGNAL_ENGINE = "SIGNAL_DECISION_ENGINE"
EXPECTED_SAFETY_VERSION = "AURA v0.5.3.19"
EXPECTED_SAFETY_ENGINE = "EXECUTION_SAFETY"

DEFAULT_SIGNAL_INPUT = Path(r"regime_output\signal_decision\signal_decision.json")
DEFAULT_SAFETY_INPUT = Path(r"regime_output\execution_safety\execution_safety.json")
DEFAULT_OUTPUT = Path(
    r"regime_output\execution_specification\execution_specification.json"
)

SYMBOLS = ("BTC/USD", "ETH/USD")
REQUIRED_SYMBOLS = SYMBOLS  # BTC/ETH are always considered even if somehow
# absent from either upstream payload's decisions. Kept as a separate name
# (aliased to SYMBOLS) so the rest of this file reads unambiguously --
# mirrors the same alias in v0.5.3.13-.19. Note this file already treats
# every symbol fully independently (one symbol's BLOCKED status has never
# affected another's, or the overall_status, beyond ready_count) -- so
# widening from a fixed BTC/ETH pair to the full discovered coin universe
# needs no new required-vs-optional gating logic, only a wider loop.

# --- Direction policy: explicit, closed, empty by default. -----------------
# Do not add "BEAR x LOW ATR x POSITIVE bar-2" to either set without a
# reviewed, documented research decision. This module must never infer a
# direction from a regime label on its own.
VALIDATED_LONG_ENTRY_REGIME_LABELS: frozenset[str] = frozenset()
VALIDATED_SHORT_ENTRY_REGIME_LABELS: frozenset[str] = frozenset()

# Order-shape constants. MARKET is the only order type any upstream stage
# computes support for (no limit-price calculation exists anywhere in the
# chain); GTC matches the only time-in-force the original v0.5.3.22
# draft assumed. Both are mechanical order attributes, not trading
# direction or sizing decisions.
ORDER_TYPE = "MARKET"
TIME_IN_FORCE = "GTC"
EXECUTION_SPEC_VERSION = "1.0"


def stable_json(obj: Any) -> str:
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Input file not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        payload = json.load(f)
    if not isinstance(payload, dict):
        raise ValueError("Input JSON must contain an object at the top level.")
    return payload


def load_sizing_config(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    payload = load_json(path)
    return payload


def determine_side(regime_state: Any) -> str | None:
    """Explicit, allowlist-based direction lookup. Returns None (never a
    guess) for anything not on an allowlist, including the current frozen
    BEAR candidate."""
    if regime_state in VALIDATED_LONG_ENTRY_REGIME_LABELS:
        return "BUY"
    if regime_state in VALIDATED_SHORT_ENTRY_REGIME_LABELS:
        return "SELL"
    return None


def positive_quantity(value: Any) -> Decimal | None:
    if isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not number.is_finite() or number <= 0:
        return None
    return number


def verify_signal_decision(payload: dict[str, Any]) -> list[str]:
    """Mirrors v0.5.3.14's own verification of v0.5.3.13: v0.5.3.13 does
    not publish a self state_hash, so this layer trusts (and requires)
    the same input_state_hash_verified flag that v0.5.3.14 requires,
    rather than recomputing a hash v0.5.3.13 never produced."""
    errors: list[str] = []
    if payload.get("engine") != EXPECTED_SIGNAL_ENGINE:
        errors.append("WRONG_SIGNAL_ENGINE")
    if payload.get("agent_version") != EXPECTED_SIGNAL_VERSION:
        errors.append("WRONG_SIGNAL_VERSION")
    if payload.get("decision_status") != "DECIDED":
        errors.append("SIGNAL_NOT_DECIDED")
    if payload.get("input_state_hash_verified") is not True:
        errors.append("SIGNAL_UPSTREAM_HASH_NOT_VERIFIED")
    if payload.get("frozen_configuration_verified") is not True:
        errors.append("SIGNAL_CONFIGURATION_NOT_VERIFIED")
    return errors


def canonical_execution_safety_hash(payload: dict[str, Any]) -> str:
    """Reconstructs v0.5.3.19's own canonical payload (see its finalize())
    so this layer verifies the supplied state_hash against the actual
    content rather than trusting it blindly."""
    canonical = {
        "agent_version": payload.get("agent_version"),
        "engine": payload.get("engine"),
        "decision_status": payload.get("decision_status"),
        "overall_execution_safety": payload.get("overall_execution_safety"),
        "execution_authorized": payload.get("execution_authorized"),
        "paper_execution_authorized": payload.get("paper_execution_authorized"),
        "live_execution_authorized": payload.get("live_execution_authorized"),
        "kill_switch_active": payload.get("kill_switch_active"),
        "orders_enabled": payload.get("orders_enabled"),
        "paper_execution_enabled": payload.get("paper_execution_enabled"),
        "live_execution_enabled": payload.get("live_execution_enabled"),
        "upstream_state_id": payload.get("upstream_state_id"),
        "upstream_hash_verified": payload.get("upstream_hash_verified"),
        "frozen_configuration_verified": payload.get("frozen_configuration_verified"),
        "observed_execution_verified": payload.get("observed_execution_verified"),
        "decisions": payload.get("decisions"),
        "blocked_reasons": payload.get("blocked_reasons"),
    }
    return sha256_text(stable_json(canonical))


def verify_execution_safety(payload: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if payload.get("engine") != EXPECTED_SAFETY_ENGINE:
        errors.append("WRONG_SAFETY_ENGINE")
    if payload.get("agent_version") != EXPECTED_SAFETY_VERSION:
        errors.append("WRONG_SAFETY_VERSION")
    if payload.get("decision_status") != "DECIDED":
        errors.append("SAFETY_NOT_DECIDED")

    supplied_hash = payload.get("state_hash")
    if not isinstance(supplied_hash, str) or not supplied_hash:
        errors.append("MISSING_SAFETY_STATE_HASH")
    else:
        try:
            calculated = canonical_execution_safety_hash(payload)
        except (TypeError, ValueError):
            errors.append("SAFETY_STATE_NOT_HASHABLE")
        else:
            if calculated != supplied_hash:
                errors.append("SAFETY_STATE_HASH_MISMATCH")
        if payload.get("state_id") != f"ES-{supplied_hash[:24]}":
            errors.append("SAFETY_STATE_ID_MISMATCH")

    if payload.get("kill_switch_active") is not False:
        errors.append("KILL_SWITCH_ACTIVE")
    if payload.get("paper_execution_authorized") is not True:
        errors.append("PAPER_EXECUTION_NOT_AUTHORIZED")
    if payload.get("execution_authorized") is not True:
        errors.append("EXECUTION_NOT_AUTHORIZED")
    if payload.get("live_execution_authorized") is not False:
        errors.append("LIVE_EXECUTION_AUTHORIZED_FLAG_UNEXPECTED")

    return errors


def build_client_order_id(symbol: str, safety_state_id: Any, signal_state_hash: Any) -> str:
    slug = symbol.replace("/", "").lower()
    basis = stable_json({"safety": safety_state_id, "signal": signal_state_hash, "symbol": symbol})
    digest = sha256_text(basis)[:12]
    cid = f"aura-{slug}-{digest}"
    assert len(cid) <= 48
    return cid


def base_result(signal_path: Path, safety_path: Path) -> dict[str, Any]:
    return {
        "agent_version": VERSION,
        "engine": "EXECUTION_SPECIFICATION_BUILDER",
        "decision_status": "BLOCKED",
        "overall_status": "BLOCKED",
        "generated_from_signal": str(signal_path.resolve()),
        "generated_from_safety": str(safety_path.resolve()),
        "decisions": {},
        "blocked_reasons": [],
        "state_hash": None,
        "state_id": None,
        "guardrails": {
            "single_source_of_truth": True,
            "market_data_fetch": False,
            "signal_recalculation": False,
            "risk_recalculation": False,
            "implicit_direction_inference": False,
            "position_sizing_fabrication": False,
            "execution_authorization_self_granted": False,
            "live_execution": False,
            "fail_closed": True,
        },
    }


def build_specs(
    signal_payload: dict[str, Any],
    safety_payload: dict[str, Any],
    sizing_config: dict[str, Any],
    signal_path: Path,
    safety_path: Path,
) -> dict[str, Any]:
    result = base_result(signal_path, safety_path)

    signal_errors = verify_signal_decision(signal_payload)
    safety_errors = verify_execution_safety(safety_payload)
    shared_errors = sorted(set(signal_errors + safety_errors))

    if shared_errors:
        result["blocked_reasons"] = shared_errors
        # Envelope-level failure -- the upstream payloads themselves may be
        # too malformed to trust their "decisions" keys, so report at least
        # REQUIRED_SYMBOLS plus whatever symbols either side did manage to
        # name.
        reported_symbols = set(REQUIRED_SYMBOLS)
        for payload in (signal_payload, safety_payload):
            decisions = payload.get("decisions")
            if isinstance(decisions, dict):
                reported_symbols.update(decisions.keys())
        for symbol in sorted(reported_symbols):
            result["decisions"][symbol] = {
                "symbol": symbol,
                "status": "BLOCKED",
                "reason": "UPSTREAM_VALIDATION_FAILED",
            }
        return finalize(result)

    signal_decisions = signal_payload.get("decisions", {})
    safety_decisions = safety_payload.get("decisions", {})

    ready_count = 0

    # Every symbol reported by either upstream source gets its own spec
    # attempt; REQUIRED_SYMBOLS are added even if somehow absent. Each
    # symbol was already fully independent before this change (one
    # symbol's BLOCKED status never affected another's), so no new
    # required-vs-optional gating is introduced here -- this just widens
    # the loop to the full discovered coin universe.
    selected_symbols = sorted(
        set(signal_decisions.keys()) | set(safety_decisions.keys()) | set(REQUIRED_SYMBOLS)
    )

    for symbol in selected_symbols:
        signal_item = signal_decisions.get(symbol)
        safety_item = safety_decisions.get(symbol)

        if not isinstance(signal_item, dict):
            result["decisions"][symbol] = {
                "symbol": symbol,
                "status": "BLOCKED",
                "reason": "MISSING_SIGNAL_DECISION",
            }
            continue
        if not isinstance(safety_item, dict):
            result["decisions"][symbol] = {
                "symbol": symbol,
                "status": "BLOCKED",
                "reason": "MISSING_SAFETY_DECISION",
            }
            continue

        if safety_item.get("execution_authorized") is not True:
            result["decisions"][symbol] = {
                "symbol": symbol,
                "status": "BLOCKED",
                "reason": "SYMBOL_EXECUTION_NOT_AUTHORIZED",
            }
            continue

        regime_state = signal_item.get("regime_state")
        side = determine_side(regime_state)
        if side is None:
            result["decisions"][symbol] = {
                "symbol": symbol,
                "status": "BLOCKED",
                "reason": "NO_VALIDATED_EXECUTION_DIRECTION",
                "regime_state": regime_state,
            }
            continue

        quantity = positive_quantity(sizing_config.get(symbol))
        if quantity is None:
            result["decisions"][symbol] = {
                "symbol": symbol,
                "status": "BLOCKED",
                "reason": "MISSING_QUANTITY_SOURCE",
                "regime_state": regime_state,
                "side": side,
            }
            continue

        client_order_id = build_client_order_id(
            symbol, safety_payload.get("state_id"), signal_payload.get("input_state_hash")
        )

        result["decisions"][symbol] = {
            "symbol": symbol,
            "status": "EXECUTION_SPEC_READY",
            "reason": "VALIDATED_DIRECTION_AND_QUANTITY_PRESENT",
            "regime_state": regime_state,
            "execution_specification": {
                "execution_spec_version": EXECUTION_SPEC_VERSION,
                "exchange": "ALPACA",
                "account_mode": "PAPER",
                "live_execution": False,
                "kill_switch": False,
                "execution_authorized": True,
                "paper_execution_authorized": True,
                "symbol": symbol,
                "side": side,
                "quantity": str(quantity),
                "order_type": ORDER_TYPE,
                "time_in_force": TIME_IN_FORCE,
                "client_order_id": client_order_id,
            },
        }
        ready_count += 1

    result["decision_status"] = "DECIDED"
    result["overall_status"] = "EXECUTION_SPEC_READY" if ready_count else "NO_EXECUTION_SPEC"
    return finalize(result)


def finalize(result: dict[str, Any]) -> dict[str, Any]:
    canonical = {
        "agent_version": result["agent_version"],
        "engine": result["engine"],
        "decision_status": result["decision_status"],
        "overall_status": result["overall_status"],
        "decisions": result["decisions"],
        "blocked_reasons": result["blocked_reasons"],
    }
    result["state_hash"] = sha256_text(stable_json(canonical))
    result["state_id"] = f"XS-{result['state_hash'][:24]}"
    return result


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False, allow_nan=False)
        f.write("\n")


def print_report(result: dict[str, Any], output_path: Path) -> None:
    print("=" * 96)
    print(f"{VERSION} — EXECUTION SPECIFICATION BUILDER")
    print("=" * 96)
    print()
    print("MODE                 : RESEARCH ONLY")
    print("IMPLICIT DIRECTION   : DISABLED (allowlist only, empty by default)")
    print("POSITION SIZING      : EXTERNAL CONFIG ONLY (never fabricated)")
    print()
    print(f"STATUS               : {result['decision_status']}")
    print(f"OVERALL              : {result['overall_status']}")
    print(f"STATE ID             : {result['state_id']}")
    print()
    reported_symbols = sorted(set(result["decisions"].keys()) | set(REQUIRED_SYMBOLS))
    for symbol in reported_symbols:
        item = result["decisions"].get(symbol, {})
        label = symbol + ("  [REQUIRED]" if symbol in REQUIRED_SYMBOLS else "  [OPTIONAL]")
        print(label)
        print(f"  STATUS             : {item.get('status')}")
        print(f"  REASON             : {item.get('reason')}")
        if "regime_state" in item:
            print(f"  REGIME STATE       : {item.get('regime_state')}")
        if item.get("status") == "EXECUTION_SPEC_READY":
            spec = item["execution_specification"]
            print(f"  SIDE               : {spec['side']}")
            print(f"  QUANTITY           : {spec['quantity']}")
            print(f"  CLIENT ORDER ID    : {spec['client_order_id']}")
        print()
    if result["blocked_reasons"]:
        print("FAIL-CLOSED REASONS")
        print("-" * 96)
        for reason in result["blocked_reasons"]:
            print(f"  - {reason}")
        print()
    print(f"OUTPUT               : {output_path.resolve()}")
    print("=" * 96)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--signal-input", type=Path, default=DEFAULT_SIGNAL_INPUT)
    parser.add_argument("--safety-input", type=Path, default=DEFAULT_SAFETY_INPUT)
    parser.add_argument(
        "--sizing-config",
        type=Path,
        default=None,
        help="JSON object of {symbol: positive quantity}. Required for any "
        "symbol to reach EXECUTION_SPEC_READY; omitted entirely means every "
        "symbol fails closed with MISSING_QUANTITY_SOURCE.",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    try:
        signal_payload = load_json(args.signal_input)
        safety_payload = load_json(args.safety_input)
        sizing_config = load_sizing_config(args.sizing_config)
        result = build_specs(
            signal_payload, safety_payload, sizing_config, args.signal_input, args.safety_input
        )
        write_json(args.output, result)
        print_report(result, args.output)
        return 0
    except Exception as exc:
        result = base_result(args.signal_input, args.safety_input)
        result["blocked_reasons"] = [f"UNEXPECTED_ENGINE_ERROR:{type(exc).__name__}"]
        result = finalize(result)
        try:
            write_json(args.output, result)
            print_report(result, args.output)
        except Exception:
            pass
        print(f"FAIL-CLOSED ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
