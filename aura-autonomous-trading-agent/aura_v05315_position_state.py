#!/usr/bin/env python3
"""
AURA v0.5.3.15 — Position State Manager

LOCKED ARCHITECTURE
-------------------
Consumes ONLY the canonical risk-gate output produced by
AURA v0.5.3.14 — Risk Gate.

This layer deliberately does NOT:
- fetch market data;
- calculate indicators;
- recalculate signals;
- recalculate risk;
- choose position size;
- create an exchange position;
- place orders;
- perform paper execution;
- perform live execution.

Its responsibility is to translate an authenticated Risk Gate decision into
an explicit deterministic POSITION STATE. A RISK_PASS is permission to enter
this layer; it is NOT proof that a position exists.

Position states in this research build:
    NO_SIGNAL        -> FLAT
    RISK_PASS        -> ENTRY_CANDIDATE
    anything invalid -> BLOCKED

OPEN / CLOSED / EXIT states are deliberately NOT fabricated here. They
require an observed execution/reconciliation source, which is not yet part
of the v0.5.3 contract chain. This prevents a signal from becoming a false
position merely because the Risk Gate passed.

Default input:
    regime_output/risk_gate/risk_gate.json

Default output:
    regime_output/position_state/position_state.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

VERSION = "AURA v0.5.3.15"
EXPECTED_SOURCE_VERSION = "AURA v0.5.3.14"
EXPECTED_SOURCE_ENGINE = "RISK_GATE"
DEFAULT_INPUT = Path(r"regime_output\risk_gate\risk_gate.json")
DEFAULT_OUTPUT = Path(r"regime_output\position_state\position_state.json")
SYMBOLS = ("BTC/USD", "ETH/USD")
REQUIRED_SYMBOLS = SYMBOLS  # BTC/ETH remain the only symbols that can ever
# block the whole position-state decision if missing/invalid/inconsistent.
# Kept as a separate name (aliased to SYMBOLS) so the rest of this file
# reads unambiguously -- mirrors the same alias in v0.5.3.13/.14.
ALLOWED_UPSTREAM = {"NO_SIGNAL", "RISK_PASS"}
# An optional symbol may also legitimately arrive BLOCKED (v0.5.3.14 blocks
# an individual optional symbol without blocking the required pair).
ALLOWED_UPSTREAM_OPTIONAL = ALLOWED_UPSTREAM | {"BLOCKED"}


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
        "engine": "POSITION_STATE",
        "decision_status": "BLOCKED",
        "overall_position_state": "BLOCKED",
        "generated_from": str(input_path.resolve()),
        "input_risk_gate_hash": None,
        "upstream_state_id": None,
        "upstream_hash_verified": False,
        "frozen_configuration_verified": False,
        "position_truth_source": "NONE_IN_RESEARCH_BUILD",
        "positions_observed": False,
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
            "paper_execution": False,
            "live_execution": False,
            "fail_closed": True,
        },
    }


def risk_gate_hash(payload: dict[str, Any]) -> str:
    return sha256_text(stable_json(payload))


def verify_upstream(payload: dict[str, Any]) -> list[str]:
    errors: list[str] = []

    if payload.get("engine") != EXPECTED_SOURCE_ENGINE:
        errors.append("WRONG_UPSTREAM_ENGINE")
    if payload.get("agent_version") != EXPECTED_SOURCE_VERSION:
        errors.append("WRONG_UPSTREAM_VERSION")
    if payload.get("decision_status") != "DECIDED":
        errors.append("UPSTREAM_NOT_DECIDED")

    if payload.get("upstream_state_hash_verified") is not True:
        errors.append("UPSTREAM_STATE_HASH_NOT_VERIFIED")
    if payload.get("frozen_configuration_verified") is not True:
        errors.append("UPSTREAM_CONFIGURATION_NOT_VERIFIED")
    if not payload.get("upstream_state_id"):
        errors.append("MISSING_UPSTREAM_STATE_ID")

    guardrails = payload.get("guardrails")
    if not isinstance(guardrails, dict):
        errors.append("MISSING_UPSTREAM_GUARDRAILS")
    else:
        expected_false = (
            "market_data_fetch",
            "indicator_recalculation",
            "signal_recalculation",
            "risk_recalculation",
            "position_sizing",
            "position_creation",
            "exchange_state_mutation",
            "orders_allowed",
            "paper_execution",
            "live_execution",
        )
        if guardrails.get("single_source_of_truth") is not True:
            errors.append("UPSTREAM_SST_NOT_ENFORCED")
        for key in expected_false:
            if guardrails.get(key) is not False:
                errors.append(f"UPSTREAM_GUARDRAIL_VIOLATION:{key}")

    decisions = payload.get("decisions")
    if not isinstance(decisions, dict):
        errors.append("MISSING_UPSTREAM_DECISIONS")
        return sorted(set(errors))

    # Only REQUIRED_SYMBOLS (BTC/ETH) gate the whole decision here. Every
    # other symbol v0.5.3.14 reports is validated independently, per-symbol,
    # in build_position_state() below -- one optional symbol's bad/blocked
    # upstream data must never block another symbol's position state.
    for symbol in REQUIRED_SYMBOLS:
        errors.extend(
            risk_decision_consistency_errors(symbol, decisions.get(symbol), allow_blocked=False)
        )

    return sorted(set(errors))


def risk_decision_consistency_errors(
    symbol: str, item: Any, *, allow_blocked: bool
) -> list[str]:
    """
    Check that a single symbol's upstream risk decision is internally
    consistent (a known risk_decision value whose risk_authorized flag
    agrees with it). No risk is recalculated here.
    """
    if not isinstance(item, dict):
        return [f"MISSING_OR_INVALID_SYMBOL:{symbol}"]

    upstream = item.get("risk_decision")
    authorized = item.get("risk_authorized")
    allowed = ALLOWED_UPSTREAM_OPTIONAL if allow_blocked else ALLOWED_UPSTREAM

    errors: list[str] = []
    if upstream not in allowed:
        errors.append(f"INVALID_RISK_DECISION:{symbol}:{upstream!r}")
    if upstream == "RISK_PASS" and authorized is not True:
        errors.append(f"RISK_PASS_NOT_AUTHORIZED:{symbol}")
    if upstream == "NO_SIGNAL" and authorized is not False:
        errors.append(f"NO_SIGNAL_AUTHORIZATION_INCONSISTENT:{symbol}")
    if upstream == "BLOCKED" and authorized is not False:
        errors.append(f"BLOCKED_AUTHORIZATION_INCONSISTENT:{symbol}")

    return errors


def build_position_state(payload: dict[str, Any], input_path: Path) -> dict[str, Any]:
    result = base_result(input_path)
    result["input_risk_gate_hash"] = risk_gate_hash(payload)
    result["upstream_state_id"] = payload.get("upstream_state_id")
    result["upstream_hash_verified"] = payload.get("upstream_state_hash_verified") is True
    result["frozen_configuration_verified"] = payload.get("frozen_configuration_verified") is True

    errors = verify_upstream(payload)
    if errors:
        result["blocked_reasons"] = errors
        return finalize(result)

    signal_states: list[str] = []
    optional_blocked_symbols: list[str] = []
    decisions = payload["decisions"]

    # Every symbol v0.5.3.14 actually reported gets its own position state;
    # REQUIRED_SYMBOLS are added even if somehow absent (verify_upstream
    # already gated a truly missing/invalid required symbol above, so this
    # is just defensive). Mirrors the required-vs-optional pattern already
    # shipped in v0.5.3.12/.13/.14.
    selected = sorted(set(decisions.keys()) | set(REQUIRED_SYMBOLS))

    for symbol in selected:
        is_required = symbol in REQUIRED_SYMBOLS
        item = decisions.get(symbol)

        if is_required:
            # Already verified consistent (NO_SIGNAL/RISK_PASS only) by
            # verify_upstream() above.
            risk_decision = item["risk_decision"]
            risk_authorized = item["risk_authorized"]
            if risk_decision == "RISK_PASS":
                position_state = "ENTRY_CANDIDATE"
                reason = "RISK_PASS_AWAITING_EXECUTION"
            else:
                position_state = "FLAT"
                reason = "NO_RISK_AUTHORIZED_ENTRY"
            signal_states.append(position_state)
        else:
            symbol_errors = risk_decision_consistency_errors(symbol, item, allow_blocked=True)
            risk_decision = item.get("risk_decision") if isinstance(item, dict) else None
            risk_authorized = item.get("risk_authorized") if isinstance(item, dict) else False

            if symbol_errors:
                position_state = "BLOCKED"
                reason = "UPSTREAM_RISK_DECISION_INVALID"
                optional_blocked_symbols.append(symbol)
            elif risk_decision == "RISK_PASS":
                position_state = "ENTRY_CANDIDATE"
                reason = "RISK_PASS_AWAITING_EXECUTION"
                signal_states.append(position_state)
            elif risk_decision == "NO_SIGNAL":
                position_state = "FLAT"
                reason = "NO_RISK_AUTHORIZED_ENTRY"
                signal_states.append(position_state)
            else:
                # BLOCKED upstream -- this optional symbol's own bad data
                # blocks only its own position state, never the cycle.
                position_state = "BLOCKED"
                reason = "UPSTREAM_RISK_BLOCKED"
                optional_blocked_symbols.append(symbol)

        result["decisions"][symbol] = {
            "symbol": symbol,
            "upstream_risk_decision": risk_decision,
            "risk_authorized": risk_authorized,
            "position_state": position_state,
            "position_exists": False,
            "position_id": None,
            "reason": reason,
        }

    # No observed execution/reconciliation source exists in this contract.
    # Therefore OPEN can never be asserted by this module.
    result["decision_status"] = "DECIDED"
    if any(state == "ENTRY_CANDIDATE" for state in signal_states):
        result["overall_position_state"] = "ENTRY_CANDIDATE"
    elif signal_states:
        result["overall_position_state"] = "FLAT"
    else:
        result["overall_position_state"] = "BLOCKED"

    if optional_blocked_symbols:
        result["optional_symbols_blocked"] = sorted(optional_blocked_symbols)

    return finalize(result)


def finalize(result: dict[str, Any]) -> dict[str, Any]:
    # Hash only the deterministic state payload, excluding the hash/id fields.
    canonical = {
        "agent_version": result["agent_version"],
        "engine": result["engine"],
        "decision_status": result["decision_status"],
        "overall_position_state": result["overall_position_state"],
        "upstream_state_id": result["upstream_state_id"],
        "upstream_hash_verified": result["upstream_hash_verified"],
        "frozen_configuration_verified": result["frozen_configuration_verified"],
        "position_truth_source": result["position_truth_source"],
        "positions_observed": result["positions_observed"],
        "decisions": result["decisions"],
        "blocked_reasons": result["blocked_reasons"],
    }
    result["state_hash"] = sha256_text(stable_json(canonical))
    result["state_id"] = f"PS-{result['state_hash'][:24]}"
    return result


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False, allow_nan=False)
        f.write("\n")


def print_report(result: dict[str, Any], output_path: Path) -> None:
    print("=" * 96)
    print(f"{VERSION} — POSITION STATE")
    print("=" * 96)
    print()
    print("MODE                 : RESEARCH ONLY")
    print("ORDERS               : DISABLED")
    print("PAPER EXECUTION      : DISABLED")
    print("LIVE EXECUTION       : DISABLED")
    print("POSITION CREATION    : DISABLED")
    print("RECONCILIATION SOURCE: NOT YET IMPLEMENTED")
    print()
    print("POSITION STATE")
    print("-" * 96)
    print(f"STATUS               : {result['decision_status']}")
    print(f"OVERALL STATE        : {result['overall_position_state']}")
    print(f"UPSTREAM STATE ID    : {result['upstream_state_id']}")
    print(f"UPSTREAM HASH VERIFIED: {result['upstream_hash_verified']}")
    print(f"CONFIG VERIFIED      : {result['frozen_configuration_verified']}")
    print(f"STATE ID             : {result['state_id']}")
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
            print("  POSITION STATE     : BLOCKED")
            print("  POSITION EXISTS    : False")
            print()
            continue
        print(f"  UPSTREAM RISK      : {item['upstream_risk_decision']}")
        print(f"  POSITION STATE     : {item['position_state']}")
        print(f"  POSITION EXISTS    : {item['position_exists']}")
        print(f"  POSITION ID        : {item['position_id']}")
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
        result = build_position_state(payload, args.input)
        write_json(args.output, result)
        print_report(result, args.output)
        return 0
    except Exception as exc:
        result = base_result(args.input)
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
