"""
Tests for aura_v05326_paper_order_submission.py (Phase 5, entries only:
glue between v0.5.3.23's multi-symbol execution_specification.json and
v0.5.3.22's one-symbol-per-call adapter).

submit_one() invokes the REAL v0.5.3.22 script as a subprocess in its
default validate-only mode (no --submit-paper), which needs no Alpaca
credentials and never touches the network -- so these are genuine
integration tests, not mocks, without requiring live credentials.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "aura_v05326_paper_order_submission.py"
ADAPTER_SCRIPT = ROOT / "aura_v05322_alpaca_paper_execution_adapter.py"

spec = importlib.util.spec_from_file_location("aura_v05326_paper_order_submission", SCRIPT_PATH)
pos = importlib.util.module_from_spec(spec)
sys.modules["aura_v05326_paper_order_submission"] = pos
spec.loader.exec_module(pos)  # type: ignore[union-attr]


def valid_execution_spec(symbol: str = "BTC/USD", client_order_id: str = "test-coid-1") -> dict:
    return {
        "execution_spec_version": "1.0",
        "exchange": "ALPACA",
        "account_mode": "PAPER",
        "live_execution": False,
        "kill_switch": False,
        "execution_authorized": True,
        "paper_execution_authorized": True,
        "symbol": symbol,
        "side": "BUY",
        "quantity": "0.01",
        "order_type": "MARKET",
        "time_in_force": "GTC",
        "client_order_id": client_order_id,
    }


def ready_decision(symbol: str) -> dict:
    return {
        "symbol": symbol,
        "status": "EXECUTION_SPEC_READY",
        "reason": "VALIDATED_DIRECTION_AND_QUANTITY_PRESENT",
        "regime_state": "BEAR x LOW ATR x POSITIVE bar-2",
        "execution_specification": valid_execution_spec(symbol, client_order_id=f"coid-{symbol.replace('/', '-')}"),
    }


def blocked_decision(symbol: str, reason: str = "NO_VALIDATED_EXECUTION_DIRECTION") -> dict:
    return {"symbol": symbol, "status": "BLOCKED", "reason": reason}


def exec_spec_payload(decisions: dict) -> dict:
    return {
        "agent_version": pos.EXPECTED_EXEC_SPEC_VERSION,
        "engine": pos.EXPECTED_EXEC_SPEC_ENGINE,
        "decision_status": "DECIDED",
        "decisions": decisions,
        "blocked_reasons": [],
    }


# --------------------------------------------------------------------------
# ready_specs
# --------------------------------------------------------------------------

def test_ready_specs_includes_only_execution_spec_ready():
    payload = exec_spec_payload({
        "BTC/USD": ready_decision("BTC/USD"),
        "ETH/USD": blocked_decision("ETH/USD"),
    })
    specs = pos.ready_specs(payload)
    assert set(specs.keys()) == {"BTC/USD"}
    assert specs["BTC/USD"]["symbol"] == "BTC/USD"


def test_ready_specs_excludes_malformed_execution_specification():
    bad = ready_decision("BTC/USD")
    bad["execution_specification"] = "not-a-dict"
    payload = exec_spec_payload({"BTC/USD": bad})
    assert pos.ready_specs(payload) == {}


def test_ready_specs_empty_when_no_decisions():
    assert pos.ready_specs({}) == {}
    assert pos.ready_specs({"decisions": "not-a-dict"}) == {}


# --------------------------------------------------------------------------
# submit_one -- real subprocess call to the actual v0.5.3.22 adapter,
# always in validate-only mode (no credentials needed/used).
# --------------------------------------------------------------------------

def test_submit_one_validates_without_submitting(tmp_path: Path):
    outcome = pos.submit_one(ADAPTER_SCRIPT, valid_execution_spec(), tmp_path, "BTC/USD", submit_paper=False)
    assert outcome["status"] == "VALIDATED_NO_SUBMISSION"
    assert outcome["symbol"] == "BTC/USD"
    assert outcome["side"] == "BUY"


def test_submit_one_reports_adapter_failure_for_bad_script_path(tmp_path: Path):
    outcome = pos.submit_one(tmp_path / "does_not_exist.py", valid_execution_spec(), tmp_path, "BTC/USD", submit_paper=False)
    assert outcome["status"] == "ADAPTER_FAILED"
    assert outcome["returncode"] != 0


def test_submit_one_writes_spec_and_result_files_under_symbol_dir(tmp_path: Path):
    pos.submit_one(ADAPTER_SCRIPT, valid_execution_spec(), tmp_path, "BTC/USD", submit_paper=False)
    symbol_dir = tmp_path / "BTC_USD"
    assert (symbol_dir / "spec.json").exists()
    assert (symbol_dir / "result.json").exists()


# --------------------------------------------------------------------------
# build_and_submit -- end-to-end against the real adapter, validate-only.
# --------------------------------------------------------------------------

def test_build_and_submit_processes_only_ready_symbols(tmp_path: Path):
    payload = exec_spec_payload({
        "BTC/USD": ready_decision("BTC/USD"),
        "ETH/USD": blocked_decision("ETH/USD"),
    })
    result = pos.build_and_submit(
        payload, Path("exec_spec.json"), ADAPTER_SCRIPT, tmp_path, submit_paper=False,
    )
    assert result["decision_status"] == "DECIDED"
    assert result["ready_symbol_count"] == 1
    assert set(result["results"].keys()) == {"BTC/USD"}
    assert result["results"]["BTC/USD"]["status"] == "VALIDATED_NO_SUBMISSION"
    assert result["validated_only_count"] == 1
    assert result["submitted_count"] == 0


def test_build_and_submit_no_ready_symbols_is_decided_not_blocked(tmp_path: Path):
    payload = exec_spec_payload({"BTC/USD": blocked_decision("BTC/USD")})
    result = pos.build_and_submit(
        payload, Path("exec_spec.json"), ADAPTER_SCRIPT, tmp_path, submit_paper=False,
    )
    assert result["decision_status"] == "DECIDED"
    assert result["ready_symbol_count"] == 0
    assert result["results"] == {}


def test_build_and_submit_guardrails_reflect_submit_paper_flag(tmp_path: Path):
    payload = exec_spec_payload({})
    result_off = pos.build_and_submit(payload, Path("x.json"), ADAPTER_SCRIPT, tmp_path, submit_paper=False)
    result_on = pos.build_and_submit(payload, Path("x.json"), ADAPTER_SCRIPT, tmp_path, submit_paper=True)
    assert result_off["guardrails"]["submit_paper_forwarded"] is False
    assert result_on["guardrails"]["submit_paper_forwarded"] is True
    assert result_off["guardrails"]["exit_orders_submitted"] is False
    assert result_on["guardrails"]["exit_orders_submitted"] is False


# --------------------------------------------------------------------------
# CLI smoke test -- always validate-only (no --submit-paper), so no
# credentials are needed.
# --------------------------------------------------------------------------

def test_cli_runs_end_to_end_validate_only(tmp_path: Path):
    payload = exec_spec_payload({"BTC/USD": ready_decision("BTC/USD")})
    spec_path = tmp_path / "execution_specification.json"
    spec_path.write_text(json.dumps(payload), encoding="utf-8")

    output_path = tmp_path / "out.json"
    work_dir = tmp_path / "work"

    proc = subprocess.run(
        [
            sys.executable, str(SCRIPT_PATH),
            "--execution-spec-input", str(spec_path),
            "--output", str(output_path),
            "--work-dir", str(work_dir),
        ],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    result = json.loads(output_path.read_text(encoding="utf-8"))
    assert result["decision_status"] == "DECIDED"
    assert result["submit_paper_requested"] is False
    assert result["results"]["BTC/USD"]["status"] == "VALIDATED_NO_SUBMISSION"


def test_cli_fails_closed_on_missing_execution_spec(tmp_path: Path):
    output_path = tmp_path / "out.json"
    proc = subprocess.run(
        [
            sys.executable, str(SCRIPT_PATH),
            "--execution-spec-input", str(tmp_path / "does_not_exist.json"),
            "--output", str(output_path),
            "--work-dir", str(tmp_path / "work"),
        ],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert proc.returncode == 1
    result = json.loads(output_path.read_text(encoding="utf-8"))
    assert result["decision_status"] == "BLOCKED"
    assert result["blocked_reasons"]
