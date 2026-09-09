"""
Contract tests for AURA v0.5.3.15 Position State Manager.

Covers the required-vs-optional symbol handling added on top of the
original locked BTC/ETH-only architecture: REQUIRED_SYMBOLS (BTC/ETH)
remain the only symbols whose absence or invalid/inconsistent risk
decision can block the whole position-state decision; every other symbol
v0.5.3.14 reports (including one v0.5.3.14 itself individually blocked)
is evaluated independently.

Fixtures build v0.5.3.14-shaped upstream payloads directly (structurally
faithful to the real module's output schema), matching the convention
used by the other contract test files in this repo.
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


posstate = _load("aura_v05315_position_state.py", "aura_v05315_posstate")

REQUIRED_SYMBOLS = posstate.REQUIRED_SYMBOLS


def risk_pass() -> dict:
    return {"risk_decision": "RISK_PASS", "risk_authorized": True}


def no_signal() -> dict:
    return {"risk_decision": "NO_SIGNAL", "risk_authorized": False}


def blocked() -> dict:
    return {"risk_decision": "BLOCKED", "risk_authorized": False}


def make_risk_gate_payload(symbol_specs: dict) -> dict:
    decisions = {}
    for symbol, spec in symbol_specs.items():
        if spec is None:
            continue
        decisions[symbol] = {
            "symbol": symbol,
            "upstream_decision": "SIGNAL_CANDIDATE" if spec["risk_decision"] == "RISK_PASS" else "NO_SIGNAL",
            "risk_decision": spec["risk_decision"],
            "risk_authorized": spec["risk_authorized"],
            "reason": "TEST_FIXTURE",
        }

    return {
        "agent_version": "AURA v0.5.3.14",
        "engine": "RISK_GATE",
        "decision_status": "DECIDED",
        "overall_risk_decision": "RISK_PASS",
        "risk_authorized": True,
        "execution_permitted": False,
        "generated_from": "test-fixture",
        "input_decision_hash": "testfixturehash",
        "input_decision_hash_verified": True,
        "upstream_state_id": "MS-testfixture0000000000",
        "upstream_state_hash_verified": True,
        "frozen_configuration_verified": True,
        "risk_checks": {},
        "decisions": decisions,
        "blocked_reasons": [],
        "guardrails": {
            "single_source_of_truth": True,
            "source_engine": "AURA v0.5.3.13",
            "indicator_recalculation": False,
            "market_data_fetch": False,
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


def run(symbol_specs: dict) -> dict:
    payload = make_risk_gate_payload(symbol_specs)
    return posstate.build_position_state(payload, Path("test-fixture-input.json"))


# ---------------------------------------------------------------------------
# Required-pair (BTC/ETH) baseline behavior -- unchanged from the original
# architecture.
# ---------------------------------------------------------------------------


def test_risk_pass_on_both_required_symbols_is_entry_candidate():
    result = run({"BTC/USD": risk_pass(), "ETH/USD": risk_pass()})

    assert result["decision_status"] == "DECIDED"
    assert result["overall_position_state"] == "ENTRY_CANDIDATE"
    for symbol in REQUIRED_SYMBOLS:
        assert result["decisions"][symbol]["position_state"] == "ENTRY_CANDIDATE"
        assert result["decisions"][symbol]["position_exists"] is False


def test_no_signal_on_both_required_symbols_is_flat():
    result = run({"BTC/USD": no_signal(), "ETH/USD": no_signal()})

    assert result["overall_position_state"] == "FLAT"
    for symbol in REQUIRED_SYMBOLS:
        assert result["decisions"][symbol]["position_state"] == "FLAT"


def test_missing_required_symbol_blocks_whole_decision():
    result = run({"BTC/USD": risk_pass(), "ETH/USD": None})

    assert result["overall_position_state"] == "BLOCKED"
    assert any("MISSING_OR_INVALID_SYMBOL:ETH/USD" in r for r in result["blocked_reasons"])


def test_required_symbol_invalid_risk_decision_blocks_whole_decision():
    result = run({"BTC/USD": blocked(), "ETH/USD": no_signal()})

    assert result["overall_position_state"] == "BLOCKED"
    assert any("INVALID_RISK_DECISION:BTC/USD" in r for r in result["blocked_reasons"])


# ---------------------------------------------------------------------------
# Optional (all-coin-universe) symbol handling
# ---------------------------------------------------------------------------


def test_optional_symbol_risk_pass_becomes_entry_candidate_independently():
    result = run({"BTC/USD": no_signal(), "ETH/USD": no_signal(), "AVAX/USD": risk_pass()})

    assert result["decision_status"] == "DECIDED"
    assert result["overall_position_state"] == "ENTRY_CANDIDATE"
    assert result["decisions"]["AVAX/USD"]["position_state"] == "ENTRY_CANDIDATE"
    assert "optional_symbols_blocked" not in result


def test_optional_symbol_blocked_upstream_does_not_block_required_pair():
    result = run({"BTC/USD": risk_pass(), "ETH/USD": no_signal(), "AVAX/USD": blocked()})

    assert result["decision_status"] == "DECIDED"
    assert result["overall_position_state"] == "ENTRY_CANDIDATE"
    assert result["decisions"]["AVAX/USD"]["position_state"] == "BLOCKED"
    assert result["decisions"]["AVAX/USD"]["reason"] == "UPSTREAM_RISK_BLOCKED"
    assert result.get("optional_symbols_blocked") == ["AVAX/USD"]
    # Required pair unaffected.
    assert result["decisions"]["BTC/USD"]["position_state"] == "ENTRY_CANDIDATE"


def test_full_coin_universe_optional_symbols_evaluated_independently():
    result = run({
        "BTC/USD": no_signal(),
        "ETH/USD": no_signal(),
        "AVAX/USD": risk_pass(),
        "SOL/USD": no_signal(),
        "DOGE/USD": blocked(),
    })

    assert result["overall_position_state"] == "ENTRY_CANDIDATE"
    assert result["decisions"]["AVAX/USD"]["position_state"] == "ENTRY_CANDIDATE"
    assert result["decisions"]["SOL/USD"]["position_state"] == "FLAT"
    assert result["decisions"]["DOGE/USD"]["position_state"] == "BLOCKED"
    assert result.get("optional_symbols_blocked") == ["DOGE/USD"]


def test_only_optional_blocked_symbols_and_required_flat_is_overall_flat():
    # No ENTRY_CANDIDATE anywhere -- required pair FLAT, one optional symbol
    # individually blocked. Overall must be FLAT, not BLOCKED: the optional
    # symbol's bad data must never flip the aggregate to BLOCKED.
    result = run({"BTC/USD": no_signal(), "ETH/USD": no_signal(), "AVAX/USD": blocked()})

    assert result["overall_position_state"] == "FLAT"
    assert result.get("optional_symbols_blocked") == ["AVAX/USD"]


# ---------------------------------------------------------------------------
# print_report() smoke test -- must not reference the old fixed SYMBOLS tuple
# ---------------------------------------------------------------------------


def test_print_report_covers_all_reported_symbols_including_optional(capsys):
    result = run({"BTC/USD": risk_pass(), "ETH/USD": no_signal(), "AVAX/USD": blocked()})

    posstate.print_report(result, Path("test-output.json"))
    out = capsys.readouterr().out

    assert "BTC/USD  [REQUIRED]" in out
    assert "ETH/USD  [REQUIRED]" in out
    assert "AVAX/USD  [OPTIONAL, BLOCKED" in out
