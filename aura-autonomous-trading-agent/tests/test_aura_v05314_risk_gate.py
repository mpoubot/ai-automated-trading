"""
Contract tests for AURA v0.5.3.14 Risk Gate.

Covers the required-vs-optional symbol handling added on top of the
original locked BTC/ETH-only architecture: REQUIRED_SYMBOLS (BTC/ETH)
remain the only symbols whose absence, desynchronized timestamp, or
candidate-consistency failure can block the whole risk-gate decision;
every other symbol v0.5.3.13 reports is evaluated independently.

Fixtures build v0.5.3.13-shaped upstream payloads directly (structurally
faithful to the real module's output schema) rather than running the full
v0.5.3.12/.13 chain, matching the convention used by the v0.5.3.23 tests.
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


gate = _load("aura_v05314_risk_gate.py", "aura_v05314_gate")

FROZEN_CANDIDATE = gate.FROZEN_CANDIDATE
REQUIRED_SYMBOLS = gate.REQUIRED_SYMBOLS
MIRROR_CANDIDATE = "BULL_OR_NEUTRAL x LOW x NON_POSITIVE"


def signal_candidate(timestamp: str = "2026-09-01T00:00:00Z", regime_state: str = FROZEN_CANDIDATE) -> dict:
    return {
        "decision": "SIGNAL_CANDIDATE",
        "candidate_match": True,
        "reason": "FROZEN_CANDIDATE_MATCH",
        "regime_state": regime_state,
        "state_timestamp": timestamp,
    }


def no_signal(timestamp: str = "2026-09-01T00:00:00Z") -> dict:
    return {
        "decision": "NO_SIGNAL",
        "candidate_match": False,
        "reason": "FROZEN_CANDIDATE_NOT_PRESENT",
        "regime_state": "BULL x HIGH ATR x POSITIVE bar-2",
        "state_timestamp": timestamp,
    }


def blocked_symbol(timestamp: str = "2026-09-01T00:00:00Z") -> dict:
    return {
        "decision": "BLOCKED",
        "candidate_match": False,
        "reason": "SYMBOL_DATA_INVALID",
        "regime_state": None,
        "state_timestamp": timestamp,
    }


def make_signal_decision(symbol_specs: dict) -> dict:
    decisions = {}
    for symbol, spec in symbol_specs.items():
        if spec is None:
            continue
        decisions[symbol] = {
            "symbol": symbol,
            "decision": spec["decision"],
            "market_state_valid": True,
            "candidate_match": spec["candidate_match"],
            "state_timestamp": spec["state_timestamp"],
            "state_id": None,
            "regime_state": spec["regime_state"],
            "reason": spec["reason"],
        }

    return {
        "agent_version": "AURA v0.5.3.13",
        "engine": "SIGNAL_DECISION_ENGINE",
        "decision_status": "DECIDED",
        "overall_decision": "SIGNAL_CANDIDATE",
        "generated_from": "test-fixture",
        "input_state_id": "MS-testfixture0000000000",
        "input_state_hash": "testfixturehash",
        "input_state_hash_verified": True,
        "frozen_configuration_verified": True,
        "invalid_reasons": [],
        "decisions": decisions,
        "guardrails": {
            "single_source_of_truth": True,
            "source_engine": "AURA v0.5.3.12",
            "indicator_recalculation": False,
            "market_data_fetch": False,
            "lookahead_allowed": False,
            "orders_allowed": False,
            "paper_execution": False,
            "live_execution": False,
            "strategy_changed": False,
            "parameters_changed": False,
            "fail_closed": True,
        },
    }


def run(symbol_specs: dict) -> dict:
    decision = make_signal_decision(symbol_specs)
    return gate.evaluate_risk_gate(decision, Path("test-fixture-input.json"))


# ---------------------------------------------------------------------------
# Required-pair (BTC/ETH) baseline behavior -- unchanged from the original
# single-candidate, BTC/ETH-only architecture.
# ---------------------------------------------------------------------------


def test_signal_candidate_on_both_required_symbols_passes_risk_gate():
    result = run({"BTC/USD": signal_candidate(), "ETH/USD": signal_candidate()})

    assert result["decision_status"] == "DECIDED"
    assert result["overall_risk_decision"] == "RISK_PASS"
    assert result["risk_authorized"] is True
    # Execution must remain hard-disabled regardless of risk authorization.
    assert result["execution_permitted"] is False
    for symbol in REQUIRED_SYMBOLS:
        assert result["decisions"][symbol]["risk_decision"] == "RISK_PASS"


def test_no_signal_on_both_required_symbols_is_no_signal():
    result = run({"BTC/USD": no_signal(), "ETH/USD": no_signal()})

    assert result["overall_risk_decision"] == "NO_SIGNAL"
    assert result["risk_authorized"] is False


def test_missing_required_symbol_blocks_whole_decision():
    result = run({"BTC/USD": signal_candidate(), "ETH/USD": None})

    assert result["decision_status"] == "BLOCKED"
    assert result["overall_risk_decision"] == "BLOCKED"
    assert "MISSING_REQUIRED_SYMBOL:ETH/USD" in result["blocked_reasons"]


def test_required_symbol_timestamp_desync_blocks_whole_decision():
    result = run({
        "BTC/USD": signal_candidate(timestamp="2026-09-01T00:00:00Z"),
        "ETH/USD": signal_candidate(timestamp="2026-09-01T01:00:00Z"),
    })

    assert result["decision_status"] == "BLOCKED"
    assert "SYMBOL_STATE_TIMESTAMPS_NOT_SYNCHRONIZED" in result["blocked_reasons"]


def test_required_symbol_candidate_match_inconsistent_blocks_whole_decision():
    bad = signal_candidate()
    bad["candidate_match"] = False  # decision says SIGNAL_CANDIDATE, flag disagrees
    result = run({"BTC/USD": bad, "ETH/USD": no_signal()})

    assert result["decision_status"] == "BLOCKED"
    assert "CANDIDATE_MATCH_FLAG_INCONSISTENT:BTC/USD" in result["blocked_reasons"]


def test_required_symbol_unexpected_upstream_decision_blocks_whole_decision():
    result = run({"BTC/USD": blocked_symbol(), "ETH/USD": no_signal()})

    assert result["decision_status"] == "BLOCKED"
    assert result["overall_risk_decision"] == "BLOCKED"
    assert result["blocked_reasons"] == ["UNEXPECTED_UPSTREAM_DECISION"]
    assert result["decisions"]["BTC/USD"]["risk_decision"] == "BLOCKED"


def test_mirror_candidate_regime_state_still_passes_required_consistency_check():
    # .13 normalizes both FROZEN_CANDIDATE and MIRROR_CANDIDATE to the same
    # decision/candidate_match/reason contract -- .14 must not need to know
    # which candidate matched.
    result = run({
        "BTC/USD": signal_candidate(regime_state=MIRROR_CANDIDATE),
        "ETH/USD": no_signal(),
    })

    assert result["decision_status"] == "DECIDED"
    assert result["overall_risk_decision"] == "RISK_PASS"
    assert result["decisions"]["BTC/USD"]["risk_decision"] == "RISK_PASS"


# ---------------------------------------------------------------------------
# Optional (all-coin-universe) symbol handling
# ---------------------------------------------------------------------------


def test_optional_symbol_stale_timestamp_does_not_block_required_pair():
    result = run({
        "BTC/USD": signal_candidate(timestamp="2026-09-01T00:00:00Z"),
        "ETH/USD": signal_candidate(timestamp="2026-09-01T00:00:00Z"),
        "AVAX/USD": no_signal(timestamp="2026-08-25T00:00:00Z"),  # days stale
    })

    assert result["decision_status"] == "DECIDED"
    assert result["overall_risk_decision"] == "RISK_PASS"
    assert result["decisions"]["AVAX/USD"]["risk_decision"] == "NO_SIGNAL"
    assert "optional_symbols_blocked" not in result


def test_optional_symbol_candidate_inconsistency_blocks_only_that_symbol():
    bad_avax = signal_candidate()
    bad_avax["candidate_match"] = False  # inconsistent, same as the required-symbol case
    result = run({
        "BTC/USD": signal_candidate(),
        "ETH/USD": signal_candidate(),
        "AVAX/USD": bad_avax,
    })

    assert result["decision_status"] == "DECIDED"
    assert result["overall_risk_decision"] == "RISK_PASS"
    assert result["decisions"]["AVAX/USD"]["risk_decision"] == "BLOCKED"
    assert result["decisions"]["AVAX/USD"]["reason"] == "CANDIDATE_CONSISTENCY_CHECK_FAILED"
    assert result.get("optional_symbols_blocked") == ["AVAX/USD"]
    # The required pair's own authorization is untouched.
    assert result["decisions"]["BTC/USD"]["risk_decision"] == "RISK_PASS"


def test_full_coin_universe_optional_symbols_evaluated_independently():
    result = run({
        "BTC/USD": signal_candidate(),
        "ETH/USD": no_signal(),
        "AVAX/USD": signal_candidate(regime_state=MIRROR_CANDIDATE, timestamp="2026-08-20T00:00:00Z"),
        "SOL/USD": no_signal(timestamp="2026-08-15T00:00:00Z"),
    })

    assert result["overall_risk_decision"] == "RISK_PASS"
    assert result["decisions"]["AVAX/USD"]["risk_decision"] == "RISK_PASS"
    assert result["decisions"]["SOL/USD"]["risk_decision"] == "NO_SIGNAL"
    assert "optional_symbols_blocked" not in result
    assert result["execution_permitted"] is False


# ---------------------------------------------------------------------------
# print_report() smoke test -- must not reference the old fixed SYMBOLS tuple
# ---------------------------------------------------------------------------


def test_print_report_covers_all_reported_symbols_including_optional(capsys):
    bad_avax = signal_candidate()
    bad_avax["candidate_match"] = False
    result = run({
        "BTC/USD": signal_candidate(),
        "ETH/USD": signal_candidate(),
        "AVAX/USD": bad_avax,
    })

    gate.print_report(result, Path("test-output.json"))
    out = capsys.readouterr().out

    assert "BTC/USD  [REQUIRED]" in out
    assert "ETH/USD  [REQUIRED]" in out
    assert "AVAX/USD  [OPTIONAL, BLOCKED" in out
