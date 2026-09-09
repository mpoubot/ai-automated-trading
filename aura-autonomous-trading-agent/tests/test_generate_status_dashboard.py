#!/usr/bin/env python3
"""Tests for scripts/generate_status_dashboard.py's required-vs-optional
symbol handling.

Loaded via importlib (script lives outside any package), matching the
convention used elsewhere in tests/ (e.g.
tests/test_aura_v05323_execution_specification_builder.py).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "generate_status_dashboard.py"

spec = importlib.util.spec_from_file_location("aura_generate_status_dashboard", SCRIPT_PATH)
dashboard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dashboard)


def make_signal(symbols_and_states: dict[str, str]) -> dict:
    return {
        "decisions": {
            symbol: {"regime_state": state, "decision": "SIGNAL_CANDIDATE"}
            for symbol, state in symbols_and_states.items()
        }
    }


def make_safety(symbols: list[str], *, authorized: bool = False) -> dict:
    return {
        "decisions": {
            symbol: {"execution_authorized": authorized, "reason": "RESEARCH_ONLY"}
            for symbol in symbols
        }
    }


def test_no_runtime_state_yet_renders_placeholder_with_no_cards():
    page = dashboard.render(None, None, None, None)
    assert "No runtime cycle has been published yet" in page
    # Never crashes, never fabricates a card for data that doesn't exist.
    assert "<h2>BTC/USD" not in page
    assert "<h2>ETH/USD" not in page


def test_required_symbols_always_render_even_with_no_decision_data():
    runtime_state = {"cycle_id": "c1", "status": "PASS", "kill_switch_active": False, "execution_authorized": False}
    page = dashboard.render(runtime_state, None, None, None)
    # BTC/USD and ETH/USD are REQUIRED_SYMBOLS: they render even with
    # nothing published for them yet, same as before this change.
    assert "<h2>BTC/USD" in page
    assert "<h2>ETH/USD" in page


def test_optional_discovered_symbol_gets_its_own_card():
    runtime_state = {"cycle_id": "c2", "status": "PASS", "kill_switch_active": False, "execution_authorized": False}
    signal = make_signal({"BTC/USD": "BEAR", "ETH/USD": "BEAR", "AVAX/USD": "BULL"})
    safety = make_safety(["BTC/USD", "ETH/USD", "AVAX/USD"])
    page = dashboard.render(runtime_state, signal, safety, None)
    assert "<h2>BTC/USD" in page
    assert "<h2>ETH/USD" in page
    assert "<h2>AVAX/USD" in page


def test_optional_symbol_missing_from_other_upstream_sources_does_not_crash():
    runtime_state = {"cycle_id": "c3", "status": "PASS", "kill_switch_active": False, "execution_authorized": False}
    # AVAX/USD only appears in `signal`, not in `safety` or `spec` -- the
    # per-symbol card must still render using dashes for the missing pieces,
    # never raise, and never cascade into dropping BTC/ETH.
    signal = make_signal({"BTC/USD": "BEAR", "ETH/USD": "BEAR", "AVAX/USD": "BULL"})
    safety = make_safety(["BTC/USD", "ETH/USD"])
    page = dashboard.render(runtime_state, signal, safety, None)
    assert "<h2>AVAX/USD" in page
    assert "<h2>BTC/USD" in page
    assert "<h2>ETH/USD" in page


def test_required_symbols_render_even_if_absent_from_every_upstream_source():
    # Pathological case: signal/safety/spec exist for this cycle but happen
    # to report nothing for ETH/USD (e.g. it was BLOCKED upstream with an
    # empty decisions entry). REQUIRED_SYMBOLS must still guarantee a card.
    runtime_state = {"cycle_id": "c4", "status": "PASS", "kill_switch_active": False, "execution_authorized": False}
    signal = make_signal({"BTC/USD": "BEAR"})
    safety = make_safety(["BTC/USD"])
    page = dashboard.render(runtime_state, signal, safety, None)
    assert "<h2>BTC/USD" in page
    assert "<h2>ETH/USD" in page


def test_header_no_longer_hardcodes_exactly_two_symbols():
    runtime_state = None
    page = dashboard.render(runtime_state, None, None, None)
    assert "(BTC/USD, ETH/USD) •" not in page
    assert "full discovered coin universe" in page


def test_symbol_card_smoke_no_upstream_data_for_symbol():
    card = dashboard.symbol_card("DOGE/USD", None, None, None)
    assert "DOGE/USD" in card
    assert "NOT RUN" in card


if __name__ == "__main__":
    import sys

    failures = 0
    tests = [
        test_no_runtime_state_yet_renders_placeholder_with_no_cards,
        test_required_symbols_always_render_even_with_no_decision_data,
        test_optional_discovered_symbol_gets_its_own_card,
        test_optional_symbol_missing_from_other_upstream_sources_does_not_crash,
        test_required_symbols_render_even_if_absent_from_every_upstream_source,
        test_header_no_longer_hardcodes_exactly_two_symbols,
        test_symbol_card_smoke_no_upstream_data_for_symbol,
    ]
    for t in tests:
        try:
            t()
            print(f"PASS: {t.__name__}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL: {t.__name__}: {exc}")
    print(f"{len(tests) - failures}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
