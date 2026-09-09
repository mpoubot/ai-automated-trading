#!/usr/bin/env python3
"""AURA v0.5.3.22 — isolated Alpaca PAPER execution adapter.

This adapter is deliberately downstream of the validated AURA chain. It accepts
an explicit execution specification and may submit ONLY to Alpaca's paper
endpoint. It never calculates a signal, risk decision, position size, or fill.

Safety invariants:
- TradingClient(..., paper=True) is mandatory.
- No live endpoint selection exists in this module.
- Submission requires --submit-paper AND AURA_ALPACA_PAPER_ORDERS_ENABLED=true.
- kill_switch must be false and execution_authorized/paper_execution_authorized
  must both be true in the supplied specification.
- quantity must be supplied by the upstream deterministic execution specification.
- client_order_id is required for idempotency.
- Duplicate client_order_id is rejected before submission when possible.
- The adapter never claims a fill or position; it records broker observation.

Default invocation is validation-only and performs NO network order submission.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce, OrderType
from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest


VERSION = "AURA v0.5.3.22"

# Any well-formed USD-quoted crypto pair is accepted here -- the same
# discovered-universe shape already validated in
# aura_v05321_alpaca_market_data.py's discover_crypto_universe() (its own
# _USD_PAIR_RE). This adapter is deliberately isolated and does not call
# Alpaca's Assets API itself; it validates the SHAPE of the symbol, and
# Alpaca's own order-submission API remains the final authority on whether
# a given symbol is actually tradable -- exactly as it already is today for
# BTC/USD and ETH/USD. This deliberately replaces the old fixed
# {"BTC/USD", "ETH/USD"} allowlist so the full discovered coin universe can
# reach submission; it is not a change to any other safety gate below
# (kill switch, execution_authorized, quantity, client_order_id).
SUPPORTED_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9]{2,10}/USD$")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def fail(message: str) -> None:
    raise RuntimeError(message)


def decimal_positive(value, field: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        fail(f"INVALID_{field.upper()}")
    if not number.is_finite() or number <= 0:
        fail(f"INVALID_{field.upper()}")
    return number


def load_spec(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        fail("EXECUTION_SPEC_NOT_OBJECT")
    return payload


def validate_spec(spec: dict) -> dict:
    if spec.get("execution_spec_version") != "1.0":
        fail("UNSUPPORTED_EXECUTION_SPEC_VERSION")
    if spec.get("exchange") != "ALPACA":
        fail("WRONG_EXECUTION_EXCHANGE")
    if spec.get("account_mode") != "PAPER":
        fail("NON_PAPER_ACCOUNT_MODE")
    if spec.get("live_execution") is not False:
        fail("LIVE_EXECUTION_NOT_FALSE")
    if spec.get("kill_switch") is True:
        fail("KILL_SWITCH_ACTIVE")
    if spec.get("execution_authorized") is not True:
        fail("EXECUTION_NOT_AUTHORIZED")
    if spec.get("paper_execution_authorized") is not True:
        fail("PAPER_EXECUTION_NOT_AUTHORIZED")

    symbol = spec.get("symbol")
    if not isinstance(symbol, str) or not SUPPORTED_SYMBOL_PATTERN.match(symbol):
        fail(f"UNSUPPORTED_SYMBOL:{symbol}")

    side = spec.get("side")
    if side not in {"BUY", "SELL"}:
        fail("INVALID_SIDE")

    quantity = decimal_positive(spec.get("quantity"), "quantity")
    order_type = spec.get("order_type")
    if order_type not in {"MARKET", "LIMIT"}:
        fail("INVALID_ORDER_TYPE")

    tif = spec.get("time_in_force")
    if tif not in {"GTC", "IOC", "DAY"}:
        fail("INVALID_TIME_IN_FORCE")

    client_order_id = spec.get("client_order_id")
    if not isinstance(client_order_id, str) or not client_order_id.strip():
        fail("MISSING_CLIENT_ORDER_ID")
    if len(client_order_id) > 48:
        fail("CLIENT_ORDER_ID_TOO_LONG")

    result = dict(spec)
    result["quantity_decimal"] = str(quantity)
    return result


SIDE_MAP = {"BUY": OrderSide.BUY, "SELL": OrderSide.SELL}


def build_request(spec: dict):
    # Explicit, closed mapping — no fallback branch. validate_spec() has
    # already fail-closed on any spec["side"] outside {"BUY", "SELL"}, but
    # build_request() must independently refuse to turn an unexpected value
    # into a SELL (or BUY) order if it is ever called on unvalidated input.
    side = SIDE_MAP.get(spec["side"])
    if side is None:
        fail("INVALID_SIDE")
    tif = {
        "GTC": TimeInForce.GTC,
        "IOC": TimeInForce.IOC,
        "DAY": TimeInForce.DAY,
    }[spec["time_in_force"]]
    common = dict(
        symbol=spec["symbol"],
        qty=spec["quantity_decimal"],
        side=side,
        time_in_force=tif,
        client_order_id=spec["client_order_id"],
    )
    if spec["order_type"] == "MARKET":
        return MarketOrderRequest(**common)
    limit_price = decimal_positive(spec.get("limit_price"), "limit_price")
    return LimitOrderRequest(**common, limit_price=limit_price)


def submit(spec: dict) -> dict:
    key = os.getenv("ALPACA_PAPER_API_KEY")
    secret = os.getenv("ALPACA_PAPER_SECRET_KEY")
    if not key or not secret:
        fail("MISSING_ALPACA_PAPER_CREDENTIALS")

    client = TradingClient(key, secret, paper=True)

    # Idempotency preflight: if the broker already knows this client order ID,
    # return the existing broker observation instead of creating a duplicate.
    try:
        existing = client.get_order_by_client_id(spec["client_order_id"])
    except Exception:
        existing = None
    if existing is not None:
        return {
            "adapter_version": VERSION,
            "status": "ALREADY_EXISTS",
            "broker_order_id": str(existing.id),
            "client_order_id": spec["client_order_id"],
            "broker_status": str(existing.status),
            "observed_at": now(),
            "paper": True,
            "live": False,
        }

    request = build_request(spec)
    order = client.submit_order(order_data=request)
    return {
        "adapter_version": VERSION,
        "status": "SUBMITTED",
        "broker_order_id": str(order.id),
        "client_order_id": spec["client_order_id"],
        "broker_status": str(order.status),
        "observed_at": now(),
        "paper": True,
        "live": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--submit-paper", action="store_true",
                        help="Actually submit to Alpaca PAPER; otherwise validate only.")
    args = parser.parse_args()

    result = {
        "adapter_version": VERSION,
        "observed_at": now(),
        "paper": True,
        "live": False,
        "status": "BLOCKED",
    }
    try:
        spec = validate_spec(load_spec(args.input))
        result.update({
            "status": "VALIDATED",
            "symbol": spec["symbol"],
            "side": spec["side"],
            "quantity": spec["quantity_decimal"],
            "order_type": spec["order_type"],
            "time_in_force": spec["time_in_force"],
            "client_order_id": spec["client_order_id"],
        })

        if not args.submit_paper:
            result["status"] = "VALIDATED_NO_SUBMISSION"
        elif os.getenv("AURA_ALPACA_PAPER_ORDERS_ENABLED", "false").lower() != "true":
            fail("PAPER_ORDER_SUBMISSION_NOT_ENABLED")
        else:
            result.update(submit(spec))

        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(result, indent=2))
        return 0
    except Exception as exc:
        result.update({"status": "FAIL_CLOSED", "error": f"{type(exc).__name__}: {exc}"})
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print(f"FAIL-CLOSED: {result['error']}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
