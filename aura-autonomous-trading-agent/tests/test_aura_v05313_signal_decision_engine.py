"""
Contract tests for AURA v0.5.3.13 Signal Decision Engine.

Covers the multi-candidate (FROZEN_CANDIDATE + MIRROR_CANDIDATE) and
all-coin-universe (required-vs-optional symbol) behavior added on top of
the original locked BTC/ETH-only, single-candidate architecture.

Fixtures are built directly against the real module's own hashing helpers
(stable_json / sha256_text) so a passing test here is meaningful evidence
about the real .12 -> .13 contract, not just this module in isolation.
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


engine = _load("aura_v05313_signal_decision_engine.py", "aura_v05313_engine")

FROZEN_CANDIDATE = engine.FROZEN_CANDIDATE
MIRROR_CANDIDATE = engine.MIRROR_CANDIDATE
REQUIRED_SYMBOLS = engine.REQUIRED_SYMBOLS


def _build_canonical_and_hash(symbol_timestamps: dict) -> tuple[dict, str, str]:
    canonical = {
        "symbols": {
            symbol: {"timestamp": ts}
            for symbol, ts in symbol_timestamps.items()
        }
    }
    state_hash = engine.sha256_text(engine.stable_json(canonical))
    timestamps = [f"{s}:{symbol_timestamps[s]}" for s in sorted(symbol_timestamps)]
    state_id = f"MS-{engine.sha256_text('|'.join(timestamps) + '|' + state_hash)[:24]}"
    return canonical, state_hash, state_id


def frozen_symbol(timestamp: str = "2026-09-01T00:00:00Z") -> dict:
    return {"regime_state": FROZEN_CANDIDATE, "frozen_candidate_match": True, "timestamp": timestamp}


def mirror_symbol(timestamp: str = "2026-09-01T00:00:00Z") -> dict:
    return {"regime_state": MIRROR_CANDIDATE, "frozen_candidate_match": False, "timestamp": timestamp}


def neutral_symbol(timestamp: str = "2026-09-01T00:00:00Z") -> dict:
    return {"regime_state": "BULL x HIGH ATR x POSITIVE bar-2", "frozen_candidate_match": False, "timestamp": timestamp}


def make_snapshot(symbol_specs: dict) -> dict:
    """
    symbol_specs: symbol -> spec dict (as produced by frozen_symbol/mirror_symbol/
    neutral_symbol, or a custom dict with the same keys, optionally overriding
    "data_status" / "market_state_valid") or None (symbol entirely absent from
    the top-level "symbols" map, e.g. to simulate a missing required symbol).
    """
    present = {s: v for s, v in symbol_specs.items() if v is not None}
    symbol_timestamps = {s: v.get("timestamp", "2026-09-01T00:00:00Z") for s, v in present.items()}
    canonical, state_hash, state_id = _build_canonical_and_hash(symbol_timestamps)

    symbols_payload = {}
    for symbol, spec in present.items():
        symbols_payload[symbol] = {
            "data_status": spec.get("data_status", "VALID"),
            "market_state_valid": spec.get("market_state_valid", True),
            "market_state": {
                "timestamp": spec.get("timestamp", "2026-09-01T00:00:00Z"),
                "regime_state": spec.get("regime_state"),
                "frozen_candidate_match": spec.get("frozen_candidate_match", False),
            },
        }

    return {
        "agent_version": "AURA v0.5.3.12",
        "engine": "MARKET_STATE_ENGINE",
        "data_status": "VALID",
        "market_state_valid": True,
        "guardrails": {
            "single_source_of_truth": True,
            "no_recalculation_downstream": True,
            "orders_allowed": False,
            "paper_execution": False,
            "live_execution": False,
            "strategy_changed": False,
            "parameters_changed": False,
        },
        "frozen_configuration": {
            "candidate": engine.FROZEN_CANDIDATE,
            "atr_threshold_pct": engine.FROZEN_ATR_THRESHOLD_PCT,
            "ema_period_4h": engine.FROZEN_EMA_PERIOD_4H,
            "atr_period_1h": engine.FROZEN_ATR_PERIOD_1H,
            "bar_2_lag_hours": engine.FROZEN_BAR_2_LAG_HOURS,
        },
        "state_hash": state_hash,
        "state_id": state_id,
        "canonical_state": canonical,
        "symbols": symbols_payload,
    }


def run(symbol_specs: dict) -> dict:
    snapshot = make_snapshot(symbol_specs)
    return engine.process(snapshot, Path("test-fixture-input.json"))


# ---------------------------------------------------------------------------
# Candidate recognition (FROZEN_CANDIDATE + MIRROR_CANDIDATE)
# ---------------------------------------------------------------------------


def test_frozen_candidate_on_both_required_symbols_is_signal_candidate():
    decision = run({"BTC/USD": frozen_symbol(), "ETH/USD": frozen_symbol()})

    assert decision["decision_status"] == "DECIDED"
    assert decision["overall_decision"] == "SIGNAL_CANDIDATE"
    for symbol in REQUIRED_SYMBOLS:
        item = decision["decisions"][symbol]
        assert item["decision"] == "SIGNAL_CANDIDATE"
        assert item["candidate_match"] is True
        assert item["reason"] == "FROZEN_CANDIDATE_MATCH"


def test_mirror_candidate_alone_is_also_signal_candidate():
    decision = run({"BTC/USD": mirror_symbol(), "ETH/USD": neutral_symbol()})

    assert decision["overall_decision"] == "SIGNAL_CANDIDATE"
    btc = decision["decisions"]["BTC/USD"]
    assert btc["decision"] == "SIGNAL_CANDIDATE"
    assert btc["candidate_match"] is True
    # Downstream (.14/.23) literal-string contract must be preserved even
    # for the mirror candidate -- direction is never decided here.
    assert btc["reason"] == "FROZEN_CANDIDATE_MATCH"

    eth = decision["decisions"]["ETH/USD"]
    assert eth["decision"] == "NO_SIGNAL"
    assert eth["reason"] == "FROZEN_CANDIDATE_NOT_PRESENT"


def test_neither_candidate_present_is_no_signal():
    decision = run({"BTC/USD": neutral_symbol(), "ETH/USD": neutral_symbol()})

    assert decision["decision_status"] == "DECIDED"
    assert decision["overall_decision"] == "NO_SIGNAL"
    for symbol in REQUIRED_SYMBOLS:
        assert decision["decisions"][symbol]["decision"] == "NO_SIGNAL"


def test_candidate_match_inconsistent_still_detected_for_frozen_candidate():
    bad = frozen_symbol()
    bad["frozen_candidate_match"] = False  # .12 says no match, but regime_state says FROZEN_CANDIDATE
    decision = run({"BTC/USD": bad, "ETH/USD": neutral_symbol()})

    assert decision["overall_decision"] == "BLOCKED"
    assert decision["decisions"]["BTC/USD"]["reason"] == "CANDIDATE_MATCH_INCONSISTENT"


def test_mirror_candidate_with_frozen_flag_true_is_inconsistent():
    # .12 has no notion of MIRROR_CANDIDATE, so frozen_candidate_match must
    # only ever be True when regime_state == FROZEN_CANDIDATE. A mirror
    # regime_state with frozen_candidate_match=True is a source-data
    # inconsistency, not a recognized signal.
    bad = mirror_symbol()
    bad["frozen_candidate_match"] = True
    decision = run({"BTC/USD": bad, "ETH/USD": neutral_symbol()})

    assert decision["overall_decision"] == "BLOCKED"
    assert decision["decisions"]["BTC/USD"]["reason"] == "CANDIDATE_MATCH_INCONSISTENT"


# ---------------------------------------------------------------------------
# Required vs. optional symbol handling (all-coin-universe support)
# ---------------------------------------------------------------------------


def test_missing_required_symbol_blocks_whole_decision():
    decision = run({"BTC/USD": frozen_symbol(), "ETH/USD": None})

    assert decision["decision_status"] == "BLOCKED"
    assert decision["overall_decision"] == "BLOCKED"
    assert "MISSING_REQUIRED_SYMBOL:ETH/USD" in decision["invalid_reasons"]


def test_optional_symbol_blocked_does_not_cascade_to_overall_decision():
    decision = run({
        "BTC/USD": frozen_symbol(),
        "ETH/USD": neutral_symbol(),
        "AVAX/USD": {"regime_state": None, "market_state_valid": False, "timestamp": "2026-09-01T00:00:00Z"},
    })

    assert decision["decision_status"] == "DECIDED"
    assert decision["overall_decision"] == "SIGNAL_CANDIDATE"
    assert decision["decisions"]["AVAX/USD"]["decision"] == "BLOCKED"
    assert decision.get("optional_symbols_blocked") == ["AVAX/USD"]


def test_required_symbol_blocked_still_blocks_whole_decision_even_with_optional_symbols_present():
    decision = run({
        "BTC/USD": {"regime_state": None, "market_state_valid": False, "timestamp": "2026-09-01T00:00:00Z"},
        "ETH/USD": frozen_symbol(),
        "AVAX/USD": neutral_symbol(),
    })

    assert decision["decision_status"] == "BLOCKED"
    assert decision["overall_decision"] == "BLOCKED"
    assert "ONE_OR_MORE_REQUIRED_SYMBOL_DECISIONS_BLOCKED" in decision["invalid_reasons"]
    # AVAX/USD's own (valid) evaluation is still recorded even though it
    # cannot save an overall decision blocked by a required symbol.
    assert decision["decisions"]["AVAX/USD"]["decision"] == "NO_SIGNAL"


def test_full_coin_universe_all_optional_symbols_evaluated_independently():
    decision = run({
        "BTC/USD": frozen_symbol(),
        "ETH/USD": neutral_symbol(),
        "AVAX/USD": mirror_symbol(),
        "SOL/USD": neutral_symbol(),
    })

    assert decision["overall_decision"] == "SIGNAL_CANDIDATE"
    assert decision["decisions"]["AVAX/USD"]["decision"] == "SIGNAL_CANDIDATE"
    assert decision["decisions"]["SOL/USD"]["decision"] == "NO_SIGNAL"
    assert "optional_symbols_blocked" not in decision


# ---------------------------------------------------------------------------
# print_report() smoke test -- must not reference the old fixed SYMBOLS tuple
# ---------------------------------------------------------------------------


def test_print_report_covers_all_reported_symbols_including_optional(capsys):
    decision = run({
        "BTC/USD": frozen_symbol(),
        "ETH/USD": neutral_symbol(),
        "AVAX/USD": {"regime_state": None, "market_state_valid": False, "timestamp": "2026-09-01T00:00:00Z"},
    })

    engine.print_report(decision, Path("test-output.json"))
    out = capsys.readouterr().out

    assert "BTC/USD  [REQUIRED]" in out
    assert "ETH/USD  [REQUIRED]" in out
    assert "AVAX/USD  [OPTIONAL, BLOCKED" in out
