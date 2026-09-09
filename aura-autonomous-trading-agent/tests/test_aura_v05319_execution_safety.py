"""
Contract tests for AURA v0.5.3.19 Execution Safety / Kill Switch.

Covers the required-vs-optional symbol handling added on top of the
original locked BTC/ETH-only architecture: REQUIRED_SYMBOLS (BTC/ETH)
remain the only symbols whose missing/invalid reconciliation state can
block the whole safety report; every other symbol v0.5.3.18 reports gets
its own verdict independently.

Critically, execution_authorized / paper_execution_authorized /
live_execution_authorized must stay hard-coded False in every case tested
here, for every symbol, regardless of kill_switch, config flags, or
reconciliation state -- this file is a research-only, non-authorizing
report layer, and the required-vs-optional split changes only which
symbol's bad data can block the report, never what gets authorized.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(module_filename: str, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, ROOT / module_filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


safety = _load("aura_v05319_execution_safety.py", "aura_v05319_safety")

REQUIRED_SYMBOLS = safety.REQUIRED_SYMBOLS


def make_reconciliation_payload(symbol_states: dict, *, observed_execution_verified: bool = True) -> dict:
    decisions = {}
    for symbol, state in symbol_states.items():
        if state is None:
            continue
        decisions[symbol] = {
            "symbol": symbol,
            "ledger_event": "TEST_FIXTURE",
            "observed_order_status": "TEST_FIXTURE",
            "reconciliation_state": state,
            "execution_reconciled": state in {"NO_ORDER_CONFIRMED", "RECONCILED_EXECUTION", "EXECUTION_NOT_FILLED"},
            "position_exists": False,
            "position_id": None,
            "fill_price": None,
            "fill_timestamp": None,
            "reason": "TEST_FIXTURE",
        }

    overall = "CONFLICT" if "RECONCILIATION_CONFLICT" in symbol_states.values() else "RECONCILED"

    payload = {
        "agent_version": "AURA v0.5.3.18",
        "engine": "RECONCILIATION",
        "decision_status": "DECIDED",
        "overall_reconciliation": overall,
        "generated_from_ledger": "test-fixture",
        "generated_from_observed_execution": "test-fixture",
        "input_ledger_hash": "testfixturehash",
        "input_observed_execution_hash": "testfixturehash",
        "upstream_state_id": "LE-testfixture0000000000",
        "upstream_hash_verified": True,
        "frozen_configuration_verified": True,
        "observed_execution_verified": observed_execution_verified,
        "decisions": decisions,
        "blocked_reasons": [],
        "guardrails": {
            "single_source_of_truth": True,
            "source_engine": "AURA v0.5.3.17",
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
            "reconciliation_only": True,
            "position_creation_from_observation": False,
            "fabricated_fill": False,
            "fabricated_position": False,
            "fail_closed": True,
        },
    }

    canonical = {
        "agent_version": payload["agent_version"],
        "engine": payload["engine"],
        "decision_status": payload["decision_status"],
        "overall_reconciliation": payload["overall_reconciliation"],
        "input_ledger_hash": payload["input_ledger_hash"],
        "input_observed_execution_hash": payload["input_observed_execution_hash"],
        "upstream_state_id": payload["upstream_state_id"],
        "upstream_hash_verified": payload["upstream_hash_verified"],
        "frozen_configuration_verified": payload["frozen_configuration_verified"],
        "observed_execution_verified": payload["observed_execution_verified"],
        "decisions": payload["decisions"],
        "blocked_reasons": payload["blocked_reasons"],
    }
    payload["state_hash"] = safety.sha256_text(safety.stable_json(canonical))
    payload["state_id"] = f"RC-{payload['state_hash'][:24]}"
    return payload


def run(symbol_states: dict, *, kill_switch: bool = True) -> dict:
    reconciliation = make_reconciliation_payload(symbol_states)
    config = dict(safety.DEFAULT_SAFETY_CONFIG)
    config["kill_switch"] = kill_switch
    return safety.build_safety(reconciliation, Path("test-fixture-input.json"), config)


def assert_never_authorized(result: dict) -> None:
    assert result["execution_authorized"] is False
    assert result["paper_execution_authorized"] is False
    assert result["live_execution_authorized"] is False
    for item in result["decisions"].values():
        assert item["execution_authorized"] is False


# ---------------------------------------------------------------------------
# Required-pair (BTC/ETH) baseline behavior -- unchanged from the original
# architecture. execution_authorized must stay False in every scenario.
# ---------------------------------------------------------------------------


def test_kill_switch_active_blocks_everything_including_clean_required_pair():
    result = run(
        {"BTC/USD": "NO_ORDER_CONFIRMED", "ETH/USD": "RECONCILED_EXECUTION"},
        kill_switch=True,
    )

    assert result["decision_status"] == "DECIDED"
    assert result["overall_execution_safety"] == "BLOCKED"
    assert "KILL_SWITCH_ACTIVE" in result["blocked_reasons"]
    assert_never_authorized(result)


def test_kill_switch_off_with_clean_required_pair_is_safe_no_order():
    result = run(
        {"BTC/USD": "NO_ORDER_CONFIRMED", "ETH/USD": "RECONCILED_EXECUTION"},
        kill_switch=False,
    )

    assert result["overall_execution_safety"] == "SAFE_NO_ORDER"
    assert_never_authorized(result)
    assert result["decisions"]["ETH/USD"]["execution_safety"] == "RECONCILED_NO_NEW_ORDER"


def test_missing_required_symbol_blocks_whole_decision():
    result = run({"BTC/USD": "NO_ORDER_CONFIRMED", "ETH/USD": None}, kill_switch=False)

    assert result["overall_execution_safety"] == "BLOCKED"
    assert any("MISSING_OR_INVALID_SYMBOL:ETH/USD" in r for r in result["blocked_reasons"])
    assert_never_authorized(result)


def test_required_symbol_invalid_reconciliation_state_blocks_whole_decision():
    result = run({"BTC/USD": "NOT_A_REAL_STATE", "ETH/USD": "NO_ORDER_CONFIRMED"}, kill_switch=False)

    assert result["overall_execution_safety"] == "BLOCKED"
    assert any("INVALID_RECONCILIATION_STATE:BTC/USD" in r for r in result["blocked_reasons"])
    assert_never_authorized(result)


# ---------------------------------------------------------------------------
# Optional (all-coin-universe) symbol handling
# ---------------------------------------------------------------------------


def test_optional_symbol_reported_independently_kill_switch_off():
    result = run(
        {"BTC/USD": "NO_ORDER_CONFIRMED", "ETH/USD": "NO_ORDER_CONFIRMED", "AVAX/USD": "RECONCILED_EXECUTION"},
        kill_switch=False,
    )

    assert result["decision_status"] == "DECIDED"
    assert result["overall_execution_safety"] == "SAFE_NO_ORDER"
    assert result["decisions"]["AVAX/USD"]["execution_safety"] == "RECONCILED_NO_NEW_ORDER"
    assert "optional_symbols_blocked" not in result
    assert_never_authorized(result)


def test_optional_symbol_conflict_does_not_block_required_pair_or_overall():
    result = run(
        {"BTC/USD": "NO_ORDER_CONFIRMED", "ETH/USD": "NO_ORDER_CONFIRMED", "AVAX/USD": "RECONCILIATION_CONFLICT"},
        kill_switch=False,
    )

    assert result["decision_status"] == "DECIDED"
    assert result["overall_execution_safety"] == "SAFE_NO_ORDER"
    assert result["decisions"]["AVAX/USD"]["execution_safety"] == "BLOCKED"
    assert result.get("optional_symbols_blocked") == ["AVAX/USD"]
    assert result["decisions"]["BTC/USD"]["execution_safety"] == "NO_ORDER"
    assert_never_authorized(result)


def test_optional_symbol_structurally_invalid_blocks_only_that_symbol():
    result = run(
        {"BTC/USD": "NO_ORDER_CONFIRMED", "ETH/USD": "NO_ORDER_CONFIRMED", "AVAX/USD": "GARBAGE_STATE"},
        kill_switch=False,
    )

    assert result["decision_status"] == "DECIDED"
    assert result["overall_execution_safety"] == "SAFE_NO_ORDER"
    assert result["decisions"]["AVAX/USD"]["execution_safety"] == "BLOCKED"
    assert result["decisions"]["AVAX/USD"]["reason"] == "UPSTREAM_VALIDATION_FAILED"
    assert result.get("optional_symbols_blocked") == ["AVAX/USD"]
    assert_never_authorized(result)


def test_kill_switch_active_still_blocks_optional_symbols_without_double_listing():
    result = run(
        {"BTC/USD": "NO_ORDER_CONFIRMED", "ETH/USD": "NO_ORDER_CONFIRMED", "AVAX/USD": "RECONCILED_EXECUTION"},
        kill_switch=True,
    )

    assert result["overall_execution_safety"] == "BLOCKED"
    assert result["decisions"]["AVAX/USD"]["execution_safety"] == "BLOCKED"
    assert result["decisions"]["AVAX/USD"]["reason"] == "KILL_SWITCH_ACTIVE"
    # Kill switch is already the single global blocked_reason; an optional
    # symbol being blocked purely by the kill switch is not separately
    # flagged as its own optional-symbol problem.
    assert "optional_symbols_blocked" not in result
    assert_never_authorized(result)


# ---------------------------------------------------------------------------
# print_report() smoke test -- must not reference the old fixed SYMBOLS tuple
# ---------------------------------------------------------------------------


def test_print_report_covers_all_reported_symbols_including_optional(capsys):
    result = run(
        {"BTC/USD": "NO_ORDER_CONFIRMED", "ETH/USD": "NO_ORDER_CONFIRMED", "AVAX/USD": "RECONCILIATION_CONFLICT"},
        kill_switch=False,
    )

    safety.print_report(result, Path("test-output.json"))
    out = capsys.readouterr().out

    assert "BTC/USD  [REQUIRED]" in out
    assert "ETH/USD  [REQUIRED]" in out
    assert "AVAX/USD  [OPTIONAL, BLOCKED" in out
