#!/usr/bin/env python3
"""
AURA v0.5.3.19 — Execution Safety / Kill Switch

LOCKED ARCHITECTURE
-------------------
Consumes ONLY the authenticated v0.5.3.18 Reconciliation output and a
local, explicit safety configuration. This layer is deterministic and
fail-closed.

It deliberately does NOT:
- fetch market data;
- calculate indicators;
- recalculate signals or risk;
- size positions;
- create positions;
- fabricate execution evidence;
- call MEXC or any exchange;
- place orders;
- mutate position state;
- override reconciliation;
- enable live execution by itself.

Its responsibility is to provide the final deterministic execution-safety
verdict for the current pipeline state. A positive verdict is an
AUTHORIZATION CONDITION, not an order instruction. The actual executor must
still enforce its own execution-mode controls.

Default input:
    regime_output/reconciliation/reconciliation.json

Default output:
    regime_output/execution_safety/execution_safety.json

Safety configuration may be supplied with --config. If omitted, the module
uses the immutable research-only defaults embedded below:
    kill_switch = True
    paper_execution_enabled = False
    live_execution_enabled = False
    orders_enabled = False

This means a normal run cannot authorize any order.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

VERSION = "AURA v0.5.3.19"
EXPECTED_SOURCE_VERSION = "AURA v0.5.3.18"
EXPECTED_SOURCE_ENGINE = "RECONCILIATION"

DEFAULT_INPUT = Path(
    r"regime_output\reconciliation\reconciliation.json"
)
DEFAULT_OUTPUT = Path(
    r"regime_output\execution_safety\execution_safety.json"
)

SYMBOLS = ("BTC/USD", "ETH/USD")
REQUIRED_SYMBOLS = SYMBOLS  # BTC/ETH remain the only symbols that can ever
# block the whole execution-safety decision if missing/invalid. Kept as a
# separate name (aliased to SYMBOLS) so the rest of this file reads
# unambiguously -- mirrors the same alias in v0.5.3.13-.18. Note this file's
# execution_authorized is hard-coded False in every branch regardless of
# symbol or config (see build_safety() below) -- this required-vs-optional
# split only affects which symbol's missing/invalid upstream data can block
# the whole report, never whether anything gets authorized.

# v0.5.3.x remains research-only. These defaults are intentionally
# conservative and must not be interpreted as a live-trading configuration.
DEFAULT_SAFETY_CONFIG: dict[str, Any] = {
    "kill_switch": True,
    "orders_enabled": False,
    "paper_execution_enabled": False,
    "live_execution_enabled": False,
}


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


def load_config(path: Path | None) -> dict[str, Any]:
    if path is None:
        return dict(DEFAULT_SAFETY_CONFIG)
    payload = load_json(path)
    config = dict(DEFAULT_SAFETY_CONFIG)
    for key in DEFAULT_SAFETY_CONFIG:
        if key in payload:
            if not isinstance(payload[key], bool):
                raise ValueError(f"Safety config value must be boolean: {key}")
            config[key] = payload[key]
    return config


def canonical_reconciliation_hash(payload: dict[str, Any]) -> str:
    canonical = {
        "agent_version": payload.get("agent_version"),
        "engine": payload.get("engine"),
        "decision_status": payload.get("decision_status"),
        "overall_reconciliation": payload.get("overall_reconciliation"),
        "input_ledger_hash": payload.get("input_ledger_hash"),
        "input_observed_execution_hash": payload.get(
            "input_observed_execution_hash"
        ),
        "upstream_state_id": payload.get("upstream_state_id"),
        "upstream_hash_verified": payload.get("upstream_hash_verified"),
        "frozen_configuration_verified": payload.get(
            "frozen_configuration_verified"
        ),
        "observed_execution_verified": payload.get(
            "observed_execution_verified"
        ),
        "decisions": payload.get("decisions"),
        "blocked_reasons": payload.get("blocked_reasons"),
    }
    return sha256_text(stable_json(canonical))


def verify_reconciliation(payload: dict[str, Any]) -> tuple[bool, list[str]]:
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
            calculated = canonical_reconciliation_hash(payload)
        except (TypeError, ValueError):
            errors.append("UPSTREAM_STATE_NOT_HASHABLE")
        else:
            if calculated != supplied_hash:
                errors.append("UPSTREAM_STATE_HASH_MISMATCH")
        if payload.get("state_id") != f"RC-{supplied_hash[:24]}":
            errors.append("UPSTREAM_STATE_ID_MISMATCH")

    if payload.get("upstream_hash_verified") is not True:
        errors.append("UPSTREAM_HASH_NOT_VERIFIED")
    if payload.get("frozen_configuration_verified") is not True:
        errors.append("UPSTREAM_CONFIGURATION_NOT_VERIFIED")
    if payload.get("observed_execution_verified") is not True:
        errors.append("OBSERVED_EXECUTION_NOT_VERIFIED")

    guardrails = payload.get("guardrails")
    if not isinstance(guardrails, dict):
        errors.append("MISSING_UPSTREAM_GUARDRAILS")
    else:
        required_true = (
            "single_source_of_truth",
            "reconciliation_only",
            "position_creation_from_observation",
        )
        # position_creation_from_observation is required to be FALSE; the
        # name is retained to explicitly document that this capability is off.
        if guardrails.get("single_source_of_truth") is not True:
            errors.append("UPSTREAM_SST_NOT_ENFORCED")
        if guardrails.get("reconciliation_only") is not True:
            errors.append("UPSTREAM_RECONCILIATION_ONLY_NOT_ENFORCED")
        if guardrails.get("position_creation_from_observation") is not False:
            errors.append("UPSTREAM_POSITION_CREATION_FROM_OBSERVATION_VIOLATION")
        for key in (
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
            "fabricated_fill",
            "fabricated_position",
        ):
            if guardrails.get(key) is not False:
                errors.append(f"UPSTREAM_GUARDRAIL_VIOLATION:{key}")

    decisions = payload.get("decisions")
    if not isinstance(decisions, dict):
        errors.append("MISSING_UPSTREAM_DECISIONS")
        return False, sorted(set(errors))

    # Only REQUIRED_SYMBOLS (BTC/ETH) gate the whole decision here. Every
    # other symbol v0.5.3.18 reports is validated independently, per-symbol,
    # in build_safety() below.
    for symbol in REQUIRED_SYMBOLS:
        errors.extend(reconciliation_state_errors_for_symbol(symbol, decisions.get(symbol)))

    return len(errors) == 0, sorted(set(errors))


ALLOWED_RECONCILIATION_STATES = {
    "NO_ORDER_CONFIRMED",
    "RECONCILED_EXECUTION",
    "EXECUTION_NOT_FILLED",
    "EXECUTION_PENDING",
    "RECONCILIATION_CONFLICT",
    "BLOCKED",
}
# NOTE: kept identical to the original set -- does NOT include "UNVERIFIED",
# even though v0.5.3.18's own missing-observation-file fallback can
# legitimately produce that state. That gap predates this change and is
# out of scope for the required-vs-optional symbol handling here; flagging
# it rather than silently expanding this allowlist.


def reconciliation_state_errors_for_symbol(symbol: str, item: Any) -> list[str]:
    """
    Structural validity check for a single symbol's v0.5.3.18 reconciliation
    entry. No reconciliation is recalculated here.
    """
    if not isinstance(item, dict):
        return [f"MISSING_OR_INVALID_SYMBOL:{symbol}"]

    reconciliation = item.get("reconciliation_state")
    if reconciliation not in ALLOWED_RECONCILIATION_STATES:
        return [f"INVALID_RECONCILIATION_STATE:{symbol}:{reconciliation!r}"]

    return []


def base_result(input_path: Path, config: dict[str, Any]) -> dict[str, Any]:
    return {
        "agent_version": VERSION,
        "engine": "EXECUTION_SAFETY",
        "decision_status": "BLOCKED",
        "overall_execution_safety": "BLOCKED",
        "execution_authorized": False,
        "paper_execution_authorized": False,
        "live_execution_authorized": False,
        "kill_switch_active": config["kill_switch"],
        "orders_enabled": config["orders_enabled"],
        "paper_execution_enabled": config["paper_execution_enabled"],
        "live_execution_enabled": config["live_execution_enabled"],
        "generated_from": str(input_path.resolve()),
        "input_reconciliation_hash": None,
        "upstream_state_id": None,
        "upstream_hash_verified": False,
        "frozen_configuration_verified": False,
        "observed_execution_verified": False,
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
            "kill_switch_bypass": False,
            "reconciliation_bypass": False,
            "manual_position_claim": False,
            "fabricated_execution": False,
            "fail_closed": True,
        },
    }


def build_safety(
    reconciliation: dict[str, Any],
    input_path: Path,
    config: dict[str, Any],
) -> dict[str, Any]:
    result = base_result(input_path, config)
    result["input_reconciliation_hash"] = reconciliation.get("state_hash")
    result["upstream_state_id"] = reconciliation.get("state_id")
    result["upstream_hash_verified"] = reconciliation.get("upstream_hash_verified") is True
    result["frozen_configuration_verified"] = (
        reconciliation.get("frozen_configuration_verified") is True
    )
    result["observed_execution_verified"] = (
        reconciliation.get("observed_execution_verified") is True
    )

    upstream_ok, errors = verify_reconciliation(reconciliation)
    if not upstream_ok:
        result["blocked_reasons"] = errors
        # Envelope-level failure -- scoped to REQUIRED_SYMBOLS only, same as
        # the global gate that produced it.
        for symbol in REQUIRED_SYMBOLS:
            result["decisions"][symbol] = {
                "symbol": symbol,
                "reconciliation_state": None,
                "execution_safety": "BLOCKED",
                "execution_authorized": False,
                "reason": "UPSTREAM_VALIDATION_FAILED",
            }
        return finalize(result)

    decisions = reconciliation["decisions"]

    # Any active kill switch is an absolute veto. It cannot be overridden by
    # an upstream signal, paper intent, configuration flag, or AI proposal.
    if config["kill_switch"]:
        result["blocked_reasons"].append("KILL_SWITCH_ACTIVE")

    optional_blocked_symbols: list[str] = []

    # Every symbol v0.5.3.18 actually reported gets its own safety verdict;
    # REQUIRED_SYMBOLS are added even if somehow absent (verify_reconciliation
    # already gated a truly missing/invalid required symbol above, so this
    # is just defensive). Mirrors the required-vs-optional pattern already
    # shipped in v0.5.3.12-.18. execution_authorized stays False in every
    # branch below regardless -- this loop only decides what gets reported,
    # never what gets authorized.
    selected = sorted(set(decisions.keys()) | set(REQUIRED_SYMBOLS))

    for symbol in selected:
        is_required = symbol in REQUIRED_SYMBOLS
        item = decisions.get(symbol)

        if not is_required:
            symbol_errors = reconciliation_state_errors_for_symbol(symbol, item)
            if symbol_errors:
                result["decisions"][symbol] = {
                    "symbol": symbol,
                    "reconciliation_state": (
                        item.get("reconciliation_state") if isinstance(item, dict) else None
                    ),
                    "execution_safety": "BLOCKED",
                    "execution_authorized": False,
                    "reason": "UPSTREAM_VALIDATION_FAILED",
                }
                optional_blocked_symbols.append(symbol)
                continue

        state = item.get("reconciliation_state")

        if config["kill_switch"]:
            safety = "BLOCKED"
            authorized = False
            reason = "KILL_SWITCH_ACTIVE"
        elif state == "RECONCILIATION_CONFLICT":
            safety = "BLOCKED"
            authorized = False
            reason = "RECONCILIATION_CONFLICT"
        elif state == "BLOCKED":
            safety = "BLOCKED"
            authorized = False
            reason = "RECONCILIATION_BLOCKED"
        elif state == "EXECUTION_PENDING":
            safety = "BLOCKED"
            authorized = False
            reason = "EXECUTION_NOT_RECONCILED"
        elif state in {"NO_ORDER_CONFIRMED", "EXECUTION_NOT_FILLED"}:
            safety = "NO_ORDER"
            authorized = False
            reason = "NO_EXECUTABLE_POSITION_CHANGE"
        elif state == "RECONCILED_EXECUTION":
            # Reconciliation proves an execution occurred; it does not grant
            # permission to create another order. This layer is not an order
            # generator and therefore remains non-authorizing for execution.
            safety = "RECONCILED_NO_NEW_ORDER"
            authorized = False
            reason = "RECONCILIATION_IS_NOT_ORDER_AUTHORITY"
        else:
            safety = "BLOCKED"
            authorized = False
            reason = "UNHANDLED_RECONCILIATION_STATE"

        if safety == "BLOCKED" and not is_required and not config["kill_switch"]:
            # An optional symbol's own bad reconciliation state (conflict/
            # blocked/pending) is recorded for that symbol only; it never
            # adds to blocked_reasons or affects the required pair's report.
            optional_blocked_symbols.append(symbol)

        result["decisions"][symbol] = {
            "symbol": symbol,
            "reconciliation_state": state,
            "execution_safety": safety,
            "execution_authorized": authorized,
            "reason": reason,
        }

    if optional_blocked_symbols:
        result["optional_symbols_blocked"] = sorted(set(optional_blocked_symbols))

    # v0.5.3.19 never turns a reconciliation result into an executable order.
    # The configuration flags are recorded for auditability but do not bypass
    # the hard research-only boundary.
    result["paper_execution_authorized"] = False
    result["live_execution_authorized"] = False
    result["execution_authorized"] = False
    result["decision_status"] = "DECIDED"

    if result["blocked_reasons"]:
        result["overall_execution_safety"] = "BLOCKED"
    else:
        result["overall_execution_safety"] = "SAFE_NO_ORDER"

    return finalize(result)


def finalize(result: dict[str, Any]) -> dict[str, Any]:
    canonical = {
        "agent_version": result["agent_version"],
        "engine": result["engine"],
        "decision_status": result["decision_status"],
        "overall_execution_safety": result["overall_execution_safety"],
        "execution_authorized": result["execution_authorized"],
        "paper_execution_authorized": result["paper_execution_authorized"],
        "live_execution_authorized": result["live_execution_authorized"],
        "kill_switch_active": result["kill_switch_active"],
        "orders_enabled": result["orders_enabled"],
        "paper_execution_enabled": result["paper_execution_enabled"],
        "live_execution_enabled": result["live_execution_enabled"],
        "upstream_state_id": result["upstream_state_id"],
        "upstream_hash_verified": result["upstream_hash_verified"],
        "frozen_configuration_verified": result[
            "frozen_configuration_verified"
        ],
        "observed_execution_verified": result["observed_execution_verified"],
        "decisions": result["decisions"],
        "blocked_reasons": result["blocked_reasons"],
    }
    result["state_hash"] = sha256_text(stable_json(canonical))
    result["state_id"] = f"ES-{result['state_hash'][:24]}"
    return result


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False, allow_nan=False)
        f.write("\n")


def print_report(result: dict[str, Any], output_path: Path) -> None:
    print("=" * 96)
    print(f"{VERSION} — EXECUTION SAFETY / KILL SWITCH")
    print("=" * 96)
    print()
    print("MODE                 : RESEARCH ONLY")
    print("REAL ORDERS          : DISABLED")
    print("MEXC / EXCHANGE CALL : DISABLED")
    print("PAPER EXECUTION      : DISABLED")
    print("LIVE EXECUTION       : DISABLED")
    print("KILL SWITCH          : " + ("ACTIVE" if result["kill_switch_active"] else "INACTIVE"))
    print()
    print("EXECUTION SAFETY")
    print("-" * 96)
    print(f"STATUS               : {result['decision_status']}")
    print(f"OVERALL SAFETY       : {result['overall_execution_safety']}")
    print(f"EXECUTION AUTHORIZED : {result['execution_authorized']}")
    print(f"PAPER AUTHORIZED     : {result['paper_execution_authorized']}")
    print(f"LIVE AUTHORIZED      : {result['live_execution_authorized']}")
    print(f"UPSTREAM STATE ID    : {result['upstream_state_id']}")
    print(f"UPSTREAM HASH VERIFIED: {result['upstream_hash_verified']}")
    print(f"CONFIG VERIFIED      : {result['frozen_configuration_verified']}")
    print(f"OBSERVATION VERIFIED : {result['observed_execution_verified']}")
    print(f"STATE ID             : {result['state_id']}")
    print()

    reported_symbols = sorted(set(result["decisions"].keys()) | set(REQUIRED_SYMBOLS))
    optional_blocked = set(result.get("optional_symbols_blocked", []))

    for symbol in reported_symbols:
        item = result["decisions"].get(symbol, {})
        label = symbol
        if symbol in REQUIRED_SYMBOLS:
            label += "  [REQUIRED]"
        elif symbol in optional_blocked:
            label += "  [OPTIONAL, BLOCKED -- did not affect overall decision]"
        else:
            label += "  [OPTIONAL]"
        print(label)
        print(f"  RECONCILIATION     : {item.get('reconciliation_state')}")
        print(f"  SAFETY             : {item.get('execution_safety')}")
        print(f"  AUTHORIZED         : {item.get('execution_authorized')}")
        print(f"  REASON             : {item.get('reason')}")
        print()

    if result["blocked_reasons"]:
        print("FAIL-CLOSED REASONS")
        print("-" * 96)
        for reason in result["blocked_reasons"]:
            print(f"  - {reason}")
        print()

    print(f"OUTPUT               : {output_path.resolve()}")
    print("=" * 96)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=VERSION)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--once", action="store_true", help="Run one deterministic evaluation.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    try:
        reconciliation = load_json(args.input)
        config = load_config(args.config)
        result = build_safety(reconciliation, args.input, config)
        write_json(args.output, result)
        print_report(result, args.output)
        return 0
    except (FileNotFoundError, json.JSONDecodeError, ValueError, TypeError) as exc:
        print(f"FAIL-CLOSED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
