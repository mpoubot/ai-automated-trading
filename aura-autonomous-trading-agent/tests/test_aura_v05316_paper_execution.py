"""
Contract tests for AURA v0.5.3.16 Paper Execution.

Covers the required-vs-optional symbol handling added on top of the
original locked BTC/ETH-only architecture: REQUIRED_SYMBOLS (BTC/ETH)
remain the only symbols whose absence or invalid position state can block
the whole paper-execution decision; every other symbol v0.5.3.15 reports
(including one v0.5.3.15 itself individually blocked) is evaluated
independently.
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


paperexec = _load("aura_v05316_paper_execution.py", "aura_v05316_paperexec")

REQUIRED_SYMBOLS = paperexec.REQUIRED_SYMBOLS


def entry_candidate() -> dict:
    return {"position_state": "ENTRY_CANDIDATE", "position_exists": False}


def flat() -> dict:
    return {"position_state": "FLAT", "position_exists": False}


def blocked() -> dict:
    return {"position_state": "BLOCKED", "position_exists": False}


def make_position_state_payload(symbol_specs: dict) -> dict:
    decisions = {}
    for symbol, spec in symbol_specs.items():
        if spec is None:
            continue
        decisions[symbol] = {
            "symbol": symbol,
            "upstream_risk_decision": "RISK_PASS" if spec["position_state"] == "ENTRY_CANDIDATE" else "NO_SIGNAL",
            "risk_authorized": spec["position_state"] == "ENTRY_CANDIDATE",
            "position_state": spec["position_state"],
            "position_exists": spec["position_exists"],
            "position_id": None,
            "reason": "TEST_FIXTURE",
        }

    payload = {
        "agent_version": "AURA v0.5.3.15",
        "engine": "POSITION_STATE",
        "decision_status": "DECIDED",
        "overall_position_state": "ENTRY_CANDIDATE",
        "generated_from": "test-fixture",
        "input_risk_gate_hash": "testfixturehash",
        "upstream_state_id": "MS-testfixture0000000000",
        "upstream_hash_verified": True,
        "frozen_configuration_verified": True,
        "position_truth_source": "NONE_IN_RESEARCH_BUILD",
        "positions_observed": False,
        "decisions": decisions,
        "blocked_reasons": [],
        "guardrails": {
            "single_source_of_truth": True,
            "source_engine": "AURA v0.5.3.14",
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
    payload["state_hash"] = paperexec.sha256_text(paperexec.stable_json(canonical))
    payload["state_id"] = f"PS-{payload['state_hash'][:24]}"
    return payload


def run(symbol_specs: dict) -> dict:
    payload = make_position_state_payload(symbol_specs)
    return paperexec.build_paper_execution(payload, Path("test-fixture-input.json"))


# ---------------------------------------------------------------------------
# Required-pair (BTC/ETH) baseline behavior -- unchanged from the original
# architecture.
# ---------------------------------------------------------------------------


def test_entry_candidate_on_both_required_symbols_produces_paper_order_intent():
    result = run({"BTC/USD": entry_candidate(), "ETH/USD": entry_candidate()})

    assert result["decision_status"] == "DECIDED"
    assert result["overall_execution_decision"] == "PAPER_ORDER_INTENT"
    assert result["paper_execution_allowed"] is True
    assert result["live_execution_permitted"] is False
    for symbol in REQUIRED_SYMBOLS:
        item = result["decisions"][symbol]
        assert item["execution_decision"] == "PAPER_ORDER_INTENT"
        assert item["position_exists"] is False


def test_flat_on_both_required_symbols_is_no_order():
    result = run({"BTC/USD": flat(), "ETH/USD": flat()})

    assert result["overall_execution_decision"] == "NO_ORDER"
    assert result["paper_execution_allowed"] is False


def test_missing_required_symbol_blocks_whole_decision():
    result = run({"BTC/USD": entry_candidate(), "ETH/USD": None})

    assert result["overall_execution_decision"] == "BLOCKED"
    assert any("MISSING_OR_INVALID_SYMBOL:ETH/USD" in r for r in result["blocked_reasons"])


def test_required_symbol_invalid_position_state_blocks_whole_decision():
    result = run({"BTC/USD": blocked(), "ETH/USD": flat()})

    assert result["overall_execution_decision"] == "BLOCKED"
    assert any("INVALID_POSITION_STATE:BTC/USD" in r for r in result["blocked_reasons"])


def test_required_symbol_position_exists_true_blocks_whole_decision():
    bad = entry_candidate()
    bad["position_exists"] = True  # v0.5.3.15 must never assert an open position
    result = run({"BTC/USD": bad, "ETH/USD": flat()})

    assert result["overall_execution_decision"] == "BLOCKED"
    assert any("POSITION_EXISTENCE_NOT_FALSE:BTC/USD" in r for r in result["blocked_reasons"])


# ---------------------------------------------------------------------------
# Optional (all-coin-universe) symbol handling
# ---------------------------------------------------------------------------


def test_optional_symbol_entry_candidate_produces_intent_independently():
    result = run({"BTC/USD": flat(), "ETH/USD": flat(), "AVAX/USD": entry_candidate()})

    assert result["decision_status"] == "DECIDED"
    assert result["overall_execution_decision"] == "PAPER_ORDER_INTENT"
    assert result["decisions"]["AVAX/USD"]["execution_decision"] == "PAPER_ORDER_INTENT"
    assert "optional_symbols_blocked" not in result


def test_optional_symbol_blocked_upstream_does_not_block_required_pair():
    result = run({"BTC/USD": entry_candidate(), "ETH/USD": flat(), "AVAX/USD": blocked()})

    assert result["decision_status"] == "DECIDED"
    assert result["overall_execution_decision"] == "PAPER_ORDER_INTENT"
    assert result["decisions"]["AVAX/USD"]["execution_decision"] == "BLOCKED"
    assert result.get("optional_symbols_blocked") == ["AVAX/USD"]
    assert result["decisions"]["BTC/USD"]["execution_decision"] == "PAPER_ORDER_INTENT"


def test_optional_symbol_position_exists_true_blocks_only_that_symbol():
    bad_avax = entry_candidate()
    bad_avax["position_exists"] = True
    result = run({"BTC/USD": entry_candidate(), "ETH/USD": flat(), "AVAX/USD": bad_avax})

    assert result["decision_status"] == "DECIDED"
    assert result["decisions"]["AVAX/USD"]["execution_decision"] == "BLOCKED"
    assert result.get("optional_symbols_blocked") == ["AVAX/USD"]
    assert result["decisions"]["BTC/USD"]["execution_decision"] == "PAPER_ORDER_INTENT"


def test_full_coin_universe_optional_symbols_evaluated_independently():
    result = run({
        "BTC/USD": flat(),
        "ETH/USD": flat(),
        "AVAX/USD": entry_candidate(),
        "SOL/USD": flat(),
        "DOGE/USD": blocked(),
    })

    assert result["overall_execution_decision"] == "PAPER_ORDER_INTENT"
    assert result["decisions"]["AVAX/USD"]["execution_decision"] == "PAPER_ORDER_INTENT"
    assert result["decisions"]["SOL/USD"]["execution_decision"] == "NO_ORDER"
    assert result["decisions"]["DOGE/USD"]["execution_decision"] == "BLOCKED"
    assert result.get("optional_symbols_blocked") == ["DOGE/USD"]
    assert result["live_execution_permitted"] is False


# ---------------------------------------------------------------------------
# print_report() smoke test -- must not reference the old fixed SYMBOLS tuple
# ---------------------------------------------------------------------------


def test_print_report_covers_all_reported_symbols_including_optional(capsys):
    result = run({"BTC/USD": entry_candidate(), "ETH/USD": flat(), "AVAX/USD": blocked()})

    paperexec.print_report(result, Path("test-output.json"))
    out = capsys.readouterr().out

    assert "BTC/USD  [REQUIRED]" in out
    assert "ETH/USD  [REQUIRED]" in out
    assert "AVAX/USD  [OPTIONAL, BLOCKED" in out
