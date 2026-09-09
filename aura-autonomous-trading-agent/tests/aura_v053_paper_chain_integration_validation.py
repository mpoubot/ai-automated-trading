#!/usr/bin/env python3
"""
AURA v0.5.3 â€” Paper Chain Integration Validation

Validates the actual paper-path composition:

    Position State fixture
      -> .16 Paper Execution
      -> .17 Decision / Execution Ledger
      -> .20 Paper Fill Simulator
      -> .18 Reconciliation
      -> .19 Execution Safety

The test is completely offline and never calls an exchange.
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
SCRIPTS = {
    "16": ROOT / "aura_v05316_paper_execution.py",
    "17": ROOT / "aura_v05317_decision_execution_ledger.py",
    "18": ROOT / "aura_v05318_reconciliation.py",
    "19": ROOT / "aura_v05319_execution_safety.py",
    "20": ROOT / "aura_v05320_paper_fill_simulator.py",
}
SYMBOLS = ("BTC/USD", "ETH/USD")


def stable_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False, allow_nan=False)
        f.write("\n")


def load(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        value = json.load(f)
    if not isinstance(value, dict):
        raise AssertionError(f"Expected JSON object: {path}")
    return value


def run(script: Path, input_path: Path | None, output_path: Path, extra: list[str] | None = None) -> tuple[int, str]:
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


def make_position_state(tmp: Path) -> Path:
    decisions: dict[str, Any] = {}
    for symbol in SYMBOLS:
        decisions[symbol] = {
            "symbol": symbol,
            "position_state": "ENTRY_CANDIDATE",
            "position_exists": False,
            "position_id": None,
            "fill_price": None,
            "fill_timestamp": None,
        }

    payload: dict[str, Any] = {
        "agent_version": "AURA v0.5.3.15",
        "engine": "POSITION_STATE",
        "decision_status": "DECIDED",
        "overall_position_state": "ENTRY_CANDIDATE",
        "upstream_state_id": "MS-INTEGRATION-FIXTURE",
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
    canonical = {
        "agent_version": payload["agent_version"],
        "engine": payload["engine"],
        "decision_status": payload["decision_status"],
        "overall_position_state": payload["overall_position_state"],
        "upstream_state_id": payload["upstream_state_id"],
        "upstream_hash_verified": payload["upstream_hash_verified"],
        "frozen_configuration_verified": payload["frozen_configuration_verified"],
        "position_truth_source": payload["position_truth_source"],
        "positions_observed": payload["positions_observed"],
        "decisions": payload["decisions"],
        "blocked_reasons": payload["blocked_reasons"],
    }
    payload["state_hash"] = sha256_text(stable_json(canonical))
    payload["state_id"] = f"PS-{payload['state_hash'][:24]}"

    path = tmp / "position_state.json"
    write(path, payload)
    return path


def main() -> int:
    print("=" * 96)
    print("AURA v0.5.3 â€” PAPER CHAIN INTEGRATION VALIDATION")
    print("=" * 96)
    print("OFFLINE ONLY â€” NO MEXC / NO LIVE ORDERS / NO PRODUCTION STATE")
    print()

    with tempfile.TemporaryDirectory(prefix="aura_paper_chain_") as td:
        tmp = Path(td)
        for key, path in SCRIPTS.items():
            if not path.exists():
                print(f"[FAIL] Missing .{key}: {path}")
                return 1

        ps = make_position_state(tmp)

        pe = tmp / "paper_execution.json"
        rc, out = run(SCRIPTS["16"], ps, pe)
        if rc != 0:
            print("[FAIL] .16")
            print(out[-2000:])
            return 1
        pe_payload = load(pe)
        if pe_payload.get("overall_execution_decision") != "PAPER_ORDER_INTENT":
            print("[FAIL] .16 did not create paper intent")
            return 1

        ledger = tmp / "ledger.json"
        rc, out = run(SCRIPTS["17"], pe, ledger)
        if rc != 0:
            print("[FAIL] .17")
            print(out[-2000:])
            return 1
        ledger_payload = load(ledger)
        if ledger_payload.get("overall_ledger_decision") != "INTENTS_RECORDED":
            print("[FAIL] .17 did not record paper intents")
            return 1

        config = tmp / "fill_config.json"
        write(
            config,
            {
                "simulation_timestamp": "2026-08-30T10:00:00Z",
                "fill_policy": "FILLED",
                "fill_prices": {"BTC/USD": 100000.0, "ETH/USD": 4000.0},
            },
        )

        observed = tmp / "observed_execution.json"
        rc, out = run(SCRIPTS["20"], ledger, observed, ["--config", str(config)])
        if rc != 0:
            print("[FAIL] .20")
            print(out[-2000:])
            return 1
        observed_payload = load(observed)
        for symbol in SYMBOLS:
            item = observed_payload["observations"][symbol]
            if item["order_status"] != "FILLED" or item["position_exists"] is not True:
                print(f"[FAIL] .20 did not simulate fill for {symbol}")
                return 1

        reconciliation = tmp / "reconciliation.json"
        rc, out = run(SCRIPTS["18"], ledger, reconciliation, ["--observed-input", str(observed)])
        if rc != 0:
            print("[FAIL] .18")
            print(out[-2000:])
            return 1
        reconciliation_payload = load(reconciliation)
        if reconciliation_payload.get("overall_reconciliation") != "RECONCILED":
            print("[FAIL] .18 did not reconcile simulated fills")
            return 1
        for symbol in SYMBOLS:
            item = reconciliation_payload["decisions"][symbol]
            if item["reconciliation_state"] != "RECONCILED_EXECUTION":
                print(f"[FAIL] .18 state for {symbol}: {item['reconciliation_state']}")
                return 1

        safety = tmp / "execution_safety.json"
        rc, out = run(SCRIPTS["19"], reconciliation, safety)
        if rc != 0:
            print("[FAIL] .19")
            print(out[-2000:])
            return 1
        safety_payload = load(safety)
        if safety_payload.get("execution_authorized") is not False:
            print("[FAIL] .19 unexpectedly authorized execution")
            return 1
        if safety_payload.get("live_execution_authorized") is not False:
            print("[FAIL] .19 unexpectedly authorized live execution")
            return 1

    print("[PASS] .15 -> .16 -> .17 -> .20 -> .18 -> .19")
    print("[PASS] Simulated fills reconcile correctly")
    print("[PASS] Execution safety remains non-authorizing")
    print("RESULT : PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

