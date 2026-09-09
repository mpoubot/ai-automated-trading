#!/usr/bin/env python3
"""
AURA v0.5.3.25 — Position Sizing (Phase 4 of the agreed 5-phase plan)

Purpose
--------
Produces the --sizing-config JSON that v0.5.3.23 (Execution Specification
Builder) requires but never computes itself -- v0.5.3.23's own docstring
is explicit that no file anywhere in the v0.5.3.12-.20 chain computes a
position size, and that quantity must be supplied externally. This module
is that external, explicit, versioned source, computing size as a FIXED
fraction of LIVE Alpaca paper account equity, gated by a hard cap on
concurrent open positions -- both values Martin's own explicit choice
(10% of equity per position, max 5 concurrent positions), never inferred.

Consumes:
    1. Live Alpaca PAPER account state -- equity and currently-open
       positions -- fetched read-only via alpaca-py's TradingClient
       (paper=True is mandatory, same invariant v0.5.3.22 enforces for
       order submission). THIS IS A DELIBERATE EXCEPTION to this chain's
       usual "consume only one locked upstream JSON" pattern: no file
       anywhere in v0.5.3.12-.24 tracks live account equity or the
       broker's own view of open positions, and fabricating either from
       downstream JSON state would be exactly the kind of invented
       authority this whole chain refuses to allow elsewhere. Alpaca's
       own account state is the only honest source for "how much money do
       I actually have" and "what does the broker say is actually open."
       Read-only: get_account() and get_all_positions() only. No order is
       ever placed by this module.
    2. The same already-fetched 1H closed-bars CSV v0.5.3.12/v0.5.3.24
       already read (data/prospective_alpaca/alpaca_1h_closed_bars.csv),
       for each symbol's latest close -- to convert a dollar amount into
       a quantity. Reused via v0.5.3.12's own load_input(), not
       reimplemented, matching this module's price data to exactly what
       the rest of the live cycle already sees.

This layer deliberately does NOT:
- place, cancel, or modify any order -- read-only Alpaca calls only;
- decide direction, signal, or risk eligibility (that remains
  v0.5.3.13/.14/.23's job -- this module sizes a symbol whether or not it
  will actually be allowed to trade this cycle; v0.5.3.23's own empty
  VALIDATED_LONG_ENTRY_REGIME_LABELS / VALIDATED_SHORT_ENTRY_REGIME_LABELS
  remain the actual gate on whether any order is ever built);
- change the 10%-of-equity / 5-position-cap constants without an
  explicit CLI override -- both are Martin's own reviewed numbers, never
  silently tuned by this module;
- size a symbol that already has an open position at Alpaca. Nothing in
  this chain models adding to / averaging into an existing position, so
  a symbol already open is excluded from sizing entirely
  (ALREADY_OPEN_POSITION_FOR_SYMBOL) rather than being given a second,
  additive quantity;
- fabricate equity, a price, or a position when the live account call or
  the bars file doesn't have one -- every such gap fails that symbol
  closed (excluded from the sizing config), never a fallback/default
  value.

Concurrency cap
-----------------
open_position_count is read directly from Alpaca's get_all_positions()
-- the broker's own count, not a reconstruction from this repo's
reconciliation/ledger chain (which is itself downstream evidence ABOUT
what Alpaca did, not a live view of it). When that count is already at or
above --max-concurrent-positions (default 5), EVERY symbol not already
open is excluded (MAX_CONCURRENT_POSITIONS_REACHED) -- this module
cannot know in advance which symbol(s) v0.5.3.13/.14 will actually try to
enter this cycle, so it doesn't guess; it simply refuses to offer a size
for anything once the cap is reached.

Default inputs:
    Live Alpaca PAPER account (ALPACA_PAPER_API_KEY/SECRET_KEY, falling
    back to ALPACA_API_KEY/SECRET_KEY, loaded from a local .env if
    present -- same convention as v0.5.3.21). Missing credentials fail
    closed (exit 2) before any network call, same as v0.5.3.21.
    data/prospective_alpaca/alpaca_1h_closed_bars.csv

Default outputs:
    regime_output/position_sizing/position_sizing.json   (full decision
        record: per-symbol reasoning, guardrails, hash chain)
    regime_output/position_sizing/sizing_config.json      (the bare
        {"SYMBOL": quantity, ...} object, ready to pass straight to
        v0.5.3.23's --sizing-config)
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent


def _load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# Reuse v0.5.3.12's own bar-loading logic (not reimplemented) so this
# module's price data always matches exactly what the rest of the live
# cycle sees from the same file.
MSE = _load_module("aura_v05312_market_state_engine", "aura_v05312_market_state_engine.py")

VERSION = "AURA v0.5.3.25"

DEFAULT_BARS_INPUT = Path(r"data\prospective_alpaca\alpaca_1h_closed_bars.csv")
DEFAULT_OUTPUT = Path(r"regime_output\position_sizing\position_sizing.json")
DEFAULT_SIZING_CONFIG_OUTPUT = Path(r"regime_output\position_sizing\sizing_config.json")

# REQUIRED_SYMBOLS across the rest of this chain -- widen with --symbols
# exactly like v0.5.3.21/.22/.23 already support for the full discovered
# universe.
SYMBOLS = ("BTC/USD", "ETH/USD")

# Martin's explicit, reviewed choices -- never silently tuned by this
# module. Override only via --fraction-of-equity / --max-concurrent-
# positions, both logged into every output for traceability.
POSITION_SIZE_FRACTION_OF_EQUITY = 0.10
MAX_CONCURRENT_POSITIONS = 5


def stable_json(obj: Any) -> str:
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_credentials() -> tuple[str, str] | None:
    key = os.getenv("ALPACA_PAPER_API_KEY") or os.getenv("ALPACA_API_KEY")
    secret = os.getenv("ALPACA_PAPER_SECRET_KEY") or os.getenv("ALPACA_SECRET_KEY")
    if not key or not secret:
        return None
    return key, secret


def fetch_account_state(client: Any) -> dict[str, Any]:
    """The only function in this module that makes a live network call.
    Kept to a thin read-only wrapper around exactly two Alpaca calls so
    everything else (compute_sizing) can be unit-tested with a plain
    dict, no live credentials or mock broker required."""
    account = client.get_account()
    equity = float(account.equity)
    positions = client.get_all_positions()
    open_symbols = sorted({str(p.symbol) for p in positions})
    return {
        "equity": equity,
        "open_symbols": open_symbols,
        "open_position_count": len(open_symbols),
    }


def latest_close_by_symbol(bars) -> dict[str, float]:
    """bars: the DataFrame returned by MSE.load_input(). Returns each
    symbol's most recent valid close, or omits the symbol entirely if it
    has none -- never a stale/fabricated fallback price."""
    result: dict[str, float] = {}
    clean = bars.dropna(subset=["timestamp", "close", "symbol"])
    for symbol, g in clean.groupby("symbol"):
        g = g.sort_values("timestamp")
        close = g.iloc[-1]["close"]
        try:
            close = float(close)
        except (TypeError, ValueError):
            continue
        if close > 0:
            result[symbol] = close
    return result


def compute_sizing(
    *,
    equity: Any,
    open_symbols: set[str],
    open_position_count: int,
    latest_close: dict[str, float],
    symbols: list[str],
    fraction_of_equity: float = POSITION_SIZE_FRACTION_OF_EQUITY,
    max_concurrent_positions: int = MAX_CONCURRENT_POSITIONS,
) -> dict[str, dict[str, Any]]:
    """Pure function of already-fetched account state + prices -- no I/O
    here, so this is fully unit-testable without live credentials or a
    real broker connection. Returns one decision per requested symbol;
    only the eligible/positive-quantity entries become the sizing_config
    handed to v0.5.3.23."""
    decisions: dict[str, dict[str, Any]] = {}
    equity_valid = isinstance(equity, (int, float)) and not isinstance(equity, bool) and equity > 0
    at_cap = open_position_count >= max_concurrent_positions

    for symbol in symbols:
        if symbol in open_symbols:
            decisions[symbol] = {
                "symbol": symbol,
                "eligible": False,
                "quantity": None,
                "reason": "ALREADY_OPEN_POSITION_FOR_SYMBOL",
            }
            continue

        if at_cap:
            decisions[symbol] = {
                "symbol": symbol,
                "eligible": False,
                "quantity": None,
                "reason": "MAX_CONCURRENT_POSITIONS_REACHED",
            }
            continue

        if not equity_valid:
            decisions[symbol] = {
                "symbol": symbol,
                "eligible": False,
                "quantity": None,
                "reason": "INVALID_EQUITY",
            }
            continue

        close = latest_close.get(symbol)
        if not isinstance(close, (int, float)) or isinstance(close, bool) or close <= 0:
            decisions[symbol] = {
                "symbol": symbol,
                "eligible": False,
                "quantity": None,
                "reason": "NO_BARS_FOR_SYMBOL",
            }
            continue

        dollar_size = equity * fraction_of_equity
        quantity = round(dollar_size / close, 8)
        if quantity <= 0:
            decisions[symbol] = {
                "symbol": symbol,
                "eligible": False,
                "quantity": None,
                "reason": "COMPUTED_QUANTITY_NOT_POSITIVE",
            }
            continue

        decisions[symbol] = {
            "symbol": symbol,
            "eligible": True,
            "quantity": quantity,
            "reason": "SIZED_AT_FRACTION_OF_EQUITY",
            "reference_close": close,
            "dollar_size": round(dollar_size, 2),
        }

    return decisions


def base_result(
    bars_path: Path,
    fraction_of_equity: float,
    max_concurrent_positions: int,
) -> dict[str, Any]:
    return {
        "agent_version": VERSION,
        "engine": "POSITION_SIZING",
        "decision_status": "BLOCKED",
        "generated_from_bars": str(bars_path.resolve()),
        "fraction_of_equity": fraction_of_equity,
        "max_concurrent_positions": max_concurrent_positions,
        "equity": None,
        "open_symbols": [],
        "open_position_count": None,
        "decisions": {},
        "blocked_reasons": [],
        "state_hash": None,
        "state_id": None,
        "guardrails": {
            "live_account_data_fetch": True,
            "market_data_fetch": False,
            "indicator_recalculation": False,
            "signal_recalculation": False,
            "risk_recalculation": False,
            "position_creation": False,
            "exchange_state_mutation": False,
            "orders_allowed": False,
            "order_submission": False,
            "paper_execution": False,
            "live_execution": False,
            "paper_only": True,
            "averaging_into_existing_position": False,
            "fabricated_equity": False,
            "fabricated_position": False,
            "fabricated_price": False,
            "fail_closed": True,
        },
    }


def finalize(result: dict[str, Any]) -> dict[str, Any]:
    canonical = {
        "agent_version": result["agent_version"],
        "engine": result["engine"],
        "decision_status": result["decision_status"],
        "fraction_of_equity": result["fraction_of_equity"],
        "max_concurrent_positions": result["max_concurrent_positions"],
        "equity": result["equity"],
        "open_symbols": result["open_symbols"],
        "open_position_count": result["open_position_count"],
        "decisions": result["decisions"],
        "blocked_reasons": result["blocked_reasons"],
    }
    result["state_hash"] = sha256_text(stable_json(canonical))
    result["state_id"] = f"PS-{result['state_hash'][:24]}"
    return result


def build_sizing(
    account_state: dict[str, Any],
    latest_close: dict[str, float],
    symbols: list[str],
    bars_path: Path,
    fraction_of_equity: float,
    max_concurrent_positions: int,
) -> dict[str, Any]:
    result = base_result(bars_path, fraction_of_equity, max_concurrent_positions)

    equity = account_state.get("equity")
    open_symbols = set(account_state.get("open_symbols", []))
    open_position_count = account_state.get("open_position_count")

    result["equity"] = equity
    result["open_symbols"] = sorted(open_symbols)
    result["open_position_count"] = open_position_count

    if not isinstance(open_position_count, int) or isinstance(open_position_count, bool):
        result["blocked_reasons"] = ["INVALID_OPEN_POSITION_COUNT"]
        return finalize(result)

    result["decision_status"] = "DECIDED"
    result["decisions"] = compute_sizing(
        equity=equity,
        open_symbols=open_symbols,
        open_position_count=open_position_count,
        latest_close=latest_close,
        symbols=symbols,
        fraction_of_equity=fraction_of_equity,
        max_concurrent_positions=max_concurrent_positions,
    )
    return finalize(result)


def sizing_config_from_result(result: dict[str, Any]) -> dict[str, float]:
    """Extracts the bare {"SYMBOL": quantity, ...} object v0.5.3.23's
    --sizing-config expects -- nothing else, since v0.5.3.23's
    load_sizing_config() reads this file as a flat mapping."""
    return {
        symbol: item["quantity"]
        for symbol, item in result["decisions"].items()
        if item.get("eligible") and item.get("quantity")
    }


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False, allow_nan=False)
        f.write("\n")


def print_report(result: dict[str, Any], output_path: Path, sizing_config_path: Path) -> None:
    print("=" * 96)
    print(f"{VERSION} — POSITION SIZING (Phase 4)")
    print("=" * 96)
    print()
    print("MODE                 : PAPER ACCOUNT, READ-ONLY")
    print("ORDERS               : DISABLED")
    print("LIVE EXECUTION       : DISABLED")
    print(f"FRACTION OF EQUITY   : {result['fraction_of_equity']:.1%} per position")
    print(f"MAX CONCURRENT POS.  : {result['max_concurrent_positions']}")
    print()
    print("ACCOUNT STATE")
    print("-" * 96)
    print(f"STATUS               : {result['decision_status']}")
    print(f"EQUITY               : {result['equity']}")
    print(f"OPEN POSITION COUNT  : {result['open_position_count']}")
    print(f"OPEN SYMBOLS         : {result['open_symbols']}")
    print(f"STATE ID             : {result['state_id']}")
    print()

    for symbol, item in sorted(result["decisions"].items()):
        print(symbol)
        print(f"  ELIGIBLE           : {item['eligible']}")
        print(f"  QUANTITY           : {item['quantity']}")
        print(f"  REASON             : {item['reason']}")
        if item.get("reference_close") is not None:
            print(f"  REFERENCE CLOSE    : {item['reference_close']}")
            print(f"  DOLLAR SIZE        : {item['dollar_size']}")
        print()

    if result["blocked_reasons"]:
        print("FAIL-CLOSED REASONS")
        print("-" * 96)
        for reason in result["blocked_reasons"]:
            print(f"  - {reason}")
        print()

    print(f"FULL RECORD OUTPUT   : {output_path.resolve()}")
    print(f"SIZING CONFIG OUTPUT : {sizing_config_path.resolve()}")
    print("=" * 96)


def main() -> int:
    # Fills in only what's missing from the environment -- never
    # shadows a real deployment credential, same convention as v0.5.3.21.
    load_dotenv()

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bars-input", type=Path, default=DEFAULT_BARS_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--sizing-config-output", type=Path, default=DEFAULT_SIZING_CONFIG_OUTPUT)
    parser.add_argument(
        "--symbols",
        type=str,
        default=",".join(SYMBOLS),
        help="Comma-separated symbol list to size (default: the required BTC/USD,ETH/USD pair).",
    )
    parser.add_argument("--fraction-of-equity", type=float, default=POSITION_SIZE_FRACTION_OF_EQUITY)
    parser.add_argument("--max-concurrent-positions", type=int, default=MAX_CONCURRENT_POSITIONS)
    args = parser.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]

    if args.fraction_of_equity <= 0 or args.fraction_of_equity > 1:
        print("FAIL-CLOSED: --fraction-of-equity must be in (0, 1].", file=sys.stderr)
        return 2
    if args.max_concurrent_positions <= 0:
        print("FAIL-CLOSED: --max-concurrent-positions must be positive.", file=sys.stderr)
        return 2

    credentials = load_credentials()
    if credentials is None:
        print("FAIL-CLOSED: Alpaca paper credentials are missing.", file=sys.stderr)
        return 2
    key, secret = credentials

    result = base_result(args.bars_input, args.fraction_of_equity, args.max_concurrent_positions)

    try:
        # Imported here, not at module load time, so this module can be
        # imported/tested (compute_sizing, sizing_config_from_result,
        # etc.) in an environment without alpaca-py's trading client
        # actually being exercised.
        from alpaca.trading.client import TradingClient

        client = TradingClient(key, secret, paper=True)
        account_state = fetch_account_state(client)
    except Exception as exc:
        result["blocked_reasons"] = [f"ALPACA_ACCOUNT_FETCH_FAILED:{type(exc).__name__}"]
        result = finalize(result)
        try:
            write_json(args.output, result)
            write_json(args.sizing_config_output, {})
            print_report(result, args.output, args.sizing_config_output)
        except Exception:
            pass
        print(f"FAIL-CLOSED: {exc}", file=sys.stderr)
        return 1

    try:
        bars = MSE.load_input(args.bars_input)
    except FileNotFoundError as exc:
        result["blocked_reasons"] = [f"MISSING_BARS_INPUT:{exc}"]
        result = finalize(result)
        try:
            write_json(args.output, result)
            write_json(args.sizing_config_output, {})
            print_report(result, args.output, args.sizing_config_output)
        except Exception:
            pass
        print(f"FAIL-CLOSED: {exc}", file=sys.stderr)
        return 1

    latest_close = latest_close_by_symbol(bars)

    result = build_sizing(
        account_state,
        latest_close,
        symbols,
        args.bars_input,
        args.fraction_of_equity,
        args.max_concurrent_positions,
    )
    sizing_config = sizing_config_from_result(result)

    write_json(args.output, result)
    write_json(args.sizing_config_output, sizing_config)
    print_report(result, args.output, args.sizing_config_output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
