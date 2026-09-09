#!/usr/bin/env python3
"""
AURA v0.5.3.17 — Decision / Execution Ledger

LOCKED ARCHITECTURE
-------------------
Consumes ONLY the authenticated Paper Execution output produced by
AURA v0.5.3.16 — Paper Execution.

This layer is OBSERVATIONAL / AUDIT ONLY. It does NOT:
- fetch market data;
- calculate indicators;
- recalculate signals or risk;
- size positions;
- create positions;
- place orders;
- call MEXC or any exchange;
- fabricate fills or prices;
- mutate Position State;
- authorize execution.

Its responsibility is to create a deterministic ledger record of what the
upstream execution layer decided. A ledger record is evidence of a decision;
it is never permission to trade.

Important distinction:
    PAPER_ORDER_INTENT -> LEDGER RECORD
    does NOT mean        POSITION EXISTS

Default input:
    regime_output/paper_execution/paper_execution.json

Default output:
    regime_output/decision_execution_ledger/decision_execution_ledger.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

VERSION = "AURA v0.5.3.17"
EXPECTED_SOURCE_VERSION = "AURA v0.5.3.16"
EXPECTED_SOURCE_ENGINE = "PAPER_EXECUTION"
DEFAULT_INPUT = Path(r"regime_output\paper_execution\paper_execution.json")
DEFAULT_OUTPUT = Path(
    r"regime_output\decision_execution_ledger\decision_execution_ledger.json"
)
SYMBOLS = ("BTC/USD", "ETH/USD")
REQUIRED_SYMBOLS = SYMBOLS  # BTC/ETH remain the only symbols that can ever
# block the whole ledger decision if missing/structurally invalid. Kept as
# a separate name (aliased to SYMBOLS) so the rest of this file reads
# unambiguously -- mirrors the same alias in v0.5.3.13-.16.


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
        raise ValueError("Input JSON must contain an object at the top level.")
    return payload


def base_result(input_path: Path) -> dict[str, Any]:
    return {
        "agent_version": VERSION,
        "engine": "DECISION_EXECUTION_LEDGER",
        "decision_status": "BLOCKED",
        "overall_ledger_decision": "BLOCKED",
        "generated_from": str(input_path.resolve()),
        "input_paper_execution_hash": None,
        "upstream_state_id": None,
        "upstream_hash_verified": False,
        "frozen_configuration_verified": False,
        "ledger_entries": {},
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
            "paper_execution": False,
            "live_execution": False,
            "ledger_authorization": False,
            "ledger_state_mutation": False,
            "position_claim_from_ledger": False,
            "fail_closed": True,
        },
    }


def canonical_paper_execution_hash(payload: dict[str, Any]) -> str:
    """Reconstruct the exact deterministic hash payload used by v0.5.3.16."""
    canonical = {
        "agent_version": payload.get("agent_version"),
        "engine": payload.get("engine"),
        "decision_status": payload.get("decision_status"),
        "overall_execution_decision": payload.get("overall_execution_decision"),
        "paper_execution_allowed": payload.get("paper_execution_allowed"),
        "live_execution_permitted": payload.get("live_execution_permitted"),
        "upstream_state_id": payload.get("upstream_state_id"),
        "upstream_hash_verified": payload.get("upstream_hash_verified"),
        "frozen_configuration_verified": payload.get(
            "frozen_configuration_verified"
        ),
        "decisions": payload.get("decisions"),
        "blocked_reasons": payload.get("blocked_reasons"),
    }
    return sha256_text(stable_json(canonical))


def ledger_entry_errors_for_symbol(symbol: str, item: Any) -> list[str]:
    """
    Structural validity check for a single symbol's v0.5.3.16 entry. No
    execution is recalculated here. "BLOCKED" is already an accepted
    execution_decision value -- both the required-symbol global-failure
    path and an optional symbol individually blocked upstream use it.
    """
    if not isinstance(item, dict):
        return [f"MISSING_OR_INVALID_SYMBOL:{symbol}"]

    errors: list[str] = []
    execution_decision = item.get("execution_decision")
    intent = item.get("paper_order_intent")
    position_exists = item.get("position_exists")
    position_id = item.get("position_id")
    fill_price = item.get("fill_price")
    fill_timestamp = item.get("fill_timestamp")

    if execution_decision not in {"NO_ORDER", "PAPER_ORDER_INTENT", "BLOCKED"}:
        errors.append(f"INVALID_EXECUTION_DECISION:{symbol}:{execution_decision!r}")

    if not isinstance(intent, bool):
        errors.append(f"INVALID_PAPER_ORDER_INTENT:{symbol}")

    if position_exists is not False:
        errors.append(f"POSITION_CLAIM_NOT_FALSE:{symbol}")
    if position_id is not None:
        errors.append(f"POSITION_ID_PRESENT:{symbol}")

    if intent is True:
        if execution_decision != "PAPER_ORDER_INTENT":
            errors.append(f"INTENT_DECISION_INCONSISTENT:{symbol}")
        if item.get("paper_order_status") != "PENDING_EXECUTION":
            errors.append(f"INTENT_STATUS_INVALID:{symbol}")
        if fill_price is not None:
            errors.append(f"FABRICATED_FILL_PRICE:{symbol}")
        if fill_timestamp is not None:
            errors.append(f"FABRICATED_FILL_TIMESTAMP:{symbol}")
    elif execution_decision == "PAPER_ORDER_INTENT":
        errors.append(f"INTENT_FALSE_FOR_INTENT_DECISION:{symbol}")

    return errors


def verify_upstream(payload: dict[str, Any]) -> tuple[bool, list[str]]:
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
            calculated_hash = canonical_paper_execution_hash(payload)
        except (TypeError, ValueError):
            errors.append("UPSTREAM_STATE_NOT_HASHABLE")
        else:
            if calculated_hash != supplied_hash:
                errors.append("UPSTREAM_STATE_HASH_MISMATCH")

        expected_state_id = f"PE-{supplied_hash[:24]}"
        if payload.get("state_id") != expected_state_id:
            errors.append("UPSTREAM_STATE_ID_MISMATCH")

    if payload.get("upstream_hash_verified") is not True:
        errors.append("UPSTREAM_HASH_NOT_VERIFIED")
    if payload.get("frozen_configuration_verified") is not True:
        errors.append("UPSTREAM_CONFIGURATION_NOT_VERIFIED")
    if not isinstance(payload.get("upstream_state_id"), str) or not payload.get(
        "upstream_state_id"
    ):
        errors.append("MISSING_UPSTREAM_STATE_ID")

    if payload.get("live_execution_permitted") is not False:
        errors.append("LIVE_EXECUTION_NOT_DISABLED")

    decisions = payload.get("decisions")
    if not isinstance(decisions, dict):
        errors.append("MISSING_UPSTREAM_DECISIONS")
        return False, sorted(set(errors))

    # Structural validity gates the whole decision only for REQUIRED_SYMBOLS;
    # every other symbol v0.5.3.16 reported is checked independently, per-
    # symbol, in build_ledger() below -- one optional symbol's bad data
    # must never block another symbol's ledger entry. Mirrors the required-
    # vs-optional pattern already shipped in v0.5.3.12-.16.
    for symbol in REQUIRED_SYMBOLS:
        errors.extend(ledger_entry_errors_for_symbol(symbol, decisions.get(symbol)))

    # v0.5.3.16 itself counts ANY symbol's ENTRY_CANDIDATE (required or
    # optional) toward paper_execution_allowed / overall_execution_decision
    # -- this cross-check must match that same total scope, not just the
    # required pair, or a legitimate optional-symbol intent would look like
    # an upstream inconsistency.
    intent_count = sum(
        1
        for item in decisions.values()
        if isinstance(item, dict) and item.get("paper_order_intent") is True
    )

    allowed = payload.get("paper_execution_allowed")
    if allowed is not (intent_count > 0):
        errors.append("OVERALL_PAPER_EXECUTION_PERMISSION_INCONSISTENT")

    if intent_count == 0 and payload.get("overall_execution_decision") != "NO_ORDER":
        errors.append("OVERALL_NO_ORDER_INCONSISTENT")
    if intent_count > 0 and payload.get("overall_execution_decision") != "PAPER_ORDER_INTENT":
        errors.append("OVERALL_INTENT_INCONSISTENT")

    guardrails = payload.get("guardrails")
    if not isinstance(guardrails, dict):
        errors.append("MISSING_UPSTREAM_GUARDRAILS")
    else:
        if guardrails.get("single_source_of_truth") is not True:
            errors.append("UPSTREAM_SST_NOT_ENFORCED")
        expected_false = (
            "market_data_fetch",
            "indicator_recalculation",
            "signal_recalculation",
            "risk_recalculation",
            "position_sizing",
            "position_creation",
            "exchange_state_mutation",
            "orders_allowed",
            "live_execution",
            "fabricated_fill_price",
            "position_claim_from_intent",
        )
        for key in expected_false:
            if guardrails.get(key) is not False:
                errors.append(f"UPSTREAM_GUARDRAIL_VIOLATION:{key}")

    return len(errors) == 0, sorted(set(errors))


def build_ledger(payload: dict[str, Any], input_path: Path) -> dict[str, Any]:
    result = base_result(input_path)
    result["input_paper_execution_hash"] = payload.get("state_hash")
    result["upstream_state_id"] = payload.get("state_id")
    result["upstream_hash_verified"] = payload.get("upstream_hash_verified") is True
    result["frozen_configuration_verified"] = (
        payload.get("frozen_configuration_verified") is True
    )

    upstream_ok, errors = verify_upstream(payload)
    if not upstream_ok:
        result["blocked_reasons"] = errors
        # Envelope-level failure -- scoped to REQUIRED_SYMBOLS only, same as
        # the global gate that produced it.
        for symbol in REQUIRED_SYMBOLS:
            item = payload.get("decisions", {}).get(symbol)
            result["ledger_entries"][symbol] = {
                "symbol": symbol,
                "ledger_event": "BLOCKED",
                "upstream_execution_decision": (
                    item.get("execution_decision") if isinstance(item, dict) else None
                ),
                "paper_order_intent": False,
                "order_status": "BLOCKED",
                "position_exists": False,
                "position_id": None,
                "fill_price": None,
                "fill_timestamp": None,
                "reason": "UPSTREAM_VALIDATION_FAILED",
            }
        return finalize(result)

    decisions = payload["decisions"]
    entry_count = 0
    optional_blocked_symbols: list[str] = []

    # Every symbol v0.5.3.16 actually reported gets its own ledger entry;
    # REQUIRED_SYMBOLS are added even if somehow absent (verify_upstream
    # already gated a truly missing/invalid required symbol above, so this
    # is just defensive). Mirrors the required-vs-optional pattern already
    # shipped in v0.5.3.12-.16.
    selected = sorted(set(decisions.keys()) | set(REQUIRED_SYMBOLS))

    for symbol in selected:
        is_required = symbol in REQUIRED_SYMBOLS
        item = decisions.get(symbol)

        if not is_required:
            symbol_errors = ledger_entry_errors_for_symbol(symbol, item)
            if symbol_errors:
                result["ledger_entries"][symbol] = {
                    "symbol": symbol,
                    "ledger_event": "BLOCKED",
                    "upstream_execution_decision": (
                        item.get("execution_decision") if isinstance(item, dict) else None
                    ),
                    "paper_order_intent": False,
                    "order_status": "BLOCKED",
                    "position_exists": False,
                    "position_id": None,
                    "fill_price": None,
                    "fill_timestamp": None,
                    "reason": "UPSTREAM_VALIDATION_FAILED",
                }
                optional_blocked_symbols.append(symbol)
                continue

        execution_decision = item["execution_decision"]
        intent = item["paper_order_intent"]

        if execution_decision == "PAPER_ORDER_INTENT" and intent is True:
            ledger_event = "PAPER_ORDER_INTENT_RECORDED"
            order_status = "PENDING_EXECUTION"
            reason = "UPSTREAM_PAPER_INTENT_RECORDED_ONLY"
            entry_count += 1
        elif execution_decision == "NO_ORDER" and intent is False:
            ledger_event = "NO_ORDER_RECORDED"
            order_status = "NOT_APPLICABLE"
            reason = "UPSTREAM_NO_ORDER_RECORDED"
        elif execution_decision == "BLOCKED" and not is_required:
            # An optional symbol .16 itself individually blocked -- a
            # legitimate passthrough, not a data-integrity problem.
            ledger_event = "BLOCKED"
            order_status = "BLOCKED"
            reason = "UPSTREAM_EXECUTION_BLOCKED"
            optional_blocked_symbols.append(symbol)
        else:
            ledger_event = "BLOCKED"
            order_status = "BLOCKED"
            reason = "INCONSISTENT_UPSTREAM_EXECUTION_STATE"
            if not is_required:
                optional_blocked_symbols.append(symbol)

        result["ledger_entries"][symbol] = {
            "symbol": symbol,
            "ledger_event": ledger_event,
            "upstream_execution_decision": execution_decision,
            "paper_order_intent": intent,
            "order_status": order_status,
            "position_exists": False,
            "position_id": None,
            "fill_price": None,
            "fill_timestamp": None,
            "reason": reason,
        }

    if optional_blocked_symbols:
        result["optional_symbols_blocked"] = sorted(set(optional_blocked_symbols))

    result["decision_status"] = "DECIDED"
    result["overall_ledger_decision"] = (
        "INTENTS_RECORDED" if entry_count else "NO_ORDER_RECORDED"
    )
    return finalize(result)


def finalize(result: dict[str, Any]) -> dict[str, Any]:
    canonical = {
        "agent_version": result["agent_version"],
        "engine": result["engine"],
        "decision_status": result["decision_status"],
        "overall_ledger_decision": result["overall_ledger_decision"],
        "input_paper_execution_hash": result["input_paper_execution_hash"],
        "upstream_state_id": result["upstream_state_id"],
        "upstream_hash_verified": result["upstream_hash_verified"],
        "frozen_configuration_verified": result["frozen_configuration_verified"],
        "ledger_entries": result["ledger_entries"],
        "blocked_reasons": result["blocked_reasons"],
    }
    result["state_hash"] = sha256_text(stable_json(canonical))
    result["state_id"] = f"LE-{result['state_hash'][:24]}"
    return result


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False, allow_nan=False)
        f.write("\n")


def print_report(result: dict[str, Any], output_path: Path) -> None:
    print("=" * 96)
    print(f"{VERSION} — DECISION / EXECUTION LEDGER")
    print("=" * 96)
    print()
    print("MODE                 : RESEARCH ONLY")
    print("ORDERS               : DISABLED")
    print("MEXC / EXCHANGE CALL : DISABLED")
    print("LIVE EXECUTION       : DISABLED")
    print("LEDGER AUTHORIZATION : DISABLED")
    print("POSITION MUTATION    : DISABLED")
    print()
    print("DECISION / EXECUTION LEDGER")
    print("-" * 96)
    print(f"STATUS               : {result['decision_status']}")
    print(f"OVERALL LEDGER       : {result['overall_ledger_decision']}")
    print(f"UPSTREAM STATE ID    : {result['upstream_state_id']}")
    print(f"UPSTREAM HASH VERIFIED: {result['upstream_hash_verified']}")
    print(f"CONFIG VERIFIED      : {result['frozen_configuration_verified']}")
    print(f"STATE ID             : {result['state_id']}")
    print()

    reported_symbols = sorted(set(result["ledger_entries"].keys()) | set(REQUIRED_SYMBOLS))
    optional_blocked = set(result.get("optional_symbols_blocked", []))

    for symbol in reported_symbols:
        item = result["ledger_entries"].get(symbol)
        label = symbol
        if symbol in REQUIRED_SYMBOLS:
            label += "  [REQUIRED]"
        elif symbol in optional_blocked:
            label += "  [OPTIONAL, BLOCKED -- did not affect overall decision]"
        else:
            label += "  [OPTIONAL]"
        print(label)
        if not item:
            print("  LEDGER EVENT       : BLOCKED")
            print("  POSITION EXISTS    : False")
            print()
            continue
        print(f"  LEDGER EVENT       : {item['ledger_event']}")
        print(f"  UPSTREAM DECISION  : {item['upstream_execution_decision']}")
        print(f"  PAPER ORDER INTENT : {item['paper_order_intent']}")
        print(f"  ORDER STATUS       : {item['order_status']}")
        print(f"  POSITION EXISTS    : {item['position_exists']}")
        print(f"  POSITION ID        : {item['position_id']}")
        print(f"  FILL PRICE         : {item['fill_price']}")
        print(f"  REASON             : {item['reason']}")
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
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--once", action="store_true", help="Run once and exit")
    args = parser.parse_args()

    try:
        payload = load_json(args.input)
        result = build_ledger(payload, args.input)
        write_json(args.output, result)
        print_report(result, args.output)
        return 0
    except Exception as exc:
        result = base_result(args.input)
        result["blocked_reasons"] = [
            f"UNEXPECTED_ENGINE_ERROR:{type(exc).__name__}"
        ]
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
