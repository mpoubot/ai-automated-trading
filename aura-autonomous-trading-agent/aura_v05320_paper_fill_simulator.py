#!/usr/bin/env python3
"""
AURA v0.5.3.20 — Paper Fill Simulator

RESEARCH-ONLY EXECUTION ENVIRONMENT
-----------------------------------
Consumes ONLY the authenticated v0.5.3.17 Decision / Execution Ledger and
produces an explicit, deterministic SIMULATED OBSERVED EXECUTION snapshot for
v0.5.3.18 Reconciliation.

This module is deliberately NOT part of the execution-authority chain. It:
- never fetches market data;
- never calculates indicators;
- never recalculates signals or risk;
- never sizes positions;
- never calls MEXC or any exchange;
- never places live orders;
- never mutates Position State;
- never fabricates a claim that a live position exists.

The output is simulation evidence, not exchange evidence. It is marked with
observation_source = PAPER_FILL_SIMULATOR and simulation = True. The
v0.5.3.18 reconciliation contract can consume this snapshot in the paper
research environment because reconciliation treats its observed input as
data and does not grant execution authority.

Pipeline:
    v0.5.3.16 Paper Order Intent
        -> v0.5.3.17 Decision / Execution Ledger
        -> v0.5.3.20 Paper Fill Simulator
        -> simulated observed_execution.json
        -> v0.5.3.18 Reconciliation

The simulator is deterministic. It never uses the current clock. A simulation
configuration supplies the timestamp and, for filled intents, the fill price.
If no configuration is supplied, the safe default is PENDING for every paper
order intent and REJECTED for NO_ORDER records. A BLOCKED ledger entry (the
Risk Gate or another upstream stage refused the whole cycle for this symbol,
e.g. an unsynchronized required-symbol market-data timestamp) is also a
legitimate, expected ledger_event -- v0.5.3.17 documents it as an already-
accepted execution_decision value. It always carries no position/fill claim,
exactly like NO_ORDER_RECORDED, so it is simulated the same way: REJECTED,
no position, no fabricated fill -- tagged with its own simulation_reason so
it is never conflated with an ordinary no-order cycle. The same block can
also appear at the envelope level: v0.5.3.17 itself reports
decision_status "BLOCKED" (rather than "DECIDED") when its own upstream
verification cascades into a full-cycle block, and in that state every
required symbol's ledger_entries carries a "BLOCKED" event by construction.
This module accepts that ledger the same way -- it is still a fully-formed,
hash-verifiable document -- and simulates it identically, symbol by symbol.

Default input:
    regime_output/decision_execution_ledger/decision_execution_ledger.json

Default output:
    regime_input/observed_execution/observed_execution.json

Optional configuration JSON:
{
  "simulation_timestamp": "2026-08-30T10:00:00Z",
  "fill_policy": "FILLED",
  "fill_prices": {
    "BTC/USD": 100000.0,
    "ETH/USD": 4000.0
  }
}

Supported fill_policy values:
    PENDING
    FILLED
    REJECTED
    CANCELED

For FILLED, every PAPER_ORDER_INTENT symbol must have a positive numeric
fill_prices entry. No price is inferred from market data.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

VERSION = "AURA v0.5.3.20"
EXPECTED_SOURCE_VERSION = "AURA v0.5.3.17"
EXPECTED_SOURCE_ENGINE = "DECISION_EXECUTION_LEDGER"
OBSERVED_ENGINE = "OBSERVED_EXECUTION"

DEFAULT_INPUT = Path(
    r"regime_output\decision_execution_ledger\decision_execution_ledger.json"
)
DEFAULT_OUTPUT = Path(
    r"regime_input\observed_execution\observed_execution.json"
)

SYMBOLS = ("BTC/USD", "ETH/USD")
ALLOWED_LEDGER_EVENTS = {
    "NO_ORDER_RECORDED",
    "PAPER_ORDER_INTENT_RECORDED",
    "BLOCKED",
}
ALLOWED_POLICIES = {"PENDING", "FILLED", "REJECTED", "CANCELED"}
DEFAULT_CONFIG: dict[str, Any] = {
    "simulation_timestamp": "2026-08-30T10:00:00Z",
    "fill_policy": "PENDING",
    "fill_prices": {},
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
    config = dict(DEFAULT_CONFIG)
    if path is None:
        return config

    payload = load_json(path)
    for key in ("simulation_timestamp", "fill_policy", "fill_prices"):
        if key in payload:
            config[key] = payload[key]

    timestamp = config["simulation_timestamp"]
    if not isinstance(timestamp, str) or not timestamp:
        raise ValueError("simulation_timestamp must be a non-empty string")

    policy = config["fill_policy"]
    if policy not in ALLOWED_POLICIES:
        raise ValueError(
            f"fill_policy must be one of {sorted(ALLOWED_POLICIES)}"
        )

    prices = config["fill_prices"]
    if not isinstance(prices, dict):
        raise ValueError("fill_prices must be a JSON object")

    return config


def canonical_ledger_hash(payload: dict[str, Any]) -> str:
    """Reconstruct the exact deterministic hash payload used by v0.5.3.17."""
    canonical = {
        "agent_version": payload.get("agent_version"),
        "engine": payload.get("engine"),
        "decision_status": payload.get("decision_status"),
        "overall_ledger_decision": payload.get("overall_ledger_decision"),
        "input_paper_execution_hash": payload.get("input_paper_execution_hash"),
        "upstream_state_id": payload.get("upstream_state_id"),
        "upstream_hash_verified": payload.get("upstream_hash_verified"),
        "frozen_configuration_verified": payload.get("frozen_configuration_verified"),
        "ledger_entries": payload.get("ledger_entries"),
        "blocked_reasons": payload.get("blocked_reasons"),
    }
    return sha256_text(stable_json(canonical))


def verify_ledger(payload: dict[str, Any]) -> tuple[bool, list[str]]:
    errors: list[str] = []

    if payload.get("engine") != EXPECTED_SOURCE_ENGINE:
        errors.append("WRONG_UPSTREAM_ENGINE")
    if payload.get("agent_version") != EXPECTED_SOURCE_VERSION:
        errors.append("WRONG_UPSTREAM_VERSION")
    # "BLOCKED" is v0.5.3.17's own legitimate envelope-level outcome (e.g.
    # cascaded from v0.5.3.14's required-symbol timestamp-synchronization
    # gate, propagated through v0.5.3.15/.16's identically-strict upstream
    # checks). It still carries a fully-formed, hash-verifiable
    # ledger_entries block with "BLOCKED" events for both required symbols
    # -- there is nothing structurally wrong with it, so it is simulated
    # the same way a per-symbol BLOCKED event is: no order, no position, no
    # fabricated fill. Any other value is still rejected fail-closed.
    if payload.get("decision_status") not in {"DECIDED", "BLOCKED"}:
        errors.append("UPSTREAM_NOT_DECIDED")

    supplied_hash = payload.get("state_hash")
    if not isinstance(supplied_hash, str) or not supplied_hash:
        errors.append("MISSING_UPSTREAM_STATE_HASH")
    else:
        try:
            calculated = canonical_ledger_hash(payload)
        except (TypeError, ValueError):
            errors.append("UPSTREAM_STATE_NOT_HASHABLE")
        else:
            if calculated != supplied_hash:
                errors.append("UPSTREAM_STATE_HASH_MISMATCH")

        if payload.get("state_id") != f"LE-{supplied_hash[:24]}":
            errors.append("UPSTREAM_STATE_ID_MISMATCH")

    if payload.get("upstream_hash_verified") is not True:
        errors.append("UPSTREAM_HASH_NOT_VERIFIED")
    if payload.get("frozen_configuration_verified") is not True:
        errors.append("UPSTREAM_CONFIGURATION_NOT_VERIFIED")

    guardrails = payload.get("guardrails")
    if not isinstance(guardrails, dict):
        errors.append("MISSING_UPSTREAM_GUARDRAILS")
    else:
        if guardrails.get("single_source_of_truth") is not True:
            errors.append("UPSTREAM_SST_NOT_ENFORCED")
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
            "ledger_authorization",
            "ledger_state_mutation",
            "position_claim_from_ledger",
        ):
            if guardrails.get(key) is not False:
                errors.append(f"UPSTREAM_GUARDRAIL_VIOLATION:{key}")

    entries = payload.get("ledger_entries")
    if not isinstance(entries, dict):
        errors.append("MISSING_UPSTREAM_LEDGER_ENTRIES")
        return False, sorted(set(errors))

    for symbol in SYMBOLS:
        item = entries.get(symbol)
        if not isinstance(item, dict):
            errors.append(f"MISSING_OR_INVALID_SYMBOL:{symbol}")
            continue

        event = item.get("ledger_event")
        if event not in ALLOWED_LEDGER_EVENTS:
            errors.append(f"INVALID_LEDGER_EVENT:{symbol}:{event!r}")

        if item.get("position_exists") is not False:
            errors.append(f"POSITION_CLAIM_NOT_FALSE:{symbol}")
        if item.get("position_id") is not None:
            errors.append(f"POSITION_ID_PRESENT:{symbol}")
        if item.get("fill_price") is not None:
            errors.append(f"FILL_PRICE_PRESENT_IN_LEDGER:{symbol}")
        if item.get("fill_timestamp") is not None:
            errors.append(f"FILL_TIMESTAMP_PRESENT_IN_LEDGER:{symbol}")

    return len(errors) == 0, sorted(set(errors))


def base_result(
    ledger_path: Path,
    config: dict[str, Any],
) -> dict[str, Any]:
    return {
        "agent_version": VERSION,
        "engine": OBSERVED_ENGINE,
        "snapshot_status": "OBSERVED",
        "observation_source": "PAPER_FILL_SIMULATOR",
        "simulation": True,
        "simulation_timestamp": config["simulation_timestamp"],
        "simulation_policy": config["fill_policy"],
        "generated_from_ledger": str(ledger_path.resolve()),
        "input_ledger_hash": None,
        "input_ledger_state_id": None,
        "observations": {},
        "snapshot_hash": None,
        "guardrails": {
            "research_only": True,
            "simulation_only": True,
            "exchange_state_mutation": False,
            "orders_allowed": False,
            "live_execution": False,
            "market_data_fetch": False,
            "indicator_recalculation": False,
            "signal_recalculation": False,
            "risk_recalculation": False,
            "position_sizing": False,
            "position_creation": False,
            "mexc_calls": False,
            "fabricated_live_execution": False,
            "live_position_claim": False,
            "uses_current_clock": False,
            "fail_closed": True,
        },
    }


def build_observed(
    ledger: dict[str, Any],
    ledger_path: Path,
    config: dict[str, Any],
) -> dict[str, Any]:
    result = base_result(ledger_path, config)
    result["input_ledger_hash"] = ledger.get("state_hash")
    result["input_ledger_state_id"] = ledger.get("state_id")

    ledger_ok, errors = verify_ledger(ledger)
    if not ledger_ok:
        raise ValueError("UPSTREAM_LEDGER_INVALID: " + ",".join(errors))

    policy = config["fill_policy"]
    prices = config["fill_prices"]
    timestamp = config["simulation_timestamp"]
    ledger_hash = ledger["state_hash"]

    for symbol in SYMBOLS:
        entry = ledger["ledger_entries"][symbol]
        event = entry["ledger_event"]

        if event == "NO_ORDER_RECORDED":
            result["observations"][symbol] = {
                "symbol": symbol,
                "order_status": "REJECTED",
                "position_exists": False,
                "position_id": None,
                "fill_price": None,
                "fill_timestamp": None,
                "simulation_reason": "NO_ORDER_IN_UPSTREAM_LEDGER",
            }
            continue

        if event == "BLOCKED":
            # The cycle was refused upstream of v0.5.3.17 for this symbol
            # (e.g. Risk Gate's required-symbol timestamp-synchronization
            # gate). No order was ever recorded, so this is simulated
            # identically to NO_ORDER_RECORDED -- REJECTED, no position, no
            # fabricated fill -- but tagged with its own reason so a
            # genuinely blocked cycle is never indistinguishable from an
            # ordinary no-signal cycle in the simulated observation.
            result["observations"][symbol] = {
                "symbol": symbol,
                "order_status": "REJECTED",
                "position_exists": False,
                "position_id": None,
                "fill_price": None,
                "fill_timestamp": None,
                "simulation_reason": "UPSTREAM_LEDGER_BLOCKED",
            }
            continue

        if event != "PAPER_ORDER_INTENT_RECORDED":
            raise ValueError(f"Unsupported ledger event for {symbol}: {event}")

        if policy == "PENDING":
            result["observations"][symbol] = {
                "symbol": symbol,
                "order_status": "PENDING",
                "position_exists": False,
                "position_id": None,
                "fill_price": None,
                "fill_timestamp": None,
                "simulation_reason": "PAPER_INTENT_LEFT_PENDING_BY_POLICY",
            }
        elif policy in {"REJECTED", "CANCELED"}:
            result["observations"][symbol] = {
                "symbol": symbol,
                "order_status": policy,
                "position_exists": False,
                "position_id": None,
                "fill_price": None,
                "fill_timestamp": None,
                "simulation_reason": f"PAPER_INTENT_{policy}_BY_POLICY",
            }
        elif policy == "FILLED":
            price = prices.get(symbol)
            if not isinstance(price, (int, float)) or isinstance(price, bool) or price <= 0:
                raise ValueError(
                    f"FILLED policy requires a positive numeric fill_prices entry for {symbol}"
                )
            position_id = f"SIM-{sha256_text(f'{ledger_hash}:{symbol}:{timestamp}:{price}')[:24]}"
            result["observations"][symbol] = {
                "symbol": symbol,
                "order_status": "FILLED",
                "position_exists": True,
                "position_id": position_id,
                "fill_price": float(price),
                "fill_timestamp": timestamp,
                "simulation_reason": "PAPER_INTENT_FILLED_BY_DETERMINISTIC_POLICY",
            }

    return finalize(result)


def canonical_observed_hash(payload: dict[str, Any]) -> str:
    """Match the canonical observed hash required by v0.5.3.18."""
    canonical = {
        "agent_version": payload.get("agent_version"),
        "engine": payload.get("engine"),
        "snapshot_status": payload.get("snapshot_status"),
        "observations": payload.get("observations"),
    }
    return sha256_text(stable_json(canonical))


def finalize(payload: dict[str, Any]) -> dict[str, Any]:
    payload["snapshot_hash"] = canonical_observed_hash(payload)
    return payload


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False, allow_nan=False)
        f.write("\n")


def print_report(payload: dict[str, Any], output_path: Path) -> None:
    print("=" * 96)
    print(f"{VERSION} — PAPER FILL SIMULATOR")
    print("=" * 96)
    print()
    print("MODE                 : RESEARCH ONLY")
    print("SIMULATION ONLY      : ENABLED")
    print("MEXC / EXCHANGE CALL : DISABLED")
    print("LIVE ORDERS          : DISABLED")
    print("LIVE POSITION CLAIM  : DISABLED")
    print("MARKET DATA FETCH    : DISABLED")
    print(f"SIMULATION POLICY    : {payload['simulation_policy']}")
    print(f"SIMULATION TIMESTAMP : {payload['simulation_timestamp']}")
    print()
    print("SIMULATED OBSERVATION")
    print("-" * 96)
    print(f"INPUT LEDGER STATE   : {payload['input_ledger_state_id']}")
    print(f"INPUT LEDGER HASH    : {payload['input_ledger_hash']}")
    print(f"SNAPSHOT HASH        : {payload['snapshot_hash']}")
    print()

    for symbol in SYMBOLS:
        item = payload["observations"][symbol]
        print(symbol)
        print(f"  ORDER STATUS       : {item['order_status']}")
        print(f"  POSITION EXISTS    : {item['position_exists']}")
        print(f"  POSITION ID        : {item['position_id']}")
        print(f"  FILL PRICE         : {item['fill_price']}")
        print(f"  FILL TIMESTAMP     : {item['fill_timestamp']}")
        print(f"  REASON             : {item['simulation_reason']}")
        print()

    print(f"OUTPUT               : {output_path.resolve()}")
    print("NOTE                 : This file is simulation evidence, not exchange evidence.")
    print("=" * 96)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--config", type=Path, default=None)
    args = parser.parse_args()

    try:
        ledger = load_json(args.input)
        config = load_config(args.config)
        result = build_observed(ledger, args.input, config)
        write_json(args.output, result)
        print_report(result, args.output)
        return 0
    except (FileNotFoundError, ValueError, TypeError, json.JSONDecodeError) as exc:
        print("=" * 96)
        print(f"{VERSION} — FAIL-CLOSED")
        print("=" * 96)
        print(f"ERROR                : {exc}")
        print("SIMULATED OUTPUT     : NOT GENERATED")
        print("LIVE EXECUTION       : DISABLED")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
