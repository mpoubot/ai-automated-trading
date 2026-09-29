#!/usr/bin/env python3
"""Unit tests for AURA v0.5.3.68 earnings blackout gate. Pure decision
logic -- no network, no filesystem, `.67.EarningsCalendarState` is always
a lightweight duck-typed fake here (this module only ever reads
`.status`/`.calendar_by_symbol`/`.error`)."""
from __future__ import annotations

import importlib.util
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


M = _load("aura_v05368_earnings_blackout_gate", ROOT / "aura_v05368_earnings_blackout_gate.py")


class FakeCalendarState:
    def __init__(self, *, status="OK", calendar_by_symbol=None, error=None):
        self.status = status
        self.calendar_by_symbol = calendar_by_symbol or {}
        self.error = error


# ============================================================================
# market_date_from_utc
# ============================================================================


def test_market_date_from_utc_same_calendar_day():
    # 12:00 UTC in September is 08:00 EDT -- same calendar date.
    now = datetime(2026, 9, 23, 12, 0, 0, tzinfo=timezone.utc)
    assert M.market_date_from_utc(now) == date(2026, 9, 23)


def test_market_date_from_utc_crosses_back_a_day_late_utc_evening():
    # 03:00 UTC is 23:00 the PREVIOUS day in America/New_York (EDT, UTC-4).
    now = datetime(2026, 9, 24, 3, 0, 0, tzinfo=timezone.utc)
    assert M.market_date_from_utc(now) == date(2026, 9, 23)


# ============================================================================
# is_symbol_in_earnings_blackout_today -- pure
# ============================================================================


def test_is_blackout_true_when_date_matches():
    calendar = {"AAPL": (date(2026, 9, 23),)}
    assert M.is_symbol_in_earnings_blackout_today("AAPL", calendar, date(2026, 9, 23)) is True


def test_is_blackout_false_when_date_does_not_match():
    calendar = {"AAPL": (date(2026, 10, 1),)}
    assert M.is_symbol_in_earnings_blackout_today("AAPL", calendar, date(2026, 9, 23)) is False


def test_is_blackout_false_when_symbol_absent():
    calendar = {"MSFT": (date(2026, 9, 23),)}
    assert M.is_symbol_in_earnings_blackout_today("AAPL", calendar, date(2026, 9, 23)) is False


def test_is_blackout_case_insensitive_symbol_lookup():
    calendar = {"AAPL": (date(2026, 9, 23),)}
    assert M.is_symbol_in_earnings_blackout_today("aapl", calendar, date(2026, 9, 23)) is True


# ============================================================================
# evaluate_earnings_blackout -- the core distinction this module has to
# get right: whole-calendar-unavailable (block everyone) vs. symbol simply
# absent from an OK calendar (allow normally, e.g. every ETF).
# ============================================================================


def test_evaluate_allows_symbol_not_reporting_today():
    state = FakeCalendarState(calendar_by_symbol={"AAPL": (date(2026, 10, 1),)})
    verdict = M.evaluate_earnings_blackout("AAPL", state, as_of_date=date(2026, 9, 23))
    assert verdict.allowed is True
    assert verdict.reasons == ()
    assert verdict.blackout_date is None
    assert verdict.calendar_status == "OK"


def test_evaluate_blocks_symbol_reporting_today():
    state = FakeCalendarState(calendar_by_symbol={"AAPL": (date(2026, 9, 23),)})
    verdict = M.evaluate_earnings_blackout("AAPL", state, as_of_date=date(2026, 9, 23))
    assert verdict.allowed is False
    assert verdict.blackout_date == date(2026, 9, 23)
    assert len(verdict.reasons) == 1
    assert verdict.reasons[0].startswith("EARNINGS_BLACKOUT:AAPL")


def test_evaluate_allows_etf_never_in_any_calendar_entry_when_status_ok():
    # DIA/XLK/etc. -- confirmed OK status, just never a key in the dict.
    # This must be ALLOW, never confused with data-unavailability.
    state = FakeCalendarState(status="OK", calendar_by_symbol={"AAPL": (date(2026, 9, 23),)})
    verdict = M.evaluate_earnings_blackout("DIA", state, as_of_date=date(2026, 9, 23))
    assert verdict.allowed is True
    assert verdict.calendar_status == "OK"


def test_evaluate_blocks_every_symbol_when_calendar_unavailable_even_an_etf():
    # The exact distinction this module's docstring calls out: UNAVAILABLE
    # must block even DIA, which would otherwise (correctly) never be
    # blocked for a data-availability reason.
    state = FakeCalendarState(status="UNAVAILABLE", error="ConnectionError: timed out")
    verdict = M.evaluate_earnings_blackout("DIA", state, as_of_date=date(2026, 9, 23))
    assert verdict.allowed is False
    assert verdict.calendar_status == "UNAVAILABLE"
    assert "EARNINGS_CALENDAR_UNAVAILABLE" in verdict.reasons[0]
    assert "ConnectionError" in verdict.reasons[0]


def test_evaluate_unavailable_blocks_even_a_symbol_with_a_stale_calendar_entry():
    # status=UNAVAILABLE always wins, regardless of what calendar_by_symbol
    # happens to contain (it should be {} in practice, but this proves the
    # status check is checked FIRST and is authoritative).
    state = FakeCalendarState(status="UNAVAILABLE", calendar_by_symbol={"AAPL": (date(2026, 12, 25),)})
    verdict = M.evaluate_earnings_blackout("AAPL", state, as_of_date=date(2026, 9, 23))
    assert verdict.allowed is False
    assert "EARNINGS_CALENDAR_UNAVAILABLE" in verdict.reasons[0]


def test_evaluate_duck_types_a_calendar_state_missing_error_attribute():
    class MinimalState:
        status = "UNAVAILABLE"
        calendar_by_symbol = {}

    verdict = M.evaluate_earnings_blackout("AAPL", MinimalState(), as_of_date=date(2026, 9, 23))
    assert verdict.allowed is False


# ============================================================================
# build_earnings_blackout_check_fn -- contract match with
# `.53.run_cycle`'s enforcement_check_fn: (symbol, direction, quantity,
# decision) -> verdict; direction-agnostic by design.
# ============================================================================


def test_check_fn_matches_enforcement_check_fn_contract():
    state = FakeCalendarState(calendar_by_symbol={"AAPL": (date(2026, 9, 23),)})
    check = M.build_earnings_blackout_check_fn(state, as_of_date=date(2026, 9, 23))
    verdict = check("AAPL", "OPEN_LONG", "10", object())
    assert verdict.allowed is False


def test_check_fn_blocks_short_entries_identically_to_long_entries():
    state = FakeCalendarState(calendar_by_symbol={"TSLA": (date(2026, 9, 23),)})
    check = M.build_earnings_blackout_check_fn(state, as_of_date=date(2026, 9, 23))
    long_verdict = check("TSLA", "OPEN_LONG", "10", None)
    short_verdict = check("TSLA", "OPEN_SHORT", "10", None)
    assert long_verdict.allowed is False
    assert short_verdict.allowed is False


# ============================================================================
# combine_enforcement_check_fns -- generic AND-composition
# ============================================================================


def test_combine_all_none_returns_none():
    assert M.combine_enforcement_check_fns(None, None) is None


def test_combine_single_none_entry_ignored_returns_the_other_unwrapped_behavior():
    def allow_check(symbol, direction, quantity, decision):
        return M.EarningsBlackoutVerdict(allowed=True, reasons=(), calendar_status="OK")

    combined = M.combine_enforcement_check_fns(None, allow_check)
    assert combined is not None
    result = combined("AAPL", "OPEN_LONG", "10", None)
    assert result.allowed is True


def test_combine_both_allow_yields_allow():
    def allow_a(symbol, direction, quantity, decision):
        return {"allowed": True, "reasons": []}

    def allow_b(symbol, direction, quantity, decision):
        return {"allowed": True, "reasons": []}

    combined = M.combine_enforcement_check_fns(allow_a, allow_b)
    result = combined("AAPL", "OPEN_LONG", "10", None)
    assert result.allowed is True
    assert result.reasons == ()


def test_combine_one_blocks_overall_blocks_and_merges_reasons():
    def allow_a(symbol, direction, quantity, decision):
        return {"allowed": True, "reasons": []}

    def block_b(symbol, direction, quantity, decision):
        return {"allowed": False, "reasons": ["SOME_PORTFOLIO_BLOCK_REASON"]}

    combined = M.combine_enforcement_check_fns(allow_a, block_b)
    result = combined("AAPL", "OPEN_LONG", "10", None)
    assert result.allowed is False
    assert result.reasons == ("SOME_PORTFOLIO_BLOCK_REASON",)


def test_combine_never_short_circuits_both_checks_always_called():
    calls = []

    def block_a(symbol, direction, quantity, decision):
        calls.append("a")
        return {"allowed": False, "reasons": ["A_BLOCKED"]}

    def block_b(symbol, direction, quantity, decision):
        calls.append("b")
        return {"allowed": False, "reasons": ["B_BLOCKED"]}

    combined = M.combine_enforcement_check_fns(block_a, block_b)
    result = combined("AAPL", "OPEN_LONG", "10", None)
    assert calls == ["a", "b"]  # both ran, despite `a` already blocking
    assert result.allowed is False
    assert set(result.reasons) == {"A_BLOCKED", "B_BLOCKED"}


def test_combine_object_style_verdict_with_dict_style_verdict_mixed():
    def object_style(symbol, direction, quantity, decision):
        return M.EarningsBlackoutVerdict(allowed=True, reasons=(), calendar_status="OK")

    def dict_style(symbol, direction, quantity, decision):
        return {"allowed": False, "reasons": ["DICT_STYLE_BLOCK"]}

    combined = M.combine_enforcement_check_fns(object_style, dict_style)
    result = combined("AAPL", "OPEN_LONG", "10", None)
    assert result.allowed is False
    assert result.reasons == ("DICT_STYLE_BLOCK",)
    assert len(result.component_verdicts) == 2
