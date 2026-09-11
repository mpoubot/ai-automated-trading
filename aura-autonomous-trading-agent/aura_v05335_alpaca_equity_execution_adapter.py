#!/usr/bin/env python3
"""
AURA v0.5.3.35 -- Alpaca Equity / ETF Execution Adapter

Milestone 2 of the approved Alpaca-equity execution floor
(.34 -> .35 -> .36 -> .37 -> .38, per Martin's 2026-09-11 GO messages).
This is a REAL, NEW adapter for stocks and ETFs on Alpaca -- not an
adaptation of v0.5.3.22, which the capability audit already established
only supports Alpaca's crypto-pair notation (`^[A-Z0-9]{2,10}/USD$`) and
structurally cannot accept a real ticker like "AAPL".

============================================================================
0. Pre-code inspection (done before writing anything below, per explicit
   instruction) -- alpaca-py 0.44.0, the version pinned in requirements.txt,
   directly imported and introspected this session, not assumed:
============================================================================

- `alpaca.trading.enums.PositionIntent` is REAL and exists in the installed
  SDK: BUY_TO_OPEN / BUY_TO_CLOSE / SELL_TO_OPEN / SELL_TO_CLOSE.
  `MarketOrderRequest`/`LimitOrderRequest` both genuinely accept a
  `position_intent` field. This is the exact mechanism that resolves
  Martin's "CRITICAL POSITION SEMANTICS" requirement honestly: Alpaca's
  own API already distinguishes "sell to close a long" from "sell to open
  a short" as a first-class field, separate from plain `side`. This
  adapter uses it -- it does not infer position intent from `side` alone,
  anywhere.
- `alpaca.trading.models.Asset` genuinely has exactly the fields Martin
  asked for: `tradable`, `shortable`, `easy_to_borrow`, `fractionable`,
  plus `marginable`, `asset_class`, `exchange`, `status`, `min_order_size`,
  `min_trade_increment`, `price_increment`. `TradingClient.get_asset()`
  and `.get_all_assets()` are the real, existing methods that return it
  (mirrors `.21`'s own already-proven use of the Assets API, generalized
  from crypto to equities).
- `alpaca.trading.enums.AssetClass` is `{US_EQUITY, US_OPTION, CRYPTO,
  CRYPTO_PERP}` -- Alpaca's own SDK has **no separate ETF asset class**.
  Both stocks and ETFs are `US_EQUITY` at the SDK level; AURA's own
  STOCK/ETF distinction (from `.34`) is an AURA-side classification, not
  something Alpaca's API itself reports. Recorded here so this isn't
  silently assumed later.
- `TradingClient.submit_order()` / `.get_order_by_client_id()` are the
  same two calls `.22` already uses -- reused the same way, in a
  completely separate module.

============================================================================
1. Layering -- deterministic construction/validation kept structurally
   separate from network-capable submission, per explicit instruction
============================================================================

    canonical spec (.33, already built)
            |
            v
    validate_equity_spec()          <- pure, no network call
            |
            v
    build_order_request_spec()      <- pure, deterministic, JSON-serializable
            |
            v
    to_alpaca_order_request()       <- pure, builds the real SDK object
            |
            v
    submit()                        <- the ONLY function that touches the
                                        network; paper-only; gated by two
                                        independent switches, off by default

`fetch_asset_metadata()` is the one other network-capable function --
a READ-ONLY metadata lookup (`TradingClient.get_asset()`), never a
submission call. This is disclosed precisely, not left to inference: see
`NETWORK_CAPABLE_FUNCTIONS` at the bottom of this file, and the
"real code, no live call" test that walks the module's own source to
confirm no OTHER function references the trading client.

============================================================================
2. Instrument resolution via .34 -- always re-derived from REAL asset
   data, never accepted as an untrusted black box
============================================================================

This adapter does not accept a caller-supplied `.34` InstrumentMetadata
record for the SHORT-safety decision. It always derives its own, fresh,
directly from the `alpaca_asset` data it was given for THIS call (real, in
production; mocked, in every test) -- the same "never trust a caller-
supplied safety assertion alone, always independently recompute" discipline
`.31` already established for MEXC (its own docstring: "a caller-supplied
safety_state is only ever used as an assertion to be checked against a
fresh recomputation, never as trusted input"). A caller cannot short-
circuit the shortability gate by handing this adapter a pre-built,
already-confirmed-shortable metadata record while the real Alpaca data
says otherwise -- the record is always rebuilt from `alpaca_asset` right
here, right before the decision that needs it.

============================================================================
3. Shortability translation -- disclosed as a design choice, not a proven
   Alpaca requirement (Martin's own instruction: "easy_to_borrow should be
   treated according to the actual Alpaca requirement discovered during
   implementation" -- this sandbox has no live network access to verify
   Alpaca's own documentation or a live account against, so this is stated
   honestly as UNVERIFIED, per this project's CLAIMED/PROVEN/UNVERIFIED
   discipline, not presented as confirmed)
============================================================================

    shortable is True  AND easy_to_borrow is True   -> "SHORTABLE"
    shortable is False                              -> "NOT_SHORTABLE"
    anything else (shortable True but easy_to_borrow
      not True, either field missing/None, or an
      unreadable asset lookup)                      -> "UNKNOWN"

This is the CONSERVATIVE reading: hard-to-borrow names (shortable=True,
easy_to_borrow=False) are treated as UNKNOWN rather than SHORTABLE, so
`.34`'s BORROW_UNKNOWN fail-closed path applies to them too, rather than
assuming a broker's "technically shortable but constrained" flag is
equivalent to an uncomplicated short. **UNVERIFIED -- requires checking
Alpaca's actual published documentation or live account behavior before
this is promoted from "reasonable, disclosed design choice" to "confirmed
correct."** Flagged explicitly in the implementation report, not buried
here.

============================================================================
4. What this module deliberately does NOT do
============================================================================

No authorization (`.36`'s job). No replay protection (`.37`'s job). No
Supervisor (`.38`'s job). No orchestrator. No modification to `.19`, `.22`,
`.33`, `.34`, or the MEXC `.27`-`.32` spine -- all read-only inputs to this
module, verified untouched (git diff empty) after this file was written.
No live execution -- `TradingClient(..., paper=True)` is mandatory in
`submit()`, exactly like `.22`'s own invariant, and there is no live
endpoint selection anywhere in this file. Real submission additionally
requires BOTH `--submit-paper` on the CLI AND
`AURA_ALPACA_EQUITY_PAPER_ORDERS_ENABLED=true` in the environment -- a
NAME DELIBERATELY DISTINCT from `.22`/`.26`'s existing
`AURA_ALPACA_PAPER_ORDERS_ENABLED`, so enabling the existing crypto-paper
gate can never accidentally enable equity submission, or vice versa.

No real MEXC credentials anywhere in this file (out of scope entirely).
"""
from __future__ import annotations

import argparse
import json
import os
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, PositionIntent, TimeInForce
from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest

VERSION = "AURA v0.5.3.35"
ENGINE = "ALPACA_EQUITY_EXECUTION_ADAPTER"
SCHEMA_VERSION = "1.0"

ROOT = Path(__file__).resolve().parent


def fail(message: str) -> None:
    raise RuntimeError(message)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------- #
# Dynamic imports of v0.5.3.33 / v0.5.3.34 -- same _load_*_module()
# pattern already established by .30/.31 (read directly from their
# source before writing this file). Neither module is modified; both are
# used strictly as data/vocabulary sources.
# --------------------------------------------------------------------- #

def _load_canonical_spec_module():
    try:
        import aura_v05333_canonical_execution_specification as mod
        return mod
    except ImportError:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "aura_v05333_canonical_execution_specification",
            ROOT / "aura_v05333_canonical_execution_specification.py",
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return mod


def _load_instrument_metadata_module():
    try:
        import aura_v05334_asset_instrument_metadata as mod
        return mod
    except ImportError:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "aura_v05334_asset_instrument_metadata",
            ROOT / "aura_v05334_asset_instrument_metadata.py",
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return mod


# --------------------------------------------------------------------- #
# Direction -> (OrderSide, PositionIntent) -- the explicit, structural
# mapping that keeps LONG/SHORT open/close distinct. Reuses .29/.33/.34's
# own canonical DIRECTIONS enum values as keys.
# --------------------------------------------------------------------- #

DIRECTION_TO_SIDE_POSITION_INTENT: dict[str, tuple[OrderSide, PositionIntent]] = {
    "OPEN_LONG": (OrderSide.BUY, PositionIntent.BUY_TO_OPEN),
    "CLOSE_LONG": (OrderSide.SELL, PositionIntent.SELL_TO_CLOSE),
    "OPEN_SHORT": (OrderSide.SELL, PositionIntent.SELL_TO_OPEN),
    "CLOSE_SHORT": (OrderSide.BUY, PositionIntent.BUY_TO_CLOSE),
}

# Plain-string mirror (for the deterministic, JSON-serializable
# build_order_request_spec() output -- never leaks an SDK enum object
# into a plain dict).
DIRECTION_TO_SIDE_POSITION_INTENT_STR: dict[str, tuple[str, str]] = {
    d: (side.value, intent.value) for d, (side, intent) in DIRECTION_TO_SIDE_POSITION_INTENT.items()
}


def _decimal(value: Any, field: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        fail(f"INVALID_{field.upper()}")
    if not number.is_finite() or number <= 0:
        fail(f"INVALID_{field.upper()}")
    return number


def _is_fractional(quantity: Decimal) -> bool:
    return quantity != quantity.to_integral_value()


# --------------------------------------------------------------------- #
# Shortability translation -- see module docstring §3. UNVERIFIED against
# live Alpaca documentation/account behaviour; disclosed as such.
# --------------------------------------------------------------------- #

def alpaca_asset_to_shortability_status(shortable: Any, easy_to_borrow: Any) -> str:
    if shortable is True and easy_to_borrow is True:
        return "SHORTABLE"
    if shortable is False:
        return "NOT_SHORTABLE"
    return "UNKNOWN"


# --------------------------------------------------------------------- #
# NETWORK-CAPABLE: read-only asset metadata lookup. Never a submission
# call. See module docstring §1.
# --------------------------------------------------------------------- #

def fetch_asset_metadata(client: Any, symbol: str) -> dict[str, Any]:
    """Wraps TradingClient.get_asset(symbol) -- a READ-ONLY Alpaca Assets
    API call, exactly like .21's already-proven discover_crypto_universe()
    but for one named equity/ETF symbol instead of the crypto universe.
    `client` is any object exposing .get_asset(symbol) -- a real
    TradingClient in production, a fake test double in every test in this
    milestone (Martin's explicit instruction: mocked/fake responses only,
    no live Alpaca order required to prove this adapter works).

    Extracts exactly the fields this adapter needs and nothing else.
    Absent/None fields are passed through honestly, never fabricated."""
    asset = client.get_asset(symbol)

    def g(name: str) -> Any:
        value = getattr(asset, name, None)
        # alpaca-py enum fields (asset_class, exchange, status) carry a
        # .value; plain fields (bool/str/Decimal) do not.
        return getattr(value, "value", value)

    return {
        "symbol": g("symbol"),
        "asset_class": g("asset_class"),
        "exchange": g("exchange"),
        "status": g("status"),
        "tradable": getattr(asset, "tradable", None),
        "shortable": getattr(asset, "shortable", None),
        "easy_to_borrow": getattr(asset, "easy_to_borrow", None),
        "fractionable": getattr(asset, "fractionable", None),
        "marginable": getattr(asset, "marginable", None),
        "min_order_size": g("min_order_size"),
        "min_trade_increment": g("min_trade_increment"),
        "price_increment": g("price_increment"),
    }


# --------------------------------------------------------------------- #
# PURE: validation. No network call. Fails closed via fail() on the
# first violation -- never returns a partially-valid result.
# --------------------------------------------------------------------- #

def validate_equity_spec(
    spec: dict[str, Any],
    alpaca_asset: dict[str, Any],
) -> dict[str, Any]:
    """Validates a .33 canonical execution spec for STOCK/ETF/ALPACA
    submission, given REAL (or, in tests, faithfully mocked) Alpaca asset
    data for the spec's own symbol. Always rebuilds its own fresh .34
    InstrumentMetadata record from `alpaca_asset` -- see module docstring
    §2. Returns a normalized dict on success; raises RuntimeError (via
    fail()) on the first violation."""
    canon = _load_canonical_spec_module()
    meta = _load_instrument_metadata_module()

    if not isinstance(spec, dict):
        fail("EXECUTION_SPEC_NOT_OBJECT")
    ok, errors = canon.verify_canonical_specification(spec)
    if not ok:
        fail(f"MALFORMED_CANONICAL_SPEC:{','.join(errors)}")

    asset_class = spec.get("asset_class")
    if asset_class not in ("STOCK", "ETF"):
        fail(f"UNSUPPORTED_ASSET_CLASS_FOR_EQUITY_ADAPTER:{asset_class}")
    if spec.get("venue") != "ALPACA":
        fail(f"VENUE_NOT_ALPACA:{spec.get('venue')}")

    direction = spec.get("direction")
    if direction not in DIRECTION_TO_SIDE_POSITION_INTENT:
        fail(f"INVALID_DIRECTION:{direction!r}")

    if spec.get("order_type") not in ("MARKET", "LIMIT"):
        fail(f"INVALID_ORDER_TYPE:{spec.get('order_type')}")
    if spec.get("order_type") == "LIMIT" and spec.get("limit_price") is None:
        fail("LIMIT_ORDER_MISSING_LIMIT_PRICE")

    symbol = spec.get("symbol")
    if not isinstance(symbol, str) or not symbol:
        fail("MISSING_SYMBOL")
    if alpaca_asset.get("symbol") not in (None, symbol):
        fail(f"ASSET_METADATA_SYMBOL_MISMATCH:{alpaca_asset.get('symbol')}!={symbol}")

    client_order_id = spec.get("client_order_id")
    if not isinstance(client_order_id, str) or not client_order_id.strip():
        fail("MISSING_CLIENT_ORDER_ID")

    quantity = _decimal(spec.get("quantity"), "quantity")

    # --- tradability -------------------------------------------------- #
    if alpaca_asset.get("tradable") is not True:
        fail(f"ASSET_NOT_TRADABLE:{symbol}")

    # --- fractional quantity ------------------------------------------ #
    if _is_fractional(quantity) and alpaca_asset.get("fractionable") is not True:
        fail(f"FRACTIONAL_QUANTITY_NOT_SUPPORTED:{symbol}")

    # --- instrument metadata, always freshly derived from alpaca_asset  #
    shortability_status = alpaca_asset_to_shortability_status(
        alpaca_asset.get("shortable"), alpaca_asset.get("easy_to_borrow"),
    )
    instrument_metadata = meta.build_instrument_metadata(
        instrument_type=asset_class,
        contract_class=None,
        symbol=symbol,
        venue="ALPACA",
        shortability_status=shortability_status,
    )

    capability = meta.classify_direction_capability(
        asset_class, None, direction, shortability_status=shortability_status,
    )
    if direction == "OPEN_SHORT" and capability != meta.BORROW_CONFIRMED:
        fail(f"SHORT_NOT_PERMITTED:{capability}")
    if capability == meta.NOT_APPLICABLE:
        fail(f"DIRECTION_NOT_APPLICABLE_TO_INSTRUMENT:{direction}/{asset_class}")

    return {
        "spec": spec,
        "alpaca_asset": alpaca_asset,
        "instrument_metadata": instrument_metadata,
        "shortability_status": shortability_status,
        "direction_capability": capability,
        "symbol": symbol,
        "quantity": quantity,
        "direction": direction,
    }


# --------------------------------------------------------------------- #
# PURE: deterministic order-request construction, separate from
# submission (Martin's explicit instruction). JSON-serializable -- no SDK
# object, no network call.
# --------------------------------------------------------------------- #

def build_order_request_spec(spec: dict[str, Any], alpaca_asset: dict[str, Any]) -> dict[str, Any]:
    validated = validate_equity_spec(spec, alpaca_asset)
    direction = validated["direction"]
    side_str, position_intent_str = DIRECTION_TO_SIDE_POSITION_INTENT_STR[direction]

    order_spec: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "engine": ENGINE,
        "agent_version": VERSION,
        "symbol": validated["symbol"],
        "quantity": str(validated["quantity"]),
        "side": side_str,
        "position_intent": position_intent_str,
        "direction": direction,
        "order_type": spec["order_type"],
        "time_in_force": spec.get("time_in_force") or "GTC",
        "client_order_id": spec["client_order_id"],
        "shortability_status": validated["shortability_status"],
        "direction_capability": validated["direction_capability"],
        "built_at": now(),
    }
    if spec.get("order_type") == "LIMIT":
        order_spec["limit_price"] = spec["limit_price"]
    return order_spec


TIF_MAP = {"GTC": TimeInForce.GTC, "IOC": TimeInForce.IOC, "DAY": TimeInForce.DAY}


def to_alpaca_order_request(order_spec: dict[str, Any]):
    """Pure: builds the real alpaca-py SDK request object from an
    already-validated, deterministic order_spec. Still no network call --
    constructing a request object does not submit it."""
    side = OrderSide(order_spec["side"])
    position_intent = PositionIntent(order_spec["position_intent"])
    tif = TIF_MAP.get(order_spec["time_in_force"])
    if tif is None:
        fail(f"INVALID_TIME_IN_FORCE:{order_spec['time_in_force']}")

    common = dict(
        symbol=order_spec["symbol"],
        qty=order_spec["quantity"],
        side=side,
        time_in_force=tif,
        client_order_id=order_spec["client_order_id"],
        position_intent=position_intent,
    )
    if order_spec["order_type"] == "MARKET":
        return MarketOrderRequest(**common)
    limit_price = _decimal(order_spec.get("limit_price"), "limit_price")
    return LimitOrderRequest(**common, limit_price=limit_price)


# --------------------------------------------------------------------- #
# NETWORK-CAPABLE: the only submission path. Paper-only, mandatory.
# --------------------------------------------------------------------- #

def submit(order_spec: dict[str, Any], client: Any) -> dict[str, Any]:
    """The ONLY function in this module that submits an order. `client`
    must already be constructed with paper=True by the caller -- this
    function does not construct a TradingClient itself (main() does, and
    main() hardcodes paper=True, mirroring .22's own invariant exactly).

    Idempotency preflight via get_order_by_client_id(), same pattern as
    .22, before ever calling submit_order()."""
    try:
        existing = client.get_order_by_client_id(order_spec["client_order_id"])
    except Exception:
        existing = None
    if existing is not None:
        return {
            "adapter_version": VERSION,
            "status": "ALREADY_EXISTS",
            "broker_order_id": str(getattr(existing, "id", None)),
            "client_order_id": order_spec["client_order_id"],
            "broker_status": str(getattr(existing, "status", None)),
            "observed_at": now(),
            "paper": True,
            "live": False,
        }

    request = to_alpaca_order_request(order_spec)
    order = client.submit_order(order_data=request)
    return {
        "adapter_version": VERSION,
        "status": "SUBMITTED",
        "broker_order_id": str(getattr(order, "id", None)),
        "client_order_id": order_spec["client_order_id"],
        "broker_status": str(getattr(order, "status", None)),
        "observed_at": now(),
        "paper": True,
        "live": False,
    }


# --------------------------------------------------------------------- #
# CLI -- mirrors .22's exact validate-by-default posture and two-switch
# submission discipline, with a DISTINCT env var name (see module
# docstring §4).
# --------------------------------------------------------------------- #

EQUITY_PAPER_ORDERS_ENV_VAR = "AURA_ALPACA_EQUITY_PAPER_ORDERS_ENABLED"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="Canonical .33 execution spec JSON")
    parser.add_argument("--asset-input", type=Path, help="Pre-fetched/mocked Alpaca asset metadata JSON")
    parser.add_argument("--fetch-live-asset-metadata", action="store_true",
                         help="Fetch REAL Alpaca asset metadata via a read-only Assets API call "
                              "(TradingClient.get_asset) instead of --asset-input. Read-only; never submits.")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--submit-paper", action="store_true",
                         help="Actually submit to Alpaca PAPER; otherwise validate/build only.")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "adapter_version": VERSION,
        "observed_at": now(),
        "paper": True,
        "live": False,
        "status": "BLOCKED",
    }
    try:
        spec = json.loads(args.input.read_text(encoding="utf-8"))

        if args.fetch_live_asset_metadata:
            key = os.getenv("ALPACA_PAPER_API_KEY")
            secret = os.getenv("ALPACA_PAPER_SECRET_KEY")
            if not key or not secret:
                fail("MISSING_ALPACA_PAPER_CREDENTIALS_FOR_METADATA_FETCH")
            metadata_client = TradingClient(key, secret, paper=True)
            alpaca_asset = fetch_asset_metadata(metadata_client, spec.get("symbol"))
        elif args.asset_input:
            alpaca_asset = json.loads(args.asset_input.read_text(encoding="utf-8"))
        else:
            fail("MUST_SUPPLY_EITHER_--asset-input_OR_--fetch-live-asset-metadata")

        order_spec = build_order_request_spec(spec, alpaca_asset)
        result.update({"status": "VALIDATED", "order_spec": order_spec})

        if not args.submit_paper:
            result["status"] = "VALIDATED_NO_SUBMISSION"
        elif os.getenv(EQUITY_PAPER_ORDERS_ENV_VAR, "false").lower() != "true":
            fail(f"EQUITY_PAPER_ORDER_SUBMISSION_NOT_ENABLED:{EQUITY_PAPER_ORDERS_ENV_VAR}")
        else:
            key = os.getenv("ALPACA_PAPER_API_KEY")
            secret = os.getenv("ALPACA_PAPER_SECRET_KEY")
            if not key or not secret:
                fail("MISSING_ALPACA_PAPER_CREDENTIALS")
            client = TradingClient(key, secret, paper=True)
            result.update(submit(order_spec, client))

        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2, default=str) + "\n", encoding="utf-8")
        print(json.dumps(result, indent=2, default=str))
        return 0
    except Exception as exc:
        result.update({"status": "FAIL_CLOSED", "error": f"{type(exc).__name__}: {exc}"})
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2, default=str) + "\n", encoding="utf-8")
        print(f"FAIL-CLOSED: {result['error']}")
        return 1


# Disclosure list, per the requirement to confirm exactly which functions
# in this module are network-capable. Checked directly by a dedicated
# test that this list is complete (no other module-level function
# references `client.` or `TradingClient(`).
NETWORK_CAPABLE_FUNCTIONS = frozenset({"fetch_asset_metadata", "submit", "main"})


if __name__ == "__main__":
    raise SystemExit(main())
