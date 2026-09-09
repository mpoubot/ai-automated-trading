#!/usr/bin/env python3
"""Contract tests for AURA v0.5.3.22 Alpaca PAPER adapter."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT / "aura_v05322_alpaca_paper_execution_adapter.py"

BASE = {
    "execution_spec_version": "1.0",
    "exchange": "ALPACA",
    "account_mode": "PAPER",
    "live_execution": False,
    "kill_switch": False,
    "execution_authorized": True,
    "paper_execution_authorized": True,
    "symbol": "BTC/USD",
    "side": "BUY",
    "quantity": "0.01",
    "order_type": "MARKET",
    "time_in_force": "GTC",
    "client_order_id": "aura-test-05322-001",
}


def run(spec: dict, *extra: str) -> tuple[int, dict]:
    with tempfile.TemporaryDirectory() as d:
        inp = Path(d) / "spec.json"
        out = Path(d) / "out.json"
        inp.write_text(json.dumps(spec), encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, str(ADAPTER), "--input", str(inp), "--output", str(out), *extra],
            cwd=ROOT, text=True, capture_output=True,
        )
        return proc.returncode, json.loads(out.read_text(encoding="utf-8"))


def expect(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)
    print(f"PASS: {name}")


def main() -> int:
    rc, result = run(BASE)
    expect("valid spec validates without submission", rc == 0 and result["status"] == "VALIDATED_NO_SUBMISSION")
    expect("validation never claims live", result["live"] is False)
    expect("validation never claims broker submission", "broker_order_id" not in result)

    bad = dict(BASE, account_mode="LIVE")
    rc, result = run(bad)
    expect("LIVE account is rejected", rc != 0 and result["status"] == "FAIL_CLOSED")

    bad = dict(BASE, kill_switch=True)
    rc, result = run(bad)
    expect("kill switch blocks", rc != 0 and result["status"] == "FAIL_CLOSED")

    bad = dict(BASE, quantity=None)
    rc, result = run(bad)
    expect("missing quantity blocks", rc != 0 and result["status"] == "FAIL_CLOSED")

    bad = dict(BASE, client_order_id="")
    rc, result = run(bad)
    expect("missing idempotency key blocks", rc != 0 and result["status"] == "FAIL_CLOSED")

    bad = dict(BASE, live_execution=True)
    rc, result = run(bad)
    expect("live_execution=true blocks", rc != 0 and result["status"] == "FAIL_CLOSED")

    # All-coin-universe: any well-formed USD-quoted crypto pair validates,
    # not just the original fixed BTC/USD, ETH/USD pair -- this is the
    # actual order-submission allowlist, so it is tested directly here.
    good = dict(BASE, symbol="AVAX/USD", client_order_id="aura-test-05322-002")
    rc, result = run(good)
    expect("a discovered symbol beyond BTC/ETH validates", rc == 0 and result["status"] == "VALIDATED_NO_SUBMISSION")
    expect("validated symbol echoed back", result["symbol"] == "AVAX/USD")

    bad = dict(BASE, symbol="NOT-A-SYMBOL")
    rc, result = run(bad)
    expect("a malformed symbol still fails closed", rc != 0 and result["status"] == "FAIL_CLOSED")

    bad = dict(BASE, symbol="BTC/EUR")
    rc, result = run(bad)
    expect("a non-USD-quoted pair still fails closed", rc != 0 and result["status"] == "FAIL_CLOSED")

    print("AURA v0.5.3.22 CONTRACT: 11/11 PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
