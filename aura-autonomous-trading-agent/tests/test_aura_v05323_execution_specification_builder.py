"""
Contract tests for AURA v0.5.3.23 Execution Specification Builder.

These tests construct v0.5.3.13 / v0.5.3.19-shaped fixtures directly rather
than running the full v0.5.3.12-.19 chain (which requires real closed-bar
market data). The fixtures are structurally faithful to the real modules'
output schemas (field names, hash/state-id conventions) so a passing test
here is meaningful evidence about the real contract, not just about this
module in isolation.

Note: the real v0.5.3.19 hard-codes execution_authorized /
paper_execution_authorized to False on every run (research-only boundary,
untouched by this change). The "authorized" fixtures below represent what
v0.5.3.19 output would look like IF that boundary were ever deliberately
opened — they test this module's own logic on self-consistent input, they
do not claim the real chain can produce such input today.
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


builder = _load("aura_v05323_execution_specification_builder.py", "aura_v05323_builder")
adapter = _load("aura_v05322_alpaca_paper_execution_adapter.py", "aura_v05322_adapter")

SYMBOLS = ("BTC/USD", "ETH/USD")
FROZEN_BEAR_CANDIDATE = "BEAR x LOW ATR x POSITIVE bar-2"


def make_signal_payload(regime_state_by_symbol: dict) -> dict:
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
        "decisions": {
            symbol: {
                "symbol": symbol,
                "decision": "SIGNAL_CANDIDATE",
                "market_state_valid": True,
                "candidate_match": True,
                "state_timestamp": "2026-09-01T00:00:00Z",
                "state_id": None,
                "regime_state": regime_state_by_symbol[symbol],
                "reason": "FROZEN_CANDIDATE_MATCH",
            }
            for symbol in SYMBOLS
        },
    }


def make_safety_payload(
    *,
    execution_authorized: bool = True,
    paper_execution_authorized: bool = True,
    kill_switch_active: bool = False,
    per_symbol_authorized: dict | None = None,
) -> dict:
    per_symbol_authorized = per_symbol_authorized or {s: execution_authorized for s in SYMBOLS}
    result = {
        "agent_version": "AURA v0.5.3.19",
        "engine": "EXECUTION_SAFETY",
        "decision_status": "DECIDED",
        "overall_execution_safety": "SAFE_NO_ORDER",
        "execution_authorized": execution_authorized,
        "paper_execution_authorized": paper_execution_authorized,
        "live_execution_authorized": False,
        "kill_switch_active": kill_switch_active,
        "orders_enabled": True,
        "paper_execution_enabled": True,
        "live_execution_enabled": False,
        "upstream_state_id": "RC-testfixture0000000000",
        "upstream_hash_verified": True,
        "frozen_configuration_verified": True,
        "observed_execution_verified": True,
        "decisions": {
            symbol: {
                "symbol": symbol,
                "reconciliation_state": "NO_ORDER_CONFIRMED",
                "execution_safety": "SAFE_NO_ORDER" if per_symbol_authorized[symbol] else "BLOCKED",
                "execution_authorized": per_symbol_authorized[symbol],
                "reason": "TEST_FIXTURE",
            }
            for symbol in SYMBOLS
        },
        "blocked_reasons": [],
    }
    state_hash = builder.canonical_execution_safety_hash(result)
    result["state_hash"] = state_hash
    result["state_id"] = f"ES-{state_hash[:24]}"
    return result


def run_build(signal_payload, safety_payload, sizing_config=None):
    return builder.build_specs(
        signal_payload,
        safety_payload,
        sizing_config or {},
        Path("test-signal.json"),
        Path("test-safety.json"),
    )


def expect(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)
    print(f"PASS: {name}")


def test_bear_regime_never_produces_sell_or_buy():
    signal = make_signal_payload({s: FROZEN_BEAR_CANDIDATE for s in SYMBOLS})
    safety = make_safety_payload()
    result = run_build(signal, safety, sizing_config={s: 0.01 for s in SYMBOLS})

    expect("overall status is NO_EXECUTION_SPEC", result["overall_status"] == "NO_EXECUTION_SPEC")
    for symbol in SYMBOLS:
        item = result["decisions"][symbol]
        expect(f"{symbol} blocked", item["status"] == "BLOCKED")
        expect(
            f"{symbol} reason is NO_VALIDATED_EXECUTION_DIRECTION (not a SELL mapping)",
            item["reason"] == "NO_VALIDATED_EXECUTION_DIRECTION",
        )
        expect(f"{symbol} spec has no 'side' key", "execution_specification" not in item)


def test_kill_switch_active_blocks_before_direction_is_even_considered():
    signal = make_signal_payload({s: FROZEN_BEAR_CANDIDATE for s in SYMBOLS})
    safety = make_safety_payload(kill_switch_active=True)
    result = run_build(signal, safety, sizing_config={s: 0.01 for s in SYMBOLS})

    expect(
        "decision_status is BLOCKED (upstream validation failed, matches project convention)",
        result["decision_status"] == "BLOCKED",
    )
    expect("KILL_SWITCH_ACTIVE is a blocked reason", "KILL_SWITCH_ACTIVE" in result["blocked_reasons"])
    for symbol in SYMBOLS:
        expect(f"{symbol} blocked on upstream validation", result["decisions"][symbol]["status"] == "BLOCKED")


def test_safety_hash_mismatch_blocks_closed():
    signal = make_signal_payload({s: FROZEN_BEAR_CANDIDATE for s in SYMBOLS})
    safety = make_safety_payload()
    safety["execution_authorized"] = False  # mutate after hashing -> hash mismatch
    result = run_build(signal, safety, sizing_config={s: 0.01 for s in SYMBOLS})

    expect(
        "hash mismatch is caught",
        "SAFETY_STATE_HASH_MISMATCH" in result["blocked_reasons"]
        or "EXECUTION_NOT_AUTHORIZED" in result["blocked_reasons"],
    )
    expect("no symbol reaches EXECUTION_SPEC_READY", result["overall_status"] != "EXECUTION_SPEC_READY")


def test_validated_long_entry_without_quantity_fails_closed_not_defaulted():
    original = builder.VALIDATED_LONG_ENTRY_REGIME_LABELS
    try:
        builder.VALIDATED_LONG_ENTRY_REGIME_LABELS = frozenset({"TEST_VALIDATED_LONG"})
        signal = make_signal_payload({s: "TEST_VALIDATED_LONG" for s in SYMBOLS})
        safety = make_safety_payload()
        result = run_build(signal, safety, sizing_config={})  # no sizing config at all

        for symbol in SYMBOLS:
            item = result["decisions"][symbol]
            expect(f"{symbol} blocked without a quantity source", item["status"] == "BLOCKED")
            expect(f"{symbol} reason is MISSING_QUANTITY_SOURCE", item["reason"] == "MISSING_QUANTITY_SOURCE")
            expect(f"{symbol} side was still determined ({item.get('side')})", item.get("side") == "BUY")
    finally:
        builder.VALIDATED_LONG_ENTRY_REGIME_LABELS = original


def test_validated_long_entry_with_quantity_produces_spec_accepted_by_v05322_adapter():
    """End-to-end proof: a hypothetically-validated long-entry regime label,
    with quantity supplied, produces an Execution Specification that the
    CANONICAL v0.5.3.22 adapter's own validate_spec() accepts outright."""
    original = builder.VALIDATED_LONG_ENTRY_REGIME_LABELS
    try:
        builder.VALIDATED_LONG_ENTRY_REGIME_LABELS = frozenset({"TEST_VALIDATED_LONG"})
        signal = make_signal_payload({s: "TEST_VALIDATED_LONG" for s in SYMBOLS})
        safety = make_safety_payload()
        result = run_build(signal, safety, sizing_config={"BTC/USD": 0.01, "ETH/USD": 0.05})

        expect("overall status is EXECUTION_SPEC_READY", result["overall_status"] == "EXECUTION_SPEC_READY")
        for symbol in SYMBOLS:
            item = result["decisions"][symbol]
            expect(f"{symbol} spec ready", item["status"] == "EXECUTION_SPEC_READY")
            spec = item["execution_specification"]
            expect(f"{symbol} side is BUY", spec["side"] == "BUY")

            # This is the actual contract proof: hand the built spec to the
            # real, canonical .22 adapter's own validator.
            errors = None
            try:
                validated = adapter.validate_spec(dict(spec))
                errors = []
            except RuntimeError as exc:
                errors = [str(exc)]
            expect(f"{symbol} spec accepted by v0.5.3.22 adapter validate_spec ({errors})", errors == [])
            expect(f"{symbol} adapter-validated side matches", validated["side"] == "BUY")
    finally:
        builder.VALIDATED_LONG_ENTRY_REGIME_LABELS = original


def test_missing_symbol_execution_authorization_blocks_that_symbol_only():
    signal = make_signal_payload({s: FROZEN_BEAR_CANDIDATE for s in SYMBOLS})
    safety = make_safety_payload(per_symbol_authorized={"BTC/USD": True, "ETH/USD": False})
    result = run_build(signal, safety, sizing_config={s: 0.01 for s in SYMBOLS})

    # BTC/USD still blocks on direction (BEAR is not validated); ETH/USD
    # blocks earlier, on authorization. Both must be BLOCKED, for different
    # reasons, and neither may silently pass.
    expect("BTC/USD blocked", result["decisions"]["BTC/USD"]["status"] == "BLOCKED")
    expect("ETH/USD blocked on authorization", result["decisions"]["ETH/USD"]["reason"] == "SYMBOL_EXECUTION_NOT_AUTHORIZED")


# ---------------------------------------------------------------------------
# All-coin-universe: symbols beyond the fixed BTC/ETH pair are picked up
# from whatever the upstream signal/safety decisions actually report, and
# remain fully independent of each other and of the required pair.
# ---------------------------------------------------------------------------


def _with_symbol(signal_payload, safety_payload, symbol, *, regime_state, authorized):
    signal_payload = dict(signal_payload)
    signal_payload["decisions"] = dict(signal_payload["decisions"])
    signal_payload["decisions"][symbol] = {
        "symbol": symbol,
        "decision": "SIGNAL_CANDIDATE",
        "market_state_valid": True,
        "candidate_match": True,
        "state_timestamp": "2026-09-01T00:00:00Z",
        "state_id": None,
        "regime_state": regime_state,
        "reason": "FROZEN_CANDIDATE_MATCH",
    }
    safety_payload = dict(safety_payload)
    safety_payload["decisions"] = dict(safety_payload["decisions"])
    safety_payload["decisions"][symbol] = {
        "symbol": symbol,
        "reconciliation_state": "NO_ORDER_CONFIRMED",
        "execution_safety": "SAFE_NO_ORDER" if authorized else "BLOCKED",
        "execution_authorized": authorized,
        "reason": "TEST_FIXTURE",
    }
    # The safety payload carries its own self-hash (state_hash/state_id)
    # that build_specs() verifies -- adding a symbol after make_safety_payload()
    # already hashed it would otherwise trip SAFETY_STATE_HASH_MISMATCH.
    # Recompute exactly as make_safety_payload() does.
    state_hash = builder.canonical_execution_safety_hash(safety_payload)
    safety_payload["state_hash"] = state_hash
    safety_payload["state_id"] = f"ES-{state_hash[:24]}"
    return signal_payload, safety_payload


def test_optional_symbol_beyond_required_pair_gets_its_own_spec_attempt():
    original = builder.VALIDATED_LONG_ENTRY_REGIME_LABELS
    try:
        builder.VALIDATED_LONG_ENTRY_REGIME_LABELS = frozenset({"TEST_VALIDATED_LONG"})
        signal = make_signal_payload({s: FROZEN_BEAR_CANDIDATE for s in SYMBOLS})
        safety = make_safety_payload()
        signal, safety = _with_symbol(
            signal, safety, "AVAX/USD", regime_state="TEST_VALIDATED_LONG", authorized=True
        )
        result = run_build(signal, safety, sizing_config={**{s: 0.01 for s in SYMBOLS}, "AVAX/USD": 1.5})

        expect("overall status is EXECUTION_SPEC_READY", result["overall_status"] == "EXECUTION_SPEC_READY")
        avax = result["decisions"]["AVAX/USD"]
        expect("AVAX/USD spec ready", avax["status"] == "EXECUTION_SPEC_READY")
        expect("AVAX/USD side is BUY", avax["execution_specification"]["side"] == "BUY")
        # Required BTC/ETH pair (still BEAR, unvalidated) stays blocked and
        # unaffected by AVAX's readiness.
        for symbol in SYMBOLS:
            expect(f"{symbol} still blocked on direction", result["decisions"][symbol]["status"] == "BLOCKED")
    finally:
        builder.VALIDATED_LONG_ENTRY_REGIME_LABELS = original


def test_optional_symbol_missing_quantity_blocks_only_that_symbol():
    original = builder.VALIDATED_LONG_ENTRY_REGIME_LABELS
    try:
        builder.VALIDATED_LONG_ENTRY_REGIME_LABELS = frozenset({"TEST_VALIDATED_LONG"})
        signal = make_signal_payload({s: "TEST_VALIDATED_LONG" for s in SYMBOLS})
        safety = make_safety_payload()
        signal, safety = _with_symbol(
            signal, safety, "AVAX/USD", regime_state="TEST_VALIDATED_LONG", authorized=True
        )
        # No sizing entry at all for AVAX/USD.
        result = run_build(signal, safety, sizing_config={s: 0.01 for s in SYMBOLS})

        expect("overall status is EXECUTION_SPEC_READY", result["overall_status"] == "EXECUTION_SPEC_READY")
        for symbol in SYMBOLS:
            expect(f"{symbol} spec ready", result["decisions"][symbol]["status"] == "EXECUTION_SPEC_READY")
        avax = result["decisions"]["AVAX/USD"]
        expect("AVAX/USD blocked without a quantity source", avax["status"] == "BLOCKED")
        expect("AVAX/USD reason is MISSING_QUANTITY_SOURCE", avax["reason"] == "MISSING_QUANTITY_SOURCE")
    finally:
        builder.VALIDATED_LONG_ENTRY_REGIME_LABELS = original


def main() -> int:
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for test in tests:
        test()
    print(f"AURA v0.5.3.23 CONTRACT: {len(tests)}/{len(tests)} PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
