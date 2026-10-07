#!/usr/bin/env python3
"""AURA v0.5.3.81 -- Options Cost Model (O7).

WHAT THIS MODULE IS, AND IS DELIBERATELY NOT
------------------------------------------------------------------------
A pure, stateless cost-ESTIMATION function: given a multi-leg options
structure's current per-leg quotes, a quantity, and commission/regulatory
fee rates, it estimates the round-trip (open + close) dollar cost of
trading that structure. It replaces the flat 0.10% round-trip assumption
the equity/crypto research track uses (`cost_pct`/`DEFAULT_COST_BPS`,
see `aura_regime_backtest_v2.py`) -- which doesn't transfer to options,
since premiums and spread width vary enormously by strike/expiry/
liquidity rather than scaling with notional the way equity spreads
roughly do.

It fetches NO data itself, submits NO orders, and is not restricted to
2-leg verticals (unlike O4/O5/O6's v1 scope) -- this is generic
arithmetic over however many legs a structure has, so it's reusable for
single-leg, vertical, or any future wider structure without change.

TWO COST COMPONENTS, COMPUTED SEPARATELY AND SUMMED
------------------------------------------------------------------------
1. SPREAD COST -- Martin's confirmed convention (AskUserQuestion,
   2026-10-07): always assume the worse side of the quote, full width,
   no optimistic fill assumption, matching DELTAX V2's live convention
   ("always price the worse side of the quote") rather than its separate
   backtest-only slippage-fraction sensitivity grid. For ONE leg, the
   round-trip spread cost (open + close, regardless of which side is
   opened first) works out to exactly the quoted spread width once,
   per contract: opening a long leg pays ask (half-spread worse than
   mid), closing it receives bid (half-spread worse than mid again, in
   the other direction) -- net versus trading at mid both times is
   `(ask - mid) + (mid - bid) = ask - bid`. The same arithmetic holds
   for a short leg opened at bid and closed at ask. So spread cost is
   direction-independent: `round_trip_spread_cost_per_contract =
   ask - bid`, computed once per leg, never doubled.

2. COMMISSION + REGULATORY FEES -- Martin's confirmed scope
   (AskUserQuestion, 2026-10-07): REQUIRED caller-supplied config, NO
   built-in defaults. Alpaca charges no options commission of its own,
   but real regulatory pass-through fees apply (OCC clearing fee, ORF,
   FINRA TAF, FINRA CAT) -- confirmed to exist via Alpaca's own fee
   disclosures, but the exact current per-contract rates live in a
   periodically-updated fee-schedule PDF, not something this module
   hardcodes and risks going silently stale. TAF applies to SELLS only;
   ORF/OCC/CAT apply to both sides -- this module keeps that real
   buy/sell asymmetry by requiring SEPARATE buy-side and sell-side
   combined rates, rather than one flat per-contract figure, and makes
   no claim about how those two numbers break down internally. Same
   "never invent a number" discipline `.374`'s required, no-default
   `max_quote_age_seconds` parameter already uses in this build.

No live Alpaca call anywhere in this file. NETWORK_CAPABLE_FUNCTIONS is
empty, same disclosed property every other decision-only module in this
build carries.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

VERSION = "AURA v0.5.3.81"
ENGINE = "OPTIONS_COST_MODEL"

DEFAULT_MULTIPLIER = Decimal("100")  # standard US equity option contract multiplier

REQUIRED_CONFIG_KEYS: frozenset[str] = frozenset({
    "commission_per_contract",
    "regulatory_fee_per_contract_buy",
    "regulatory_fee_per_contract_sell",
})

# Pure cost-estimation module -- no network call anywhere.
NETWORK_CAPABLE_FUNCTIONS: frozenset[str] = frozenset()


def fail(message: str) -> None:
    raise RuntimeError(message)


def _decimal(value: Any, field: str) -> Decimal:
    try:
        d = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        fail(f"INVALID_DECIMAL:{field}={value!r}")
        raise  # pragma: no cover -- fail() always raises
    if not d.is_finite():
        fail(f"INVALID_DECIMAL:{field}={value!r}")
    return d


# ============================================================================
# Config validation -- fails closed (RuntimeError) if any required rate is
# missing or malformed. There is deliberately no DEFAULT_COST_CONFIG with
# real numbers in it (see module docstring, item 2) -- every call must
# supply its own current rates.
# ============================================================================


def validate_cost_config(config: Any) -> dict[str, Decimal]:
    if not isinstance(config, dict):
        fail(f"INVALID_COST_CONFIG:{config!r}")
    missing = REQUIRED_CONFIG_KEYS - set(config.keys())
    if missing:
        fail(f"MISSING_REQUIRED_CONFIG_KEYS:{sorted(missing)!r}")

    validated: dict[str, Decimal] = {}
    for key in REQUIRED_CONFIG_KEYS:
        value = _decimal(config[key], key)
        if value < 0:
            fail(f"NEGATIVE_COST_RATE:{key}={value}")
        validated[key] = value
    return validated


# ============================================================================
# Per-leg quote handling -- same fail-closed vocabulary O2's own
# `data_status` field and O6's `_leg_quote_mid` use: a leg whose quote
# isn't `"OK"` makes that leg's cost estimate unusable, never guessed.
# ============================================================================


def _leg_bid_ask(contract_record: Any) -> tuple[Decimal | None, Decimal | None, str]:
    """`contract_record` is expected to be one entry from O2's
    `fetch_option_chain()["contracts"]` (i.e. has `"data_status"` and
    `"broker_latest_quote"` keys), same shape O6 consumes."""
    if not isinstance(contract_record, dict):
        return None, None, "MISSING_QUOTE_RECORD"
    status = contract_record.get("data_status")
    if status != "OK":
        return None, None, status if isinstance(status, str) else "MISSING_QUOTE_RECORD"
    quote = contract_record.get("broker_latest_quote")
    if not isinstance(quote, dict):
        return None, None, "MISSING_QUOTE_RECORD"
    try:
        bid = Decimal(str(quote.get("bid_price")))
        ask = Decimal(str(quote.get("ask_price")))
    except (InvalidOperation, TypeError, ValueError):
        return None, None, "INVALID_QUOTE"
    if not bid.is_finite() or not ask.is_finite() or bid <= 0 or ask <= 0 or ask < bid:
        return None, None, "INVALID_QUOTE"
    return bid, ask, "OK"


def leg_round_trip_spread_cost(contract_record: Any, *, qty: int, multiplier: Decimal = DEFAULT_MULTIPLIER) -> dict[str, Any]:
    """Pure. Returns `{"spread_cost": Decimal|None, "status": "OK"|<failure>,
    "bid": Decimal|None, "ask": Decimal|None}`. `spread_cost` is the
    round-trip dollar cost of trading `qty` contracts of this ONE leg at
    the full bid-ask width (see module docstring, item 1) -- direction-
    independent, so `side` is not an input here."""
    if not isinstance(qty, int) or isinstance(qty, bool) or qty <= 0:
        fail(f"INVALID_QTY:{qty!r}")
    multiplier_d = _decimal(multiplier, "multiplier")

    bid, ask, status = _leg_bid_ask(contract_record)
    if status != "OK":
        return {"spread_cost": None, "status": status, "bid": bid, "ask": ask}

    spread_cost = (ask - bid) * multiplier_d * qty
    return {"spread_cost": spread_cost, "status": "OK", "bid": bid, "ask": ask}


def leg_round_trip_fee_cost(side: str, *, qty: int, rates: dict[str, Decimal]) -> Decimal:
    """Pure. `side` is the leg's OPENING side ("BUY" or "SELL") -- the
    closing transaction is always the opposite, so a round trip always
    incurs exactly one buy-side and one sell-side commission+regulatory
    charge, regardless of which side opened first. `rates` is the dict
    `validate_cost_config()` returns."""
    if side not in ("BUY", "SELL"):
        fail(f"INVALID_LEG_SIDE:{side!r}")
    if not isinstance(qty, int) or isinstance(qty, bool) or qty <= 0:
        fail(f"INVALID_QTY:{qty!r}")

    commission = rates["commission_per_contract"]
    per_contract = (
        commission + rates["regulatory_fee_per_contract_buy"]
        + commission + rates["regulatory_fee_per_contract_sell"]
    )
    return per_contract * qty


# ============================================================================
# The one entry point -- a full structure's estimated round-trip cost.
# ============================================================================


def estimate_round_trip_cost(
    legs: list[dict[str, Any]],
    leg_quotes: dict[str, Any],
    *,
    qty: int,
    config: dict[str, Any],
    multiplier: Decimal = DEFAULT_MULTIPLIER,
) -> dict[str, Any]:
    """Pure. `legs`: `[{"occ_symbol": str, "side": "BUY"|"SELL"}, ...]` --
    any number of legs, 1 or more (this module is not restricted to
    2-leg verticals, unlike O4/O5/O6's v1 execution scope). `leg_quotes`:
    `{occ_symbol: <one entry from O2's fetch_option_chain()["contracts"]>}`
    for each leg. `qty`: contracts per leg (same convention O4/O6 use --
    a 1:1 ratio across legs; a future wider-ratio structure would need
    its own per-leg qty, out of scope here same as it is for O4/O5/O6).
    `config`: REQUIRED, validated via `validate_cost_config()` -- no
    built-in defaults (see module docstring, item 2).

    Returns `{"status": "OK"|"QUOTE_NOT_USABLE", "total_cost": Decimal|
    None, "spread_cost": Decimal|None, "fee_cost": Decimal, "legs":
    [{"occ_symbol", "side", "spread_cost", "fee_cost", "quote_status"}]}`.
    `fee_cost` is always computable (never quote-dependent) even when
    `spread_cost`/`total_cost` come back `None` for a leg with an
    unusable quote -- the two components fail independently, same
    "never let one bad input block everything else computable" shape
    `.374`'s per-contract anomaly handling uses."""
    if not isinstance(legs, list) or not legs:
        fail(f"INVALID_LEGS:{legs!r}")

    rates = validate_cost_config(config)

    leg_results: list[dict[str, Any]] = []
    total_spread_cost = Decimal("0")
    total_fee_cost = Decimal("0")
    all_quotes_ok = True

    for index, leg in enumerate(legs):
        if not isinstance(leg, dict):
            fail(f"INVALID_LEG:leg[{index}]={leg!r}")
        occ_symbol = leg.get("occ_symbol")
        side = leg.get("side")
        if not isinstance(occ_symbol, str) or not occ_symbol:
            fail(f"MISSING_OCC_SYMBOL:leg[{index}]={leg!r}")
        if side not in ("BUY", "SELL"):
            fail(f"INVALID_LEG_SIDE:leg[{index}]={side!r}")

        record = leg_quotes.get(occ_symbol) if isinstance(leg_quotes, dict) else None
        spread_result = leg_round_trip_spread_cost(record, qty=qty, multiplier=multiplier)
        fee_cost = leg_round_trip_fee_cost(side, qty=qty, rates=rates)
        total_fee_cost += fee_cost

        if spread_result["status"] != "OK":
            all_quotes_ok = False
        else:
            total_spread_cost += spread_result["spread_cost"]

        leg_results.append({
            "occ_symbol": occ_symbol,
            "side": side,
            "spread_cost": str(spread_result["spread_cost"]) if spread_result["spread_cost"] is not None else None,
            "fee_cost": str(fee_cost),
            "quote_status": spread_result["status"],
        })

    if not all_quotes_ok:
        return {
            "schema_version": 1,
            "engine": ENGINE,
            "status": "QUOTE_NOT_USABLE",
            "spread_cost": None,
            "fee_cost": str(total_fee_cost),
            "total_cost": None,
            "legs": leg_results,
        }

    return {
        "schema_version": 1,
        "engine": ENGINE,
        "status": "OK",
        "spread_cost": str(total_spread_cost),
        "fee_cost": str(total_fee_cost),
        "total_cost": str(total_spread_cost + total_fee_cost),
        "legs": leg_results,
    }
