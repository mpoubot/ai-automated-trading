#!/usr/bin/env python3
"""
AURA v0.5.3.16 — Paper Execution

LOCKED ARCHITECTURE
-------------------

Consumes ONLY the canonical Position State output produced by
AURA v0.5.3.15 — Position State.

This layer deliberately does NOT:
- fetch market data;
- calculate indicators;
- recalculate signals;
- recalculate risk;
- choose position size;
- create an exchange position;
- place live orders;
- call MEXC;
- fabricate execution prices;
- claim that a position exists.

Its responsibility is to translate an authenticated Position State into
a deterministic PAPER ORDER INTENT.

Important distinction:

    ENTRY_CANDIDATE
        -> PAPER_ORDER_INTENT

does NOT mean:

    POSITION EXISTS

A paper order intent represents an execution request in the research
pipeline. It is not an observed fill and must not be treated as one.

Position existence requires a later observed execution/reconciliation
source.

Default input:
    regime_output/position_state/position_state.json

Default output:
    regime_output/paper_execution/paper_execution.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any


VERSION = "AURA v0.5.3.16"
EXPECTED_SOURCE_VERSION = "AURA v0.5.3.15"
EXPECTED_SOURCE_ENGINE = "POSITION_STATE"

DEFAULT_INPUT = Path(
    r"regime_output\position_state\position_state.json"
)

DEFAULT_OUTPUT = Path(
    r"regime_output\paper_execution\paper_execution.json"
)

SYMBOLS = ("BTC/USD", "ETH/USD")
REQUIRED_SYMBOLS = SYMBOLS  # BTC/ETH remain the only symbols that can ever
# block the whole paper-execution decision if missing/invalid. Kept as a
# separate name (aliased to SYMBOLS) so the rest of this file reads
# unambiguously -- mirrors the same alias in v0.5.3.13/.14/.15.

ALLOWED_POSITION_STATES = {
    "FLAT",
    "ENTRY_CANDIDATE",
}
# An optional symbol may also legitimately arrive BLOCKED (v0.5.3.15 blocks
# an individual optional symbol without blocking the required pair).
ALLOWED_POSITION_STATES_OPTIONAL = ALLOWED_POSITION_STATES | {"BLOCKED"}


def stable_json(obj: Any) -> str:
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Input file not found: {path}")

    with path.open("r", encoding="utf-8") as f:
        payload = json.load(f)

    if not isinstance(payload, dict):
        raise ValueError(
            "Input JSON must contain an object at the top level."
        )

    return payload


def base_result(input_path: Path) -> dict[str, Any]:
    return {
        "agent_version": VERSION,
        "engine": "PAPER_EXECUTION",
        "decision_status": "BLOCKED",
        "overall_execution_decision": "BLOCKED",
        "paper_execution_allowed": False,
        "live_execution_permitted": False,
        "generated_from": str(input_path.resolve()),
        "input_position_state_hash": None,
        "input_position_state_hash_verified": False,
        "upstream_state_id": None,
        "upstream_hash_verified": False,
        "frozen_configuration_verified": False,
        "decisions": {},
        "blocked_reasons": [],
        "state_hash": None,
        "state_id": None,
        "guardrails": {
            "single_source_of_truth": True,
            "source_engine": EXPECTED_SOURCE_VERSION,
            "market_data_fetch": False,
            "indicator_recalculation": False,
            "signal_recalculation": False,
            "risk_recalculation": False,
            "position_sizing": False,
            "position_creation": False,
            "exchange_state_mutation": False,
            "orders_allowed": False,
            "live_execution": False,
            "fabricated_fill_price": False,
            "position_claim_from_intent": False,
            "fail_closed": True,
        },
    }


def canonical_position_state_hash(
    payload: dict[str, Any],
) -> str:
    """
    Reconstruct the deterministic state payload used by v0.5.3.15.

    This verifies that the upstream state_hash corresponds to the
    actual state content rather than trusting the supplied hash alone.
    """

    canonical = {
        "agent_version": payload.get("agent_version"),
        "engine": payload.get("engine"),
        "decision_status": payload.get("decision_status"),
        "overall_position_state": payload.get(
            "overall_position_state"
        ),
        "upstream_state_id": payload.get("upstream_state_id"),
        "upstream_hash_verified": payload.get(
            "upstream_hash_verified"
        ),
        "frozen_configuration_verified": payload.get(
            "frozen_configuration_verified"
        ),
        "position_truth_source": payload.get(
            "position_truth_source"
        ),
        "positions_observed": payload.get(
            "positions_observed"
        ),
        "decisions": payload.get("decisions"),
        "blocked_reasons": payload.get("blocked_reasons"),
    }

    return sha256_text(stable_json(canonical))


def position_state_errors(
    symbol: str, item: Any, *, allow_blocked: bool
) -> list[str]:
    """
    Check that a single symbol's upstream position state is one this layer
    is allowed to act on, with position_exists still False (v0.5.3.15 can
    never assert an open position). No position state is recalculated here.
    """
    if not isinstance(item, dict):
        return [f"MISSING_OR_INVALID_SYMBOL:{symbol}"]

    errors: list[str] = []
    position_state = item.get("position_state")
    position_exists = item.get("position_exists")
    allowed = ALLOWED_POSITION_STATES_OPTIONAL if allow_blocked else ALLOWED_POSITION_STATES

    if position_state not in allowed:
        errors.append(f"INVALID_POSITION_STATE:{symbol}:{position_state!r}")

    if position_exists is not False:
        errors.append(f"POSITION_EXISTENCE_NOT_FALSE:{symbol}")

    return errors


def verify_upstream(
    payload: dict[str, Any],
) -> tuple[bool, list[str]]:
    errors: list[str] = []

    if payload.get("engine") != EXPECTED_SOURCE_ENGINE:
        errors.append("WRONG_UPSTREAM_ENGINE")

    if payload.get("agent_version") != EXPECTED_SOURCE_VERSION:
        errors.append("WRONG_UPSTREAM_VERSION")

    if payload.get("decision_status") != "DECIDED":
        errors.append("UPSTREAM_NOT_DECIDED")

    supplied_hash = payload.get("state_hash")

    if not isinstance(supplied_hash, str) or not supplied_hash:
        errors.append("MISSING_UPSTREAM_STATE_HASH")
    else:
        try:
            calculated_hash = canonical_position_state_hash(
                payload
            )
        except (TypeError, ValueError):
            errors.append("UPSTREAM_STATE_NOT_HASHABLE")
        else:
            if calculated_hash != supplied_hash:
                errors.append("UPSTREAM_STATE_HASH_MISMATCH")

    if payload.get("upstream_hash_verified") is not True:
        errors.append("UPSTREAM_HASH_NOT_VERIFIED")

    if payload.get("frozen_configuration_verified") is not True:
        errors.append("UPSTREAM_CONFIGURATION_NOT_VERIFIED")

    if not isinstance(payload.get("state_id"), str):
        errors.append("MISSING_UPSTREAM_STATE_ID")

    decisions = payload.get("decisions")

    if not isinstance(decisions, dict):
        errors.append("MISSING_UPSTREAM_DECISIONS")
        return False, sorted(set(errors))

    # Only REQUIRED_SYMBOLS (BTC/ETH) gate the whole decision here. Every
    # other symbol v0.5.3.15 reports is validated independently, per-symbol,
    # in build_paper_execution() below.
    for symbol in REQUIRED_SYMBOLS:
        errors.extend(
            position_state_errors(symbol, decisions.get(symbol), allow_blocked=False)
        )

    guardrails = payload.get("guardrails")

    if not isinstance(guardrails, dict):
        errors.append("MISSING_UPSTREAM_GUARDRAILS")
    else:
        if guardrails.get("single_source_of_truth") is not True:
            errors.append("UPSTREAM_SST_NOT_ENFORCED")

        forbidden_true = (
            "market_data_fetch",
            "indicator_recalculation",
            "signal_recalculation",
            "risk_recalculation",
            "position_sizing",
            "position_creation",
            "exchange_state_mutation",
            "orders_allowed",
            "live_execution",
        )

        for key in forbidden_true:
            if guardrails.get(key) is not False:
                errors.append(
                    f"UPSTREAM_GUARDRAIL_VIOLATION:{key}"
                )

    return len(errors) == 0, sorted(set(errors))


def build_paper_execution(
    payload: dict[str, Any],
    input_path: Path,
) -> dict[str, Any]:

    result = base_result(input_path)

    result["input_position_state_hash"] = payload.get(
        "state_hash"
    )

    result["upstream_state_id"] = payload.get("state_id")

    result["upstream_hash_verified"] = (
        payload.get("upstream_hash_verified") is True
    )

    result["frozen_configuration_verified"] = (
        payload.get("frozen_configuration_verified") is True
    )

    upstream_ok, upstream_errors = verify_upstream(payload)

    if not upstream_ok:
        result["blocked_reasons"] = upstream_errors

        # Envelope-level failure -- scoped to REQUIRED_SYMBOLS only, same as
        # the global gate that produced it.
        for symbol in REQUIRED_SYMBOLS:
            item = payload.get("decisions", {}).get(symbol)

            result["decisions"][symbol] = {
                "symbol": symbol,
                "upstream_position_state": (
                    item.get("position_state")
                    if isinstance(item, dict)
                    else None
                ),
                "execution_decision": "BLOCKED",
                "paper_order_intent": False,
                "paper_order_status": "BLOCKED",
                "position_exists": False,
                "position_id": None,
                "reason": "UPSTREAM_VALIDATION_FAILED",
            }

        return finalize(result)

    decisions = payload["decisions"]

    order_count = 0
    optional_blocked_symbols: list[str] = []

    # Every symbol v0.5.3.15 actually reported gets its own execution
    # decision; REQUIRED_SYMBOLS are added even if somehow absent
    # (verify_upstream already gated a truly missing/invalid required
    # symbol above, so this is just defensive). Mirrors the required-vs-
    # optional pattern already shipped in v0.5.3.12-.15.
    selected = sorted(set(decisions.keys()) | set(REQUIRED_SYMBOLS))

    for symbol in selected:
        is_required = symbol in REQUIRED_SYMBOLS
        item = decisions.get(symbol)

        if not is_required:
            symbol_errors = position_state_errors(symbol, item, allow_blocked=True)
            if symbol_errors:
                result["decisions"][symbol] = {
                    "symbol": symbol,
                    "upstream_position_state": (
                        item.get("position_state") if isinstance(item, dict) else None
                    ),
                    "execution_decision": "BLOCKED",
                    "paper_order_intent": False,
                    "paper_order_status": "BLOCKED",
                    "position_exists": False,
                    "position_id": None,
                    "fill_price": None,
                    "fill_timestamp": None,
                    "reason": "UPSTREAM_POSITION_STATE_INVALID",
                }
                optional_blocked_symbols.append(symbol)
                continue

        position_state = item["position_state"]

        if position_state == "ENTRY_CANDIDATE":

            result["decisions"][symbol] = {
                "symbol": symbol,
                "upstream_position_state": position_state,
                "execution_decision": "PAPER_ORDER_INTENT",
                "paper_order_intent": True,
                "paper_order_status": "PENDING_EXECUTION",
                "position_exists": False,
                "position_id": None,
                "fill_price": None,
                "fill_timestamp": None,
                "reason": (
                    "ENTRY_CANDIDATE_ACCEPTED_FOR_PAPER_EXECUTION"
                ),
            }

            order_count += 1

        elif position_state == "FLAT":

            result["decisions"][symbol] = {
                "symbol": symbol,
                "upstream_position_state": position_state,
                "execution_decision": "NO_ORDER",
                "paper_order_intent": False,
                "paper_order_status": "NOT_APPLICABLE",
                "position_exists": False,
                "position_id": None,
                "fill_price": None,
                "fill_timestamp": None,
                "reason": "NO_POSITION_ENTRY_CANDIDATE",
            }

        else:
            # BLOCKED upstream on an optional symbol -- position_state_errors
            # already allows this value, so route it the same as an
            # explicitly-invalid optional symbol above. A required symbol
            # can never reach this branch: verify_upstream() already
            # restricted it to FLAT/ENTRY_CANDIDATE only.
            result["decisions"][symbol] = {
                "symbol": symbol,
                "upstream_position_state": position_state,
                "execution_decision": "BLOCKED",
                "paper_order_intent": False,
                "paper_order_status": "BLOCKED",
                "position_exists": False,
                "position_id": None,
                "fill_price": None,
                "fill_timestamp": None,
                "reason": "UPSTREAM_POSITION_BLOCKED",
            }
            if not is_required:
                optional_blocked_symbols.append(symbol)

    if optional_blocked_symbols:
        result["optional_symbols_blocked"] = sorted(optional_blocked_symbols)

    result["decision_status"] = "DECIDED"

    if order_count:
        result["overall_execution_decision"] = (
            "PAPER_ORDER_INTENT"
        )
        result["paper_execution_allowed"] = True
    else:
        result["overall_execution_decision"] = "NO_ORDER"
        result["paper_execution_allowed"] = False

    # Critical safety invariant:
    #
    # Paper execution can create an intent, but cannot claim that
    # a position exists.
    result["live_execution_permitted"] = False

    return finalize(result)


def finalize(result: dict[str, Any]) -> dict[str, Any]:

    canonical = {
        "agent_version": result["agent_version"],
        "engine": result["engine"],
        "decision_status": result["decision_status"],
        "overall_execution_decision": result[
            "overall_execution_decision"
        ],
        "paper_execution_allowed": result[
            "paper_execution_allowed"
        ],
        "live_execution_permitted": result[
            "live_execution_permitted"
        ],
        "upstream_state_id": result["upstream_state_id"],
        "upstream_hash_verified": result[
            "upstream_hash_verified"
        ],
        "frozen_configuration_verified": result[
            "frozen_configuration_verified"
        ],
        "decisions": result["decisions"],
        "blocked_reasons": result["blocked_reasons"],
    }

    result["state_hash"] = sha256_text(
        stable_json(canonical)
    )

    result["state_id"] = (
        f"PE-{result['state_hash'][:24]}"
    )

    return result


def write_json(
    path: Path,
    payload: dict[str, Any],
) -> None:

    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", encoding="utf-8") as f:
        json.dump(
            payload,
            f,
            indent=2,
            ensure_ascii=False,
            allow_nan=False,
        )
        f.write("\n")


def print_report(
    result: dict[str, Any],
    output_path: Path,
) -> None:

    print("=" * 96)
    print(f"{VERSION} — PAPER EXECUTION")
    print("=" * 96)
    print()

    print("MODE                 : RESEARCH ONLY")
    print("REAL ORDERS          : DISABLED")
    print("MEXC / EXCHANGE CALL : DISABLED")
    print("LIVE EXECUTION       : DISABLED")
    print("MARKET DATA FETCH    : DISABLED")
    print("FILL PRICE FABRICATION: DISABLED")
    print("POSITION CLAIM       : DISABLED")
    print()

    print("PAPER EXECUTION")
    print("-" * 96)

    print(
        f"STATUS               : "
        f"{result['decision_status']}"
    )

    print(
        f"OVERALL DECISION     : "
        f"{result['overall_execution_decision']}"
    )

    print(
        f"PAPER EXECUTION ALLOWED: "
        f"{result['paper_execution_allowed']}"
    )

    print(
        f"LIVE EXECUTION       : "
        f"{result['live_execution_permitted']}"
    )

    print(
        f"UPSTREAM STATE ID    : "
        f"{result['upstream_state_id']}"
    )

    print(
        f"UPSTREAM HASH VERIFIED: "
        f"{result['upstream_hash_verified']}"
    )

    print(
        f"CONFIG VERIFIED      : "
        f"{result['frozen_configuration_verified']}"
    )

    print(
        f"STATE ID             : "
        f"{result['state_id']}"
    )

    print()

    reported_symbols = sorted(set(result["decisions"].keys()) | set(REQUIRED_SYMBOLS))
    optional_blocked = set(result.get("optional_symbols_blocked", []))

    for symbol in reported_symbols:

        item = result["decisions"].get(symbol)

        label = symbol
        if symbol in REQUIRED_SYMBOLS:
            label += "  [REQUIRED]"
        elif symbol in optional_blocked:
            label += "  [OPTIONAL, BLOCKED -- did not affect overall decision]"
        else:
            label += "  [OPTIONAL]"
        print(label)

        if not item:
            print("  EXECUTION DECISION : BLOCKED")
            print("  PAPER ORDER INTENT : False")
            print("  POSITION EXISTS    : False")
            print()
            continue

        print(
            f"  UPSTREAM STATE     : "
            f"{item['upstream_position_state']}"
        )

        print(
            f"  EXECUTION DECISION : "
            f"{item['execution_decision']}"
        )

        print(
            f"  PAPER ORDER INTENT : "
            f"{item['paper_order_intent']}"
        )

        print(
            f"  ORDER STATUS       : "
            f"{item['paper_order_status']}"
        )

        print(
            f"  POSITION EXISTS    : "
            f"{item['position_exists']}"
        )

        print(
            f"  POSITION ID        : "
            f"{item['position_id']}"
        )

        print(
            f"  FILL PRICE         : "
            f"{item.get('fill_price')}"
        )

        print(
            f"  REASON             : "
            f"{item['reason']}"
        )

        print()

    if result["blocked_reasons"]:

        print("FAIL-CLOSED REASONS")
        print("-" * 96)

        for reason in result["blocked_reasons"]:
            print(f"  - {reason}")

        print()

    print(
        f"OUTPUT               : "
        f"{output_path.resolve()}"
    )

    print("=" * 96)


def main() -> int:

    parser = argparse.ArgumentParser(
        description=__doc__
    )

    parser.add_argument(
        "--input",
        type=Path,
        default=DEFAULT_INPUT,
    )

    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
    )

    parser.add_argument(
        "--once",
        action="store_true",
        help="Run once and exit",
    )

    args = parser.parse_args()

    try:

        payload = load_json(args.input)

        result = build_paper_execution(
            payload,
            args.input,
        )

        write_json(
            args.output,
            result,
        )

        print_report(
            result,
            args.output,
        )

        return 0

    except Exception as exc:

        result = base_result(args.input)

        result["blocked_reasons"] = [
            f"UNEXPECTED_ENGINE_ERROR:{type(exc).__name__}"
        ]

        result = finalize(result)

        try:
            write_json(
                args.output,
                result,
            )

            print_report(
                result,
                args.output,
            )

        except Exception:
            pass

        print(
            f"FAIL-CLOSED ERROR: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )

        return 1


if __name__ == "__main__":
    raise SystemExit(main())