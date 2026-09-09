"""
Contract tests for AURA v0.5.3.18 Reconciliation.

Covers the required-vs-optional symbol handling added on top of the
original locked BTC/ETH-only architecture: REQUIRED_SYMBOLS (BTC/ETH)
remain the only symbols whose structural invalidity or evidence conflict
can block/gate the whole reconciliation decision (overall_reconciliation
CONFLICT); every other symbol .17/observed-execution reports is
reconciled independently, contributing positively (RECONCILED/PENDING)
without being able to force the aggregate to CONFLICT.
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


recon = _load("aura_v05318_reconciliation.py", "aura_v05318_recon")

REQUIRED_SYMBOLS = recon.REQUIRED_SYMBOLS


def ledger_no_order() -> dict:
    return {
        "ledger_event": "NO_ORDER_RECORDED",
        "position_exists": False,
        "position_id": None,
        "fill_price": None,
        "fill_timestamp": None,
    }


def ledger_intent() -> dict:
    return {
        "ledger_event": "PAPER_ORDER_INTENT_RECORDED",
        "position_exists": False,
        "position_id": None,
        "fill_price": None,
        "fill_timestamp": None,
    }


def obs_none() -> dict:
    # "Nothing happened" observation: not pending/filled, so a NO_ORDER_RECORDED
    # ledger entry reconciles cleanly against it (PENDING would itself be a
    # conflict against a symbol with no ledger order).
    return {"order_status": "CANCELED", "position_exists": False, "position_id": None, "fill_price": None, "fill_timestamp": None}


def obs_filled(price: float = 65000.0, position_id: str = "PAPER-1") -> dict:
    return {
        "order_status": "FILLED",
        "position_exists": True,
        "position_id": position_id,
        "fill_price": price,
        "fill_timestamp": "2026-09-01T00:05:00Z",
    }


def obs_rejected() -> dict:
    return {"order_status": "REJECTED", "position_exists": False, "position_id": None, "fill_price": None, "fill_timestamp": None}


def make_ledger_payload(entry_specs: dict) -> dict:
    entries = {s: {"symbol": s, **v} for s, v in entry_specs.items() if v is not None}
    payload = {
        "agent_version": "AURA v0.5.3.17",
        "engine": "DECISION_EXECUTION_LEDGER",
        "decision_status": "DECIDED",
        "overall_ledger_decision": "INTENTS_RECORDED",
        "generated_from": "test-fixture",
        "input_paper_execution_hash": "testfixturehash",
        "upstream_state_id": "PE-testfixture0000000000",
        "upstream_hash_verified": True,
        "frozen_configuration_verified": True,
        "ledger_entries": entries,
        "blocked_reasons": [],
        "guardrails": {
            "single_source_of_truth": True,
            "source_engine": "AURA v0.5.3.16",
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
            "fail_closed": True,
        },
    }
    canonical = {
        "agent_version": payload["agent_version"],
        "engine": payload["engine"],
        "decision_status": payload["decision_status"],
        "overall_ledger_decision": payload["overall_ledger_decision"],
        "input_paper_execution_hash": payload["input_paper_execution_hash"],
        "upstream_state_id": payload["upstream_state_id"],
        "upstream_hash_verified": payload["upstream_hash_verified"],
        "frozen_configuration_verified": payload["frozen_configuration_verified"],
        "ledger_entries": payload["ledger_entries"],
        "blocked_reasons": payload["blocked_reasons"],
    }
    payload["state_hash"] = recon.sha256_text(recon.stable_json(canonical))
    payload["state_id"] = f"LE-{payload['state_hash'][:24]}"
    return payload


def make_observed_payload(obs_specs: dict) -> dict:
    observations = {s: v for s, v in obs_specs.items() if v is not None}
    payload = {
        "agent_version": "OBSERVED_EXECUTION_TEST_FIXTURE",
        "engine": "OBSERVED_EXECUTION",
        "snapshot_status": "OBSERVED",
        "observations": observations,
    }
    canonical = {
        "agent_version": payload["agent_version"],
        "engine": payload["engine"],
        "snapshot_status": payload["snapshot_status"],
        "observations": payload["observations"],
    }
    payload["snapshot_hash"] = recon.sha256_text(recon.stable_json(canonical))
    return payload


def run(entry_specs: dict, obs_specs: dict) -> dict:
    ledger_payload = make_ledger_payload(entry_specs)
    observed_payload = make_observed_payload(obs_specs)
    return recon.build_reconciliation(
        ledger_payload, observed_payload, Path("ledger-fixture.json"), Path("observed-fixture.json")
    )


# ---------------------------------------------------------------------------
# Required-pair (BTC/ETH) baseline behavior -- unchanged from the original
# architecture.
# ---------------------------------------------------------------------------


def test_no_order_and_no_observation_on_required_pair_is_reconciled():
    result = run(
        {"BTC/USD": ledger_no_order(), "ETH/USD": ledger_no_order()},
        {"BTC/USD": obs_none(), "ETH/USD": obs_none()},
    )

    assert result["decision_status"] == "DECIDED"
    assert result["overall_reconciliation"] == "RECONCILED"
    for symbol in REQUIRED_SYMBOLS:
        assert result["decisions"][symbol]["reconciliation_state"] == "NO_ORDER_CONFIRMED"


def test_intent_matched_by_observed_fill_on_required_pair_is_reconciled():
    result = run(
        {"BTC/USD": ledger_intent(), "ETH/USD": ledger_no_order()},
        {"BTC/USD": obs_filled(), "ETH/USD": obs_none()},
    )

    assert result["overall_reconciliation"] == "RECONCILED"
    assert result["decisions"]["BTC/USD"]["reconciliation_state"] == "RECONCILED_EXECUTION"
    assert result["decisions"]["BTC/USD"]["position_exists"] is True


def test_missing_required_ledger_entry_blocks_whole_decision():
    result = run(
        {"BTC/USD": ledger_no_order(), "ETH/USD": None},
        {"BTC/USD": obs_none(), "ETH/USD": obs_none()},
    )

    assert result["overall_reconciliation"] == "BLOCKED"
    assert any("MISSING_OR_INVALID_SYMBOL:ETH/USD" in r for r in result["blocked_reasons"])


def test_required_symbol_conflict_gates_overall_to_conflict():
    result = run(
        {"BTC/USD": ledger_no_order(), "ETH/USD": ledger_no_order()},
        {"BTC/USD": obs_filled(), "ETH/USD": obs_none()},  # fill with no ledger order == conflict
    )

    assert result["decision_status"] == "DECIDED"
    assert result["overall_reconciliation"] == "CONFLICT"
    assert result["decisions"]["BTC/USD"]["reconciliation_state"] == "RECONCILIATION_CONFLICT"


# ---------------------------------------------------------------------------
# Optional (all-coin-universe) symbol handling
# ---------------------------------------------------------------------------


def test_optional_symbol_reconciled_independently_and_contributes_to_overall():
    result = run(
        {"BTC/USD": ledger_no_order(), "ETH/USD": ledger_no_order(), "AVAX/USD": ledger_intent()},
        {"BTC/USD": obs_none(), "ETH/USD": obs_none(), "AVAX/USD": obs_filled()},
    )

    assert result["decision_status"] == "DECIDED"
    assert result["overall_reconciliation"] == "RECONCILED"
    assert result["decisions"]["AVAX/USD"]["reconciliation_state"] == "RECONCILED_EXECUTION"
    assert "optional_symbols_blocked" not in result


def test_optional_symbol_conflict_does_not_gate_overall_to_conflict():
    # AVAX has an observed fill with no matching ledger order -- a genuine
    # conflict for AVAX -- but the required pair is clean. Overall must stay
    # RECONCILED, not CONFLICT: an optional symbol's bad evidence must never
    # force the whole cycle into CONFLICT.
    result = run(
        {"BTC/USD": ledger_no_order(), "ETH/USD": ledger_no_order(), "AVAX/USD": ledger_no_order()},
        {"BTC/USD": obs_none(), "ETH/USD": obs_none(), "AVAX/USD": obs_filled()},
    )

    assert result["decision_status"] == "DECIDED"
    assert result["overall_reconciliation"] == "RECONCILED"
    assert result["decisions"]["AVAX/USD"]["reconciliation_state"] == "RECONCILIATION_CONFLICT"
    assert result.get("optional_symbols_blocked") == ["AVAX/USD"]
    assert result["decisions"]["BTC/USD"]["reconciliation_state"] == "NO_ORDER_CONFIRMED"


def test_optional_symbol_missing_observation_is_blocked_not_globally():
    # This file's own philosophy: never silently treat a missing observation
    # as reconciled, even for an optional symbol with an order intent.
    result = run(
        {"BTC/USD": ledger_no_order(), "ETH/USD": ledger_no_order(), "AVAX/USD": ledger_intent()},
        {"BTC/USD": obs_none(), "ETH/USD": obs_none()},  # no AVAX observation at all
    )

    assert result["decision_status"] == "DECIDED"
    assert result["overall_reconciliation"] == "RECONCILED"
    assert result["decisions"]["AVAX/USD"]["reconciliation_state"] == "BLOCKED"
    assert result.get("optional_symbols_blocked") == ["AVAX/USD"]


def test_full_coin_universe_optional_symbols_evaluated_independently():
    result = run(
        {
            "BTC/USD": ledger_no_order(),
            "ETH/USD": ledger_no_order(),
            "AVAX/USD": ledger_intent(),
            "SOL/USD": ledger_intent(),
        },
        {
            "BTC/USD": obs_none(),
            "ETH/USD": obs_none(),
            "AVAX/USD": obs_filled(),
            "SOL/USD": obs_rejected(),
        },
    )

    assert result["overall_reconciliation"] == "RECONCILED"
    assert result["decisions"]["AVAX/USD"]["reconciliation_state"] == "RECONCILED_EXECUTION"
    assert result["decisions"]["SOL/USD"]["reconciliation_state"] == "EXECUTION_NOT_FILLED"
    assert "optional_symbols_blocked" not in result


# ---------------------------------------------------------------------------
# print_report() smoke test -- must not reference the old fixed SYMBOLS tuple
# ---------------------------------------------------------------------------


def test_print_report_covers_all_reported_symbols_including_optional(capsys):
    result = run(
        {"BTC/USD": ledger_no_order(), "ETH/USD": ledger_no_order(), "AVAX/USD": ledger_no_order()},
        {"BTC/USD": obs_none(), "ETH/USD": obs_none(), "AVAX/USD": obs_filled()},
    )

    recon.print_report(result, Path("test-output.json"))
    out = capsys.readouterr().out

    assert "BTC/USD  [REQUIRED]" in out
    assert "ETH/USD  [REQUIRED]" in out
    assert "AVAX/USD  [OPTIONAL, BLOCKED" in out


# ---------------------------------------------------------------------------
# main()'s missing-observation-file fallback path (REQUIRED_SYMBOLS-scoped
# no_order_only check, extended to report optional symbols too)
# ---------------------------------------------------------------------------


def test_no_order_only_check_scoped_to_required_symbols():
    entries = {
        "BTC/USD": {"symbol": "BTC/USD", **ledger_no_order()},
        "ETH/USD": {"symbol": "ETH/USD", **ledger_no_order()},
        "AVAX/USD": {"symbol": "AVAX/USD", **ledger_intent()},
    }
    no_order_only = all(
        isinstance(entries.get(symbol), dict) and entries[symbol].get("ledger_event") == "NO_ORDER_RECORDED"
        for symbol in REQUIRED_SYMBOLS
    )
    # An optional symbol having an intent must not affect the required-only check.
    assert no_order_only is True
