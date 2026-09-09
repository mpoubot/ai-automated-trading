"""
Contract tests for AURA v0.5.3.17 Decision / Execution Ledger.

Covers the required-vs-optional symbol handling added on top of the
original locked BTC/ETH-only architecture: REQUIRED_SYMBOLS (BTC/ETH)
remain the only symbols whose structural invalidity can block the whole
ledger decision; every other symbol v0.5.3.16 reports (including one
v0.5.3.16 itself individually blocked) gets its own ledger entry
independently. Also covers the intent-count cross-check now spanning the
full symbol set, since v0.5.3.16 itself counts any symbol toward
paper_execution_allowed / overall_execution_decision.
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


ledger = _load("aura_v05317_decision_execution_ledger.py", "aura_v05317_ledger")

REQUIRED_SYMBOLS = ledger.REQUIRED_SYMBOLS


def paper_order_intent() -> dict:
    return {
        "execution_decision": "PAPER_ORDER_INTENT",
        "paper_order_intent": True,
        "paper_order_status": "PENDING_EXECUTION",
        "position_exists": False,
        "position_id": None,
        "fill_price": None,
        "fill_timestamp": None,
    }


def no_order() -> dict:
    return {
        "execution_decision": "NO_ORDER",
        "paper_order_intent": False,
        "paper_order_status": "NOT_APPLICABLE",
        "position_exists": False,
        "position_id": None,
        "fill_price": None,
        "fill_timestamp": None,
    }


def blocked() -> dict:
    return {
        "execution_decision": "BLOCKED",
        "paper_order_intent": False,
        "paper_order_status": "BLOCKED",
        "position_exists": False,
        "position_id": None,
        "fill_price": None,
        "fill_timestamp": None,
    }


def make_paper_execution_payload(symbol_specs: dict) -> dict:
    decisions = {}
    for symbol, spec in symbol_specs.items():
        if spec is None:
            continue
        decisions[symbol] = {"symbol": symbol, **spec}

    has_intent = any(
        spec is not None and spec["paper_order_intent"] is True for spec in symbol_specs.values()
    )

    payload = {
        "agent_version": "AURA v0.5.3.16",
        "engine": "PAPER_EXECUTION",
        "decision_status": "DECIDED",
        "overall_execution_decision": "PAPER_ORDER_INTENT" if has_intent else "NO_ORDER",
        "paper_execution_allowed": has_intent,
        "live_execution_permitted": False,
        "generated_from": "test-fixture",
        "input_position_state_hash": "testfixturehash",
        "input_position_state_hash_verified": True,
        "upstream_state_id": "PS-testfixture0000000000",
        "upstream_hash_verified": True,
        "frozen_configuration_verified": True,
        "decisions": decisions,
        "blocked_reasons": [],
        "guardrails": {
            "single_source_of_truth": True,
            "source_engine": "AURA v0.5.3.15",
            "market_data_fetch": False,
            "indicator_recalculation": False,
            "signal_recalculation": False,
            "risk_recalculation": False,
            "position_sizing": False,
            "position_creation": False,
            "exchange_state_mutation": False,
            "orders_allowed": False,
            "live_execution": False,
            "fabricated_fill_price": False,
            "position_claim_from_intent": False,
            "fail_closed": True,
        },
    }

    canonical = {
        "agent_version": payload["agent_version"],
        "engine": payload["engine"],
        "decision_status": payload["decision_status"],
        "overall_execution_decision": payload["overall_execution_decision"],
        "paper_execution_allowed": payload["paper_execution_allowed"],
        "live_execution_permitted": payload["live_execution_permitted"],
        "upstream_state_id": payload["upstream_state_id"],
        "upstream_hash_verified": payload["upstream_hash_verified"],
        "frozen_configuration_verified": payload["frozen_configuration_verified"],
        "decisions": payload["decisions"],
        "blocked_reasons": payload["blocked_reasons"],
    }
    payload["state_hash"] = ledger.sha256_text(ledger.stable_json(canonical))
    payload["state_id"] = f"PE-{payload['state_hash'][:24]}"
    return payload


def run(symbol_specs: dict) -> dict:
    payload = make_paper_execution_payload(symbol_specs)
    return ledger.build_ledger(payload, Path("test-fixture-input.json"))


# ---------------------------------------------------------------------------
# Required-pair (BTC/ETH) baseline behavior -- unchanged from the original
# architecture.
# ---------------------------------------------------------------------------


def test_intent_on_both_required_symbols_is_recorded():
    result = run({"BTC/USD": paper_order_intent(), "ETH/USD": paper_order_intent()})

    assert result["decision_status"] == "DECIDED"
    assert result["overall_ledger_decision"] == "INTENTS_RECORDED"
    for symbol in REQUIRED_SYMBOLS:
        entry = result["ledger_entries"][symbol]
        assert entry["ledger_event"] == "PAPER_ORDER_INTENT_RECORDED"
        assert entry["position_exists"] is False


def test_no_order_on_both_required_symbols_is_no_order_recorded():
    result = run({"BTC/USD": no_order(), "ETH/USD": no_order()})

    assert result["overall_ledger_decision"] == "NO_ORDER_RECORDED"


def test_missing_required_symbol_blocks_whole_decision():
    result = run({"BTC/USD": paper_order_intent(), "ETH/USD": None})

    assert result["overall_ledger_decision"] == "BLOCKED"
    assert any("MISSING_OR_INVALID_SYMBOL:ETH/USD" in r for r in result["blocked_reasons"])


def test_required_symbol_fabricated_fill_price_blocks_whole_decision():
    bad = paper_order_intent()
    bad["fill_price"] = 65000.0  # v0.5.3.16 must never fabricate a fill
    result = run({"BTC/USD": bad, "ETH/USD": no_order()})

    assert result["overall_ledger_decision"] == "BLOCKED"
    assert any("FABRICATED_FILL_PRICE:BTC/USD" in r for r in result["blocked_reasons"])


# ---------------------------------------------------------------------------
# Optional (all-coin-universe) symbol handling
# ---------------------------------------------------------------------------


def test_optional_symbol_intent_recorded_independently_and_counted_in_cross_check():
    # AVAX is the only symbol with an intent -- .16's own aggregate fields
    # (paper_execution_allowed / overall_execution_decision) reflect that,
    # so the cross-check must span the full symbol set, not just required.
    result = run({"BTC/USD": no_order(), "ETH/USD": no_order(), "AVAX/USD": paper_order_intent()})

    assert result["decision_status"] == "DECIDED"
    assert result["overall_ledger_decision"] == "INTENTS_RECORDED"
    assert result["ledger_entries"]["AVAX/USD"]["ledger_event"] == "PAPER_ORDER_INTENT_RECORDED"
    assert "optional_symbols_blocked" not in result


def test_optional_symbol_blocked_upstream_does_not_block_required_pair():
    result = run({"BTC/USD": paper_order_intent(), "ETH/USD": no_order(), "AVAX/USD": blocked()})

    assert result["decision_status"] == "DECIDED"
    assert result["overall_ledger_decision"] == "INTENTS_RECORDED"
    assert result["ledger_entries"]["AVAX/USD"]["ledger_event"] == "BLOCKED"
    assert result["ledger_entries"]["AVAX/USD"]["reason"] == "UPSTREAM_EXECUTION_BLOCKED"
    assert result.get("optional_symbols_blocked") == ["AVAX/USD"]
    assert result["ledger_entries"]["BTC/USD"]["ledger_event"] == "PAPER_ORDER_INTENT_RECORDED"


def test_optional_symbol_structural_inconsistency_blocks_only_that_symbol():
    bad_avax = paper_order_intent()
    bad_avax["fill_price"] = 40.0  # fabricated fill, same class of issue as the required-symbol case
    result = run({"BTC/USD": no_order(), "ETH/USD": no_order(), "AVAX/USD": bad_avax})

    assert result["decision_status"] == "DECIDED"
    assert result["ledger_entries"]["AVAX/USD"]["ledger_event"] == "BLOCKED"
    assert result.get("optional_symbols_blocked") == ["AVAX/USD"]
    assert result["ledger_entries"]["BTC/USD"]["ledger_event"] == "NO_ORDER_RECORDED"


def test_full_coin_universe_optional_symbols_evaluated_independently():
    result = run({
        "BTC/USD": no_order(),
        "ETH/USD": no_order(),
        "AVAX/USD": paper_order_intent(),
        "SOL/USD": no_order(),
        "DOGE/USD": blocked(),
    })

    assert result["overall_ledger_decision"] == "INTENTS_RECORDED"
    assert result["ledger_entries"]["AVAX/USD"]["ledger_event"] == "PAPER_ORDER_INTENT_RECORDED"
    assert result["ledger_entries"]["SOL/USD"]["ledger_event"] == "NO_ORDER_RECORDED"
    assert result["ledger_entries"]["DOGE/USD"]["ledger_event"] == "BLOCKED"
    assert result.get("optional_symbols_blocked") == ["DOGE/USD"]


# ---------------------------------------------------------------------------
# print_report() smoke test -- must not reference the old fixed SYMBOLS tuple
# ---------------------------------------------------------------------------


def test_print_report_covers_all_reported_symbols_including_optional(capsys):
    result = run({"BTC/USD": paper_order_intent(), "ETH/USD": no_order(), "AVAX/USD": blocked()})

    ledger.print_report(result, Path("test-output.json"))
    out = capsys.readouterr().out

    assert "BTC/USD  [REQUIRED]" in out
    assert "ETH/USD  [REQUIRED]" in out
    assert "AVAX/USD  [OPTIONAL, BLOCKED" in out
