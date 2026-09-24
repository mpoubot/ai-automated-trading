#!/usr/bin/env python3
"""Contract + unit tests for AURA v0.5.3.59 Sector Rotation Regime Engine.

Mirrors `.48`'s own test-file convention (`_load()` helper loading the
module directly from its file path under its canonical name, flat-bar
fixtures for close-only math, hand-computed expected ROC/score values so
each assertion is independently checkable against the module's own
documented formulas) since `.359`, like `.48`, is a pure, stateless
research function with no external dependencies to fake.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ROTATION_PATH = ROOT / "aura_v05359_sector_rotation.py"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


ROTATION = _load("aura_v05359_sector_rotation", ROTATION_PATH)


def bar(ts, o, h, l, c):
    return {"timestamp": ts, "open": o, "high": h, "low": l, "close": c}


def flat_bar(ts, price):
    return bar(ts, price, price, price, price)


def bars_from_closes(prices):
    return [flat_bar(f"t{i}", p) for i, p in enumerate(prices)]


# ============================================================================
# roc()
# ============================================================================


def test_roc_requires_positive_lookback():
    with pytest.raises(ROTATION.SectorRotationError):
        ROTATION.roc(bars_from_closes([100, 101, 102]), 0)
    with pytest.raises(ROTATION.SectorRotationError):
        ROTATION.roc(bars_from_closes([100, 101, 102]), -1)


def test_roc_insufficient_history_returns_none():
    assert ROTATION.roc(bars_from_closes([100, 102, 104]), 4) is None


def test_roc_zero_anchor_returns_none():
    assert ROTATION.roc(bars_from_closes([0, 100, 102, 104, 106]), 4) is None


def test_roc_computed_correctly():
    bars = bars_from_closes([100, 102, 104, 106, 110])
    assert ROTATION.roc(bars, 4) == pytest.approx(0.10)


# ============================================================================
# rank_sectors()
# ============================================================================


def test_rank_sectors_empty_when_benchmark_insufficient():
    universe = {"XLK": bars_from_closes([100, 102, 104, 106, 110])}
    bench = bars_from_closes([100, 101])  # insufficient for lookback=4
    assert ROTATION.rank_sectors(universe, bench, lookback=4) == ()


def test_rank_sectors_excludes_members_with_insufficient_data():
    universe = {
        "XLK": bars_from_closes([100, 102, 104, 106, 110]),
        "XLF": bars_from_closes([100, 101]),  # insufficient
    }
    bench = bars_from_closes([100, 100, 100, 100, 100])
    ranked = ROTATION.rank_sectors(universe, bench, lookback=4)
    assert [sym for sym, _rs in ranked] == ["XLK"]


def test_rank_sectors_ranks_descending_by_relative_strength():
    bench = bars_from_closes([100, 100, 100, 100, 100])  # bench ROC = 0
    universe = {
        "STRONG": bars_from_closes([100, 102, 104, 106, 112]),  # ROC .12
        "WEAK": bars_from_closes([100, 99, 98, 97, 96]),  # ROC -.04
        "MID": bars_from_closes([100, 101, 102, 103, 105]),  # ROC .05
    }
    ranked = ROTATION.rank_sectors(universe, bench, lookback=4)
    assert [sym for sym, _rs in ranked] == ["STRONG", "MID", "WEAK"]
    strong_rs = dict(ranked)["STRONG"]
    assert strong_rs == pytest.approx(0.12)


# ============================================================================
# market_regime()
# ============================================================================


def test_market_regime_unknown_when_benchmark_insufficient():
    bench = bars_from_closes([100, 99])
    haven = {"TLT": bars_from_closes([100, 102, 104, 106, 110])}
    assert ROTATION.market_regime(bench, haven, lookback=4) == "UNKNOWN"


def test_market_regime_unknown_when_no_safe_haven_data():
    bench = bars_from_closes([110, 108, 106, 104, 100])  # ROC negative
    assert ROTATION.market_regime(bench, {}, lookback=4) == "UNKNOWN"


def test_market_regime_risk_on_when_benchmark_nonnegative():
    bench = bars_from_closes([100, 102, 104, 106, 110])
    haven = {"TLT": bars_from_closes([100, 101, 102, 103, 104])}
    assert ROTATION.market_regime(bench, haven, lookback=4) == "RISK_ON"


def test_market_regime_risk_on_when_haven_does_not_beat_negative_benchmark():
    bench = bars_from_closes([110, 108, 106, 104, 100])  # ROC -.0909
    haven = {"TLT": bars_from_closes([110, 105, 100, 95, 90])}  # ROC -.1818, underperforms bench (doesn't beat it)
    assert ROTATION.market_regime(bench, haven, lookback=4) == "RISK_ON"


def test_market_regime_risk_off_when_benchmark_negative_and_haven_confirms():
    bench = bars_from_closes([110, 108, 106, 104, 100])  # ROC -.0909
    haven = {"TLT": bars_from_closes([100, 102, 104, 106, 110])}  # ROC +.10, beats bench
    assert ROTATION.market_regime(bench, haven, lookback=4) == "RISK_OFF"


# ============================================================================
# defensive_switch_triggered()
# ============================================================================


def test_defensive_switch_requires_positive_ma_period():
    with pytest.raises(ROTATION.SectorRotationError):
        ROTATION.defensive_switch_triggered(bars_from_closes([1, 2, 3]), bars_from_closes([1, 2, 3]), ma_period=0)


def test_defensive_switch_insufficient_history_returns_none():
    cyc = bars_from_closes([100, 101])
    dfn = bars_from_closes([100, 101])
    assert ROTATION.defensive_switch_triggered(cyc, dfn, ma_period=3) is None


def test_defensive_switch_zero_denominator_returns_none():
    cyc = bars_from_closes([100, 101, 102, 103])
    dfn = bars_from_closes([100, 101, 0, 103])
    assert ROTATION.defensive_switch_triggered(cyc, dfn, ma_period=3) is None


def test_defensive_switch_triggered_when_ratio_falls_below_its_own_ma():
    # ratio series: 1.0, 1.0, 1.0, 0.5 -- MA of first 3 = 1.0, last ratio 0.5 < 1.0
    cyc = bars_from_closes([100, 100, 100, 50])
    dfn = bars_from_closes([100, 100, 100, 100])
    assert ROTATION.defensive_switch_triggered(cyc, dfn, ma_period=3) is True


def test_defensive_switch_not_triggered_when_ratio_at_or_above_ma():
    cyc = bars_from_closes([100, 100, 100, 150])
    dfn = bars_from_closes([100, 100, 100, 100])
    assert ROTATION.defensive_switch_triggered(cyc, dfn, ma_period=3) is False


# ============================================================================
# analyze_sector_rotation() -- required-parameter validation
# ============================================================================


def _base_kwargs(**overrides):
    bench = bars_from_closes([100, 102, 104, 106, 110])
    # A benign, slightly-positive default safe haven with sufficient history.
    # market_regime() returns UNKNOWN whenever NO safe-haven data is
    # available at all (a deliberate fail-closed choice, see .359's own
    # docstring) -- so any test that wants a real RISK_ON/RISK_OFF regime
    # determination (rather than the UNKNOWN/NO_DATA short-circuit) needs
    # at least one safe-haven series present, even when that test isn't
    # about safe havens itself. Tests that specifically want UNKNOWN
    # override this back to {} or supply their own.
    default_haven = {"SHV": bars_from_closes([100.0, 100.1, 100.2, 100.3, 100.4])}
    kwargs = dict(
        benchmark_symbol="SPY",
        benchmark_bars=bench,
        universe_bars={},
        safe_haven_symbols=(),
        safe_haven_bars=default_haven,
        defensive_ratio_pair=None,
        defensive_ratio_bars=None,
        lookback_bars=4,
        top_n=2,
        defensive_ma_period=3,
        score_scale=5.0,
    )
    kwargs.update(overrides)
    return kwargs


def test_analyze_sector_rotation_rejects_nonpositive_top_n():
    with pytest.raises(ROTATION.SectorRotationError):
        ROTATION.analyze_sector_rotation("XLK", bars_from_closes([100, 102, 104, 106, 110]), **_base_kwargs(top_n=0))


def test_analyze_sector_rotation_rejects_nonpositive_score_scale():
    with pytest.raises(ROTATION.SectorRotationError):
        ROTATION.analyze_sector_rotation("XLK", bars_from_closes([100, 102, 104, 106, 110]), **_base_kwargs(score_scale=0))


# ============================================================================
# analyze_sector_rotation() -- tier/score decision tree
# ============================================================================


def test_no_data_when_regime_unknown():
    insufficient_bench = bars_from_closes([100, 101])  # too short for lookback=4
    result = ROTATION.analyze_sector_rotation(
        "XLK", bars_from_closes([100, 102, 104, 106, 110]), **_base_kwargs(benchmark_bars=insufficient_bench)
    )
    assert result.market_regime == "UNKNOWN"
    assert result.rotation_tier == "NO_DATA"
    assert result.rotation_score is None


def test_safe_haven_favored_in_risk_off():
    bench = bars_from_closes([110, 108, 106, 104, 100])  # ROC -.0909
    tlt_bars = bars_from_closes([100, 102, 104, 106, 110])  # ROC +.10
    result = ROTATION.analyze_sector_rotation(
        "TLT",
        tlt_bars,
        **_base_kwargs(
            benchmark_bars=bench,
            safe_haven_symbols=("TLT",),
            safe_haven_bars={"TLT": tlt_bars},
        ),
    )
    assert result.market_regime == "RISK_OFF"
    assert result.rotation_tier == "SAFE_HAVEN_FAVORED"
    assert result.is_safe_haven is True
    assert result.rotation_score is not None
    assert result.rotation_score > 0


def test_safe_haven_not_favored_in_risk_on():
    bench = bars_from_closes([100, 102, 104, 106, 110])  # ROC +.10, RISK_ON
    tlt_bars = bars_from_closes([100, 101, 102, 103, 104])
    result = ROTATION.analyze_sector_rotation(
        "TLT",
        tlt_bars,
        **_base_kwargs(
            benchmark_bars=bench,
            safe_haven_symbols=("TLT",),
            safe_haven_bars={"TLT": tlt_bars},
        ),
    )
    assert result.market_regime == "RISK_ON"
    assert result.rotation_tier == "SAFE_HAVEN_NOT_FAVORED"
    assert result.rotation_score == 0.0


def test_not_applicable_when_outside_universe_and_no_own_data():
    bench = bars_from_closes([100, 102, 104, 106, 110])  # RISK_ON
    result = ROTATION.analyze_sector_rotation(
        "ZZZ",
        bars_from_closes([100, 101]),  # insufficient own data -> rs is None
        **_base_kwargs(benchmark_bars=bench),
    )
    assert result.market_regime == "RISK_ON"
    assert result.rotation_tier == "NOT_APPLICABLE"
    assert result.rotation_score is None


def test_laggard_when_risk_on_and_relative_strength_nonpositive():
    bench = bars_from_closes([100, 102, 104, 106, 110])  # ROC +.10, RISK_ON
    weak_bars = bars_from_closes([100, 99, 98, 97, 95])  # ROC -.05
    result = ROTATION.analyze_sector_rotation(
        "WEAK", weak_bars, **_base_kwargs(benchmark_bars=bench, universe_bars={"WEAK": weak_bars})
    )
    assert result.market_regime == "RISK_ON"
    assert result.rotation_tier == "LAGGARD"
    assert result.relative_strength == pytest.approx(-0.05 - 0.10)
    assert result.rotation_score < 0


def test_leader_when_risk_on_ranked_within_top_n():
    bench = bars_from_closes([100, 100, 100, 100, 100])  # ROC 0, RISK_ON
    universe = {
        "AAA": bars_from_closes([100, 105, 110, 115, 120]),  # ROC .20 -- strongest
        "BBB": bars_from_closes([100, 102, 104, 106, 108]),  # ROC .08
        "CCC": bars_from_closes([100, 101, 102, 103, 104]),  # ROC .04 -- weakest
    }
    result = ROTATION.analyze_sector_rotation(
        "AAA", universe["AAA"], **_base_kwargs(benchmark_bars=bench, universe_bars=universe, top_n=2)
    )
    assert result.market_regime == "RISK_ON"
    assert result.rotation_tier == "LEADER"
    assert result.sector_rank == 1
    assert result.universe_size == 3
    assert result.rotation_score is not None and result.rotation_score > 0


def test_ranked_not_top_when_risk_on_but_outside_top_n():
    bench = bars_from_closes([100, 100, 100, 100, 100])
    universe = {
        "AAA": bars_from_closes([100, 105, 110, 115, 120]),  # ROC .20
        "BBB": bars_from_closes([100, 102, 104, 106, 108]),  # ROC .08
        "CCC": bars_from_closes([100, 101, 102, 103, 104]),  # ROC .04 -- rank 3, outside top_n=2
    }
    result = ROTATION.analyze_sector_rotation(
        "CCC", universe["CCC"], **_base_kwargs(benchmark_bars=bench, universe_bars=universe, top_n=2)
    )
    assert result.rotation_tier == "RANKED_NOT_TOP"
    assert result.sector_rank == 3
    assert result.rotation_score is not None and result.rotation_score > 0
    # RANKED_NOT_TOP is scaled at half strength vs an equivalent LEADER score
    leader_equivalent = ROTATION._clip(result.relative_strength * result.score_scale, 0.0, 1.0)
    assert result.rotation_score == pytest.approx(leader_equivalent * 0.5)


def test_not_favored_risk_off_overrides_positive_relative_strength():
    bench = bars_from_closes([110, 108, 106, 104, 100])  # ROC -.0909
    haven_bars = bars_from_closes([100, 102, 104, 106, 110])  # ROC +.10, confirms RISK_OFF
    cyclical_bars = bars_from_closes([100, 101, 102, 103, 106])  # own ROC +.06, still beats declining bench
    result = ROTATION.analyze_sector_rotation(
        "XLY",
        cyclical_bars,
        **_base_kwargs(
            benchmark_bars=bench,
            universe_bars={"XLY": cyclical_bars},
            safe_haven_symbols=("TLT",),
            safe_haven_bars={"TLT": haven_bars},
        ),
    )
    assert result.market_regime == "RISK_OFF"
    assert result.relative_strength is not None and result.relative_strength > 0
    assert result.rotation_tier == "NOT_FAVORED_RISK_OFF"
    assert result.rotation_score == 0.0


def test_laggard_in_risk_off_when_relative_strength_nonpositive():
    bench = bars_from_closes([110, 108, 106, 104, 100])  # ROC -.0909
    haven_bars = bars_from_closes([100, 102, 104, 106, 110])  # confirms RISK_OFF
    weak_bars = bars_from_closes([110, 107, 104, 101, 95])  # ROC well below bench
    result = ROTATION.analyze_sector_rotation(
        "XLY",
        weak_bars,
        **_base_kwargs(
            benchmark_bars=bench,
            universe_bars={"XLY": weak_bars},
            safe_haven_symbols=("TLT",),
            safe_haven_bars={"TLT": haven_bars},
        ),
    )
    assert result.market_regime == "RISK_OFF"
    assert result.rotation_tier == "LAGGARD"
    assert result.rotation_score is not None and result.rotation_score < 0


def test_rotation_score_always_clipped_to_unit_range():
    bench = bars_from_closes([100, 100, 100, 100, 100])
    huge_move = bars_from_closes([100, 200, 300, 400, 1000])  # enormous ROC
    result = ROTATION.analyze_sector_rotation(
        "AAA", huge_move, **_base_kwargs(benchmark_bars=bench, universe_bars={"AAA": huge_move}, top_n=1, score_scale=50.0)
    )
    assert result.rotation_score is not None
    assert -1.0 <= result.rotation_score <= 1.0


def test_defensive_switch_field_recorded_but_does_not_alter_tier():
    bench = bars_from_closes([100, 102, 104, 106, 110])  # RISK_ON
    universe = {"AAA": bars_from_closes([100, 105, 110, 115, 120])}
    cyc = bars_from_closes([100, 100, 100, 50])
    dfn = bars_from_closes([100, 100, 100, 100])
    result = ROTATION.analyze_sector_rotation(
        "AAA",
        universe["AAA"],
        **_base_kwargs(
            benchmark_bars=bench,
            universe_bars=universe,
            top_n=1,
            defensive_ratio_pair=("CYC", "DFN"),
            defensive_ratio_bars=(cyc, dfn),
        ),
    )
    assert result.defensive_switch_triggered is True
    assert result.rotation_tier == "LEADER"  # unaffected -- switch is informational, not gating


def test_confirmation_features_count_bullish_and_bearish():
    up_bar_seq = [bar("t0", 100, 101, 99, 100), bar("t1", 100, 106, 99, 105)]  # close(105) > open(100): bullish
    bench_seq = [bar("t0", 100, 101, 99, 100), bar("t1", 100, 101, 99, 100.5)]  # smaller move than symbol
    bull, bear = ROTATION._confirmation_features(up_bar_seq, bench_seq)
    assert bull == 2  # both own-direction and relative-to-benchmark features are bullish
    assert bear == 0


def test_analyze_sector_rotation_result_to_dict_round_trips_fields():
    bench = bars_from_closes([100, 102, 104, 106, 110])
    universe = {"AAA": bars_from_closes([100, 105, 110, 115, 120])}
    result = ROTATION.analyze_sector_rotation(
        "AAA", universe["AAA"], **_base_kwargs(benchmark_bars=bench, universe_bars=universe, top_n=1)
    )
    d = result.to_dict()
    assert d["symbol"] == "AAA"
    assert d["rotation_tier"] == "LEADER"
    assert d["rotation_score"] == result.rotation_score


# ============================================================================
# select_rotation_leaders() -- portfolio-level helper
# ============================================================================


def test_select_rotation_leaders_returns_top_n_in_risk_on():
    bench = bars_from_closes([100, 100, 100, 100, 100])
    haven = {"SHV": bars_from_closes([100.0, 100.1, 100.2, 100.3, 100.4])}
    universe = {
        "AAA": bars_from_closes([100, 105, 110, 115, 120]),
        "BBB": bars_from_closes([100, 102, 104, 106, 108]),
        "CCC": bars_from_closes([100, 101, 102, 103, 104]),
    }
    leaders = ROTATION.select_rotation_leaders(
        universe, bench, haven, subsector_map=None, subsector_bars=None, lookback_bars=4, top_n=2
    )
    assert leaders == ("AAA", "BBB")


def test_select_rotation_leaders_falls_back_to_best_safe_haven_when_not_risk_on():
    bench = bars_from_closes([110, 108, 106, 104, 100])  # ROC negative
    haven = {
        "TLT": bars_from_closes([100, 102, 104, 106, 110]),  # ROC +.10, confirms RISK_OFF, strongest haven
        "GLD": bars_from_closes([100, 101, 102, 103, 104]),  # ROC +.04
    }
    universe = {"AAA": bars_from_closes([100, 105, 110, 115, 120])}
    leaders = ROTATION.select_rotation_leaders(
        universe, bench, haven, subsector_map=None, subsector_bars=None, lookback_bars=4, top_n=2
    )
    assert leaders == ("TLT",)


def test_select_rotation_leaders_empty_when_no_safe_haven_data_and_not_risk_on():
    bench = bars_from_closes([110, 108, 106, 104, 100])
    universe = {"AAA": bars_from_closes([100, 105, 110, 115, 120])}
    leaders = ROTATION.select_rotation_leaders(
        universe, bench, {}, subsector_map=None, subsector_bars=None, lookback_bars=4, top_n=2
    )
    assert leaders == ()


def test_select_rotation_leaders_amplifies_to_stronger_subsector():
    bench = bars_from_closes([100, 100, 100, 100, 100])
    haven = {"SHV": bars_from_closes([100.0, 100.1, 100.2, 100.3, 100.4])}
    universe = {"XLK": bars_from_closes([100, 102, 104, 106, 108])}  # ROC .08
    subsector_bars = {"SMH": bars_from_closes([100, 105, 110, 115, 120])}  # ROC .20, stronger than parent
    leaders = ROTATION.select_rotation_leaders(
        universe,
        bench,
        haven,
        subsector_map={"XLK": ("SMH",)},
        subsector_bars=subsector_bars,
        lookback_bars=4,
        top_n=1,
    )
    assert leaders == ("SMH",)


def test_select_rotation_leaders_keeps_parent_when_subsector_not_stronger():
    bench = bars_from_closes([100, 100, 100, 100, 100])
    haven = {"SHV": bars_from_closes([100.0, 100.1, 100.2, 100.3, 100.4])}
    universe = {"XLK": bars_from_closes([100, 105, 110, 115, 120])}  # ROC .20
    subsector_bars = {"SMH": bars_from_closes([100, 101, 102, 103, 104])}  # ROC .04, weaker than parent
    leaders = ROTATION.select_rotation_leaders(
        universe,
        bench,
        haven,
        subsector_map={"XLK": ("SMH",)},
        subsector_bars=subsector_bars,
        lookback_bars=4,
        top_n=1,
    )
    assert leaders == ("XLK",)
