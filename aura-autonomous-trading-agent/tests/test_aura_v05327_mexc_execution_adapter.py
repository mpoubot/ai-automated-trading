#!/usr/bin/env python3
"""Contract + unit tests for AURA v0.5.3.27 MEXC execution adapter.

Mirrors v0.5.3.22's subprocess-driven contract-test style for the
validate-only/fail-closed paths (scenarios 1-9), where no network call of
any kind happens. Scenarios 10-14 need to exercise the actual submission
and outcome-classification logic -- unlike v0.5.3.22, this adapter has no
broker-assisted idempotency backstop, so its own claim step and outcome
classification carry the full safety weight and are tested directly
against an injected fake exchange, importing the module rather than
shelling out to its CLI (subprocess testing can't inject a fake exchange
without a real network attempt).

No real MEXC credentials, no real network call, anywhere in this file.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import ccxt

ROOT = Path(__file__).resolve().parents[1]
ADAPTER_PATH = ROOT / "aura_v05327_mexc_execution_adapter.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("aura_v05327_mexc_execution_adapter", ADAPTER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


MOD = _load_module()

BASE_ENTRY = {
    "execution_spec_version": "1.0",
    "exchange": "MEXC",
    "account_mode": "LIVE",
    "market_type": "swap",
    "kill_switch": False,
    "execution_authorized": True,
    "live_execution_authorized": True,
    "symbol": "BTC/USDT:USDT",
    "side": "BUY",
    "quantity": "0.01",
    "order_type": "MARKET",
    "reduce_only": False,
    "leverage": 3,
    "client_order_id": "aura-test-05327-001",
}


class FakeExchange:
    """Injectable stand-in for ccxt.mexc — no network I/O. Records every
    call so tests can assert on what was (or wasn't) attempted."""

    def __init__(self, create_order_result=None, create_order_exception=None,
                 set_leverage_exception=None):
        self.create_order_calls: list[tuple] = []
        self.set_leverage_calls: list[tuple] = []
        self._create_order_result = create_order_result
        self._create_order_exception = create_order_exception
        self._set_leverage_exception = set_leverage_exception

    def set_leverage(self, leverage, symbol):
        self.set_leverage_calls.append((leverage, symbol))
        if self._set_leverage_exception:
            raise self._set_leverage_exception

    def create_order(self, symbol, type, side, amount, params=None):
        self.create_order_calls.append((symbol, type, side, amount, params))
        if self._create_order_exception:
            raise self._create_order_exception
        return self._create_order_result or {"id": "mexc-fake-order-1", "status": "closed"}


# --------------------------------------------------------------------- #
# CLI-level contract tests (scenarios 1-9): validate-only, no network call
# --------------------------------------------------------------------- #

def run_cli(spec: dict, *extra: str) -> tuple[int, dict]:
    with tempfile.TemporaryDirectory() as d:
        inp = Path(d) / "spec.json"
        out = Path(d) / "out.json"
        inp.write_text(json.dumps(spec), encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, str(ADAPTER_PATH), "--input", str(inp), "--output", str(out), *extra],
            cwd=ROOT, text=True, capture_output=True,
        )
        return proc.returncode, json.loads(out.read_text(encoding="utf-8"))


def expect(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)
    print(f"PASS: {name}")


def test_cli_contract() -> None:
    # 1. valid BUY spec
    rc, result = run_cli(BASE_ENTRY)
    expect("1. valid BUY spec validates without submission",
           rc == 0 and result["status"] == "VALIDATED_NO_SUBMISSION")
    expect("1. validation never claims live submission", "mexc_order_id" not in result)

    # 2. valid SELL spec (reduce-only exit — no leverage required)
    sell_exit = dict(BASE_ENTRY, side="SELL", reduce_only=True, client_order_id="aura-test-05327-002")
    sell_exit.pop("leverage", None)
    rc, result = run_cli(sell_exit)
    expect("2. valid SELL (reduce-only exit) spec validates without submission",
           rc == 0 and result["status"] == "VALIDATED_NO_SUBMISSION")

    # 3. invalid symbol
    bad = dict(BASE_ENTRY, symbol="BTC-PERP", client_order_id="aura-test-05327-003")
    rc, result = run_cli(bad)
    expect("3. invalid symbol fails closed", rc == 1 and result["status"] == "FAIL_CLOSED")
    expect("3. invalid symbol reason names the field", "UNSUPPORTED_SYMBOL" in result["error"])

    # also confirm a non-perpetual / non-USDT symbol shape is rejected too
    bad_spot = dict(BASE_ENTRY, symbol="BTC/USDT", client_order_id="aura-test-05327-003b")
    rc, result = run_cli(bad_spot)
    expect("3b. spot-shaped symbol (no :USDT suffix) fails closed",
           rc == 1 and result["status"] == "FAIL_CLOSED")

    # 4. invalid side
    bad = dict(BASE_ENTRY, side="LONG", client_order_id="aura-test-05327-004")
    rc, result = run_cli(bad)
    expect("4. invalid side fails closed", rc == 1 and result["status"] == "FAIL_CLOSED")
    expect("4. invalid side reason", "INVALID_SIDE" in result["error"])

    # 5. zero/negative quantity
    bad = dict(BASE_ENTRY, quantity="0", client_order_id="aura-test-05327-005")
    rc, result = run_cli(bad)
    expect("5a. zero quantity fails closed", rc == 1 and result["status"] == "FAIL_CLOSED")

    bad = dict(BASE_ENTRY, quantity="-0.01", client_order_id="aura-test-05327-005b")
    rc, result = run_cli(bad)
    expect("5b. negative quantity fails closed", rc == 1 and result["status"] == "FAIL_CLOSED")

    # 6. wrong exchange
    bad = dict(BASE_ENTRY, exchange="ALPACA", client_order_id="aura-test-05327-006")
    rc, result = run_cli(bad)
    expect("6. wrong exchange fails closed", rc == 1 and result["status"] == "FAIL_CLOSED")
    expect("6. wrong exchange reason", "WRONG_EXECUTION_EXCHANGE" in result["error"])

    # 7. live execution not authorized
    bad = dict(BASE_ENTRY, live_execution_authorized=False, client_order_id="aura-test-05327-007")
    rc, result = run_cli(bad)
    expect("7. live_execution_authorized=False fails closed",
           rc == 1 and result["status"] == "FAIL_CLOSED")
    expect("7. reason names live authorization", "LIVE_EXECUTION_NOT_AUTHORIZED" in result["error"])

    # 8. kill switch enabled
    bad = dict(BASE_ENTRY, kill_switch=True, client_order_id="aura-test-05327-008")
    rc, result = run_cli(bad)
    expect("8. kill switch blocks", rc == 1 and result["status"] == "FAIL_CLOSED")
    expect("8. kill switch reason", "KILL_SWITCH_ACTIVE" in result["error"])

    # 9. missing client order id
    bad = dict(BASE_ENTRY)
    bad.pop("client_order_id")
    rc, result = run_cli(bad)
    expect("9a. missing client_order_id fails closed", rc == 1 and result["status"] == "FAIL_CLOSED")

    bad = dict(BASE_ENTRY, client_order_id="", )
    rc, result = run_cli(bad)
    expect("9b. empty client_order_id fails closed", rc == 1 and result["status"] == "FAIL_CLOSED")

    # extra: --submit-live without the env var double-gate still fails closed
    rc, result = run_cli(dict(BASE_ENTRY, client_order_id="aura-test-05327-009c"), "--submit-live")
    expect("extra. --submit-live without AURA_MEXC_LIVE_ORDERS_ENABLED fails closed",
           rc == 1 and result["status"] == "FAIL_CLOSED"
           and "LIVE_ORDER_SUBMISSION_NOT_ENABLED" in result["error"])

    # extra: entry (reduce_only=False) missing leverage fails closed
    bad = dict(BASE_ENTRY, client_order_id="aura-test-05327-009d")
    bad.pop("leverage")
    rc, result = run_cli(bad)
    expect("extra. entry without leverage fails closed",
           rc == 1 and result["status"] == "FAIL_CLOSED" and "INVALID_LEVERAGE" in result["error"])

    # extra: LIMIT order type rejected (out of scope per module docstring)
    bad = dict(BASE_ENTRY, order_type="LIMIT", client_order_id="aura-test-05327-009e")
    rc, result = run_cli(bad)
    expect("extra. LIMIT order_type fails closed",
           rc == 1 and result["status"] == "FAIL_CLOSED" and "INVALID_ORDER_TYPE" in result["error"])


# --------------------------------------------------------------------- #
# Direct unit tests (scenarios 10-14): submission + outcome classification
# --------------------------------------------------------------------- #

def test_duplicate_claim() -> None:
    """10. A second submit() for the same client_order_id is rejected
    before any second create_order attempt — the claim is never released,
    even though the first call already succeeded."""
    with tempfile.TemporaryDirectory() as d:
        claims_dir = Path(d) / "claims"
        spec = MOD.validate_spec(dict(BASE_ENTRY, client_order_id="aura-test-05327-010"))

        fake1 = FakeExchange()
        result1 = MOD.submit(spec, claims_dir, exchange=fake1)
        expect("10a. first submission for a fresh client_order_id succeeds",
               result1["status"] == "SUBMITTED")

        fake2 = FakeExchange()
        result2 = MOD.submit(spec, claims_dir, exchange=fake2)
        expect("10b. second submission for the SAME client_order_id is rejected as duplicate",
               result2["status"] == "DUPLICATE_CLAIM_REJECTED")
        expect("10c. the duplicate attempt never reached create_order",
               len(fake2.create_order_calls) == 0)


def test_mexc_rejection() -> None:
    """11. An explicit, unambiguous MEXC refusal (ccxt.InvalidOrder) maps
    to REJECTED, never EXECUTION_UNCERTAIN or a silent success."""
    with tempfile.TemporaryDirectory() as d:
        claims_dir = Path(d) / "claims"
        spec = MOD.validate_spec(dict(BASE_ENTRY, client_order_id="aura-test-05327-011"))
        fake = FakeExchange(create_order_exception=ccxt.InvalidOrder("mexc: invalid order"))
        result = MOD.submit(spec, claims_dir, exchange=fake)
        expect("11a. an explicit exchange refusal classifies as REJECTED",
               result["status"] == "REJECTED")
        expect("11b. rejection names the ccxt exception type",
               result["reason"] == "InvalidOrder")


def test_timeout_is_uncertain() -> None:
    """12. A timeout/connectivity failure NEVER maps to REJECTED or a
    silent success — it must be EXECUTION_UNCERTAIN, per the
    Implementation Specification ss6.1."""
    with tempfile.TemporaryDirectory() as d:
        claims_dir = Path(d) / "claims"
        spec = MOD.validate_spec(dict(BASE_ENTRY, client_order_id="aura-test-05327-012"))
        fake = FakeExchange(create_order_exception=ccxt.RequestTimeout("mexc: timed out"))
        result = MOD.submit(spec, claims_dir, exchange=fake)
        expect("12a. a timeout classifies as EXECUTION_UNCERTAIN, never REJECTED or SUBMITTED",
               result["status"] == "EXECUTION_UNCERTAIN")

        # also confirm the claim was NOT released by the uncertain outcome —
        # a second attempt for the same id must still be rejected as a
        # duplicate, exactly as the module docstring requires.
        fake2 = FakeExchange()
        result2 = MOD.submit(spec, claims_dir, exchange=fake2)
        expect("12b. a claim is never released after EXECUTION_UNCERTAIN — retry is rejected",
               result2["status"] == "DUPLICATE_CLAIM_REJECTED")


def test_successful_entry() -> None:
    """13. A successful market entry: leverage is set first, then a
    market order with no reduceOnly param, and the outcome is SUBMITTED
    with the broker's order id captured."""
    with tempfile.TemporaryDirectory() as d:
        claims_dir = Path(d) / "claims"
        spec = MOD.validate_spec(dict(BASE_ENTRY, client_order_id="aura-test-05327-013"))
        fake = FakeExchange(create_order_result={"id": "mexc-entry-1", "status": "closed"})
        result = MOD.submit(spec, claims_dir, exchange=fake)

        expect("13a. successful entry submits", result["status"] == "SUBMITTED")
        expect("13b. entry sets leverage exactly once, before the order",
               fake.set_leverage_calls == [(3, "BTC/USDT:USDT")])
        expect("13c. entry order carries no reduceOnly param",
               fake.create_order_calls[0][4] == {})
        expect("13d. broker order id captured", result["mexc_order_id"] == "mexc-entry-1")


def test_successful_reduce_only_exit() -> None:
    """14. A successful reduce-only exit: leverage is NEVER set (matches
    live_bot.py's place_exit_order(), which never calls set_leverage),
    and the order carries reduceOnly=True."""
    with tempfile.TemporaryDirectory() as d:
        claims_dir = Path(d) / "claims"
        exit_spec = dict(BASE_ENTRY, side="SELL", reduce_only=True,
                          client_order_id="aura-test-05327-014")
        exit_spec.pop("leverage", None)
        spec = MOD.validate_spec(exit_spec)

        fake = FakeExchange(create_order_result={"id": "mexc-exit-1", "status": "closed"})
        result = MOD.submit(spec, claims_dir, exchange=fake)

        expect("14a. successful reduce-only exit submits", result["status"] == "SUBMITTED")
        expect("14b. exit never calls set_leverage", fake.set_leverage_calls == [])
        expect("14c. exit order carries reduceOnly=True",
               fake.create_order_calls[0][4] == {"reduceOnly": True})
        expect("14d. broker order id captured", result["mexc_order_id"] == "mexc-exit-1")


def main() -> int:
    test_cli_contract()
    test_duplicate_claim()
    test_mexc_rejection()
    test_timeout_is_uncertain()
    test_successful_entry()
    test_successful_reduce_only_exit()
    print("AURA v0.5.3.27 CONTRACT: ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
