#!/usr/bin/env python3
"""AURA v0.5.3.22 — isolated Alpaca PAPER execution adapter. [SUPERSEDED]

*** SUPERSEDED as of 2026-09-02 ***
This was an earlier draft of the v0.5.3.22 adapter. It has been superseded
by aura_v05322_alpaca_paper_execution_adapter.py, which is the canonical
v0.5.3.22 adapter going forward (execution_spec_version-gated schema,
LIMIT order support, idempotency preflight against the broker, and the
matching tests/aura_v05322_alpaca_paper_execution_adapter_test.py).

This file and its tests (tests/test_aura_v05322_alpaca_paper_execution.py)
are kept as-is, still passing, for history/reference. Do not build new
integrations against this file's schema (ALPACA_API_KEY/ALPACA_API_SECRET,
"kill_switch_active", no execution_spec_version field) — use the adapter
above instead. It is not deleted here; that is left for a follow-up once
nothing references it.

This module is intentionally downstream of AURA's deterministic execution
specification. It never derives a signal, risk decision, position size, or
order parameters. It can submit only to Alpaca's PAPER environment.

Default mode validates the execution specification and performs NO submission.
Submission requires both --submit-paper and AURA_ALPACA_PAPER_ORDERS_ENABLED=true.
"""
from __future__ import annotations
import argparse, hashlib, json, os
from pathlib import Path
from typing import Any

VERSION = "AURA v0.5.3.22"
ALLOWED_SYMBOLS = {"BTC/USD", "ETH/USD"}
ALLOWED_SIDES = {"BUY", "SELL"}
ALLOWED_ORDER_TYPES = {"MARKET"}
ALLOWED_TIF = {"GTC"}


def stable_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def load(path: Path) -> dict[str, Any]:
    obj = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(obj, dict):
        raise ValueError("execution specification must be a JSON object")
    return obj


def validate(spec: dict[str, Any]) -> list[str]:
    e: list[str] = []
    if spec.get("exchange") != "ALPACA": e.append("WRONG_EXCHANGE")
    if spec.get("account_mode") != "PAPER": e.append("NOT_PAPER_ACCOUNT")
    if spec.get("live_execution") is not False: e.append("LIVE_EXECUTION_NOT_FALSE")
    if spec.get("execution_authorized") is not True: e.append("EXECUTION_NOT_AUTHORIZED")
    if spec.get("paper_execution_authorized") is not True: e.append("PAPER_EXECUTION_NOT_AUTHORIZED")
    if spec.get("kill_switch_active") is True: e.append("KILL_SWITCH_ACTIVE")
    if spec.get("symbol") not in ALLOWED_SYMBOLS: e.append("INVALID_SYMBOL")
    if spec.get("side") not in ALLOWED_SIDES: e.append("INVALID_SIDE")
    qty = spec.get("quantity")
    if not isinstance(qty, (int, float)) or isinstance(qty, bool) or qty <= 0: e.append("INVALID_QUANTITY")
    if spec.get("order_type") not in ALLOWED_ORDER_TYPES: e.append("INVALID_ORDER_TYPE")
    if spec.get("time_in_force") not in ALLOWED_TIF: e.append("INVALID_TIME_IN_FORCE")
    cid = spec.get("client_order_id")
    if not isinstance(cid, str) or not cid.strip(): e.append("MISSING_CLIENT_ORDER_ID")
    return sorted(set(e))


def result(spec: dict[str, Any], errors: list[str]) -> dict[str, Any]:
    return {
        "agent_version": VERSION,
        "engine": "ALPACA_PAPER_EXECUTION_ADAPTER",
        "exchange": "ALPACA",
        "account_mode": "PAPER",
        "live_execution": False,
        "validation_passed": not errors,
        "submission_requested": False,
        "order_submitted": False,
        "order_id": None,
        "client_order_id": spec.get("client_order_id"),
        "blocked_reasons": errors,
        "execution_spec_hash": hashlib.sha256(stable_json(spec).encode()).hexdigest(),
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--input", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--submit-paper", action="store_true")
    args = p.parse_args()
    spec = load(Path(args.input))
    errors = validate(spec)
    out = result(spec, errors)
    out["submission_requested"] = bool(args.submit_paper)

    if not errors and args.submit_paper:
        if os.getenv("AURA_ALPACA_PAPER_ORDERS_ENABLED") != "true":
            errors = ["PAPER_SUBMISSION_FEATURE_FLAG_NOT_ENABLED"]
            out["blocked_reasons"] = errors
            out["validation_passed"] = False
        else:
            from alpaca.trading.client import TradingClient
            from alpaca.trading.enums import OrderSide, TimeInForce
            from alpaca.trading.requests import MarketOrderRequest
            key = os.getenv("ALPACA_API_KEY")
            secret = os.getenv("ALPACA_API_SECRET")
            # Explicit, closed mapping — no fallback branch. validate() has
            # already rejected any spec["side"] outside ALLOWED_SIDES, but a
            # future change to validate() must not be able to turn an
            # unexpected value into a live SELL (or BUY) order here.
            side_map = {"BUY": OrderSide.BUY, "SELL": OrderSide.SELL}
            if not key or not secret:
                errors = ["MISSING_ALPACA_PAPER_CREDENTIALS"]
                out["blocked_reasons"] = errors
                out["validation_passed"] = False
            elif spec.get("side") not in side_map:
                errors = ["INVALID_SIDE"]
                out["blocked_reasons"] = errors
                out["validation_passed"] = False
            else:
                client = TradingClient(key, secret, paper=True)
                order = client.submit_order(order_data=MarketOrderRequest(
                    symbol=spec["symbol"],
                    qty=str(spec["quantity"]),
                    side=side_map[spec["side"]],
                    time_in_force=TimeInForce.GTC,
                    client_order_id=spec["client_order_id"],
                ))
                out["order_submitted"] = True
                out["order_id"] = str(order.id)
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(out, indent=2))
    return 0 if not errors else 1

if __name__ == "__main__":
    raise SystemExit(main())
