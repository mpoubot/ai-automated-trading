#!/usr/bin/env python3
"""
AURA Track B -- tests for the three new short-protective gates and their
integration module.

Tests are fully isolated from the real AURA repo's `TradingDecision`
(`.350`) -- a `SimpleNamespace` stand-in is used throughout, duck-typed
exactly the way `.368`'s own gate consumes it (plain attribute access:
`.symbol`, `.direction`, `.outcome`, etc; nothing here actually reads
those attributes off `decision` since every gate in this package is a
function of `(symbol, direction)` plus its own data feed, not of
`decision`'s fields -- but the fixture exists so call sites look exactly
like what `.53.run_cycle` would really pass).

The ONE real, non-isolated dependency is `aura_v05368_earnings_blackout_
gate.combine_enforcement_check_fns`, imported from its actual staged
path (via `short_protective_gates.py`'s own import fallback) -- this is
intentional: the task requires proving interop against the real function,
not a mock of it.
"""
from __future__ import annotations

import sys
from datetime import date, datetime
from types import SimpleNamespace

import pandas as pd
import pytest

from borrow_data_feeds import SyntheticFixtureBorrowFeeds, SyntheticFixtureShortInterestFeed
from borrow_fee_gate import (
    BorrowFeeVerdict,
    build_borrow_fee_check_fn,
    evaluate_borrow_fee_for_open_short,
    evaluate_open_short_for_recall,
)
from gap_tail_risk_gate import (
    build_gap_tail_risk_check_fn,
    compute_gap_aware_stop_distance,
    evaluate_gap_tail_risk,
)
from squeeze_crowding_gate import build_squeeze_crowding_check_fn, evaluate_squeeze_crowding_for_open_short
from short_protective_gates import ShortProtectiveGatesConfig, build_short_protective_check_fn


def fake_decision(**overrides) -> SimpleNamespace:
    base = dict(
        symbol="XYZ",
        direction="SHORT_LEANING",
        outcome="DECIDE_SHORT",
        base_rank_score=1.0,
        final_rank_score=1.0,
        decision_threshold=0.5,
        reasons=("fake",),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# ============================================================================
# borrow_fee_gate.py
# ============================================================================


def test_borrow_fee_hard_veto_not_easy_to_borrow():
    feed = SyntheticFixtureBorrowFeeds({"XYZ": (False, 0.01)})
    verdict = evaluate_borrow_fee_for_open_short("XYZ", feed)
    assert verdict.allowed is False
    assert "BORROW_NOT_EASY" in verdict.reasons[0]


def test_borrow_fee_hard_veto_fee_too_high():
    feed = SyntheticFixtureBorrowFeeds({"XYZ": (True, 0.35)})
    verdict = evaluate_borrow_fee_for_open_short("XYZ", feed, hard_veto_annualized_rate=0.20)
    assert verdict.allowed is False
    assert "BORROW_FEE_TOO_HIGH" in verdict.reasons[0]


def test_borrow_fee_missing_data_fails_closed():
    feed = SyntheticFixtureBorrowFeeds({})  # symbol not in fixture -> both fields None
    verdict = evaluate_borrow_fee_for_open_short("UNKNOWN", feed)
    assert verdict.allowed is False
    assert "BORROW_FEE_UNAVAILABLE" in verdict.reasons[0]


def test_borrow_fee_caution_zone_tightens_hold_time():
    feed = SyntheticFixtureBorrowFeeds({"XYZ": (True, 0.10)})  # between 0.05 and 0.20 defaults
    verdict = evaluate_borrow_fee_for_open_short("XYZ", feed)
    assert verdict.allowed is True
    assert verdict.suggested_hold_time_multiplier < 1.0
    assert "BORROW_FEE_CAUTION_ZONE" in verdict.reasons[0]


def test_borrow_fee_below_caution_zone_no_tightening():
    feed = SyntheticFixtureBorrowFeeds({"XYZ": (True, 0.01)})
    verdict = evaluate_borrow_fee_for_open_short("XYZ", feed)
    assert verdict.allowed is True
    assert verdict.suggested_hold_time_multiplier == 1.0
    assert verdict.reasons == ()


def test_borrow_fee_check_fn_scopes_to_open_short_only():
    feed = SyntheticFixtureBorrowFeeds({"XYZ": (False, 0.50)})  # would hard-veto if OPEN_SHORT
    check_fn = build_borrow_fee_check_fn(feed)
    decision = fake_decision()
    for direction in ("OPEN_LONG", "CLOSE_LONG", "CLOSE_SHORT"):
        verdict = check_fn("XYZ", direction, 10, decision)
        assert verdict.allowed is True, f"expected pass-through allow for {direction}"
    verdict = check_fn("XYZ", "OPEN_SHORT", 10, decision)
    assert verdict.allowed is False


def test_recall_forced_exit_on_fee_spike():
    feed = SyntheticFixtureBorrowFeeds({"XYZ": (True, 0.30)})  # 3x the entry rate below
    verdict = evaluate_open_short_for_recall(
        "XYZ", entry_borrow_fee_rate=0.10, borrow_feed=feed, spike_multiple_forces_exit=2.0,
    )
    assert verdict.allowed is False
    assert "BORROW_FEE_SPIKE" in verdict.reasons[0]


def test_recall_forced_exit_on_recall_flag():
    feed = SyntheticFixtureBorrowFeeds({"XYZ": (False, 0.10)})
    verdict = evaluate_open_short_for_recall("XYZ", entry_borrow_fee_rate=0.10, borrow_feed=feed)
    assert verdict.allowed is False
    assert "BORROW_RECALLED" in verdict.reasons[0]


def test_recall_allows_hold_when_fee_stable():
    feed = SyntheticFixtureBorrowFeeds({"XYZ": (True, 0.11)})
    verdict = evaluate_open_short_for_recall("XYZ", entry_borrow_fee_rate=0.10, borrow_feed=feed)
    assert verdict.allowed is True


def test_recall_fails_closed_on_missing_current_rate():
    feed = SyntheticFixtureBorrowFeeds({})  # unknown symbol -> current rate None
    verdict = evaluate_open_short_for_recall("GHOST", entry_borrow_fee_rate=0.10, borrow_feed=feed)
    assert verdict.allowed is False
    assert "BORROW_FEE_NOW_UNAVAILABLE" in verdict.reasons[0]


# ============================================================================
# squeeze_crowding_gate.py
# ============================================================================


def test_squeeze_crowding_lagged_tier_veto():
    si_feed = SyntheticFixtureShortInterestFeed(
        {"XYZ": (0.35, 8.0, date(2026, 9, 15))},
    )
    verdict = evaluate_squeeze_crowding_for_open_short(
        "XYZ", si_feed, bars_provider=None, short_interest_pct_veto=0.20, days_to_cover_veto=5.0,
    )
    assert verdict.allowed is False
    assert any("CROWDED_SHORT" in r for r in verdict.reasons)


def test_squeeze_crowding_lagged_tier_missing_data_noted_not_silent():
    si_feed = SyntheticFixtureShortInterestFeed({})  # unknown symbol -> both None
    verdict = evaluate_squeeze_crowding_for_open_short("XYZ", si_feed, bars_provider=None)
    assert verdict.allowed is True  # missing lagged data never fires a veto on its own
    assert any("LAGGED_SHORT_INTEREST_DATA_MISSING" in r for r in verdict.reasons)
    assert any("SAME_DAY_SQUEEZE_CHECK_SKIPPED" in r for r in verdict.reasons)


def _make_squeeze_bars(n: int = 25, squeeze_on_last: bool = False) -> pd.DataFrame:
    closes = [100.0 + i * 0.1 for i in range(n)]
    volumes = [1_000_000.0 for _ in range(n)]
    if squeeze_on_last:
        closes[-1] = closes[-2] * 1.15  # +15% on the last session
        volumes[-1] = volumes[-2] * 5.0  # 5x average volume
    return pd.DataFrame({"close": closes, "volume": volumes})


def test_squeeze_crowding_same_day_signature_veto():
    si_feed = SyntheticFixtureShortInterestFeed({"XYZ": (0.01, 1.0, date(2026, 9, 15))})  # calm lagged data
    bars = _make_squeeze_bars(squeeze_on_last=True)

    def bars_provider(symbol: str) -> pd.DataFrame:
        return bars

    verdict = evaluate_squeeze_crowding_for_open_short(
        "XYZ", si_feed, bars_provider,
        intraday_price_pct_threshold=0.10, intraday_volume_multiple_threshold=3.0, volume_lookback_days=20,
    )
    assert verdict.allowed is False
    assert any("SAME_DAY_SQUEEZE_SIGNATURE" in r for r in verdict.reasons)


def test_squeeze_crowding_calm_day_allows():
    si_feed = SyntheticFixtureShortInterestFeed({"XYZ": (0.01, 1.0, date(2026, 9, 15))})
    bars = _make_squeeze_bars(squeeze_on_last=False)

    def bars_provider(symbol: str) -> pd.DataFrame:
        return bars

    verdict = evaluate_squeeze_crowding_for_open_short("XYZ", si_feed, bars_provider)
    assert verdict.allowed is True


def test_squeeze_crowding_check_fn_scopes_to_open_short_only():
    si_feed = SyntheticFixtureShortInterestFeed({"XYZ": (0.35, 8.0, date(2026, 9, 15))})  # would veto if OPEN_SHORT
    check_fn = build_squeeze_crowding_check_fn(si_feed, bars_provider=None)
    decision = fake_decision()
    for direction in ("OPEN_LONG", "CLOSE_LONG", "CLOSE_SHORT"):
        verdict = check_fn("XYZ", direction, 10, decision)
        assert verdict.allowed is True
    verdict = check_fn("XYZ", "OPEN_SHORT", 10, decision)
    assert verdict.allowed is False


# ============================================================================
# gap_tail_risk_gate.py
# ============================================================================


def _make_gap_bars(n: int = 30, gap_pct: float = 0.005) -> pd.DataFrame:
    """Calm history: small, stable overnight gaps."""
    opens = []
    closes = []
    price = 100.0
    for i in range(n):
        opens.append(price * (1.0 + (gap_pct if i > 0 else 0.0)))
        price = opens[-1] * 1.001
        closes.append(price)
    return pd.DataFrame({"open": opens, "close": closes})


def _make_wild_gap_bars(n: int = 30) -> pd.DataFrame:
    """Large, volatile overnight gaps -- a fabricated large-gap-history symbol."""
    opens = []
    closes = []
    price = 100.0
    sign = 1
    for i in range(n):
        gap = 0.08 * sign  # +/-8% overnight gap every bar
        opens.append(price * (1.0 + (gap if i > 0 else 0.0)))
        price = opens[-1] * 1.0005
        closes.append(price)
        sign *= -1
    return pd.DataFrame({"open": opens, "close": closes})


def test_compute_gap_aware_stop_distance_uses_gap_when_wider_than_atr():
    bars = _make_wild_gap_bars()
    distance = compute_gap_aware_stop_distance(bars, atr=0.50, atr_multiple=2.0, overnight_gap_percentile=0.95)
    assert distance > 2.0 * 0.50  # wild gap history should dominate a tiny ATR


def test_compute_gap_aware_stop_distance_falls_back_on_insufficient_history():
    bars = pd.DataFrame({"open": [100.0, 101.0], "close": [100.5, 101.5]})  # too few rows
    distance = compute_gap_aware_stop_distance(bars, atr=0.50, atr_multiple=2.0)
    assert distance == pytest.approx(1.0)  # pure atr_multiple * atr fallback


def test_gap_tail_risk_vetoes_on_fabricated_wild_gap_symbol():
    bars = _make_wild_gap_bars()

    def bars_provider(symbol: str) -> pd.DataFrame:
        return bars

    verdict = evaluate_gap_tail_risk(
        "WILD", "OPEN_SHORT", quantity=1000, entry_price_estimate=100.0, account_equity_usd=100_000.0,
        bars_provider=bars_provider,
    )
    assert verdict.allowed is False
    assert "GAP_TAIL_RISK_TOO_LARGE" in verdict.reasons[0]


def test_gap_tail_risk_allows_on_calm_symbol_small_size():
    bars = _make_gap_bars()

    def bars_provider(symbol: str) -> pd.DataFrame:
        return bars

    verdict = evaluate_gap_tail_risk(
        "CALM", "OPEN_SHORT", quantity=5, entry_price_estimate=100.0, account_equity_usd=1_000_000.0,
        bars_provider=bars_provider,
    )
    assert verdict.allowed is True


def test_gap_tail_risk_fails_closed_on_insufficient_history():
    bars = pd.DataFrame({"open": [100.0, 101.0], "close": [100.5, 101.5]})

    def bars_provider(symbol: str) -> pd.DataFrame:
        return bars

    verdict = evaluate_gap_tail_risk(
        "THIN", "OPEN_SHORT", quantity=10, entry_price_estimate=100.0, account_equity_usd=1_000_000.0,
        bars_provider=bars_provider,
    )
    assert verdict.allowed is False
    assert "GAP_HISTORY_INSUFFICIENT" in verdict.reasons[0]


def test_gap_tail_risk_check_fn_adapter_scopes_to_open_short_only():
    bars = _make_wild_gap_bars()

    def bars_provider(symbol: str) -> pd.DataFrame:
        return bars

    check_fn = build_gap_tail_risk_check_fn(
        bars_provider,
        account_equity_lookup=lambda: 100_000.0,
        entry_price_lookup=lambda symbol: 100.0,
    )
    decision = fake_decision()
    for direction in ("OPEN_LONG", "CLOSE_LONG", "CLOSE_SHORT"):
        verdict = check_fn("WILD", direction, 1000, decision)
        assert verdict.allowed is True
    verdict = check_fn("WILD", "OPEN_SHORT", 1000, decision)
    assert verdict.allowed is False


# ============================================================================
# short_protective_gates.py -- full composition, using the REAL `.368`
# combine_enforcement_check_fns.
# ============================================================================


def _build_full_check_fn(
    *,
    not_easy_to_borrow: bool = False,
    borrow_fee_rate: float = 0.01,
    short_interest_pct: float = 0.01,
    days_to_cover: float = 1.0,
    squeeze_bars: pd.DataFrame | None = None,
    gap_bars: pd.DataFrame | None = None,
):
    borrow_feed = SyntheticFixtureBorrowFeeds({"XYZ": (not not_easy_to_borrow, borrow_fee_rate)})
    si_feed = SyntheticFixtureShortInterestFeed({"XYZ": (short_interest_pct, days_to_cover, date(2026, 9, 15))})
    bars = squeeze_bars if squeeze_bars is not None else _make_gap_bars()
    tail_bars = gap_bars if gap_bars is not None else _make_gap_bars()

    def bars_provider(symbol: str) -> pd.DataFrame:
        # squeeze_crowding_gate and gap_tail_risk_gate both read bars_provider;
        # for this test harness we let the two kinds of fabricated history
        # differ by returning whichever was supplied, defaulting to calm.
        return bars if squeeze_bars is not None else tail_bars

    return build_short_protective_check_fn(
        borrow_feed=borrow_feed,
        short_interest_feed=si_feed,
        bars_provider=bars_provider,
        account_equity_lookup=lambda: 1_000_000.0,
        entry_price_lookup=lambda symbol: 100.0,
    )


def test_composed_gate_allows_when_everything_calm():
    check_fn = _build_full_check_fn()
    decision = fake_decision()
    verdict = check_fn("XYZ", "OPEN_SHORT", 5, decision)
    assert verdict.allowed is True
    assert len(verdict.component_verdicts) == 3


def test_composed_gate_blocks_when_borrow_fee_too_high():
    check_fn = _build_full_check_fn(borrow_fee_rate=0.50)
    decision = fake_decision()
    verdict = check_fn("XYZ", "OPEN_SHORT", 5, decision)
    assert verdict.allowed is False
    assert any("BORROW_FEE_TOO_HIGH" in r for r in verdict.reasons)


def test_composed_gate_blocks_when_crowded():
    check_fn = _build_full_check_fn(short_interest_pct=0.35, days_to_cover=8.0)
    decision = fake_decision()
    verdict = check_fn("XYZ", "OPEN_SHORT", 5, decision)
    assert verdict.allowed is False
    assert any("CROWDED_SHORT" in r for r in verdict.reasons)


def test_composed_gate_blocks_when_wild_gap_history_and_large_size():
    check_fn = _build_full_check_fn(gap_bars=_make_wild_gap_bars())
    decision = fake_decision()
    verdict = check_fn("XYZ", "OPEN_SHORT", 1000, decision)
    assert verdict.allowed is False
    assert any("GAP_TAIL_RISK_TOO_LARGE" in r for r in verdict.reasons)


def test_composed_gate_ands_all_three_and_uses_real_368_combine_fn():
    # Prove real interop: the combined verdict type must be `.368`'s own
    # CombinedEnforcementVerdict, imported from its real staged path.
    from aura_v05368_earnings_blackout_gate import CombinedEnforcementVerdict

    check_fn = _build_full_check_fn(borrow_fee_rate=0.50, short_interest_pct=0.35, days_to_cover=8.0)
    decision = fake_decision()
    verdict = check_fn("XYZ", "OPEN_SHORT", 5, decision)
    assert isinstance(verdict, CombinedEnforcementVerdict)
    assert verdict.allowed is False
    # Both independent failures should be represented in the combined reasons.
    assert any("BORROW_FEE_TOO_HIGH" in r for r in verdict.reasons)
    assert any("CROWDED_SHORT" in r for r in verdict.reasons)


@pytest.mark.parametrize("direction", ["OPEN_LONG", "CLOSE_LONG", "CLOSE_SHORT"])
def test_composed_gate_passes_through_non_open_short_directions(direction):
    # Even with every underlying condition set to fail hard, non-OPEN_SHORT
    # directions must pass through untouched.
    check_fn = _build_full_check_fn(
        not_easy_to_borrow=True, borrow_fee_rate=0.90, short_interest_pct=0.90, days_to_cover=30.0,
        gap_bars=_make_wild_gap_bars(),
    )
    decision = fake_decision()
    verdict = check_fn("XYZ", direction, 1000, decision)
    assert verdict.allowed is True


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
