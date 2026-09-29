#!/usr/bin/env python3
"""
AURA v0.5.3.68 — Earnings blackout gate.

WHAT THIS MODULE IS, AND IS DELIBERATELY NOT
------------------------------------------------------------------------
DECISION ONLY, consuming `.67`'s `EarningsCalendarState`. This module adds
NO new data fetching, no scoring, no sizing -- its entire job is: given a
symbol and today's date, decide whether a NEW entry should be blocked
because that symbol reports earnings today, and produce a verdict shaped
exactly like `.53.run_cycle`'s `enforcement_check_fn` contract so it can
be dropped straight into the same choke point `.44`'s portfolio-exposure
check already uses.

Why "day-of only", and why this only ever touches NEW entries
------------------------------------------------------------------------
Martin's explicit scope decisions (AskUserQuestion, 2026-09-29): the
blackout window is "day-of only" (not N days before/after, not a BMO/AMC-
aware asymmetric window) -- so this module deliberately never consults
`.67`'s `time_hint` field for gating, only `report_date`. Separately,
`.53.run_cycle` itself already only ever calls `enforcement_check_fn` for
symbols that are NOT already held (`already_held` symbols are recorded as
`SKIPPED_ALREADY_HELD` before `enforcement_check_fn` is ever reached) --
so wiring this gate into the exact same choke point `.44` uses gets
"exits are never blocked, only new entries are" for free, structurally,
with no extra logic needed here to distinguish the two.

The one distinction this module DOES have to get right
------------------------------------------------------------------------
"We have no earnings data at all right now" (FMP down, rate-limited, cache
stale and refetch failed) is NOT the same thing as "this specific symbol
simply doesn't report earnings" (every ETF in this repo's pinned universe
-- DIA, the 11 SPDR sector ETFs -- will legitimately never appear in any
earnings calendar, ever). The former must fail closed for EVERY symbol
(Martin's confirmed choice); the latter must allow that symbol normally.
`.67`'s `EarningsCalendarState.status` is exactly this signal:
`status != "OK"` means "we don't know anything about anyone today" (block
all), while `status == "OK"` with a symbol simply absent from
`calendar_by_symbol` means "confirmed: this symbol has no earnings-
calendar entry in the fetched window" (allow). Getting this backwards
would either fail-close the entire book on every FMP hiccup for reasons
unrelated to earnings, or silently never protect ETFs' underlying
mechanics from ever being checked at all -- neither is acceptable, so it
is tested explicitly (see test file).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Callable, Sequence

VERSION = "AURA v0.5.3.68"
ENGINE = "EARNINGS_BLACKOUT_GATE"

try:
    from zoneinfo import ZoneInfo

    _MARKET_TZ = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover -- missing tzdata on some Windows installs, same fallback as `.58`
    _MARKET_TZ = None


def market_date_from_utc(now: datetime) -> date:
    """The US equity market's local calendar date for `now`, matching
    `.58`'s own America/New_York convention (this repo's `requirements.txt`
    already pins `tzdata>=2024.1`, so `_MARKET_TZ` is expected to be set in
    practice; the UTC-date fallback below is defensive only, for the same
    missing-tzdata edge case `.58` documents)."""
    if _MARKET_TZ is not None:
        return now.astimezone(_MARKET_TZ).date()
    return now.astimezone(timezone.utc).date()  # pragma: no cover -- defensive fallback only


@dataclass(frozen=True, slots=True)
class EarningsBlackoutVerdict:
    allowed: bool
    reasons: tuple[str, ...]
    calendar_status: str  # `.67` EarningsCalendarState.status, carried through for audit
    blackout_date: date | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reasons": list(self.reasons),
            "calendar_status": self.calendar_status,
            "blackout_date": self.blackout_date.isoformat() if self.blackout_date else None,
        }


def is_symbol_in_earnings_blackout_today(
    symbol: str, calendar_by_symbol: dict[str, tuple[date, ...]], as_of_date: date,
) -> bool:
    """Pure. A symbol absent from `calendar_by_symbol` entirely (the normal
    case for every non-reporting ETF) is never in blackout -- this function
    has no notion of data availability at all; that is `evaluate_earnings_
    blackout`'s job, one layer up."""
    return as_of_date in calendar_by_symbol.get(symbol.upper(), ())


def evaluate_earnings_blackout(
    symbol: str, calendar_state: Any, *, as_of_date: date,
) -> EarningsBlackoutVerdict:
    """`calendar_state` is a `.67.EarningsCalendarState` (duck-typed, like
    every other cross-module boundary in this repo -- only `.status` and
    `.calendar_by_symbol` are actually read)."""
    status = getattr(calendar_state, "status", "UNAVAILABLE")
    if status != "OK":
        error = getattr(calendar_state, "error", None)
        return EarningsBlackoutVerdict(
            allowed=False,
            reasons=(
                f"EARNINGS_CALENDAR_UNAVAILABLE:no usable earnings-calendar data this cycle "
                f"(status={status}{', error=' + error if error else ''}) -- fail-closed, no new "
                "entries for any symbol until the calendar is refreshed",
            ),
            calendar_status=status,
        )

    calendar_by_symbol = getattr(calendar_state, "calendar_by_symbol", {})
    if is_symbol_in_earnings_blackout_today(symbol, calendar_by_symbol, as_of_date):
        return EarningsBlackoutVerdict(
            allowed=False,
            reasons=(f"EARNINGS_BLACKOUT:{symbol} reports earnings on {as_of_date.isoformat()} (day-of blackout)",),
            calendar_status=status,
            blackout_date=as_of_date,
        )

    return EarningsBlackoutVerdict(allowed=True, reasons=(), calendar_status=status)


def build_earnings_blackout_check_fn(
    calendar_state: Any, *, as_of_date: date,
) -> Callable[[str, str, Any, Any], EarningsBlackoutVerdict]:
    """Returns a closure matching `.53.run_cycle`'s `enforcement_check_fn`
    contract exactly: `(symbol, direction, quantity, decision) ->
    EarningsBlackoutVerdict`. `direction`/`quantity`/`decision` are
    deliberately unused -- the blackout applies identically to a new long
    or a new short (a surprise print moves the stock either way), so this
    gate is direction-agnostic by design, not by oversight."""

    def check(symbol: str, direction: str, quantity: Any, decision: Any) -> EarningsBlackoutVerdict:
        return evaluate_earnings_blackout(symbol, calendar_state, as_of_date=as_of_date)

    return check


# ============================================================================
# Generic AND-composition -- combines this gate's check with `.44`'s
# portfolio-exposure check (or any other `enforcement_check_fn`-shaped
# callable) into one closure `.53.run_cycle` can call. Lives here (not in
# `.53`/`.55`) so neither existing, already-tested module needs to change
# to add a second independent check.
# ============================================================================


@dataclass(frozen=True, slots=True)
class CombinedEnforcementVerdict:
    allowed: bool
    reasons: tuple[str, ...]
    component_verdicts: tuple[Any, ...]  # every component's own verdict object, kept for audit


def _verdict_allowed(verdict: Any) -> bool:
    allowed = getattr(verdict, "allowed", None)
    if allowed is None and isinstance(verdict, dict):
        allowed = verdict.get("allowed")
    return bool(allowed)


def _verdict_reasons(verdict: Any) -> tuple[str, ...]:
    reasons = getattr(verdict, "reasons", None)
    if reasons is None and isinstance(verdict, dict):
        reasons = verdict.get("reasons")
    return tuple(reasons) if reasons else ()


def combine_enforcement_check_fns(
    *fns: Callable[[str, str, Any, Any], Any] | None,
) -> Callable[[str, str, Any, Any], CombinedEnforcementVerdict] | None:
    """ANDs together zero or more `enforcement_check_fn`-shaped callables.
    `None` entries are ignored (so a caller can compose e.g. `.55`'s
    portfolio-enforcement closure, which is itself sometimes `None` when
    `limits` was not supplied, with this module's earnings-blackout
    closure, without its own `None`-checking). If every entry is `None`,
    returns `None` so the caller can pass this straight into
    `.53.run_cycle`'s optional `enforcement_check_fn` param unchanged.

    ALL supplied checks are always called for every candidate -- never
    short-circuited on the first block -- so every component's own audit
    side effects (e.g. `.44`'s `decisions_by_symbol` recording) still
    happen even once an earlier check in the tuple already blocks,
    matching this repo's 'never silently skip a check' discipline."""
    live_fns = tuple(f for f in fns if f is not None)
    if not live_fns:
        return None

    def combined(symbol: str, direction: str, quantity: Any, decision: Any) -> CombinedEnforcementVerdict:
        results = tuple(f(symbol, direction, quantity, decision) for f in live_fns)
        allowed = all(_verdict_allowed(r) for r in results)
        reasons = tuple(reason for r in results for reason in _verdict_reasons(r))
        return CombinedEnforcementVerdict(allowed=allowed, reasons=reasons, component_verdicts=results)

    return combined
