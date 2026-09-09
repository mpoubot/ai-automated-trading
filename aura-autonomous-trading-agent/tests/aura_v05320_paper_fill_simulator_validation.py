#!/usr/bin/env python3
"""
AURA v0.5.3.20 — Paper Fill Simulator Contract Validation

Offline-only tests for the deterministic paper fill simulator.

Validates:
  1. no-order records become rejected observations;
  2. paper intents remain pending under the safe default policy;
  3. explicit FILLED configuration creates deterministic simulated fills;
  4. the observed snapshot hash matches v0.5.3.18's canonical contract;
  5. tampered upstream ledger input fails closed;
  6. no exchange/live-execution capability is exposed by the output;
  7. a legitimate per-symbol BLOCKED ledger entry (e.g. Risk Gate's
     required-symbol timestamp-synchronization gate) is simulated as a
     rejected, no-position observation instead of crashing fail-closed;
  8. an envelope-level BLOCKED ledger (decision_status itself "BLOCKED",
     cascaded through v0.5.3.15/.16/.17's upstream-must-be-DECIDED checks)
     is simulated the same way, symbol by symbol, instead of crashing.

Usage from repository root:
    python .\tests\aura_v05320_paper_fill_simulator_validation.py
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "aura_v05320_paper_fill_simulator.py"
SYMBOLS = ("BTC/USD", "ETH/USD")


def stable_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_ledger_hash(payload: dict[str, Any]) -> str:
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


def canonical_observed_hash(payload: dict[str, Any]) -> str:
    canonical = {
        "agent_version": payload.get("agent_version"),
        "engine": payload.get("engine"),
        "snapshot_status": payload.get("snapshot_status"),
        "observations": payload.get("observations"),
    }
    return sha256_text(stable_json(canonical))


def write(path: Path, payload: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False, allow_nan=False)
        f.write("\n")


def make_ledger(tmp: Path, intents: bool) -> Path:
    entries: dict[str, Any] = {}
    for symbol in SYMBOLS:
        if intents:
            entries[symbol] = {
                "symbol": symbol,
                "ledger_event": "PAPER_ORDER_INTENT_RECORDED",
                "upstream_execution_decision": "PAPER_ORDER_INTENT",
                "paper_order_intent": True,
                "order_status": "PENDING_EXECUTION",
                "position_exists": False,
                "position_id": None,
                "fill_price": None,
                "fill_timestamp": None,
                "reason": "UPSTREAM_PAPER_INTENT_RECORDED_ONLY",
            }
        else:
            entries[symbol] = {
                "symbol": symbol,
                "ledger_event": "NO_ORDER_RECORDED",
                "upstream_execution_decision": "NO_ORDER",
                "paper_order_intent": False,
                "order_status": "NOT_APPLICABLE",
                "position_exists": False,
                "position_id": None,
                "fill_price": None,
                "fill_timestamp": None,
                "reason": "UPSTREAM_NO_ORDER_RECORDED",
            }

    payload: dict[str, Any] = {
        "agent_version": "AURA v0.5.3.17",
        "engine": "DECISION_EXECUTION_LEDGER",
        "decision_status": "DECIDED",
        "overall_ledger_decision": "INTENTS_RECORDED" if intents else "NO_ORDER_RECORDED",
        "input_paper_execution_hash": "PE-VALIDATION",
        "upstream_state_id": "PE-VALIDATION",
        "upstream_hash_verified": True,
        "frozen_configuration_verified": True,
        "ledger_entries": entries,
        "blocked_reasons": [],
        "state_hash": None,
        "state_id": None,
        "guardrails": {
            "single_source_of_truth": True,
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
        },
    }
    payload["state_hash"] = canonical_ledger_hash(payload)
    payload["state_id"] = f"LE-{payload['state_hash'][:24]}"

    path = tmp / ("ledger_intent.json" if intents else "ledger_no_order.json")
    write(path, payload)
    return path


def make_blocked_ledger(tmp: Path) -> Path:
    """
    Mirrors a v0.5.3.17 ledger produced when v0.5.3.14's Risk Gate blocks the
    whole cycle for the required symbols (e.g. an unsynchronized required-
    symbol market-data timestamp) -- ledger_event is legitimately "BLOCKED"
    for every required symbol, per v0.5.3.17's own documented contract.
    """
    entries: dict[str, Any] = {}
    for symbol in SYMBOLS:
        entries[symbol] = {
            "symbol": symbol,
            "ledger_event": "BLOCKED",
            "upstream_execution_decision": "BLOCKED",
            "paper_order_intent": False,
            "order_status": "NOT_APPLICABLE",
            "position_exists": False,
            "position_id": None,
            "fill_price": None,
            "fill_timestamp": None,
            "reason": "UPSTREAM_OR_INTEGRITY_CHECK_FAILED",
        }

    payload: dict[str, Any] = {
        "agent_version": "AURA v0.5.3.17",
        "engine": "DECISION_EXECUTION_LEDGER",
        "decision_status": "DECIDED",
        "overall_ledger_decision": "BLOCKED",
        "input_paper_execution_hash": "PE-VALIDATION",
        "upstream_state_id": "PE-VALIDATION",
        "upstream_hash_verified": True,
        "frozen_configuration_verified": True,
        "ledger_entries": entries,
        "blocked_reasons": ["SYMBOL_STATE_TIMESTAMPS_NOT_SYNCHRONIZED"],
        "state_hash": None,
        "state_id": None,
        "guardrails": {
            "single_source_of_truth": True,
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
        },
    }
    payload["state_hash"] = canonical_ledger_hash(payload)
    payload["state_id"] = f"LE-{payload['state_hash'][:24]}"

    path = tmp / "ledger_blocked.json"
    write(path, payload)
    return path


def make_envelope_blocked_ledger(tmp: Path) -> Path:
    """
    Mirrors the ledger v0.5.3.17 actually produces when its own envelope-
    level upstream verification fails -- e.g. cascaded from v0.5.3.14's
    required-symbol timestamp-synchronization gate, propagated through
    v0.5.3.15/.16's equally strict "immediate upstream must be DECIDED"
    checks. Unlike make_blocked_ledger() above (a per-symbol BLOCKED event
    inside an otherwise-DECIDED ledger), this is the real shape seen in
    production: decision_status itself is "BLOCKED", not "DECIDED".
    """
    entries: dict[str, Any] = {}
    for symbol in SYMBOLS:
        entries[symbol] = {
            "symbol": symbol,
            "ledger_event": "BLOCKED",
            "upstream_execution_decision": "BLOCKED",
            "paper_order_intent": False,
            "order_status": "BLOCKED",
            "position_exists": False,
            "position_id": None,
            "fill_price": None,
            "fill_timestamp": None,
            "reason": "UPSTREAM_VALIDATION_FAILED",
        }

    payload: dict[str, Any] = {
        "agent_version": "AURA v0.5.3.17",
        "engine": "DECISION_EXECUTION_LEDGER",
        "decision_status": "BLOCKED",
        "overall_ledger_decision": "BLOCKED",
        "input_paper_execution_hash": "PE-VALIDATION",
        "upstream_state_id": "PE-VALIDATION",
        "upstream_hash_verified": True,
        "frozen_configuration_verified": True,
        "ledger_entries": entries,
        "blocked_reasons": ["UPSTREAM_NOT_DECIDED", "OVERALL_NO_ORDER_INCONSISTENT"],
        "state_hash": None,
        "state_id": None,
        "guardrails": {
            "single_source_of_truth": True,
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
        },
    }
    payload["state_hash"] = canonical_ledger_hash(payload)
    payload["state_id"] = f"LE-{payload['state_hash'][:24]}"

    path = tmp / "ledger_envelope_blocked.json"
    write(path, payload)
    return path


def run_simulator(input_path: Path, output_path: Path, config_path: Path | None = None) -> tuple[int, str]:
    cmd = [sys.executable, str(SCRIPT), "--input", str(input_path), "--output", str(output_path)]
    if config_path is not None:
        cmd += ["--config", str(config_path)]
    proc = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True)
    return proc.returncode, proc.stdout + proc.stderr


def load(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        value = json.load(f)
    if not isinstance(value, dict):
        raise AssertionError(f"Expected JSON object: {path}")
    return value


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> int:
    print("=" * 96)
    print("AURA v0.5.3.20 — PAPER FILL SIMULATOR VALIDATION")
    print("=" * 96)
    print("OFFLINE ONLY — NO MEXC / NO LIVE ORDERS / NO PRODUCTION STATE")
    print()

    failures: list[str] = []
    passes = 0

    with tempfile.TemporaryDirectory(prefix="aura_v05320_") as td:
        tmp = Path(td)

        def case(name: str, fn) -> None:
            nonlocal passes
            try:
                fn()
                print(f"[PASS] {name}")
                passes += 1
            except Exception as exc:
                print(f"[FAIL] {name}: {exc}")
                failures.append(f"{name}: {exc}")

        def test_no_order() -> None:
            ledger = make_ledger(tmp, intents=False)
            output = tmp / "observed_no_order.json"
            rc, out = run_simulator(ledger, output)
            assert_true(rc == 0, out[-1200:])
            payload = load(output)
            assert_true(payload["observation_source"] == "PAPER_FILL_SIMULATOR", "wrong observation source")
            for symbol in SYMBOLS:
                item = payload["observations"][symbol]
                assert_true(item["order_status"] == "REJECTED", f"{symbol} not rejected")
                assert_true(item["position_exists"] is False, f"{symbol} position claimed")
                assert_true(item["fill_price"] is None, f"{symbol} fabricated fill price")
                assert_true(item["fill_timestamp"] is None, f"{symbol} fabricated timestamp")

        case("NO_ORDER produces no-position observation", test_no_order)

        def test_blocked_ledger_event() -> None:
            ledger = make_blocked_ledger(tmp)
            output = tmp / "observed_blocked.json"
            rc, out = run_simulator(ledger, output)
            assert_true(rc == 0, out[-1200:])
            payload = load(output)
            for symbol in SYMBOLS:
                item = payload["observations"][symbol]
                assert_true(item["order_status"] == "REJECTED", f"{symbol} not rejected")
                assert_true(item["position_exists"] is False, f"{symbol} position claimed")
                assert_true(item["fill_price"] is None, f"{symbol} fabricated fill price")
                assert_true(item["fill_timestamp"] is None, f"{symbol} fabricated timestamp")
                assert_true(
                    item["simulation_reason"] == "UPSTREAM_LEDGER_BLOCKED",
                    f"{symbol} wrong simulation_reason: {item['simulation_reason']}",
                )

        case("BLOCKED ledger event is simulated, not fail-closed", test_blocked_ledger_event)

        def test_envelope_blocked_ledger() -> None:
            ledger = make_envelope_blocked_ledger(tmp)
            output = tmp / "observed_envelope_blocked.json"
            rc, out = run_simulator(ledger, output)
            assert_true(rc == 0, out[-1200:])
            payload = load(output)
            for symbol in SYMBOLS:
                item = payload["observations"][symbol]
                assert_true(item["order_status"] == "REJECTED", f"{symbol} not rejected")
                assert_true(item["position_exists"] is False, f"{symbol} position claimed")
                assert_true(item["fill_price"] is None, f"{symbol} fabricated fill price")
                assert_true(item["fill_timestamp"] is None, f"{symbol} fabricated timestamp")
                assert_true(
                    item["simulation_reason"] == "UPSTREAM_LEDGER_BLOCKED",
                    f"{symbol} wrong simulation_reason: {item['simulation_reason']}",
                )

        case("Envelope-level BLOCKED ledger (decision_status=BLOCKED) is simulated, not fail-closed", test_envelope_blocked_ledger)

        def test_default_pending() -> None:
            ledger = make_ledger(tmp, intents=True)
            output = tmp / "observed_pending.json"
            rc, out = run_simulator(ledger, output)
            assert_true(rc == 0, out[-1200:])
            payload = load(output)
            for symbol in SYMBOLS:
                item = payload["observations"][symbol]
                assert_true(item["order_status"] == "PENDING", f"{symbol} not pending")
                assert_true(item["position_exists"] is False, f"{symbol} position claimed")

        case("Default policy leaves paper intents pending", test_default_pending)

        def test_deterministic_fill() -> None:
            ledger = make_ledger(tmp, intents=True)
            config = tmp / "fill_config.json"
            write(
                config,
                {
                    "simulation_timestamp": "2026-08-30T10:00:00Z",
                    "fill_policy": "FILLED",
                    "fill_prices": {"BTC/USD": 100000.0, "ETH/USD": 4000.0},
                },
            )
            output_a = tmp / "observed_fill_a.json"
            output_b = tmp / "observed_fill_b.json"
            rc_a, out_a = run_simulator(ledger, output_a, config)
            rc_b, out_b = run_simulator(ledger, output_b, config)
            assert_true(rc_a == 0, out_a[-1200:])
            assert_true(rc_b == 0, out_b[-1200:])
            a = load(output_a)
            b = load(output_b)
            assert_true(a["snapshot_hash"] == b["snapshot_hash"], "simulation is not deterministic")
            assert_true(a["observations"] == b["observations"], "observations are not deterministic")
            for symbol in SYMBOLS:
                item = a["observations"][symbol]
                assert_true(item["order_status"] == "FILLED", f"{symbol} not filled")
                assert_true(item["position_exists"] is True, f"{symbol} missing simulated position")
                assert_true(isinstance(item["position_id"], str) and item["position_id"].startswith("SIM-"), f"{symbol} invalid simulated position id")
                assert_true(item["fill_price"] > 0, f"{symbol} invalid fill price")
                assert_true(item["fill_timestamp"] == "2026-08-30T10:00:00Z", f"{symbol} timestamp changed")

        case("Explicit FILLED policy is deterministic", test_deterministic_fill)

        def test_reconciliation_hash_contract() -> None:
            ledger = make_ledger(tmp, intents=True)
            config = tmp / "hash_config.json"
            write(
                config,
                {
                    "simulation_timestamp": "2026-08-30T10:00:00Z",
                    "fill_policy": "FILLED",
                    "fill_prices": {"BTC/USD": 100000.0, "ETH/USD": 4000.0},
                },
            )
            output = tmp / "observed_hash.json"
            rc, out = run_simulator(ledger, output, config)
            assert_true(rc == 0, out[-1200:])
            payload = load(output)
            assert_true(payload["snapshot_hash"] == canonical_observed_hash(payload), "snapshot hash does not match v0.5.3.18 contract")
            assert_true(payload["engine"] == "OBSERVED_EXECUTION", "wrong observed engine")
            assert_true(payload["snapshot_status"] == "OBSERVED", "snapshot not marked OBSERVED")

        case("Output hash matches v0.5.3.18 observed-source contract", test_reconciliation_hash_contract)

        def test_tampered_ledger() -> None:
            ledger = make_ledger(tmp, intents=True)
            payload = load(ledger)
            payload["ledger_entries"]["BTC/USD"]["ledger_event"] = "NO_ORDER_RECORDED"
            tampered = tmp / "tampered_ledger.json"
            write(tampered, payload)
            output = tmp / "should_not_exist.json"
            rc, out = run_simulator(tampered, output)
            assert_true(rc != 0, "tampered ledger unexpectedly succeeded")
            assert_true(not output.exists(), "simulator produced output from tampered ledger")
            assert_true("UPSTREAM_LEDGER_INVALID" in out or "FAIL-CLOSED" in out, "tampered ledger did not fail closed")

        case("Tampered upstream ledger fails closed", test_tampered_ledger)

        def test_guardrails() -> None:
            ledger = make_ledger(tmp, intents=True)
            output = tmp / "guardrails.json"
            rc, out = run_simulator(ledger, output)
            assert_true(rc == 0, out[-1200:])
            payload = load(output)
            guardrails = payload["guardrails"]
            for key in ("exchange_state_mutation", "orders_allowed", "live_execution", "mexc_calls", "fabricated_live_execution", "live_position_claim"):
                assert_true(guardrails.get(key) is False, f"guardrail {key} is not false")
            assert_true(guardrails.get("simulation_only") is True, "simulation_only guardrail missing")

        case("Simulator exposes no live execution authority", test_guardrails)

        assert_true(SCRIPT.exists(), "v0.5.3.20 script is missing")

    print()
    print("-" * 96)
    print(f"PASSED : {passes}")
    print(f"FAILED : {len(failures)}")
    if failures:
        print("RESULT : FAIL")
        for failure in failures:
            print(f"  - {failure}")
        return 1

    print("RESULT : PASS")
    print("PAPER FILL SIMULATOR STATUS: VALIDATED OFFLINE")
    print("NO EXCHANGE / NO LIVE EXECUTION / NO PRODUCTION STATE MUTATION")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
