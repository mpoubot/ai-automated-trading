#!/usr/bin/env python3
"""Contract + unit tests for AURA v0.5.3.48 Elliott Wave Research.

All price fixtures below were derived by running the module's own
functions against hand-picked prices and recording the actual output
(zigzag is a deterministic but non-trivial state machine -- these are not
independently hand-derived expected values, they are verified fixtures).
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EW_PATH = ROOT / "aura_v05348_elliott_wave_research.py"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


EW = _load("aura_v05348_elliott_wave_research", EW_PATH)


def flat_bar(ts, price):
    """A bar where open=high=low=close -- isolates the zigzag/rule math
    from OHLC-shape concerns and keeps fixtures exactly reproducible."""
    return {"timestamp": ts, "open": price, "high": price, "low": price, "close": price}


def bars_from(prices):
    return [flat_bar(f"t{i}", p) for i, p in enumerate(prices)]


# Verified fixture: 20 flat-bars, reversal_pct=1.0 -> 18 confirmed swings,
# 13 candidate windows, exactly 3 VALID_CANDIDATE (starts 0, 8, 12).
AMBIGUOUS_PRICES = [105, 100, 110, 104, 130, 120, 140, 125, 135, 115,
                     130, 120, 170, 140, 160, 150, 200, 170, 220, 210]

# Verified fixture: 11 flat-bars -> 9 confirmed swings, 4 candidate
# windows, exactly 1 VALID_CANDIDATE (start=0, UP, with a full ABC
# corrective candidate attached).
SINGLE_VALID_PRICES = [105, 100, 110, 104, 130, 120, 140, 125, 135, 115, 120]

# Verified fixture: 9 flat-bars -> exactly 6 confirmed swings, exactly 1
# candidate window, INVALIDATED (fails WAVE4_NO_OVERLAP_WITH_WAVE1).
ALL_INVALID_PRICES = [115, 110, 104, 130, 120, 140, 125, 138, 120]

# Verified fixture: 8 flat-bars -> only 5 confirmed swings (< 6 needed).
INSUFFICIENT_PRICES = [115, 110, 104, 130, 120, 140, 125, 140]


# ------------------------------------------------------------------------
# compute_zigzag_swings
# ------------------------------------------------------------------------

def test_reversal_pct_required_positive_zero_rejected():
    try:
        EW.compute_zigzag_swings(bars_from(SINGLE_VALID_PRICES), reversal_pct=0)
        assert False, "expected ElliottWaveError"
    except EW.ElliottWaveError:
        pass


def test_reversal_pct_none_rejected():
    try:
        EW.compute_zigzag_swings(bars_from(SINGLE_VALID_PRICES), reversal_pct=None)
        assert False, "expected ElliottWaveError"
    except EW.ElliottWaveError:
        pass


def test_reversal_pct_negative_rejected():
    try:
        EW.compute_zigzag_swings(bars_from(SINGLE_VALID_PRICES), reversal_pct=-1.0)
        assert False, "expected ElliottWaveError"
    except EW.ElliottWaveError:
        pass


def test_too_few_bars_returns_no_swings():
    assert EW.compute_zigzag_swings([], reversal_pct=1.0) == ()
    assert EW.compute_zigzag_swings(bars_from([100.0]), reversal_pct=1.0) == ()


def test_swings_alternate_high_low():
    swings = EW.compute_zigzag_swings(bars_from(AMBIGUOUS_PRICES), reversal_pct=1.0)
    assert len(swings) == 18
    for a, b in zip(swings, swings[1:]):
        assert a.kind != b.kind


def test_flat_price_series_produces_no_swings():
    swings = EW.compute_zigzag_swings(bars_from([100.0] * 20), reversal_pct=1.0)
    assert swings == ()


def test_sub_threshold_move_does_not_confirm_a_swing():
    # A ~0.5% wiggle should not confirm anything at a 1% threshold.
    prices = [100.0, 100.4, 100.0, 100.4, 100.0]
    swings = EW.compute_zigzag_swings(bars_from(prices), reversal_pct=1.0)
    assert swings == ()


# ------------------------------------------------------------------------
# compute_ewo
# ------------------------------------------------------------------------

def test_ewo_periods_must_be_valid():
    try:
        EW.compute_ewo(bars_from([100.0] * 40), fast_period=0)
        assert False, "expected ElliottWaveError"
    except EW.ElliottWaveError:
        pass
    try:
        EW.compute_ewo(bars_from([100.0] * 40), fast_period=34, slow_period=5)
        assert False, "expected ElliottWaveError"
    except EW.ElliottWaveError:
        pass


def test_ewo_none_before_slow_period_history_available():
    ewo = EW.compute_ewo(bars_from([100.0] * 40))
    assert ewo[32] is None
    assert ewo[33] is not None  # 34th bar (index 33) is the first with full slow-period history


def test_ewo_zero_on_flat_price_series():
    ewo = EW.compute_ewo(bars_from([100.0] * 40))
    assert ewo[33] == 0.0
    assert ewo[39] == 0.0


def test_ewo_empty_bars_returns_empty():
    assert EW.compute_ewo([]) == ()


# ------------------------------------------------------------------------
# evaluate_impulse_rules -- direct rule-math tests
# ------------------------------------------------------------------------

def _pt(idx, price, kind):
    return EW.SwingPoint(index=idx, timestamp=f"t{idx}", price=price, kind=kind)


def test_valid_up_impulse_passes_all_rules():
    window = (_pt(0, 100, "LOW"), _pt(1, 110, "HIGH"), _pt(2, 104, "LOW"),
              _pt(3, 130, "HIGH"), _pt(4, 120, "LOW"), _pt(5, 140, "HIGH"))
    direction, evals = EW.evaluate_impulse_rules(window)
    assert direction == "UP"
    assert all(e.passed for e in evals)
    assert {e.rule for e in evals} == {EW.RULE_WAVE2_RETRACE, EW.RULE_WAVE3_NOT_SHORTEST, EW.RULE_WAVE4_NO_OVERLAP}


def test_wave2_over_100pct_retrace_fails_rule1_up():
    # wave1 = 110-100=10; wave2 retraces to 95 (below P0=100) -> >100% retrace
    window = (_pt(0, 100, "LOW"), _pt(1, 110, "HIGH"), _pt(2, 95, "LOW"),
              _pt(3, 130, "HIGH"), _pt(4, 120, "LOW"), _pt(5, 140, "HIGH"))
    direction, evals = EW.evaluate_impulse_rules(window)
    assert direction == "UP"
    by_rule = {e.rule: e.passed for e in evals}
    assert by_rule[EW.RULE_WAVE2_RETRACE] is False


def test_wave3_shortest_fails_rule2_up():
    # wave1=110-100=10, wave3=P3-P2 small, wave5 large -> wave3 shortest
    window = (_pt(0, 100, "LOW"), _pt(1, 110, "HIGH"), _pt(2, 104, "LOW"),
              _pt(3, 107, "HIGH"), _pt(4, 105, "LOW"), _pt(5, 140, "HIGH"))
    direction, evals = EW.evaluate_impulse_rules(window)
    assert direction == "UP"
    by_rule = {e.rule: e.passed for e in evals}
    assert by_rule[EW.RULE_WAVE3_NOT_SHORTEST] is False


def test_wave4_overlap_fails_rule3_up():
    # P4 (120) must stay above P1 (130) for no-overlap; here it dips below.
    window = (_pt(0, 100, "LOW"), _pt(1, 130, "HIGH"), _pt(2, 110, "LOW"),
              _pt(3, 160, "HIGH"), _pt(4, 120, "LOW"), _pt(5, 180, "HIGH"))
    direction, evals = EW.evaluate_impulse_rules(window)
    assert direction == "UP"
    by_rule = {e.rule: e.passed for e in evals}
    assert by_rule[EW.RULE_WAVE4_NO_OVERLAP] is False


def test_valid_down_impulse_passes_all_rules():
    window = (_pt(0, 140, "HIGH"), _pt(1, 120, "LOW"), _pt(2, 130, "HIGH"),
              _pt(3, 104, "LOW"), _pt(4, 110, "HIGH"), _pt(5, 100, "LOW"))
    direction, evals = EW.evaluate_impulse_rules(window)
    assert direction == "DOWN"
    assert all(e.passed for e in evals)


def test_non_alternating_window_returns_none_direction():
    window = (_pt(0, 100, "LOW"), _pt(1, 110, "LOW"), _pt(2, 104, "LOW"),
              _pt(3, 130, "HIGH"), _pt(4, 120, "LOW"), _pt(5, 140, "HIGH"))
    direction, evals = EW.evaluate_impulse_rules(window)
    assert direction is None
    assert evals == ()


def test_wrong_length_window_returns_none_direction():
    window = (_pt(0, 100, "LOW"), _pt(1, 110, "HIGH"))
    direction, evals = EW.evaluate_impulse_rules(window)
    assert direction is None


def test_degenerate_zero_length_wave_reported_not_silently_valid():
    window = (_pt(0, 100, "LOW"), _pt(1, 100, "HIGH"), _pt(2, 100, "LOW"),
              _pt(3, 100, "HIGH"), _pt(4, 100, "LOW"), _pt(5, 100, "HIGH"))
    direction, evals = EW.evaluate_impulse_rules(window)
    assert direction == "UP"
    assert len(evals) == 1
    assert evals[0].rule == "DEGENERATE_WAVE_LENGTH"
    assert evals[0].passed is False


# ------------------------------------------------------------------------
# find_impulse_candidates / label_corrective_candidate
# ------------------------------------------------------------------------

def test_single_valid_fixture_has_exactly_one_valid_candidate_with_corrective():
    bars = bars_from(SINGLE_VALID_PRICES)
    swings = EW.compute_zigzag_swings(bars, reversal_pct=1.0)
    ewo = EW.compute_ewo(bars)
    candidates = EW.find_impulse_candidates(swings, ewo)
    assert len(candidates) == 4
    valid = [c for c in candidates if c.validity == "VALID_CANDIDATE"]
    assert len(valid) == 1
    assert valid[0].window_start_swing_index == 0
    assert valid[0].wave_direction == "UP"
    assert valid[0].corrective_candidate is not None
    assert valid[0].corrective_candidate.labels == ("A", "B", "C")


def test_corrective_candidate_none_when_fewer_than_3_pivots_follow():
    bars = bars_from(ALL_INVALID_PRICES)
    swings = EW.compute_zigzag_swings(bars, reversal_pct=1.0)
    assert len(swings) == 6
    corrective = EW.label_corrective_candidate(swings, after_index_in_swings=5)
    assert corrective is None


def test_corrective_candidate_never_fabricates_a_rule_verdict():
    bars = bars_from(SINGLE_VALID_PRICES)
    swings = EW.compute_zigzag_swings(bars, reversal_pct=1.0)
    corrective = EW.label_corrective_candidate(swings, after_index_in_swings=5)
    assert corrective is not None
    assert "no sourced invalidation rule" in corrective.note.lower()


def test_all_invalid_fixture_has_no_valid_candidates():
    bars = bars_from(ALL_INVALID_PRICES)
    swings = EW.compute_zigzag_swings(bars, reversal_pct=1.0)
    ewo = EW.compute_ewo(bars)
    candidates = EW.find_impulse_candidates(swings, ewo)
    assert len(candidates) == 1
    assert candidates[0].validity == "INVALIDATED"
    assert len(candidates[0].failed_rules) >= 1


def test_ewo_attached_to_candidate_endpoints():
    bars = bars_from(SINGLE_VALID_PRICES)
    swings = EW.compute_zigzag_swings(bars, reversal_pct=1.0)
    ewo = EW.compute_ewo(bars, fast_period=2, slow_period=3)  # small periods so this short fixture has EWO data
    candidates = EW.find_impulse_candidates(swings, ewo)
    # ewo values are whatever compute_ewo produced at those bar indices -- just confirm plumbing, not re-derive math.
    c0 = candidates[0]
    p3_idx = c0.pivots[3].index
    assert c0.ewo_at_wave3_end == ewo[p3_idx]


# ------------------------------------------------------------------------
# analyze_elliott_wave -- the 4 required ambiguity statuses
# ------------------------------------------------------------------------

def test_status_insufficient_data():
    result = EW.analyze_elliott_wave(bars_from(INSUFFICIENT_PRICES), "TEST", reversal_pct=1.0)
    assert result.ambiguity_status == "INSUFFICIENT_DATA"
    assert result.valid_candidate_count == 0
    assert result.impulse_candidates == ()


def test_status_all_candidates_invalidated():
    result = EW.analyze_elliott_wave(bars_from(ALL_INVALID_PRICES), "TEST", reversal_pct=1.0)
    assert result.ambiguity_status == "ALL_CANDIDATES_INVALIDATED"
    assert result.valid_candidate_count == 0
    assert len(result.impulse_candidates) == 1
    assert result.impulse_candidates[0].validity == "INVALIDATED"


def test_status_single_valid_candidate():
    result = EW.analyze_elliott_wave(bars_from(SINGLE_VALID_PRICES), "TEST", reversal_pct=1.0)
    assert result.ambiguity_status == "SINGLE_VALID_CANDIDATE"
    assert result.valid_candidate_count == 1


def test_status_ambiguous_multiple_candidates_not_collapsed_to_one():
    """The core requirement: when several candidates are independently
    valid, ALL of them must survive in the output -- never silently
    reduced to a single 'best' answer."""
    result = EW.analyze_elliott_wave(bars_from(AMBIGUOUS_PRICES), "TEST", reversal_pct=1.0)
    assert result.ambiguity_status == "AMBIGUOUS_MULTIPLE_CANDIDATES"
    assert result.valid_candidate_count == 3
    valid_starts = sorted(c.window_start_swing_index for c in result.impulse_candidates if c.validity == "VALID_CANDIDATE")
    assert valid_starts == [0, 8, 12]
    # every one of the 3 valid candidates' full pivot/rule evidence is present, not summarized away
    for c in result.impulse_candidates:
        if c.validity == "VALID_CANDIDATE":
            assert len(c.pivots) == 6
            assert len(c.rule_evaluations) == 3
            assert all(e.passed for e in c.rule_evaluations)


def test_reproducibility_same_inputs_same_output():
    bars = bars_from(AMBIGUOUS_PRICES)
    fixed_now = datetime(2026, 9, 13, 12, 0, 0, tzinfo=timezone.utc)
    r1 = EW.analyze_elliott_wave(bars, "TEST", reversal_pct=1.0, now=fixed_now)
    r2 = EW.analyze_elliott_wave(bars, "TEST", reversal_pct=1.0, now=fixed_now)
    assert r1.to_dict() == r2.to_dict()


def test_different_reversal_pct_can_change_result_but_still_deterministic():
    bars = bars_from(AMBIGUOUS_PRICES)
    r_tight = EW.analyze_elliott_wave(bars, "TEST", reversal_pct=1.0)
    r_wide = EW.analyze_elliott_wave(bars, "TEST", reversal_pct=50.0)
    assert len(r_wide.swings) <= len(r_tight.swings)


# ------------------------------------------------------------------------
# Look-ahead bias / no-repainting -- Martin's explicit requirement: a
# swing (and therefore any candidate built from it) may only become
# available at the point in time where the underlying reversal has
# ACTUALLY been confirmed by subsequent price action, never earlier.
# ------------------------------------------------------------------------

def test_no_repainting_prefix_consistency_across_every_truncation():
    """Analyzing bars[:k] for every k must produce swings that are an
    exact prefix of the swings produced by analyzing the full series --
    i.e. a confirmed swing never later changes value, moves, or
    disappears as more data arrives, and no swing appears before its
    confirming reversal actually happened."""
    bars = bars_from(AMBIGUOUS_PRICES)
    full_swings = EW.compute_zigzag_swings(bars, reversal_pct=1.0)
    assert len(full_swings) == 18
    for k in range(2, len(bars) + 1):
        partial_swings = EW.compute_zigzag_swings(bars[:k], reversal_pct=1.0)
        assert partial_swings == full_swings[:len(partial_swings)], f"repainting detected at k={k}"


def test_unconfirmed_final_swing_excluded_until_reversal_actually_happens():
    """At bars[:7] (up to and including the bar that reaches the future
    Wave-3 high of 140), that high has NOT yet reversed by reversal_pct,
    so it must NOT appear as a confirmed swing yet. One bar later
    (bars[:8]), the reversal has happened and it must appear -- and every
    swing confirmed at k=7 must be byte-identical at k=8 (no retroactive
    change)."""
    bars = bars_from(AMBIGUOUS_PRICES)
    swings_before = EW.compute_zigzag_swings(bars[:7], reversal_pct=1.0)
    swings_after = EW.compute_zigzag_swings(bars[:8], reversal_pct=1.0)
    assert len(swings_before) == 5
    assert len(swings_after) == 6
    assert swings_before == swings_after[:5]
    assert swings_after[5].price == 140
    assert swings_after[5].kind == "HIGH"


def test_incomplete_swing_sequence_cannot_produce_a_completed_wave_count():
    """At bars[:7] there are only 5 confirmed swings -- structurally not
    enough to form a 6-point impulse candidate. No candidate, valid or
    invalid, may be reported from unconfirmed/incomplete data."""
    bars = bars_from(AMBIGUOUS_PRICES)
    result = EW.analyze_elliott_wave(bars[:7], "TEST", reversal_pct=1.0)
    assert result.impulse_candidates == ()
    assert result.ambiguity_status == "INSUFFICIENT_DATA"

    # One bar later, exactly one candidate becomes possible -- and it is
    # the SAME candidate (by pivots) that the full series later confirms
    # for that same window, never a different one that then "repaints".
    result_next = EW.analyze_elliott_wave(bars[:8], "TEST", reversal_pct=1.0)
    assert len(result_next.impulse_candidates) == 1
    full_result = EW.analyze_elliott_wave(bars, "TEST", reversal_pct=1.0)
    first_full_candidate = next(c for c in full_result.impulse_candidates if c.window_start_swing_index == 0)
    assert result_next.impulse_candidates[0].pivots == first_full_candidate.pivots
    assert result_next.impulse_candidates[0].validity == first_full_candidate.validity


def test_growing_bar_series_never_invalidates_a_previously_valid_candidate():
    """A candidate's validity is a pure function of its own 6 pivots --
    once those pivots are confirmed, future bars (which cannot change
    already-confirmed pivots, per the no-repainting tests above) can
    never flip its verdict."""
    bars = bars_from(AMBIGUOUS_PRICES)
    r_at_8 = EW.analyze_elliott_wave(bars[:8], "TEST", reversal_pct=1.0)
    r_full = EW.analyze_elliott_wave(bars, "TEST", reversal_pct=1.0)
    c_at_8 = r_at_8.impulse_candidates[0]
    c_full_same_window = next(c for c in r_full.impulse_candidates if c.window_start_swing_index == 0)
    assert c_at_8.validity == c_full_same_window.validity == "VALID_CANDIDATE"
    assert c_at_8.pivots == c_full_same_window.pivots


# ------------------------------------------------------------------------
# Governance / non-goal invariants
# ------------------------------------------------------------------------

def test_module_has_no_trade_or_order_function():
    for name in dir(EW):
        lowered = name.lower()
        assert "submit_order" not in lowered
        assert "place_trade" not in lowered
        assert "authorize" not in lowered
        assert "execute" not in lowered


def test_module_has_no_nested_or_multi_degree_counting_function():
    assert not hasattr(EW, "count_nested_waves")
    assert not hasattr(EW, "multi_degree_wave_count")


def test_result_to_dict_carries_no_execution_fields():
    result = EW.analyze_elliott_wave(bars_from(INSUFFICIENT_PRICES), "TEST", reversal_pct=1.0)
    d = result.to_dict()
    assert "order_direction" not in d
    assert "position_size" not in d
    assert "trade_signal" not in d
    assert "execution_intent" not in d


def test_candidate_wave_direction_is_structural_not_a_trade_field_name():
    """wave_direction exists (it's the geometric shape of the pattern,
    UP/DOWN) but nothing in the candidate dict claims to be an order or
    signal field."""
    bars = bars_from(SINGLE_VALID_PRICES)
    result = EW.analyze_elliott_wave(bars, "TEST", reversal_pct=1.0)
    d = result.impulse_candidates[0].to_dict()
    assert "wave_direction" in d
    assert "order_side" not in d
    assert "signal" not in d
