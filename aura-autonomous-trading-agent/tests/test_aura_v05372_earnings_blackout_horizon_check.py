#!/usr/bin/env python3
"""Unit tests for AURA v0.5.3.72 earnings blackout HORIZON check. Pure
decision logic -- no network, no filesystem, `.67.EarningsCalendarState`
is always a lightweight duck-typed fake here, mirroring `.368`'s own test
file conventions exactly."""
from __future__ import annotations

import importlib.util
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]  # matches `.368`'s test file convention: tests/ is one level below repo root


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


M = _load("aura_v05372_earnings_blackout_horizon_check", ROOT / "aura_v05372_earnings_blackout_horizon_check.py")


class FakeCalendarState:
    def __init__(self, *, status="OK", calendar_by_symbol=None, error=None):
        self.status = status
        self.calendar_by_symbol = calendar_by_symbol or {}
        self.error = error


# ============================================================================
# horizon_calendar_days_for -- the trading-day -> padded-calendar-day math
# ============================================================================


def test_horizon_days_for_default_20_bars_is_33():
    # ceil(20 * 7/5) = 28, + 5 safety margin = 33
    assert M.horizon_calendar_days_for(20) == 33


def test_horizon_days_for_5_bars():
    # ceil(5 * 7/5) = 7, + 5 = 12
    assert M.horizon_calendar_days_for(5) == 12


def test_horizon_days_for_1_bar():
    # ceil(1 * 1.4) = 2, + 5 = 7
    assert M.horizon_calendar_days_for(1) == 7


def test_horizon_days_rejects_zero():
    with pytest.raises(ValueError, match="INVALID_MAX_HOLD_BARS"):
        M.horizon_calendar_days_for(0)


def test_horizon_days_rejects_negative():
    with pytest.raises(ValueError, match="INVALID_MAX_HOLD_BARS"):
        M.horizon_calendar_days_for(-3)


def test_default_horizon_calendar_days_constant_matches_function():
    assert M.DEFAULT_HORIZON_CALENDAR_DAYS == M.horizon_calendar_days_for(M.DEFAULT_MAX_HOLD_BARS)


# ============================================================================
# earliest_earnings_date_in_window -- pure
# ============================================================================


def test_earliest_date_in_window_finds_single_hit():
    calendar = {"AAPL": (date(2026, 10, 20),)}
    hit = M.earliest_earnings_date_in_window(
        "AAPL", calendar, start=date(2026, 10, 1), end=date(2026, 10, 31),
    )
    assert hit == date(2026, 10, 20)


def test_earliest_date_in_window_picks_earliest_of_multiple():
    # a guidance-revision 8-K-style second entry, or just two cached rows --
    # regardless of why there are two, the EARLIEST one inside the window wins.
    calendar = {"AAPL": (date(2026, 10, 25), date(2026, 10, 5))}
    hit = M.earliest_earnings_date_in_window(
        "AAPL", calendar, start=date(2026, 10, 1), end=date(2026, 10, 31),
    )
    assert hit == date(2026, 10, 5)


def test_earliest_date_in_window_ignores_dates_outside_window():
    calendar = {"AAPL": (date(2026, 11, 15),)}  # past the window end
    hit = M.earliest_earnings_date_in_window(
        "AAPL", calendar, start=date(2026, 10, 1), end=date(2026, 10, 31),
    )
    assert hit is None


def test_earliest_date_in_window_inclusive_of_both_endpoints():
    calendar = {"AAPL": (date(2026, 10, 1),), "MSFT": (date(2026, 10, 31),)}
    assert M.earliest_earnings_date_in_window("AAPL", calendar, start=date(2026, 10, 1), end=date(2026, 10, 31)) == date(2026, 10, 1)
    assert M.earliest_earnings_date_in_window("MSFT", calendar, start=date(2026, 10, 1), end=date(2026, 10, 31)) == date(2026, 10, 31)


def test_earliest_date_in_window_symbol_absent_returns_none():
    calendar = {"MSFT": (date(2026, 10, 10),)}
    hit = M.earliest_earnings_date_in_window(
        "AAPL", calendar, start=date(2026, 10, 1), end=date(2026, 10, 31),
    )
    assert hit is None


def test_earliest_date_in_window_case_insensitive_symbol_lookup():
    calendar = {"AAPL": (date(2026, 10, 10),)}
    hit = M.earliest_earnings_date_in_window(
        "aapl", calendar, start=date(2026, 10, 1), end=date(2026, 10, 31),
    )
    assert hit == date(2026, 10, 10)


# ============================================================================
# evaluate_earnings_blackout_horizon -- the core distinction (same as
# `.368`): whole-calendar-unavailable (block everyone) vs. symbol simply
# absent/out-of-window on an OK calendar (allow normally).
# ============================================================================


def test_evaluate_allows_symbol_with_no_earnings_in_window():
    state = FakeCalendarState(calendar_by_symbol={"AAPL": (date(2026, 12, 25),)})  # far outside any reasonable horizon
    verdict = M.evaluate_earnings_blackout_horizon("AAPL", state, as_of_date=date(2026, 9, 23))
    assert verdict.allowed is True
    assert verdict.reasons == ()
    assert verdict.blackout_date is None
    assert verdict.calendar_status == "OK"
    assert verdict.horizon_calendar_days == 33


def test_evaluate_blocks_symbol_reporting_today():
    state = FakeCalendarState(calendar_by_symbol={"AAPL": (date(2026, 9, 23),)})
    verdict = M.evaluate_earnings_blackout_horizon("AAPL", state, as_of_date=date(2026, 9, 23))
    assert verdict.allowed is False
    assert verdict.blackout_date == date(2026, 9, 23)
    assert "EARNINGS_BLACKOUT_HORIZON:AAPL" in verdict.reasons[0]


def test_evaluate_blocks_symbol_reporting_later_in_the_window():
    # The entire point of this module vs `.368`: earnings is NOT today, but
    # falls inside the padded hold horizon.
    state = FakeCalendarState(calendar_by_symbol={"AAPL": (date(2026, 10, 15),)})  # 22 calendar days out, inside the 33-day default window
    verdict = M.evaluate_earnings_blackout_horizon("AAPL", state, as_of_date=date(2026, 9, 23))
    assert verdict.allowed is False
    assert verdict.blackout_date == date(2026, 10, 15)


def test_evaluate_allows_symbol_reporting_just_past_the_window():
    state = FakeCalendarState(calendar_by_symbol={"AAPL": (date(2026, 10, 27),)})  # 34 calendar days out -- past the 33-day default window
    verdict = M.evaluate_earnings_blackout_horizon("AAPL", state, as_of_date=date(2026, 9, 23))
    assert verdict.allowed is True


def test_evaluate_allows_etf_never_in_any_calendar_entry_when_status_ok():
    # DIA/XLK/etc. -- confirmed OK status, just never a key in the dict.
    state = FakeCalendarState(status="OK", calendar_by_symbol={"AAPL": (date(2026, 10, 1),)})
    verdict = M.evaluate_earnings_blackout_horizon("DIA", state, as_of_date=date(2026, 9, 23))
    assert verdict.allowed is True
    assert verdict.calendar_status == "OK"


def test_evaluate_blocks_every_symbol_when_calendar_unavailable_even_an_etf():
    state = FakeCalendarState(status="UNAVAILABLE", error="ConnectionError: timed out")
    verdict = M.evaluate_earnings_blackout_horizon("DIA", state, as_of_date=date(2026, 9, 23))
    assert verdict.allowed is False
    assert verdict.calendar_status == "UNAVAILABLE"
    assert "EARNINGS_CALENDAR_UNAVAILABLE" in verdict.reasons[0]
    assert "ConnectionError" in verdict.reasons[0]


def test_evaluate_unavailable_blocks_even_a_symbol_with_a_stale_calendar_entry():
    state = FakeCalendarState(status="UNAVAILABLE", calendar_by_symbol={"AAPL": (date(2026, 9, 24),)})
    verdict = M.evaluate_earnings_blackout_horizon("AAPL", state, as_of_date=date(2026, 9, 23))
    assert verdict.allowed is False
    assert "EARNINGS_CALENDAR_UNAVAILABLE" in verdict.reasons[0]


def test_evaluate_duck_types_a_calendar_state_missing_error_attribute():
    class MinimalState:
        status = "UNAVAILABLE"
        calendar_by_symbol = {}

    verdict = M.evaluate_earnings_blackout_horizon("AAPL", MinimalState(), as_of_date=date(2026, 9, 23))
    assert verdict.allowed is False


def test_evaluate_respects_custom_max_hold_bars():
    # A shorter hold horizon (5 bars -> 12-day window) should NOT catch an
    # earnings date that the default 20-bar/33-day window would catch.
    state = FakeCalendarState(calendar_by_symbol={"AAPL": (date(2026, 10, 15),)})  # 22 days out
    verdict_default = M.evaluate_earnings_blackout_horizon("AAPL", state, as_of_date=date(2026, 9, 23))
    verdict_short = M.evaluate_earnings_blackout_horizon("AAPL", state, as_of_date=date(2026, 9, 23), max_hold_bars=5)
    assert verdict_default.allowed is False
    assert verdict_short.allowed is True
    assert verdict_short.horizon_calendar_days == 12


# ============================================================================
# build_earnings_blackout_horizon_check_fn -- contract match with
# `.53.run_cycle`'s enforcement_check_fn; direction-agnostic.
# ============================================================================


def test_check_fn_matches_enforcement_check_fn_contract():
    state = FakeCalendarState(calendar_by_symbol={"AAPL": (date(2026, 9, 23),)})
    check = M.build_earnings_blackout_horizon_check_fn(state, as_of_date=date(2026, 9, 23))
    verdict = check("AAPL", "OPEN_LONG", "10", object())
    assert verdict.allowed is False


def test_check_fn_blocks_short_entries_identically_to_long_entries():
    state = FakeCalendarState(calendar_by_symbol={"TSLA": (date(2026, 10, 1),)})
    check = M.build_earnings_blackout_horizon_check_fn(state, as_of_date=date(2026, 9, 23))
    long_verdict = check("TSLA", "OPEN_LONG", "10", None)
    short_verdict = check("TSLA", "OPEN_SHORT", "10", None)
    assert long_verdict.allowed is False
    assert short_verdict.allowed is False


def test_check_fn_passes_through_custom_max_hold_bars():
    state = FakeCalendarState(calendar_by_symbol={"AAPL": (date(2026, 10, 15),)})
    check = M.build_earnings_blackout_horizon_check_fn(state, as_of_date=date(2026, 9, 23), max_hold_bars=5)
    verdict = check("AAPL", "OPEN_LONG", "10", None)
    assert verdict.allowed is True  # outside the shorter 12-day window


# ============================================================================
# Composition with `.368` via `.368.combine_enforcement_check_fns` -- the
# whole reason this module matches `.368`'s verdict shape (`.allowed` /
# `.reasons`) duck-type exactly.
# ============================================================================


def _load_368():
    candidates = [
        ROOT / "aura_v05368_earnings_blackout_gate.py",
        Path("/mnt/user-data/uploads/AI automated trading/aura-autonomous-trading-agent/aura_v05368_earnings_blackout_gate.py"),
    ]
    for gate_path in candidates:
        if gate_path.exists():
            return _load("aura_v05368_earnings_blackout_gate", gate_path)
    pytest.skip(f".368 not found in any of {candidates} -- composition test requires the real repo file")


def test_composes_with_368_via_its_combine_enforcement_check_fns():
    gate368 = _load_368()
    # Day-of clear (no earnings today) but inside the horizon window --
    # .368 alone would ALLOW; composed with this module, the combined
    # verdict must BLOCK.
    state = FakeCalendarState(calendar_by_symbol={"AAPL": (date(2026, 10, 10),)})
    check_368 = gate368.build_earnings_blackout_check_fn(state, as_of_date=date(2026, 9, 23))
    check_371 = M.build_earnings_blackout_horizon_check_fn(state, as_of_date=date(2026, 9, 23))

    solo_368 = check_368("AAPL", "OPEN_LONG", "10", None)
    assert solo_368.allowed is True  # .368 alone: not today, so it allows

    combined_fn = gate368.combine_enforcement_check_fns(check_368, check_371)
    combined = combined_fn("AAPL", "OPEN_LONG", "10", None)
    assert combined.allowed is False  # composed: the horizon check catches it
    assert any("EARNINGS_BLACKOUT_HORIZON" in r for r in combined.reasons)


def test_composes_with_368_both_allow_when_truly_clear():
    gate368 = _load_368()
    state = FakeCalendarState(calendar_by_symbol={})  # symbol never reports, OK status
    check_368 = gate368.build_earnings_blackout_check_fn(state, as_of_date=date(2026, 9, 23))
    check_371 = M.build_earnings_blackout_horizon_check_fn(state, as_of_date=date(2026, 9, 23))
    combined_fn = gate368.combine_enforcement_check_fns(check_368, check_371)
    combined = combined_fn("DIA", "OPEN_LONG", "10", None)
    assert combined.allowed is True
    assert combined.reasons == ()
