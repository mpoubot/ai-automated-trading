#!/usr/bin/env python3
"""
AURA v0.5.3.72 -- Earnings blackout HORIZON check.

WHAT THIS MODULE IS, AND HOW IT RELATES TO `.368`
------------------------------------------------------------------------
`.368` (AURA v0.5.3.68) already blocks a NEW entry on the exact day a
symbol reports earnings ("day-of only" -- Martin's explicit scope choice,
2026-09-29). This module is a SEPARATE, narrower check answering a
different question: "if I open this position today and hold it for up to
MAX_HOLD_BARS trading days, will earnings fall somewhere inside that
hold?" -- not just today.

Like `.368`, this module is DECISION ONLY. It adds no new data fetching:
it consumes the exact same `.67.EarningsCalendarState` that `.368` does
(FMP-confirmed report dates, not SEC-inferred cadence -- that SEC-based
approach, built earlier in the same session that produced this module,
is superseded by this one; see the build report). Its verdict shape
matches `.53.run_cycle`'s `enforcement_check_fn` contract exactly, so it
composes with `.368` (and with `.44`'s portfolio-exposure check) via
`.368`'s own `combine_enforcement_check_fns` -- no modification to `.368`,
`.53`, or `.44` is needed to add this as a second, independent check.

Why a separate module instead of widening `.368` itself
------------------------------------------------------------------------
`.368`'s own docstring is explicit that its day-of-only scope is a
deliberate, previously-confirmed decision, not an oversight -- changing
`.368` to also do horizon-aware blocking would silently change behavior
for every caller already relying on its tested day-of semantics. Adding
a second, independently-composable module keeps `.368` exactly as it is
and lets this horizon check be adopted, tuned, or removed on its own.

The trading-day-to-calendar-day conversion, and why it is intentionally
padded (Martin's explicit choice, 2026-10-05)
------------------------------------------------------------------------
`MAX_HOLD_BARS = 20` (`.054_exit_engine.py`) is a count of trading-day
bars (equity strategies trade `TimeFrame.Day` bars via Alpaca -- confirmed
in `.351`). `.67`'s FMP calendar gives calendar dates, and this repo has
no NYSE trading-calendar utility. Rather than add a new third-party
calendar dependency or silently under-count (a flat 20-calendar-day
window would be SHORTER than a true 20-trading-day hold whenever a
weekend or NYSE holiday falls inside it -- a fail-OPEN bug for a
fail-closed safety gate), this module pads:

    20 trading days -> ceil(20 * 7/5) = 28 calendar days (no-holiday case)
    + 5 calendar days safety margin (covers up to ~1-2 NYSE holidays
      landing inside the window; NYSE has 9 holidays/year, roughly one
      every 5.7 weeks, so a ~4-week window plausibly contains one)
    = 33 calendar days, inclusive of today (HORIZON_CALENDAR_DAYS below)

This is a deliberate over-approximation, not a precise trading-day count:
it will occasionally flag a symbol whose earnings actually falls just
past the true 20th trading day (overly conservative), but per this
project's fail-closed discipline that is the correct side to err on for
a safety gate, and the margin is wide enough that it should never
UNDER-count in practice. If exact precision is ever needed, swap in a
real NYSE calendar (`horizon_calendar_days_for` below is the one function
that would need to change).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Callable

VERSION = "AURA v0.5.3.72"
ENGINE = "EARNINGS_BLACKOUT_HORIZON_CHECK"

# Trading-day bars -> calendar-day window, per the module docstring above.
DEFAULT_MAX_HOLD_BARS = 20  # matches `.054_exit_engine.MAX_HOLD_BARS`
_TRADING_TO_CALENDAR_RATIO = 7 / 5  # 5 trading days per 7-calendar-day week
_HOLIDAY_SAFETY_MARGIN_DAYS = 5

DEFAULT_HORIZON_CALENDAR_DAYS = math.ceil(DEFAULT_MAX_HOLD_BARS * _TRADING_TO_CALENDAR_RATIO) + _HOLIDAY_SAFETY_MARGIN_DAYS
# = ceil(28.0) + 5 = 33


def horizon_calendar_days_for(max_hold_bars: int) -> int:
    """Pure. The padded calendar-day window for an arbitrary trading-day
    hold length, using the same conservative conversion documented above.
    `max_hold_bars` must be > 0 (mirrors `.054_exit_engine`'s own
    `INVALID_MAX_HOLD_BARS` guard)."""
    if max_hold_bars <= 0:
        raise ValueError(f"INVALID_MAX_HOLD_BARS:must be > 0, got {max_hold_bars}")
    return math.ceil(max_hold_bars * _TRADING_TO_CALENDAR_RATIO) + _HOLIDAY_SAFETY_MARGIN_DAYS


@dataclass(frozen=True, slots=True)
class EarningsBlackoutHorizonVerdict:
    allowed: bool
    reasons: tuple[str, ...]
    calendar_status: str  # `.67` EarningsCalendarState.status, carried through for audit
    horizon_calendar_days: int
    blackout_date: date | None = None  # the specific earnings date that triggered a block, if any

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reasons": list(self.reasons),
            "calendar_status": self.calendar_status,
            "horizon_calendar_days": self.horizon_calendar_days,
            "blackout_date": self.blackout_date.isoformat() if self.blackout_date else None,
        }


def earliest_earnings_date_in_window(
    symbol: str, calendar_by_symbol: dict[str, tuple[date, ...]], *, start: date, end: date,
) -> date | None:
    """Pure. The earliest date in `[start, end]` (inclusive both ends) at
    which `symbol` reports, or None if it has no entry in that window --
    including a symbol entirely absent from `calendar_by_symbol` (the
    normal, expected case for every non-reporting ETF)."""
    dates_in_window = tuple(
        d for d in calendar_by_symbol.get(symbol.upper(), ()) if start <= d <= end
    )
    return min(dates_in_window) if dates_in_window else None


def evaluate_earnings_blackout_horizon(
    symbol: str,
    calendar_state: Any,
    *,
    as_of_date: date,
    max_hold_bars: int = DEFAULT_MAX_HOLD_BARS,
) -> EarningsBlackoutHorizonVerdict:
    """`calendar_state` is a `.67.EarningsCalendarState` (duck-typed, like
    `.368` -- only `.status`/`.calendar_by_symbol`/`.error` are read).
    Mirrors `.368.evaluate_earnings_blackout`'s fail-closed distinction
    exactly: calendar-wide unavailability blocks every symbol (including
    ETFs that will never themselves report), while a symbol simply absent
    from an OK calendar is allowed."""
    horizon_days = horizon_calendar_days_for(max_hold_bars)
    status = getattr(calendar_state, "status", "UNAVAILABLE")

    if status != "OK":
        error = getattr(calendar_state, "error", None)
        return EarningsBlackoutHorizonVerdict(
            allowed=False,
            reasons=(
                f"EARNINGS_CALENDAR_UNAVAILABLE:no usable earnings-calendar data this cycle "
                f"(status={status}{', error=' + error if error else ''}) -- fail-closed, no new "
                "entries for any symbol until the calendar is refreshed",
            ),
            calendar_status=status,
            horizon_calendar_days=horizon_days,
        )

    calendar_by_symbol = getattr(calendar_state, "calendar_by_symbol", {})
    window_end = as_of_date + timedelta(days=horizon_days)
    hit = earliest_earnings_date_in_window(symbol, calendar_by_symbol, start=as_of_date, end=window_end)

    if hit is not None:
        return EarningsBlackoutHorizonVerdict(
            allowed=False,
            reasons=(
                f"EARNINGS_BLACKOUT_HORIZON:{symbol} reports earnings on {hit.isoformat()}, within the "
                f"{horizon_days}-calendar-day hold horizon from {as_of_date.isoformat()} "
                f"(~{max_hold_bars} trading-day bars, padded)",
            ),
            calendar_status=status,
            horizon_calendar_days=horizon_days,
            blackout_date=hit,
        )

    return EarningsBlackoutHorizonVerdict(
        allowed=True, reasons=(), calendar_status=status, horizon_calendar_days=horizon_days,
    )


def build_earnings_blackout_horizon_check_fn(
    calendar_state: Any, *, as_of_date: date, max_hold_bars: int = DEFAULT_MAX_HOLD_BARS,
) -> Callable[[str, str, Any, Any], EarningsBlackoutHorizonVerdict]:
    """Returns a closure matching `.53.run_cycle`'s `enforcement_check_fn`
    contract exactly, identical in shape to `.368.build_earnings_blackout_
    check_fn`: `(symbol, direction, quantity, decision) ->
    EarningsBlackoutHorizonVerdict`. `direction`/`quantity`/`decision` are
    deliberately unused -- direction-agnostic by the same reasoning as
    `.368` (a surprise print moves the stock either way, so a new long and
    a new short are blocked identically)."""

    def check(symbol: str, direction: str, quantity: Any, decision: Any) -> EarningsBlackoutHorizonVerdict:
        return evaluate_earnings_blackout_horizon(
            symbol, calendar_state, as_of_date=as_of_date, max_hold_bars=max_hold_bars,
        )

    return check
