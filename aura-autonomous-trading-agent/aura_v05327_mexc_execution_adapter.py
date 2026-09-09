#!/usr/bin/env python3
"""AURA v0.5.3.27 — MEXC perpetual/swap execution adapter.

Deliberately mirrors the canonical v0.5.3.22 Alpaca PAPER adapter's shape
(validate_spec -> build_request -> submit -> classify outcome), but does
NOT copy its Alpaca-specific assumptions. Differences, and why:

    - account_mode is "LIVE", not "PAPER". MEXC's futures API has no
      sandbox/test environment (confirmed in the connection-guide
      extraction, 2026-09-09, citing MEXC's own API material) -- there is
      no broker-side paper mode to target the way Alpaca's adapter does.
      "LIVE" is simply the only account_mode MEXC ever has; it is NOT an
      instruction that this adapter is authorized to trade with real
      money by default -- see the safety invariants below.

    - No get_order_by_client_id()-style idempotency preflight. The MEXC
      runtime extraction (2026-09-09) confirmed no such call exists
      anywhere in mexc_bot/, and it remains UNKNOWN whether MEXC's API /
      ccxt's MEXC implementation supports one at all. Unlike v0.5.3.22,
      which gets a broker-assisted second line of defense against
      duplicate submission for free, this adapter's own claim step (see
      Replay-Protected Consumption below) is the ONLY line of defense --
      not a backstop behind a broker check.

    - The proven mexc_bot/core/data_fetcher.py::build_exchange() /
      mexc_bot/live_bot.py create_order()/fetch_balance()/set_leverage()
      calls (2026-09-09 extraction) are the exact calls this adapter
      wraps -- copied here rather than imported cross-folder from
      mexc_bot/, to keep this file self-contained and independently
      testable without a filesystem-path dependency on a sibling
      top-level folder. The construction is byte-for-byte the same
      exchange setup already proven live: ccxt.mexc({apiKey, secret,
      enableRateLimit: True, options: {defaultType: "swap"}}).

    - client_order_id is generated and tracked by AURA for its OWN
      replay-protection claim (below). It is deliberately NOT passed to
      MEXC's create_order() call: the proven runtime's create_order()
      call carries no client-order-id parameter at all, and the correct
      MEXC/ccxt parameter name (if MEXC's API even accepts one) is
      UNKNOWN -- not stated in the connection guide, not yet tested live.
      Passing an unverified parameter name would be a guess this file
      does not make. This is an explicit, named gap for direct API
      testing (T01-class work) to resolve, not a silent omission.

Scope, deliberately narrow (v0.5.3.27, Phase 1 of the MEXC adapter
milestone) -- matches Martin's explicit list:
    - MEXC perpetual/swap only (market_type == "swap")
    - market orders only (order_type == "MARKET"; LIMIT is rejected, not
      silently coerced -- matches the proven runtime's own scope)
    - entry (reduce_only == False, sets leverage first, matching
      live_bot.py's place_entry_order()) and reduce-only exit
      (reduce_only == True, params={"reduceOnly": True}, matching
      live_bot.py's place_exit_order()) -- reduce_only is a required,
      explicit field on every spec, never inferred from side or context
    - a deterministic client_order_id supplied by AURA (v0.5.3.23's
      build_client_order_id(), unchanged) -- this adapter does not
      generate one itself
    - AURA-side replay protection (this file's own atomic claim step)
    - fail-closed validation on every field
    - three, and only three, submission outcomes: SUBMITTED,
      EXECUTION_UNCERTAIN, REJECTED -- never anything else
    - no strategy logic, no signal generation, no independent risk
      decisions. This file does not decide whether to trade; it only
      decides whether an already-authorized instruction is well-formed
      enough to attempt, and then reports exactly what happened.

Safety invariants (mirrors v0.5.3.22's list, adapted):
    - kill_switch must be false and execution_authorized AND
      live_execution_authorized must both be true in the supplied
      specification -- there is no separate "paper_execution_authorized"
      check here, because there is no paper mode to authorize.
    - quantity, leverage (entry only), symbol, side, order_type, and
      reduce_only must all be supplied by the upstream deterministic
      execution specification -- never computed here.
    - client_order_id is required, for AURA's own replay-protection
      claim, whether or not MEXC itself ever sees it.
    - A claim, once granted, is NEVER released by this adapter -- not on
      success, not on REJECTED, not on EXECUTION_UNCERTAIN. A second
      submit() call for the same client_order_id always sees
      DUPLICATE_CLAIM_REJECTED, even after a timeout. This is the
      specific mechanism the Implementation Specification's ss3.2
      requires: a claim's fate is resolved only by Reconciliation reading
      MEXC's actual state, never by this adapter re-trying.
    - Default invocation (no --submit-live) validates the specification
      and performs NO network call of any kind -- not even a claim
      attempt. Real submission additionally requires both --submit-live
      AND AURA_MEXC_LIVE_ORDERS_ENABLED=true (double-gated, matching the
      --submit-paper / AURA_ALPACA_PAPER_ORDERS_ENABLED convention
      already used by v0.5.3.22 and mexc_bot/live_bot.py's own DRY_RUN
      gate).
"""
from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

VERSION = "AURA v0.5.3.27"

SUPPORTED_MARKET_TYPE = "swap"

# MEXC USDT-margined perpetual symbols only, CCXT style (confirmed proven
# shape: BTC/USDT:USDT, per the connection-guide/runtime extraction,
# 2026-09-09). Deliberately narrower than v0.5.3.22's any-quote-currency
# pattern -- this adapter's proven scope is USDT-margined swap only.
SUPPORTED_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9]{2,15}/USDT:USDT$")

ALLOWED_SIDES = {"BUY", "SELL"}
ALLOWED_ORDER_TYPES = {"MARKET"}  # LIMIT is explicitly out of scope -- see module docstring

DEFAULT_CLAIMS_DIR = Path(r"regime_output\mexc_execution_adapter\claims")


# --------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------- #

def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def fail(message: str) -> None:
    raise RuntimeError(message)


def decimal_positive(value: Any, field: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        fail(f"INVALID_{field.upper()}")
        raise  # unreachable, keeps type-checkers happy
    if not number.is_finite() or number <= 0:
        fail(f"INVALID_{field.upper()}")
    return number


def load_spec(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        fail("EXECUTION_SPEC_NOT_OBJECT")
    return payload


# --------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------- #

def validate_spec(spec: dict) -> dict:
    """Fail-closed validation of every field this adapter needs. Returns
    a copy of spec with normalized/derived fields added (quantity_decimal,
    leverage_int where applicable). Raises RuntimeError (via fail()) on
    the first violation -- never returns a partially-valid spec."""
    if spec.get("execution_spec_version") != "1.0":
        fail("UNSUPPORTED_EXECUTION_SPEC_VERSION")
    if spec.get("exchange") != "MEXC":
        fail("WRONG_EXECUTION_EXCHANGE")
    if spec.get("account_mode") != "LIVE":
        fail("UNSUPPORTED_ACCOUNT_MODE")
    if spec.get("market_type") != SUPPORTED_MARKET_TYPE:
        fail("UNSUPPORTED_MARKET_TYPE")

    if spec.get("kill_switch") is True:
        fail("KILL_SWITCH_ACTIVE")
    if spec.get("execution_authorized") is not True:
        fail("EXECUTION_NOT_AUTHORIZED")
    if spec.get("live_execution_authorized") is not True:
        fail("LIVE_EXECUTION_NOT_AUTHORIZED")

    symbol = spec.get("symbol")
    if not isinstance(symbol, str) or not SUPPORTED_SYMBOL_PATTERN.match(symbol):
        fail(f"UNSUPPORTED_SYMBOL:{symbol}")

    side = spec.get("side")
    if side not in ALLOWED_SIDES:
        fail("INVALID_SIDE")

    order_type = spec.get("order_type")
    if order_type not in ALLOWED_ORDER_TYPES:
        fail("INVALID_ORDER_TYPE")

    reduce_only = spec.get("reduce_only")
    if not isinstance(reduce_only, bool):
        fail("MISSING_OR_INVALID_REDUCE_ONLY")

    quantity = decimal_positive(spec.get("quantity"), "quantity")

    leverage_int: int | None = None
    if not reduce_only:
        # Entry: leverage is required, matching live_bot.py's
        # place_entry_order(), which only ever calls set_leverage() on
        # entry, never on a reduce-only exit.
        leverage = spec.get("leverage")
        if not isinstance(leverage, int) or isinstance(leverage, bool) or leverage < 1:
            fail("INVALID_LEVERAGE")
        leverage_int = leverage

    client_order_id = spec.get("client_order_id")
    if not isinstance(client_order_id, str) or not client_order_id.strip():
        fail("MISSING_CLIENT_ORDER_ID")
    if len(client_order_id) > 48:
        fail("CLIENT_ORDER_ID_TOO_LONG")
    # Restrict to a safe filename charset: this value becomes a claim
    # filename (see claim_client_order_id()) as well as a MEXC-adjacent
    # identifier -- never accept anything that could escape a directory
    # or collide across meaningfully-different ids.
    if not re.fullmatch(r"[A-Za-z0-9._-]+", client_order_id):
        fail("INVALID_CLIENT_ORDER_ID_CHARACTERS")

    expires_at = spec.get("expires_at")
    if expires_at is not None:
        if not isinstance(expires_at, str):
            fail("INVALID_EXPIRES_AT")
        try:
            expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except ValueError:
            fail("INVALID_EXPIRES_AT")
            raise
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        if expiry <= datetime.now(timezone.utc):
            fail("EXPIRED_AUTHORIZATION")

    result = dict(spec)
    result["quantity_decimal"] = str(quantity)
    if leverage_int is not None:
        result["leverage_int"] = leverage_int
    return result


# --------------------------------------------------------------------- #
# request construction
# --------------------------------------------------------------------- #

SIDE_MAP = {"BUY": "buy", "SELL": "sell"}


def build_request(spec: dict) -> dict:
    """Explicit, closed mapping from a validated spec to the exact
    positional/keyword arguments the proven create_order() call shape
    needs (mexc_bot/live_bot.py place_entry_order()/place_exit_order(),
    2026-09-09 extraction). No fallback branch: build_request() must
    independently refuse to turn an unexpected side into a buy/sell order
    even if it is ever called on unvalidated input, mirroring v0.5.3.22's
    own build_request()."""
    side = SIDE_MAP.get(spec["side"])
    if side is None:
        fail("INVALID_SIDE")

    params: dict[str, Any] = {"reduceOnly": True} if spec["reduce_only"] else {}

    return {
        "symbol": spec["symbol"],
        "type": "market",
        "side": side,
        "amount": float(spec["quantity_decimal"]),
        "params": params,
    }


# --------------------------------------------------------------------- #
# exchange construction -- adapted from mexc_bot/core/data_fetcher.py, not imported
# --------------------------------------------------------------------- #

def build_exchange(api_key: str = "", api_secret: str = ""):
    """Identical construction to the proven mexc_bot/core/data_fetcher.py
    ::build_exchange() (2026-09-09 extraction) -- copied rather than
    cross-folder-imported so this file has no filesystem dependency on
    mexc_bot/'s location. If mexc_bot/'s own build_exchange() ever
    changes, this copy must be updated to match; it is not automatically
    kept in sync."""
    import ccxt  # imported here, not at module load time, so this module
    # can be imported/tested (validate_spec, build_request, claim logic,
    # outcome classification against an injected fake exchange) in an
    # environment without ccxt actually being exercised against MEXC.

    exchange_class = getattr(ccxt, "mexc")
    return exchange_class({
        "apiKey": api_key,
        "secret": api_secret,
        "enableRateLimit": True,
        "options": {"defaultType": SUPPORTED_MARKET_TYPE},
    })


# --------------------------------------------------------------------- #
# Replay-Protected Consumption -- AURA's own claim, no broker backstop
# --------------------------------------------------------------------- #

def claim_client_order_id(claims_dir: Path, client_order_id: str) -> bool:
    """Atomic claim: True if THIS call created the claim (granted), False
    if it already existed (already claimed -- duplicate). Uses
    os.O_CREAT | os.O_EXCL, which is atomic at the OS level even across
    concurrent processes -- the filesystem equivalent of the
    "INSERT ... ON CONFLICT DO NOTHING" primitive the Implementation
    Specification (ss3.1) and the BABIL audit (Day 2B ss1.3) both call
    for, not a read-then-write check that could race.

    A granted claim is NEVER deleted by this module, under any outcome
    (see module docstring) -- there is deliberately no release_claim()
    function here."""
    claims_dir.mkdir(parents=True, exist_ok=True)
    claim_path = claims_dir / f"{client_order_id}.claimed"
    try:
        fd = os.open(str(claim_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps({"client_order_id": client_order_id, "claimed_at": now()}))
    return True


# --------------------------------------------------------------------- #
# submission + outcome classification
# --------------------------------------------------------------------- #

def classify_exception(exc: Exception) -> tuple[str, str]:
    """Maps a raised exception to exactly one of REJECTED /
    EXECUTION_UNCERTAIN, per the Implementation Specification ss6.1: only
    an explicit, unambiguous exchange refusal maps to REJECTED. A
    timeout, a connection loss, a not-more-specifically-classified
    exchange error, or anything else not recognized here ALWAYS maps to
    EXECUTION_UNCERTAIN -- never guessed into REJECTED or treated as a
    success. Returns (outcome, reason_code)."""
    try:
        import ccxt
    except ImportError:
        return "EXECUTION_UNCERTAIN", f"UNCLASSIFIED_EXCEPTION:{type(exc).__name__}"

    # Explicit, unambiguous refusals ONLY -- MEXC told us, before any
    # order existed, that this exact request is invalid/unaffordable/
    # forbidden. Safe to say no order was created.
    if isinstance(exc, (
        ccxt.InvalidOrder,
        ccxt.InsufficientFunds,
        ccxt.BadSymbol,
        ccxt.BadRequest,
        ccxt.PermissionDenied,
        ccxt.AuthenticationError,
    )):
        return "REJECTED", type(exc).__name__

    # Timeout / connectivity / rate-limit / maintenance -- the order may
    # or may not have reached MEXC. Never REJECTED, never assumed FILLED.
    if isinstance(exc, ccxt.NetworkError):
        return "EXECUTION_UNCERTAIN", type(exc).__name__

    # Any other ccxt.ExchangeError, or anything else entirely: not
    # confidently classifiable as an unambiguous refusal, so it is
    # conservatively treated the same as a timeout.
    return "EXECUTION_UNCERTAIN", type(exc).__name__


def submit(spec: dict, claims_dir: Path, exchange: Any = None) -> dict:
    """Claims the client_order_id, then attempts the MEXC submission.
    `exchange` is injectable so this function is directly unit-testable
    against a fake exchange object with no real network call -- the CLI
    entrypoint (main()) is the only caller that constructs a real one via
    build_exchange()."""
    client_order_id = spec["client_order_id"]

    granted = claim_client_order_id(claims_dir, client_order_id)
    if not granted:
        return {
            "adapter_version": VERSION,
            "status": "DUPLICATE_CLAIM_REJECTED",
            "client_order_id": client_order_id,
            "observed_at": now(),
            "live": True,
            "reason": "CLIENT_ORDER_ID_ALREADY_CLAIMED",
        }

    if exchange is None:
        key = os.getenv("MEXC_API_KEY", "")
        secret = os.getenv("MEXC_API_SECRET", "")
        if not key or not secret:
            fail("MISSING_MEXC_CREDENTIALS")
        exchange = build_exchange(key, secret)

    request = build_request(spec)

    # Leverage is set only on entry (reduce_only == False), matching
    # live_bot.py's place_entry_order() exactly. No order has been
    # attempted yet at this point, so a leverage-set failure carries no
    # duplicate-submission risk -- but the claim still stays granted
    # (per module docstring: a claim is never released), and the
    # failure is still classified rather than silently swallowed.
    if not spec["reduce_only"]:
        try:
            exchange.set_leverage(spec["leverage_int"], spec["symbol"])
        except Exception as exc:  # noqa: BLE001 -- deliberately broad, classified below
            outcome, reason = classify_exception(exc)
            return {
                "adapter_version": VERSION,
                "status": outcome,
                "client_order_id": client_order_id,
                "symbol": spec["symbol"],
                "side": spec["side"],
                "reduce_only": spec["reduce_only"],
                "observed_at": now(),
                "live": True,
                "stage": "SET_LEVERAGE",
                "reason": reason,
            }

    try:
        order = exchange.create_order(
            request["symbol"], request["type"], request["side"],
            request["amount"], params=request["params"],
        )
    except Exception as exc:  # noqa: BLE001 -- deliberately broad, classified below
        outcome, reason = classify_exception(exc)
        return {
            "adapter_version": VERSION,
            "status": outcome,
            "client_order_id": client_order_id,
            "symbol": spec["symbol"],
            "side": spec["side"],
            "reduce_only": spec["reduce_only"],
            "observed_at": now(),
            "live": True,
            "stage": "CREATE_ORDER",
            "reason": reason,
        }

    return {
        "adapter_version": VERSION,
        "status": "SUBMITTED",
        "client_order_id": client_order_id,
        "symbol": spec["symbol"],
        "side": spec["side"],
        "reduce_only": spec["reduce_only"],
        "mexc_order_id": str(order.get("id")) if isinstance(order, dict) else None,
        "raw_response": order,
        "observed_at": now(),
        "live": True,
    }


# --------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------- #

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--claims-dir", type=Path, default=DEFAULT_CLAIMS_DIR)
    parser.add_argument("--submit-live", action="store_true",
                         help="Actually submit to MEXC LIVE; otherwise validate only.")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "adapter_version": VERSION,
        "observed_at": now(),
        "live": True,
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
            "reduce_only": spec["reduce_only"],
            "client_order_id": spec["client_order_id"],
        })
        if spec.get("leverage_int") is not None:
            result["leverage"] = spec["leverage_int"]

        if not args.submit_live:
            result["status"] = "VALIDATED_NO_SUBMISSION"
        elif os.getenv("AURA_MEXC_LIVE_ORDERS_ENABLED", "false").lower() != "true":
            fail("LIVE_ORDER_SUBMISSION_NOT_ENABLED")
        else:
            result.update(submit(spec, args.claims_dir))

        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2, default=str) + "\n", encoding="utf-8")
        print(json.dumps(result, indent=2, default=str))
        # Exit code reflects whether this program ran correctly, not
        # whether the trade succeeded -- REJECTED/EXECUTION_UNCERTAIN/
        # DUPLICATE_CLAIM_REJECTED are all legitimate, well-formed
        # outcomes of a successful validate-and-attempt, same convention
        # as v0.5.3.22's own rc==0 VALIDATED_NO_SUBMISSION path. Only
        # FAIL_CLOSED (below) is a program-level failure.
        return 0
    except Exception as exc:
        result.update({"status": "FAIL_CLOSED", "error": f"{type(exc).__name__}: {exc}"})
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2, default=str) + "\n", encoding="utf-8")
        print(f"FAIL-CLOSED: {result['error']}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
