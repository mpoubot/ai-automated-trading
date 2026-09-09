#!/usr/bin/env python3
"""
AURA v0.5.3 â€” Complete Safety Chain Validation

VALIDATES ONLY THE EXISTING OFFLINE CONTRACTS.

Chain:
    v0.5.3.16 Paper Execution
    -> v0.5.3.17 Decision / Execution Ledger
    -> v0.5.3.18 Reconciliation
    -> v0.5.3.19 Execution Safety / Kill Switch

This harness is intentionally offline. It never calls an exchange, never
places an order, and never changes production execution state.

The harness creates temporary JSON fixtures and invokes the four contracts
through their CLI interfaces. It validates:
  1. clean no-order lineage;
  2. explicit observed-execution reconciliation;
  3. tampered upstream rejection;
  4. observed-source tamper rejection;
  5. kill-switch / execution-safety blocking;
  6. absence of exchange/live execution authority in every stage.

Usage from repository root:
    python .\tests\aura_v053_safety_chain_validation.py
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = {
    "16": ROOT / "aura_v05316_paper_execution.py",
    "17": ROOT / "aura_v05317_decision_execution_ledger.py",
    "18": ROOT / "aura_v05318_reconciliation.py",
    "19": ROOT / "aura_v05319_execution_safety.py",
}
SYMBOLS = ("BTC/USD", "ETH/USD")


def stable_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def run_script(script: Path, input_path: Path | None, output_path: Path, extra: list[str] | None = None) -> tuple[int, str]:
    cmd = [sys.executable, str(script)]
    if input_path is not None:
        if script.name == "aura_v05318_reconciliation.py":
            cmd += ["--ledger-input", str(input_path)]
        else:
            cmd += ["--input", str(input_path)]
    cmd += ["--output", str(output_path)]
    if extra:
        cmd += extra
    proc = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True)
    return proc.returncode, proc.stdout + proc.stderr


def load(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        value = json.load(f)
    if not isinstance(value, dict):
        raise AssertionError(f"Expected object JSON: {path}")
    return value


def write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False, allow_nan=False)
        f.write("\n")


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def assert_blocked(payload: dict[str, Any], marker: str) -> None:
    text = json.dumps(payload, sort_keys=True)
    assert_true(
        payload.get("decision_status") == "BLOCKED" or payload.get("overall_safety") == "BLOCKED" or payload.get("overall_reconciliation") == "BLOCKED",
        f"{marker}: expected blocked contract, got {payload.get('decision_status')} / {payload.get('overall_safety')} / {payload.get('overall_reconciliation')}",
    )
    assert_true(marker in text or "UPSTREAM" in text or "VALIDATION" in text or "OBSERVATION" in text, f"{marker}: no blocking evidence in payload")


def patch_hash(payload: dict[str, Any], hash_builder) -> dict[str, Any]:
    result = copy.deepcopy(payload)
    result["state_hash"] = hash_builder(result)
    return result


def canonical_position_hash(payload: dict[str, Any]) -> str:
    canonical = {
        "agent_version": payload.get("agent_version"),
        "engine": payload.get("engine"),
        "decision_status": payload.get("decision_status"),
        "overall_position_state": payload.get("overall_position_state"),
        "upstream_state_id": payload.get("upstream_state_id"),
        "upstream_hash_verified": payload.get("upstream_hash_verified"),
        "frozen_configuration_verified": payload.get("frozen_configuration_verified"),
        "position_truth_source": payload.get("position_truth_source"),
        "positions_observed": payload.get("positions_observed"),
        "decisions": payload.get("decisions"),
        "blocked_reasons": payload.get("blocked_reasons"),
    }
    return sha256_text(stable_json(canonical))


def make_position_state(tmp: Path, candidate: bool = False) -> Path:
    decisions = {}
    for symbol in SYMBOLS:
        decisions[symbol] = {
            "symbol": symbol,
            "position_state": "ENTRY_CANDIDATE" if candidate else "FLAT",
            "position_exists": False,
            "position_id": None,
            "fill_price": None,
            "fill_timestamp": None,
        }
    payload: dict[str, Any] = {
        "agent_version": "AURA v0.5.3.15",
        "engine": "POSITION_STATE",
        "decision_status": "DECIDED",
        "overall_position_state": "ENTRY_CANDIDATE" if candidate else "FLAT",
        "upstream_state_id": "MS-VALIDATION-FIXTURE",
        "upstream_hash_verified": True,
        "frozen_configuration_verified": True,
        "position_truth_source": "RECONCILIATION",
        "positions_observed": {},
        "decisions": decisions,
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
            "live_execution": False,
            "fail_closed": True,
        },
    }
    payload["state_hash"] = canonical_position_hash(payload)
    payload["state_id"] = f"PS-{payload['state_hash'][:24]}"
    path = tmp / "position_state.json"
    write(path, payload)
    return path


def canonical_observed_hash(payload: dict[str, Any]) -> str:
    canonical = {
        "agent_version": payload.get("agent_version"),
        "engine": payload.get("engine"),
        "snapshot_status": payload.get("snapshot_status"),
        "observations": payload.get("observations"),
    }
    return sha256_text(stable_json(canonical))


def make_observed(tmp: Path, filled: bool = False) -> Path:
    observations = {}
    for symbol in SYMBOLS:
        if filled:
            observations[symbol] = {
                "symbol": symbol,
                "order_status": "FILLED",
                "position_exists": True,
                "position_id": f"OBS-{symbol.replace('/', '-')}",
                "fill_price": 100000.0 if symbol == "BTC/USD" else 4000.0,
                "fill_timestamp": "2026-08-30T10:00:00Z",
            }
        else:
            observations[symbol] = {
                "symbol": symbol,
                "order_status": "REJECTED",
                "position_exists": False,
                "position_id": None,
                "fill_price": None,
                "fill_timestamp": None,
            }
    payload: dict[str, Any] = {
        "agent_version": "AURA v0.5.3.18",
        "engine": "OBSERVED_EXECUTION",
        "snapshot_status": "OBSERVED",
        "observations": observations,
        "snapshot_hash": None,
    }
    payload["snapshot_hash"] = canonical_observed_hash(payload)
    path = tmp / "observed_execution.json"
    write(path, payload)
    return path


def validate_contract_guardrails(payload: dict[str, Any], label: str) -> None:
    guardrails = payload.get("guardrails", {})
    for key in ("exchange_state_mutation", "orders_allowed", "live_execution"):
        assert_true(guardrails.get(key) is False, f"{label}: {key} must remain false")


def main() -> int:
    print("=" * 96)
    print("AURA v0.5.3 â€” COMPLETE SAFETY CHAIN VALIDATION")
    print("=" * 96)
    print("OFFLINE ONLY â€” NO MEXC / NO LIVE ORDERS / NO PRODUCTION STATE")
    print()

    failures: list[str] = []
    passes = 0

    with tempfile.TemporaryDirectory(prefix="aura_safety_chain_") as td:
        tmp = Path(td)
        cases: list[tuple[str, Any]] = []

        def case(name: str, fn) -> None:
            nonlocal passes
            try:
                fn()
                print(f"[PASS] {name}")
                passes += 1
            except Exception as exc:
                print(f"[FAIL] {name}: {exc}")
                failures.append(f"{name}: {exc}")

        def build_no_order_chain() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
            ps = make_position_state(tmp, candidate=False)
            pe_path = tmp / "paper_execution.json"
            rc, out = run_script(SCRIPTS["16"], ps, pe_path)
            assert_true(rc == 0, f".16 failed: {out[-1200:]}")
            pe = load(pe_path)

            le_path = tmp / "ledger.json"
            rc, out = run_script(SCRIPTS["17"], pe_path, le_path)
            assert_true(rc == 0, f".17 failed: {out[-1200:]}")
            le = load(le_path)

            obs = make_observed(tmp, filled=False)
            rc_path = tmp / "reconciliation.json"
            rc, out = run_script(SCRIPTS["18"], le_path, rc_path, ["--observed", str(obs)])
            assert_true(rc == 0, f".18 failed: {out[-1200:]}")
            rec = load(rc_path)

            es_path = tmp / "execution_safety.json"
            rc, out = run_script(SCRIPTS["19"], rc_path, es_path)
            assert_true(rc == 0, f".19 failed: {out[-1200:]}")
            es = load(es_path)
            return pe, le, rec, es

        def test_no_order_chain() -> None:
            pe, le, rec, es = build_no_order_chain()
            assert_true(pe.get("overall_execution_decision") == "NO_ORDER", ".16 must remain NO_ORDER")
            assert_true(le.get("overall_ledger_decision") == "NO_ORDER_RECORDED", ".17 must record NO_ORDER")
            assert_true(rec.get("overall_reconciliation") in {"RECONCILED", "NO_ORDER_CONFIRMED", "UNVERIFIED"}, ".18 produced unexpected state")
            assert_true(es.get("execution_authorized") is False, ".19 must not authorize no-order chain")
            validate_contract_guardrails(pe, ".16")
            validate_contract_guardrails(le, ".17")
            validate_contract_guardrails(rec, ".18")
            validate_contract_guardrails(es, ".19")

        case("Clean no-order lineage .16 -> .17 -> .18 -> .19", test_no_order_chain)

        def test_observation_required() -> None:
            ps = make_position_state(tmp, candidate=True)
            pe_path = tmp / "pe_candidate.json"
            rc, out = run_script(SCRIPTS["16"], ps, pe_path)
            assert_true(rc == 0, out[-1200:])
            le_path = tmp / "le_candidate.json"
            rc, out = run_script(SCRIPTS["17"], pe_path, le_path)
            assert_true(rc == 0, out[-1200:])
            rec_path = tmp / "rec_missing.json"
            rc, out = run_script(SCRIPTS["18"], le_path, rec_path, ["--observed", str(tmp / "does_not_exist.json")])
            assert_true(rc != 0, "missing observed source should fail closed")
            assert_true("Input file not found" in out or "FAIL-CLOSED" in out or "BLOCKED" in out, "missing observation did not visibly fail closed")

        case("Reconciliation requires explicit observed-execution source", test_observation_required)

        def test_valid_observed_fill() -> None:
            ps = make_position_state(tmp, candidate=True)
            pe_path = tmp / "pe_fill.json"
            rc, out = run_script(SCRIPTS["16"], ps, pe_path)
            assert_true(rc == 0, out[-1200:])
            le_path = tmp / "le_fill.json"
            rc, out = run_script(SCRIPTS["17"], pe_path, le_path)
            assert_true(rc == 0, out[-1200:])
            obs = make_observed(tmp, filled=True)
            rec_path = tmp / "rec_fill.json"
            rc, out = run_script(SCRIPTS["18"], le_path, rec_path, ["--observed", str(obs)])
            assert_true(rc == 0, out[-1200:])
            rec = load(rec_path)
            assert_true(rec.get("observed_execution_verified") is True, "valid observation was not verified")
            assert_true(rec.get("overall_reconciliation") == "RECONCILED", "valid observation did not reconcile")
            for symbol in SYMBOLS:
                item = rec["decisions"][symbol]
                assert_true(item["execution_reconciled"] is True, f"{symbol} not reconciled")
                assert_true(item["position_exists"] is True, f"{symbol} position not derived from observation")
                assert_true(item["position_id"] is not None, f"{symbol} missing observed position id")

        case("Valid explicit observed fill reconciles", test_valid_observed_fill)

        def test_tampered_ledger_rejected() -> None:
            ps = make_position_state(tmp, candidate=False)
            pe_path = tmp / "pe_tamper_ledger.json"
            rc, out = run_script(SCRIPTS["16"], ps, pe_path)
            assert_true(rc == 0, out[-1200:])
            le_path = tmp / "le_tamper.json"
            rc, out = run_script(SCRIPTS["17"], pe_path, le_path)
            assert_true(rc == 0, out[-1200:])
            le = load(le_path)
            le["ledger_entries"]["BTC/USD"]["ledger_event"] = "PAPER_ORDER_INTENT_RECORDED"
            write(le_path, le)
            obs = make_observed(tmp, filled=False)
            rec_path = tmp / "rec_tampered_ledger.json"
            rc, out = run_script(SCRIPTS["18"], le_path, rec_path, ["--observed", str(obs)])
            assert_true(rc == 0, "validation failure should be represented as blocked output, not crash")
            rec = load(rec_path)
            assert_blocked(rec, "UPSTREAM_STATE_HASH_MISMATCH")

        case("Tampered ledger is rejected by reconciliation", test_tampered_ledger_rejected)

        def test_tampered_observation_rejected() -> None:
            ps = make_position_state(tmp, candidate=True)
            pe_path = tmp / "pe_tamper_obs.json"
            rc, out = run_script(SCRIPTS["16"], ps, pe_path)
            assert_true(rc == 0, out[-1200:])
            le_path = tmp / "le_tamper_obs.json"
            rc, out = run_script(SCRIPTS["17"], pe_path, le_path)
            assert_true(rc == 0, out[-1200:])
            obs = make_observed(tmp, filled=True)
            observed = load(obs)
            observed["observations"]["BTC/USD"]["fill_price"] = 1.0
            write(obs, observed)
            rec_path = tmp / "rec_tampered_obs.json"
            rc, out = run_script(SCRIPTS["18"], le_path, rec_path, ["--observed", str(obs)])
            assert_true(rc == 0, "observation validation failure should be represented as blocked output")
            rec = load(rec_path)
            assert_blocked(rec, "OBSERVED_SNAPSHOT_HASH_MISMATCH")

        case("Tampered observed execution snapshot is rejected", test_tampered_observation_rejected)

        def test_kill_switch_blocks() -> None:
            _, _, rec, _ = build_no_order_chain()
            es_path = tmp / "es_kill.json"
            rc, out = run_script(SCRIPTS["19"], tmp / "nonexistent_reconciliation.json", es_path)
            assert_true(rc != 0, "missing reconciliation input must fail closed")
            assert_true("Input file not found" in out or "FAIL-CLOSED" in out or "BLOCKED" in out, "kill-safety boundary did not fail closed on missing upstream")
            _ = rec

        case("Execution safety fails closed when upstream is unavailable", test_kill_switch_blocks)

        def test_all_scripts_exist() -> None:
            for key, path in SCRIPTS.items():
                assert_true(path.exists(), f"v0.5.3.{key} script missing: {path}")

        case("All .16-.19 contracts are present", test_all_scripts_exist)

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
    print("SAFETY CHAIN STATUS: .16 -> .17 -> .18 -> .19 validated offline")
    print("NO EXCHANGE / NO LIVE EXECUTION / NO PRODUCTION STATE MUTATION")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

