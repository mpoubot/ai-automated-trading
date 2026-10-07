#!/usr/bin/env python3
"""AURA v0.5.3.80 -- Options Exit Engine (O6).

WHAT THIS MODULE IS, AND IS DELIBERATELY NOT
------------------------------------------------------------------------
A pure, stateless DECISION function: given the current state of an
already-open 2-leg vertical spread (entry net price, current per-leg
quotes, today's date, optionally the global kill switch and an earnings
calendar), it returns CLOSE or HOLD plus which trigger fired and why.

It owns NO ledger of open positions, fetches NO data itself, and submits
NO orders -- every input is caller-supplied, and every call is
independent of every other call. This mirrors the "decision-only, no
ledger" discipline `.368`/`.371`/`.377` all use (O4B/`.377` explicitly:
"stays fully stateless ... owns no ledger"), per Martin's explicit
confirmation (AskUserQuestion, 2026-10-07): the caller (a future O9
supervisor, or Martin himself) is responsible for tracking which
positions are open and what they were opened for -- O6 only ever answers
"given this position's state right now, should it be closed?"

DESIGN LINEAGE -- real reference pattern, not invented from scratch
------------------------------------------------------------------------
`AURA_DELTAX_v2_Options_Pattern_Reference_Audit_2026-09-29.md`'s O6
section is the one concrete reference pattern found anywhere in the
estate (DELTAX V2's `exit_intent_builder.py`): independent simple
threshold checks, not a combined score, evaluated in this fixed
PRIORITY order (first match wins): KILL_SWITCH (emergency) ->
TAKE_PROFIT (close <=50% of credit received) -> STOP_LOSS (close >=200%
of credit received) -> DTE_EXIT (<=3 calendar days to expiry) ->
EARNINGS_EXIT (earnings within 1 day). The one documented anti-pattern
to avoid -- a hardcoded per-symbol overnight whitelist baked into a
backtest and copy-pasted into live code -- has no analogue here; this
module has no symbol-specific logic anywhere.

Martin's confirmed design decisions (AskUserQuestion, 2026-10-07):
  1. Stateless, caller-supplied state -- no ledger (see above).
  2. v1 scope is NET-CREDIT vertical spreads only. `entry_net_price`
     (same signed convention `.376`'s `limit_price` uses: negative=net
     credit, positive=net debit) MUST be negative, or this module fails
     closed with `V1_SCOPE_REQUIRES_NET_CREDIT_SPREAD`. Debit-spread exit
     logic (profit-target/stop-loss relative to debit paid) is explicitly
     out of scope for v1, deferred to a later pass.
  3. TAKE_PROFIT/STOP_LOSS/DTE_EXIT thresholds default to DELTAX's
     reference numbers (50% / 200% / 3 DTE) -- not independently derived
     or validated for AURA, same role O5's TTL reuse played for equity's
     60s/60s defaults. Fully overridable via the `config` parameter,
     same `DEFAULT_*_CONFIG` + override-dict shape every other module in
     this build uses.
  4. EARNINGS_EXIT is included, composing `.367`'s `EarningsCalendarState`
     (duck-typed -- only `.status`/`.calendar_by_symbol` are read, same
     boundary `.368` uses) rather than fetching anything itself.
  5. Missing/unusable market data -- EITHER a leg's quote (O2's own
     `data_status != "OK"`) OR the earnings calendar (`.367`'s
     `status != "OK"`) -- never forces a close by itself. It only
     disables the specific trigger(s) that depend on that data for this
     one call; every other trigger (including KILL_SWITCH and DTE_EXIT,
     neither of which need quotes or a calendar) still evaluates
     normally. This is Martin's explicit answer for the earnings case,
     generalized consistently to the quote case for TAKE_PROFIT/
     STOP_LOSS, since the same reasoning applies in both: missing data
     is not itself evidence of a reason to exit.

`kill_switch_engaged` is a CALL-TIME SIGNAL, not a config toggle -- and
its meaning is the OPPOSITE of every other module's `kill_switch` field.
Everywhere else in this build (`.378`'s `DEFAULT_AUTH_CONFIG`, equity's
`.336`), `kill_switch=True` BLOCKS an action (no new authorization). Here,
`kill_switch_engaged=True` FORCES one (immediate emergency close) --
because this is an EXIT engine, and per the DELTAX reference pattern,
"a kill switch must never accidentally block the exits it triggers" (the
-X% drawdown kill switch that disables new entries is exactly the
moment open risk positions most need to close, not be trapped open).
Defaults to `False` (no forced exit) so a caller that forgets to pass it
never silently triggers a mass liquidation.

Every trigger is evaluated unconditionally on every call (never
short-circuited), for audit -- same "always run every check" discipline
`.368.combine_enforcement_check_fns` uses -- the final `action`/`trigger`/
`reason` is then picked by strict priority order over the results.

No live Alpaca call anywhere in this file. NETWORK_CAPABLE_FUNCTIONS is
empty, same disclosed property `.378`/`.379` carry.
"""
from __future__ import annotations

import importlib.util
import os
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

VERSION = "AURA v0.5.3.80"
ENGINE = "OPTIONS_EXIT_ENGINE"

VERTICAL_LEG_COUNT = 2

# Pure decision engine -- no network call anywhere in this module.
NETWORK_CAPABLE_FUNCTIONS: frozenset[str] = frozenset()

DEFAULT_EXIT_CONFIG: dict[str, Any] = {
    "take_profit_credit_pct": Decimal("0.50"),
    "stop_loss_credit_pct": Decimal("2.00"),
    "dte_exit_threshold": 3,
    "earnings_exit_horizon_days": 1,
}

PRIORITY_ORDER: tuple[str, ...] = (
    "KILL_SWITCH",
    "TAKE_PROFIT",
    "STOP_LOSS",
    "DTE_EXIT",
    "EARNINGS_EXIT",
)


def fail(message: str) -> None:
    raise RuntimeError(message)


# ============================================================================
# `.373` loader -- same try-import-then-sibling-file-path fallback every
# other module in this build uses, so this module works both when `.373`
# is already importable (installed/on sys.path) and when it's only
# sitting next to this file on disk (a fresh branch checkout).
# ============================================================================


def _load_options_metadata_module():
    try:
        import aura_v05373_options_instrument_metadata as meta  # type: ignore
        return meta
    except ImportError:
        pass
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "aura_v05373_options_instrument_metadata.py")
    if not os.path.isfile(path):
        fail(f"MISSING_DEPENDENCY:aura_v05373_options_instrument_metadata.py not found next to {__file__}")
    spec = importlib.util.spec_from_file_location("aura_v05373_options_instrument_metadata", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _decimal(value: Any, field: str) -> Decimal:
    try:
        d = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        fail(f"INVALID_DECIMAL:{field}={value!r}")
        raise  # pragma: no cover -- fail() always raises; satisfies type-checkers
    if not d.is_finite():
        fail(f"INVALID_DECIMAL:{field}={value!r}")
    return d


# ============================================================================
# Position normalization -- re-derives the structural facts it needs
# (expiry, underlying match, one-long-one-short) from each leg's OCC
# symbol rather than trusting caller-supplied fields, the same "always
# independently recompute" discipline `.376.validate_vertical_spec` uses.
# Fails closed (RuntimeError) on malformed CALLER input -- a malformed
# entry here is a caller bug, same class of error `.377`'s
# `normalize_expected_position` treats identically.
# ============================================================================


def normalize_position(position: Any, meta: Any) -> dict[str, Any]:
    if not isinstance(position, dict):
        fail(f"INVALID_POSITION:{position!r}")

    underlying_symbol = position.get("underlying_symbol")
    if not isinstance(underlying_symbol, str) or not underlying_symbol:
        fail(f"MISSING_UNDERLYING_SYMBOL:{position!r}")

    raw_legs = position.get("legs")
    if not isinstance(raw_legs, list) or len(raw_legs) != VERTICAL_LEG_COUNT:
        fail(
            "V1_SCOPE_REQUIRES_EXACTLY_"
            f"{VERTICAL_LEG_COUNT}_LEGS:{len(raw_legs) if isinstance(raw_legs, list) else raw_legs!r}"
        )

    legs: list[dict[str, Any]] = []
    expiries: set[date] = set()
    for index, raw_leg in enumerate(raw_legs):
        if not isinstance(raw_leg, dict):
            fail(f"INVALID_LEG:leg[{index}]={raw_leg!r}")
        occ_symbol = raw_leg.get("occ_symbol")
        side = raw_leg.get("side")
        if side not in ("BUY", "SELL"):
            fail(f"INVALID_LEG_SIDE:leg[{index}]={side!r}")
        try:
            parsed = meta.parse_occ_symbol(occ_symbol)
        except RuntimeError as exc:
            fail(f"UNPARSEABLE_LEG_OCC_SYMBOL:leg[{index}]={occ_symbol!r}:{exc}")
            raise  # pragma: no cover
        if parsed["underlying_symbol"] != underlying_symbol:
            fail(
                f"LEG_UNDERLYING_MISMATCH:leg[{index}]={parsed['underlying_symbol']!r} "
                f"!= position underlying_symbol={underlying_symbol!r}"
            )
        expiries.add(parsed["expiry"])
        legs.append({"occ_symbol": occ_symbol, "side": side, "expiry": parsed["expiry"]})

    if len(expiries) != 1:
        fail(f"NOT_A_VERTICAL_MIXED_EXPIRIES:{sorted(e.isoformat() for e in expiries)!r}")

    sides = {leg["side"] for leg in legs}
    if sides != {"BUY", "SELL"}:
        fail(f"NOT_A_VERTICAL_REQUIRES_ONE_LONG_ONE_SHORT_LEG:{[l['side'] for l in legs]!r}")

    return {
        "underlying_symbol": underlying_symbol,
        "legs": legs,
        "expiry": next(iter(expiries)),
    }


def days_to_expiry(expiry: date, as_of_date: date) -> int:
    """Pure. Calendar-day DTE -- same convention `.371`'s calendar-day
    approximation uses elsewhere in this repo, not a trading-day count."""
    return (expiry - as_of_date).days


# ============================================================================
# Valuation -- NOT an order. Estimates the net mid-market price to
# re-establish the SAME legs right now, using the identical signed
# convention `.376` uses for `limit_price` (BUY leg contributes +price,
# SELL leg contributes -price). The cost to CLOSE the existing position
# is the negation of that (you do the opposite of every leg). Fails
# closed per-leg via O2's own `data_status` vocabulary -- a leg whose
# quote isn't `"OK"` makes the whole valuation `"QUOTE_NOT_USABLE"`
# rather than guessing a stale or missing quote is close enough (same
# "never guess" discipline `.374._classify_quote_status` documents).
# ============================================================================


def _leg_quote_mid(contract_record: Any) -> tuple[Decimal | None, str]:
    """`contract_record` is expected to be one entry from O2's
    `fetch_option_chain()["contracts"]` (i.e. has `"data_status"` and
    `"broker_latest_quote"` keys) -- this function never fetches
    anything itself, it only reads what the caller already has."""
    if not isinstance(contract_record, dict):
        return None, "MISSING_QUOTE_RECORD"
    status = contract_record.get("data_status")
    if status != "OK":
        return None, status if isinstance(status, str) else "MISSING_QUOTE_RECORD"
    quote = contract_record.get("broker_latest_quote")
    if not isinstance(quote, dict):
        return None, "MISSING_QUOTE_RECORD"
    try:
        bid = Decimal(str(quote.get("bid_price")))
        ask = Decimal(str(quote.get("ask_price")))
    except (InvalidOperation, TypeError, ValueError):
        return None, "INVALID_QUOTE"
    if not bid.is_finite() or not ask.is_finite() or bid <= 0 or ask <= 0 or ask < bid:
        return None, "INVALID_QUOTE"
    return (bid + ask) / 2, "OK"


def compute_cost_to_close(normalized_position: dict[str, Any], leg_quotes: dict[str, Any]) -> dict[str, Any]:
    """Pure. Returns `{"cost_to_close": Decimal|None, "status": "OK"|
    "QUOTE_NOT_USABLE", "leg_statuses": {occ_symbol: status}}`.
    `cost_to_close` is the estimated net DEBIT (positive = you'd pay to
    close; can be negative if the structure has moved to where closing
    would itself generate a credit) required to close the position at
    current mid prices."""
    leg_statuses: dict[str, str] = {}
    net_open_value = Decimal("0")
    all_ok = True
    for leg in normalized_position["legs"]:
        occ_symbol = leg["occ_symbol"]
        record = leg_quotes.get(occ_symbol) if isinstance(leg_quotes, dict) else None
        mid, status = _leg_quote_mid(record)
        leg_statuses[occ_symbol] = status
        if status != "OK":
            all_ok = False
            continue
        sign = Decimal("1") if leg["side"] == "BUY" else Decimal("-1")
        net_open_value += sign * mid  # type: ignore[operator]

    if not all_ok:
        return {"cost_to_close": None, "status": "QUOTE_NOT_USABLE", "leg_statuses": leg_statuses}
    return {"cost_to_close": -net_open_value, "status": "OK", "leg_statuses": leg_statuses}


# ============================================================================
# Earnings trigger -- consumes `.367`'s `EarningsCalendarState`, duck-typed
# exactly like `.368.evaluate_earnings_blackout` does (only `.status`/
# `.calendar_by_symbol` are read). Widened from `.368`'s day-of-only
# window to an inclusive [as_of_date, as_of_date + horizon_days] window,
# per this module's own "earnings within N days" trigger (default N=1),
# a different question from `.368`'s "earnings today blocks a NEW
# entry" -- this module is about closing an EXISTING position before an
# upcoming print, not gating a new one.
# ============================================================================


def _evaluate_earnings_trigger(
    underlying_symbol: str,
    earnings_calendar_state: Any,
    *,
    as_of_date: date,
    horizon_days: int,
) -> dict[str, Any]:
    if earnings_calendar_state is None:
        return {"fired": False, "detail": "no earnings_calendar_state supplied -- trigger not evaluated"}

    status = getattr(earnings_calendar_state, "status", "UNAVAILABLE")
    if status != "OK":
        error = getattr(earnings_calendar_state, "error", None)
        return {
            "fired": False,
            "detail": (
                f"EARNINGS_CALENDAR_UNAVAILABLE:status={status}"
                f"{',error=' + error if error else ''} -- trigger skipped this cycle, not forced"
            ),
        }

    calendar_by_symbol = getattr(earnings_calendar_state, "calendar_by_symbol", {})
    dates = calendar_by_symbol.get(underlying_symbol.upper(), ())
    horizon_end = as_of_date + timedelta(days=horizon_days)
    matching = sorted(d for d in dates if as_of_date <= d <= horizon_end)
    if matching:
        return {
            "fired": True,
            "detail": (
                f"earnings report date(s) {[d.isoformat() for d in matching]} fall within the "
                f"[{as_of_date.isoformat()}, {horizon_end.isoformat()}] exit horizon"
            ),
        }
    return {
        "fired": False,
        "detail": f"no earnings report in [{as_of_date.isoformat()}, {horizon_end.isoformat()}]",
    }


# ============================================================================
# The one entry point.
# ============================================================================


def evaluate_exit(
    position: Any,
    *,
    entry_net_price: Any,
    leg_quotes: dict[str, Any],
    as_of_date: date,
    kill_switch_engaged: bool = False,
    earnings_calendar_state: Any = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Pure / stateless. Returns a decision dict:
    `{"action": "CLOSE"|"HOLD", "trigger": <name>|None, "reason": str,
    "dte": int, "credit_received": str, "cost_to_close": str|None,
    "credit_captured_pct": str|None, "triggers_evaluated": [...]}`.

    `position`: `{"underlying_symbol": str, "legs": [{"occ_symbol": str,
    "side": "BUY"|"SELL"}, {...}]}` -- exactly 2 legs, one BUY one SELL,
    same expiry (re-derived and verified from each leg's OCC symbol, not
    trusted from the caller).

    `entry_net_price`: the net price the structure was opened at, same
    signed convention `.376`'s `limit_price` uses (negative = net
    credit). MUST be negative -- v1 scope is net-credit spreads only
    (Martin's confirmed choice, 2026-10-07); a non-negative value fails
    closed with `V1_SCOPE_REQUIRES_NET_CREDIT_SPREAD`.

    `leg_quotes`: `{occ_symbol: <one entry from O2's
    fetch_option_chain()["contracts"]>}` for each of the 2 legs.

    `as_of_date`: caller-supplied "today" -- this module never calls
    `date.today()` itself, keeping it pure and deterministic for tests.

    `kill_switch_engaged`: see module docstring -- forces an immediate
    emergency close when True. Defaults to False.

    `earnings_calendar_state`: optional `.367.EarningsCalendarState`
    (duck-typed). Omit or pass `None` to skip the earnings trigger
    entirely (distinct from passing one whose `.status != "OK"`, which
    still records the trigger as evaluated-but-skipped, for audit).

    `config`: optional override of `DEFAULT_EXIT_CONFIG`.
    """
    meta = _load_options_metadata_module()
    cfg = {**DEFAULT_EXIT_CONFIG, **(config or {})}

    if not isinstance(as_of_date, date):
        fail(f"INVALID_AS_OF_DATE:{as_of_date!r}")

    normalized = normalize_position(position, meta)

    entry_price = _decimal(entry_net_price, "entry_net_price")
    if entry_price >= 0:
        fail(f"V1_SCOPE_REQUIRES_NET_CREDIT_SPREAD:entry_net_price={entry_price} (must be < 0)")
    credit_received = -entry_price

    dte = days_to_expiry(normalized["expiry"], as_of_date)

    valuation = compute_cost_to_close(normalized, leg_quotes if isinstance(leg_quotes, dict) else {})
    cost_to_close = valuation["cost_to_close"]

    credit_captured_pct: Decimal | None = None
    if cost_to_close is not None and credit_received > 0:
        credit_captured_pct = (credit_received - cost_to_close) / credit_received

    triggers: list[dict[str, Any]] = []

    # 1. KILL_SWITCH -- emergency, always evaluated, never gated on
    # quotes/DTE/anything else. See module docstring for why this is the
    # inverse convention of every other module's `kill_switch` field.
    triggers.append({
        "trigger": "KILL_SWITCH",
        "fired": bool(kill_switch_engaged),
        "detail": (
            "AURA's global kill switch is engaged -- emergency exit, bypasses every other check"
            if kill_switch_engaged else "kill switch not engaged"
        ),
    })

    # 2. TAKE_PROFIT -- close <= take_profit_credit_pct of credit received.
    tp_pct = cfg["take_profit_credit_pct"]
    tp_fired = cost_to_close is not None and cost_to_close <= tp_pct * credit_received
    triggers.append({
        "trigger": "TAKE_PROFIT",
        "fired": bool(tp_fired),
        "detail": (
            f"cost_to_close={cost_to_close} <= {tp_pct}*credit_received({credit_received})"
            f"={tp_pct * credit_received}"
            if cost_to_close is not None else f"UNAVAILABLE:{valuation['status']} -- trigger skipped this cycle"
        ),
    })

    # 3. STOP_LOSS -- cost to close has grown to >= stop_loss_credit_pct of credit received.
    sl_pct = cfg["stop_loss_credit_pct"]
    sl_fired = cost_to_close is not None and cost_to_close >= sl_pct * credit_received
    triggers.append({
        "trigger": "STOP_LOSS",
        "fired": bool(sl_fired),
        "detail": (
            f"cost_to_close={cost_to_close} >= {sl_pct}*credit_received({credit_received})"
            f"={sl_pct * credit_received}"
            if cost_to_close is not None else f"UNAVAILABLE:{valuation['status']} -- trigger skipped this cycle"
        ),
    })

    # 4. DTE_EXIT -- hard cliff, no graduated curve (DELTAX reference).
    dte_threshold = cfg["dte_exit_threshold"]
    dte_fired = dte <= dte_threshold
    triggers.append({
        "trigger": "DTE_EXIT",
        "fired": bool(dte_fired),
        "detail": f"dte={dte} <= dte_exit_threshold={dte_threshold}",
    })

    # 5. EARNINGS_EXIT.
    earnings_result = _evaluate_earnings_trigger(
        normalized["underlying_symbol"],
        earnings_calendar_state,
        as_of_date=as_of_date,
        horizon_days=cfg["earnings_exit_horizon_days"],
    )
    triggers.append({
        "trigger": "EARNINGS_EXIT",
        "fired": bool(earnings_result["fired"]),
        "detail": earnings_result["detail"],
    })

    by_name = {t["trigger"]: t for t in triggers}
    winning = next((name for name in PRIORITY_ORDER if by_name[name]["fired"]), None)

    action = "CLOSE" if winning is not None else "HOLD"
    reason = by_name[winning]["detail"] if winning is not None else "no exit trigger fired"

    return {
        "schema_version": 1,
        "engine": ENGINE,
        "underlying_symbol": normalized["underlying_symbol"],
        "legs": [{"occ_symbol": leg["occ_symbol"], "side": leg["side"]} for leg in normalized["legs"]],
        "as_of_date": as_of_date.isoformat(),
        "expiry": normalized["expiry"].isoformat(),
        "dte": dte,
        "credit_received": str(credit_received),
        "cost_to_close": str(cost_to_close) if cost_to_close is not None else None,
        "credit_captured_pct": str(credit_captured_pct) if credit_captured_pct is not None else None,
        "leg_quote_statuses": valuation["leg_statuses"],
        "action": action,
        "trigger": winning,
        "reason": reason,
        "triggers_evaluated": triggers,
    }
