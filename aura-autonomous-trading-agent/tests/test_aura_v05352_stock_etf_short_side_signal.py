#!/usr/bin/env python3
"""Contract + unit tests for AURA v0.5.3.52 Stock/ETF short-side technical
signal source.

Mirrors `.51`'s own test file structure and category list exactly
(current-bar evaluation, deterministic repeatability, missing/invalid/
incomplete OHLCV, stale data, boundary conditions, no-look-ahead/no-
repainting, evidence provenance, `.52` -> `.50` integration, absent-
evidence behavior, direct reuse of `.51`'s pinned universe, direct reuse
of `.51`'s live fetch layer, and long-only/short-only governance), applied
to the mirrored short-side module. Two complementary test strategies,
identical to `.51`'s own:

  1. `evaluate_current_bar_short` is tested against SYNTHETIC, hand-
     controlled feature DataFrames (via `_synthetic_features_short`) --
     exact, reproducible control over status/boundary conditions.
  2. `build_short_technical_regime_from_bars` is tested end-to-end against
     REALISTIC, deterministically-generated price series (via
     `_make_bars`, reused verbatim from `.51`'s own test module) --
     exercises the real `.51`-reused feature-computation pipeline.

Unlike `.51`, `.52` derives its own bearish EMA3/EMA8 downward-cross flag
LOCALLY from raw `ema_3`/`ema_8` values (current and prior row) rather
than reading a precomputed boolean column -- so, unlike `.51`'s synthetic
fixture (which sets an independent `cross_3_8` boolean directly,
decoupled from the underlying EMA values), `_synthetic_features_short`
must set genuinely consistent EMA3/EMA8 values across the tail to produce
a given cross/persistence combination. This is deliberate and is itself
part of what these tests verify -- `.52` cannot silently "cheat" a
crossover the way a hand-set boolean column could.

`.50` (`aura_v05350_decision_engine.py`) and `.51` (`aura_v05351_live_
alpaca_equity_signal_source.py`) are loaded dynamically for the `.52`
integration/reuse tests, mirroring `.50`'s and `.51`'s own test
conventions.
"""
from __future__ import annotations

import ast
import dataclasses
import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


M = _load("aura_v05352_stock_etf_short_side_signal", ROOT / "aura_v05352_stock_etf_short_side_signal.py")
M51 = _load("aura_v05351_live_alpaca_equity_signal_source", ROOT / "aura_v05351_live_alpaca_equity_signal_source.py")

NOW = datetime(2026, 9, 13, 12, 0, 0, tzinfo=timezone.utc)
NOW_ISO = NOW.isoformat()


# ============================================================================
# Shared fixtures
# ============================================================================


TEST_PARAMS = M.ShortTechnicalScoringParams(
    min_bars_required=55,
    max_bar_age_seconds=3 * 86400.0,
    ema3_cross_below_ema8_points=15.0,
    ema3_below_ema8_points=5.0,
    ema8_below_ema21_points=15.0,
    ema21_below_ema50_points=10.0,
    macd_bearish_points=10.0,
    rsi_weak_low=30.0,
    rsi_weak_high=50.0,
    rsi_weak_points=10.0,
    rsi_oversold_threshold=25.0,
    rsi_oversold_penalty_points=15.0,
    rel_volume_threshold=1.20,
    rel_volume_points=15.0,
    price_deceleration_points=5.0,
    persistence_lookback_bars=3,
    persistence_min_bars_for_bonus=3,
    persistence_points=5.0,
    squeeze_prior_move_threshold=0.01,
    squeeze_now_move_threshold=0.005,
    squeeze_penalty_points=20.0,
    score_floor=0.0,
    score_ceiling=100.0,
    early_status_max_persistence=1,
    confirmed_score_threshold=70.0,
    confirmed_min_persistence=2,
    confirming_score_threshold=45.0,
)


def _synthetic_features_short(n=60, *, overrides_last=None, ema38_tail=None):
    """A hand-controlled features-shaped DataFrame, bearish-by-default
    (ema_3 < ema_8 for every row, mirroring `.51`'s bullish-by-default
    fixture). No `cross_3_8`-style precomputed flag exists here -- `.52`
    derives its downward-cross condition from raw ema_3/ema_8 values on
    the current and prior row, so `ema38_tail` (inherited from `.51`'s
    own fixture design) is the only way to control a crossover.
    """
    idx = pd.date_range("2026-01-01", periods=n, freq="D", tz="UTC")
    data = {
        "close": [100.0] * n,
        "high": [100.5] * n,
        "low": [99.5] * n,
        "ema_3": [99.0] * n,
        "ema_8": [100.0] * n,  # ema_3 < ema_8 for every row by default -- bearish baseline
        "ema_21": [101.0] * n,
        "ema_50": [102.0] * n,  # ema_8 < ema_21 < ema_50 -- fully bearish stack by default
        "macd": [-0.5] * n,
        "macd_signal": [-0.3] * n,  # macd < macd_signal -- bearish by default
        "rsi_14": [40.0] * n,  # inside the weak/bearish band [30, 50] by default
        "rel_volume": [1.0] * n,
        "atr_pct": [1.0] * n,
        "price_acceleration": [0.0] * n,
    }
    df = pd.DataFrame(data, index=idx)
    if ema38_tail is not None:
        for offset, (e3, e8) in enumerate(reversed(ema38_tail), start=1):
            df.iloc[-offset, df.columns.get_loc("ema_3")] = e3
            df.iloc[-offset, df.columns.get_loc("ema_8")] = e8
    if overrides_last:
        for k, v in overrides_last.items():
            df.iloc[-1, df.columns.get_loc(k)] = v
    return df


def _make_bars(n=80, *, pattern=None, flat=False, base_price=100.0, volume=1_000_000, volume_spike_last=False, start=None):
    """Deterministic (no randomness), realistic-shaped OHLCV bars --
    duplicated from `.51`'s own test module (test helper, not production
    code -- both test files independently construct identical fixtures,
    mirroring how `.50`'s and `.51`'s own test files each define their
    own `_load` helper rather than importing one another's test module).
    """
    start = start or datetime(2026, 1, 1, tzinfo=timezone.utc)
    idx = pd.date_range(start=start, periods=n, freq="D", tz="UTC")
    closes = [base_price]
    for i in range(1, n):
        if flat:
            r = 0.0005 if i % 2 == 0 else -0.0005
        else:
            r = pattern[(i - 1) % len(pattern)]
        closes.append(closes[-1] * (1 + r))
    opens = [closes[0]] + closes[:-1]
    highs = [max(o, c) * 1.004 for o, c in zip(opens, closes)]
    lows = [min(o, c) * 0.996 for o, c in zip(opens, closes)]
    vols = [volume] * n
    if volume_spike_last:
        vols[-1] = int(volume * 2.0)
    return pd.DataFrame({"open": opens, "high": highs, "low": lows, "close": closes, "volume": vols}, index=idx)


PATTERN_MILD_BULLISH = [0.004, 0.003, 0.003, -0.0015, 0.003, 0.004]
# NOT the naive negation of `.51`'s PATTERN_MILD_BULLISH -- empirically
# verified (not hand-derived, per this project's own established
# convention for realistic-price-path fixtures): a symmetric negation of
# a sustained-trend pattern drives RSI to a near-0 extreme (via Wilder
# smoothing over ~80 consistent-direction bars), collapsing into the
# oversold PENALTY zone hard enough to mask the rest of the mirrored
# point budget and land on FAILED rather than CONFIRMING. This pattern
# (a long, gently declining stretch followed by a sharper recent
# leg down) reliably reproduces the intended CONFIRMING outcome at the
# same signal_score (45.0) `.51`'s own PATTERN_MILD_BULLISH produces
# (45.0, via its own RSI-extended-penalty path) -- verified directly
# against this module before being adopted as a fixture, not assumed.
PATTERN_MILD_BEARISH = [-0.0005] * 74 + [-0.006, -0.005, -0.005, -0.006, -0.005, -0.006]


# ============================================================================
# 1. Current-bar evaluation (synthetic, exact status control)
# ============================================================================


def test_evaluate_current_bar_short_confirmed():
    # Bearish-stack defaults (5+15+10+10+10=50) + persistence(5) = 55,
    # plus rel_volume confirm(15) + deceleration(5) = 75 -> CONFIRMED.
    feats = _synthetic_features_short(overrides_last={"rel_volume": 1.5, "price_acceleration": -0.01})
    result = M.evaluate_current_bar_short(feats, params=TEST_PARAMS)
    assert result["signal_score"] == pytest.approx(75.0)
    assert result["status"] == "CONFIRMED"
    assert result["persistence_bars"] == 3


def test_evaluate_current_bar_short_early_when_fresh_crossover_low_persistence():
    # A genuine downward cross ONLY on the last bar: prior two bars were
    # bullish (ema_3 >= ema_8), last bar flips bearish -> persistence == 1.
    feats = _synthetic_features_short(ema38_tail=[(100.0, 99.0), (100.0, 99.0), (99.0, 100.0)])
    result = M.evaluate_current_bar_short(feats, params=TEST_PARAMS)
    assert result["persistence_bars"] == 1
    assert result["status"] == "EARLY"
    assert "EMA3 crossed below EMA8" in result["reasons"]


def test_evaluate_current_bar_short_confirming_below_confirmed_threshold():
    # Pure bearish-stack defaults, no cross bonus (prior row also
    # bearish), no rel_volume/deceleration bonus: 5+15+10+10+10+5=55.
    result = M.evaluate_current_bar_short(_synthetic_features_short(), params=TEST_PARAMS)
    assert result["signal_score"] == pytest.approx(55.0)
    assert result["status"] == "CONFIRMING"


def test_evaluate_current_bar_short_failed_when_score_low():
    feats = _synthetic_features_short(
        overrides_last={
            "ema_3": 110.0, "ema_8": 105.0, "ema_21": 100.0, "ema_50": 95.0,  # bullish stack -- every "<" false
            "macd": 0.5, "macd_signal": 0.2,  # bullish, not bearish
            "rsi_14": 60.0,  # above the weak band, not oversold -- no bonus, no penalty
            "rel_volume": 0.5,
            "price_acceleration": 0.01,
        },
    )
    result = M.evaluate_current_bar_short(feats, params=TEST_PARAMS)
    assert result["status"] == "FAILED"
    assert result["signal_score"] < TEST_PARAMS.confirming_score_threshold


def test_evaluate_current_bar_short_rsi_oversold_penalty_applied():
    feats = _synthetic_features_short(overrides_last={"rel_volume": 0.5, "rsi_14": 10.0})
    result = M.evaluate_current_bar_short(feats, params=TEST_PARAMS)
    assert "RSI extremely oversold (squeeze/bounce risk)" in result["reasons"]
    # bearish stack (5+15+10+10=40) - oversold penalty(15) + persistence(5) = 30
    assert result["signal_score"] == pytest.approx(30.0)


def test_evaluate_current_bar_short_squeeze_penalty():
    feats = _synthetic_features_short(overrides_last={"rel_volume": 0.5})
    feats.iloc[-4, feats.columns.get_loc("close")] = 100.0
    feats.iloc[-3, feats.columns.get_loc("close")] = 100.0
    feats.iloc[-2, feats.columns.get_loc("close")] = 98.0  # prev_move = -2% < -1% threshold
    feats.iloc[-1, feats.columns.get_loc("close")] = 99.0  # now_move = +1.02% > +0.5% threshold
    result = M.evaluate_current_bar_short(feats, params=TEST_PARAMS)
    assert "Possible short squeeze / bearish momentum reversal" in result["reasons"]


def test_evaluate_current_bar_short_score_clamped_to_ceiling():
    tight_params = dataclasses.replace(TEST_PARAMS, score_ceiling=60.0)
    feats = _synthetic_features_short(overrides_last={"rel_volume": 1.5, "price_acceleration": -0.01})
    result = M.evaluate_current_bar_short(feats, params=tight_params)
    assert result["signal_score"] == pytest.approx(60.0)


def test_evaluate_current_bar_short_score_clamped_to_floor():
    tight_params = dataclasses.replace(TEST_PARAMS, score_floor=5.0)
    feats = _synthetic_features_short(
        overrides_last={
            "ema_3": 110.0, "ema_8": 105.0, "ema_21": 100.0, "ema_50": 95.0,  # bullish stack -- every "<" false
            "macd": 0.5, "macd_signal": 0.2,  # bullish, not bearish
            "rsi_14": 10.0,  # oversold -> -15 penalty, the only nonzero term
            "rel_volume": 0.5,
            "price_acceleration": 0.01,
        },
    )
    # raw score = -15 (oversold penalty only) -> clamped up to score_floor=5.0
    result = M.evaluate_current_bar_short(feats, params=tight_params)
    assert result["signal_score"] == pytest.approx(5.0)


# ============================================================================
# 2. Deterministic repeatability
# ============================================================================


def test_evaluate_current_bar_short_deterministic_same_input_same_output():
    feats = _synthetic_features_short(overrides_last={"rel_volume": 1.5, "price_acceleration": -0.01})
    r1 = M.evaluate_current_bar_short(feats, params=TEST_PARAMS)
    r2 = M.evaluate_current_bar_short(feats, params=TEST_PARAMS)
    assert r1 == r2


def test_build_short_technical_regime_from_bars_deterministic():
    bars = _make_bars(80, pattern=PATTERN_MILD_BEARISH, volume_spike_last=True)
    r1 = M.build_short_technical_regime_from_bars("AAPL", bars, params=TEST_PARAMS, universe_version="v1", now=bars.index[-1] + timedelta(hours=1))
    r2 = M.build_short_technical_regime_from_bars("AAPL", bars, params=TEST_PARAMS, universe_version="v1", now=bars.index[-1] + timedelta(hours=1))
    assert r1 == r2


# ============================================================================
# 3. Missing / invalid / incomplete OHLCV
# ============================================================================


def test_build_short_technical_regime_raises_on_none_bars():
    with pytest.raises(M.ShortSignalSourceError):
        M.build_short_technical_regime_from_bars("AAPL", None, params=TEST_PARAMS, universe_version="v1", now=NOW)


def test_build_short_technical_regime_raises_on_missing_columns():
    bad = pd.DataFrame({"close": [1.0, 2.0]}, index=pd.date_range("2026-01-01", periods=2, freq="D", tz="UTC"))
    with pytest.raises(M.ShortSignalSourceError):
        M.build_short_technical_regime_from_bars("AAPL", bad, params=TEST_PARAMS, universe_version="v1", now=NOW)


def test_build_short_technical_regime_insufficient_data_on_empty_frame():
    empty = pd.DataFrame({c: [] for c in M51.REQUIRED_BAR_COLUMNS}, index=pd.DatetimeIndex([], tz="UTC"))
    regime = M.build_short_technical_regime_from_bars("AAPL", empty, params=TEST_PARAMS, universe_version="v1", now=NOW)
    assert regime.status == "INSUFFICIENT_DATA"
    assert regime.bars_used == 0


def test_build_short_technical_regime_insufficient_data_on_too_few_bars():
    bars = _make_bars(TEST_PARAMS.min_bars_required - 1, pattern=PATTERN_MILD_BEARISH)
    regime = M.build_short_technical_regime_from_bars("AAPL", bars, params=TEST_PARAMS, universe_version="v1", now=bars.index[-1] + timedelta(hours=1))
    assert regime.status == "INSUFFICIENT_DATA"


def test_evaluate_current_bar_short_insufficient_data_on_nan_required_field():
    feats = _synthetic_features_short()
    feats.iloc[-1, feats.columns.get_loc("rsi_14")] = float("nan")
    result = M.evaluate_current_bar_short(feats, params=TEST_PARAMS)
    assert result["status"] == "INSUFFICIENT_DATA"


def test_evaluate_current_bar_short_insufficient_data_below_min_bars():
    feats = _synthetic_features_short(n=10)
    result = M.evaluate_current_bar_short(feats, params=TEST_PARAMS)
    assert result["status"] == "INSUFFICIENT_DATA"


def test_evaluate_current_bar_short_insufficient_data_on_nan_prior_ema():
    # Exercises `.52`'s own extra guard (not present in `.51`): the prior
    # row's ema_3/ema_8 must also be clean, since the downward-cross flag
    # reads it directly.
    feats = _synthetic_features_short()
    feats.iloc[-2, feats.columns.get_loc("ema_3")] = float("nan")
    result = M.evaluate_current_bar_short(feats, params=TEST_PARAMS)
    assert result["status"] == "INSUFFICIENT_DATA"
    assert "prior bar EMA3/EMA8" in result["reasons"][0]


# ============================================================================
# 4. Stale data
# ============================================================================


def test_build_short_technical_regime_stale_when_last_bar_too_old():
    bars = _make_bars(80, pattern=PATTERN_MILD_BEARISH)
    far_future_now = bars.index[-1].to_pydatetime() + timedelta(days=30)
    regime = M.build_short_technical_regime_from_bars("AAPL", bars, params=TEST_PARAMS, universe_version="v1", now=far_future_now)
    assert regime.status == "STALE_DATA"


def test_build_short_technical_regime_stale_when_bar_is_future_dated():
    bars = _make_bars(80, pattern=PATTERN_MILD_BEARISH)
    past_now = bars.index[-1].to_pydatetime() - timedelta(days=5)
    regime = M.build_short_technical_regime_from_bars("AAPL", bars, params=TEST_PARAMS, universe_version="v1", now=past_now)
    assert regime.status == "STALE_DATA"
    assert "future-dated" in regime.reasons[0]


def test_build_short_technical_regime_fresh_at_exact_boundary():
    bars = _make_bars(80, pattern=PATTERN_MILD_BEARISH)
    boundary_now = bars.index[-1].to_pydatetime() + timedelta(seconds=TEST_PARAMS.max_bar_age_seconds)
    regime = M.build_short_technical_regime_from_bars("AAPL", bars, params=TEST_PARAMS, universe_version="v1", now=boundary_now)
    assert regime.status != "STALE_DATA"


def test_build_short_technical_regime_stale_just_past_boundary():
    bars = _make_bars(80, pattern=PATTERN_MILD_BEARISH)
    just_past_now = bars.index[-1].to_pydatetime() + timedelta(seconds=TEST_PARAMS.max_bar_age_seconds + 1)
    regime = M.build_short_technical_regime_from_bars("AAPL", bars, params=TEST_PARAMS, universe_version="v1", now=just_past_now)
    assert regime.status == "STALE_DATA"


# ============================================================================
# 5. Boundary conditions
# ============================================================================


def test_build_short_technical_regime_exactly_min_bars_required_is_evaluated():
    bars = _make_bars(TEST_PARAMS.min_bars_required, pattern=PATTERN_MILD_BEARISH)
    regime = M.build_short_technical_regime_from_bars("AAPL", bars, params=TEST_PARAMS, universe_version="v1", now=bars.index[-1] + timedelta(hours=1))
    assert regime.status != "INSUFFICIENT_DATA"
    assert regime.bars_used == TEST_PARAMS.min_bars_required


def test_evaluate_current_bar_short_rsi_exactly_at_weak_band_edges():
    feats_low = _synthetic_features_short(overrides_last={"rsi_14": TEST_PARAMS.rsi_weak_low})
    feats_high = _synthetic_features_short(overrides_last={"rsi_14": TEST_PARAMS.rsi_weak_high})
    r_low = M.evaluate_current_bar_short(feats_low, params=TEST_PARAMS)
    r_high = M.evaluate_current_bar_short(feats_high, params=TEST_PARAMS)
    assert "RSI weak/bearish band" in r_low["reasons"]
    assert "RSI weak/bearish band" in r_high["reasons"]


def test_evaluate_current_bar_short_rel_volume_exactly_at_threshold_counts():
    feats = _synthetic_features_short(overrides_last={"rel_volume": TEST_PARAMS.rel_volume_threshold})
    result = M.evaluate_current_bar_short(feats, params=TEST_PARAMS)
    assert any("Relative volume" in r for r in result["reasons"])


def test_short_technical_scoring_params_rejects_invalid_fields():
    with pytest.raises(M.ShortSignalSourceError):
        dataclasses.replace(TEST_PARAMS, min_bars_required=0)
    with pytest.raises(M.ShortSignalSourceError):
        dataclasses.replace(TEST_PARAMS, max_bar_age_seconds=0)
    with pytest.raises(M.ShortSignalSourceError):
        dataclasses.replace(TEST_PARAMS, rsi_weak_low=80.0, rsi_weak_high=20.0)
    with pytest.raises(M.ShortSignalSourceError):
        dataclasses.replace(TEST_PARAMS, score_floor=90.0, score_ceiling=10.0)
    with pytest.raises(M.ShortSignalSourceError):
        dataclasses.replace(TEST_PARAMS, persistence_lookback_bars=0)


# ============================================================================
# 6. No look-ahead / no repainting
# ============================================================================


def test_no_look_ahead_appending_future_bars_does_not_change_past_regime():
    """`.52` adds no new feature COLUMNS of its own (it reuses `.51`'s
    already-verified-no-look-ahead `build_technical_features` directly),
    but it does add a new derived value (the local downward-cross check)
    read at evaluation time. This proves that value, and the regime built
    from it, is equally immune to look-ahead: evaluating the SAME
    historical cutoff produces an identical regime whether or not bars
    AFTER that cutoff exist in the DataFrame handed to `build_
    technical_features`.
    """
    full = _make_bars(120, pattern=PATTERN_MILD_BEARISH, volume_spike_last=True)
    truncated = full.iloc[:80]

    feats_full = M51.build_technical_features(full)
    feats_truncated = M51.build_technical_features(truncated)

    result_from_truncated = M.evaluate_current_bar_short(feats_truncated, params=TEST_PARAMS)
    result_from_full_sliced_to_cutoff = M.evaluate_current_bar_short(
        feats_full.loc[:truncated.index[-1]], params=TEST_PARAMS
    )
    assert result_from_truncated == result_from_full_sliced_to_cutoff


def test_no_repainting_evaluation_of_a_past_bar_is_stable():
    full = _make_bars(120, pattern=PATTERN_MILD_BEARISH, volume_spike_last=False)
    truncated = full.iloc[:80]
    regime_from_truncated = M.build_short_technical_regime_from_bars(
        "AAPL", truncated, params=TEST_PARAMS, universe_version="v1", now=truncated.index[-1] + timedelta(hours=1)
    )
    same_cutoff_again = full.iloc[:80]
    regime_again = M.build_short_technical_regime_from_bars(
        "AAPL", same_cutoff_again, params=TEST_PARAMS, universe_version="v1", now=truncated.index[-1] + timedelta(hours=1)
    )
    assert regime_from_truncated == regime_again


# ============================================================================
# 7. Evidence provenance
# ============================================================================


def test_short_technical_regime_carries_provenance_fields():
    bars = _make_bars(80, pattern=PATTERN_MILD_BEARISH, volume_spike_last=True)
    regime = M.build_short_technical_regime_from_bars("AAPL", bars, params=TEST_PARAMS, universe_version="v1", now=bars.index[-1] + timedelta(hours=1))
    assert regime.symbol == "AAPL"
    assert regime.universe_version == "v1"
    assert regime.bars_used == 80
    assert regime.last_bar_timestamp is not None
    assert len(regime.scoring_params_hash) == 64  # sha256 hex digest


def test_short_scoring_params_hash_changes_when_params_change():
    bars = _make_bars(80, pattern=PATTERN_MILD_BEARISH, volume_spike_last=True)
    r1 = M.build_short_technical_regime_from_bars("AAPL", bars, params=TEST_PARAMS, universe_version="v1", now=bars.index[-1] + timedelta(hours=1))
    other_params = dataclasses.replace(TEST_PARAMS, rel_volume_threshold=1.5)
    r2 = M.build_short_technical_regime_from_bars("AAPL", bars, params=other_params, universe_version="v1", now=bars.index[-1] + timedelta(hours=1))
    assert r1.scoring_params_hash != r2.scoring_params_hash


def test_short_technical_regime_rejects_invalid_status():
    with pytest.raises(M.ShortSignalSourceError):
        M.ShortTechnicalRegime(
            symbol="AAPL", as_of=NOW_ISO, last_bar_timestamp=NOW_ISO, status="BEARISH_EXTREME",
            signal_score=0.0, persistence_bars=0, rsi_14=None, rel_volume=None, atr_pct=None,
            price_acceleration=None, reasons=(), bars_used=0, universe_version="v1", scoring_params_hash="h",
        )


def test_short_technical_regime_status_vocabulary_reused_from_51_not_redefined():
    """Confirms the `.51` `A, DIRECT REUSE` claim for the status
    vocabulary is real, not just asserted in the docstring: `.52` uses the
    exact same frozenset objects `.51` defines, not a re-declared copy
    that could silently drift.
    """
    assert M51.TECHNICAL_STATUSES is M.load_equity_signal_module().TECHNICAL_STATUSES
    assert M51.USABLE_TECHNICAL_STATUSES is M.load_equity_signal_module().USABLE_TECHNICAL_STATUSES


# ============================================================================
# 8. `.52` -> `.50` integration
# ============================================================================

ENGINE = _load("aura_v05350_decision_engine", ROOT / "aura_v05350_decision_engine.py")


def test_short_technical_regime_integrates_into_50_candidate_evidence():
    bars = _make_bars(80, pattern=PATTERN_MILD_BEARISH, volume_spike_last=True)
    regime = M.build_short_technical_regime_from_bars("AAPL", bars, params=TEST_PARAMS, universe_version="v1", now=bars.index[-1] + timedelta(hours=1))
    assert regime.status in ("CONFIRMING", "CONFIRMED")  # empirically verified bearish pattern -> usable

    ev = ENGINE.build_candidate_evidence("AAPL", short_technical_regime=regime, now=bars.index[-1] + timedelta(hours=1))
    assert ev.short_technical_usable is True
    assert "SHORT_TECHNICAL" in ev.sources_present
    assert ENGINE.is_shortlist_eligible(ev) is True


def test_short_technical_regime_score_flows_into_50_base_rank_score_as_negative():
    bars = _make_bars(80, pattern=PATTERN_MILD_BEARISH, volume_spike_last=True)
    regime = M.build_short_technical_regime_from_bars("AAPL", bars, params=TEST_PARAMS, universe_version="v1", now=bars.index[-1] + timedelta(hours=1))
    ev = ENGINE.build_candidate_evidence("AAPL", short_technical_regime=regime, now=bars.index[-1] + timedelta(hours=1))
    score = ENGINE.compute_base_rank_score(
        ev, sentiment_weight=1.0, wave_weight=1.0, technical_weight=0.0, short_technical_weight=2.0
    )
    assert score == pytest.approx(-2.0 * (regime.signal_score / 100.0))
    assert score < 0  # SHORT-only: a usable short-technical regime never produces a positive contribution
    assert ENGINE.base_score_direction(score) == "SHORT_LEANING"


def test_failed_short_technical_regime_does_not_reach_50_shortlist():
    # `.51`'s own analogous test uses flat=True (its "not usable" case),
    # but `_make_bars(flat=True)` always ends on a down-tick (i=79 is
    # odd), which is itself a mild BEARISH tilt -- fine as a foil for
    # `.51`'s bullish scorer, but not a valid foil for `.52`'s bearish
    # scorer (empirically verified: it produces a false CONFIRMED here).
    # The correct, meaningful mirror is an UPTRENDING tape -- proving the
    # bearish scorer correctly does NOT confirm on bullish data.
    bars = _make_bars(80, pattern=PATTERN_MILD_BULLISH)
    regime = M.build_short_technical_regime_from_bars("AAPL", bars, params=TEST_PARAMS, universe_version="v1", now=bars.index[-1] + timedelta(hours=1))
    assert regime.status not in ("CONFIRMING", "CONFIRMED")  # empirically verified bullish pattern -> not usable for a short signal

    ev = ENGINE.build_candidate_evidence("AAPL", short_technical_regime=regime, now=bars.index[-1] + timedelta(hours=1))
    assert ev.short_technical_usable is False
    assert ENGINE.is_shortlist_eligible(ev) is False


def test_51_long_and_52_short_coexist_as_independent_dimensions():
    """Proves `.52` is genuinely additive, not merely non-crashing: a
    candidate carrying BOTH a usable `.51` long-technical regime and a
    usable `.52` short-technical regime combines them algebraically
    (add then subtract), and neither integration disturbs the other's
    own usability/sources-present bookkeeping.
    """
    long_bars = _make_bars(80, pattern=[0.004, 0.003, 0.003, -0.0015, 0.003, 0.004], volume_spike_last=True)
    long_regime = M51.build_technical_regime_from_bars(
        "AAPL", long_bars, params=_long_params_for_mix(), universe_version="v1", now=long_bars.index[-1] + timedelta(hours=1)
    )
    short_bars = _make_bars(80, pattern=PATTERN_MILD_BEARISH, volume_spike_last=True)
    short_regime = M.build_short_technical_regime_from_bars(
        "AAPL", short_bars, params=TEST_PARAMS, universe_version="v1", now=short_bars.index[-1] + timedelta(hours=1)
    )
    assert long_regime.status in ("CONFIRMING", "CONFIRMED")
    assert short_regime.status in ("CONFIRMING", "CONFIRMED")

    ev = ENGINE.build_candidate_evidence(
        "AAPL", technical_regime=long_regime, short_technical_regime=short_regime, now=NOW
    )
    assert ev.technical_usable is True
    assert ev.short_technical_usable is True
    assert "TECHNICAL" in ev.sources_present and "SHORT_TECHNICAL" in ev.sources_present

    score = ENGINE.compute_base_rank_score(
        ev, sentiment_weight=1.0, wave_weight=1.0, technical_weight=1.0, short_technical_weight=1.0
    )
    expected = (long_regime.signal_score / 100.0) - (short_regime.signal_score / 100.0)
    assert score == pytest.approx(expected)


def _long_params_for_mix():
    return M51.TechnicalScoringParams(
        min_bars_required=55, max_bar_age_seconds=3 * 86400.0, ema3_cross_ema8_points=15.0,
        ema3_above_ema8_points=5.0, ema8_above_ema21_points=15.0, ema21_above_ema50_points=10.0,
        macd_bullish_points=10.0, rsi_constructive_low=50.0, rsi_constructive_high=70.0,
        rsi_constructive_points=10.0, rsi_extended_threshold=75.0, rsi_extended_penalty_points=15.0,
        rel_volume_threshold=1.20, rel_volume_points=15.0, price_acceleration_points=5.0,
        persistence_lookback_bars=3, persistence_min_bars_for_bonus=3, persistence_points=5.0,
        whipsaw_prior_move_threshold=0.01, whipsaw_now_move_threshold=0.005, whipsaw_penalty_points=20.0,
        score_floor=0.0, score_ceiling=100.0, early_status_max_persistence=1, confirmed_score_threshold=70.0,
        confirmed_min_persistence=2, confirming_score_threshold=45.0,
    )


# ============================================================================
# 9. Behavior when short-technical evidence is absent
# ============================================================================


def test_50_build_candidate_evidence_unaffected_when_short_technical_absent():
    ev = ENGINE.build_candidate_evidence("BTC/USDT:USDT", now=NOW)
    assert ev.short_technical_regime is None
    assert ev.short_technical_usable is False
    assert "SHORT_TECHNICAL" not in ev.sources_present
    assert ENGINE.is_shortlist_eligible(ev) is False  # no source at all here


def test_51_own_behavior_fully_preserved_when_52_absent():
    """`.51`'s existing integration is untouched by `.52`'s existence --
    the same assertion `.51`'s own suite already makes, re-verified here
    from `.52`'s test module for direct evidence this addition is
    additive, not invasive.
    """
    bars = _make_bars(80, pattern=[0.004, 0.003, 0.003, -0.0015, 0.003, 0.004], volume_spike_last=True)
    regime = M51.build_technical_regime_from_bars(
        "AAPL", bars, params=_long_params_for_mix(), universe_version="v1", now=bars.index[-1] + timedelta(hours=1)
    )
    ev = ENGINE.build_candidate_evidence("AAPL", technical_regime=regime, now=bars.index[-1] + timedelta(hours=1))
    assert ev.technical_usable is True
    assert ev.short_technical_regime is None
    assert ev.short_technical_usable is False
    score = ENGINE.compute_base_rank_score(
        ev, sentiment_weight=1.0, wave_weight=1.0, technical_weight=2.0, short_technical_weight=0.0
    )
    assert score == pytest.approx(2.0 * (regime.signal_score / 100.0))
    assert score > 0


# ============================================================================
# 10. Pinned universe -- direct reuse of `.51`'s loader/config, not
# redefined
# ============================================================================


def test_short_signal_reuses_51_pinned_universe_directly():
    m51 = M.load_equity_signal_module()
    universe = m51.load_pinned_universe()
    assert universe.version == "v1"
    assert len(universe.symbols) == 27
    for expected in ("AAPL", "MSFT", "SPY", "QQQ"):
        assert expected in universe.symbols
    # `.52` defines no universe-loading function of its own.
    assert not hasattr(M, "load_pinned_universe")


# ============================================================================
# 11. Live fetch layer -- direct reuse of `.51`'s fetch code, fake client,
# no network/credentials required
# ============================================================================


class _FakeBarsResponse:
    def __init__(self, df):
        self.df = df


class _FakeBarsClient:
    def __init__(self, df_to_return=None, exception=None):
        self._df = df_to_return
        self._exception = exception

    def get_stock_bars(self, request):
        if self._exception is not None:
            raise self._exception
        return _FakeBarsResponse(self._df)


def _multiindex_bars(symbol, n=80, pattern=None):
    single = _make_bars(n, pattern=pattern or PATTERN_MILD_BEARISH)
    single = single.reset_index().rename(columns={"index": "timestamp"})
    single["symbol"] = symbol
    return single.set_index(["symbol", "timestamp"])


def test_module_defines_no_fetch_code_of_its_own():
    """Confirms `.52`'s `A, DIRECT REUSE` claim for the live fetch layer:
    no `fetch_recent_bars`/`AlpacaHistoricalBarsClient`/`BarsClient` is
    redefined in this module -- only the orchestration entry point that
    calls `.51`'s.
    """
    assert not hasattr(M, "fetch_recent_bars")
    assert not hasattr(M, "AlpacaHistoricalBarsClient")
    assert not hasattr(M, "BarsClient")


def test_fetch_live_short_technical_regime_wraps_fetch_exceptions_fail_closed():
    client = _FakeBarsClient(exception=RuntimeError("connection reset"))
    with pytest.raises((M.ShortSignalSourceError, M51.LiveSignalSourceError)):
        M.fetch_live_short_technical_regime(
            "AAPL", bars_client=client, params=TEST_PARAMS, lookback_bars=80, universe_version="v1", now=NOW
        )


def test_fetch_live_short_technical_regime_never_silently_falls_back_on_failure():
    client = _FakeBarsClient(exception=RuntimeError("boom"))
    raised = False
    try:
        M.fetch_live_short_technical_regime(
            "AAPL", bars_client=client, params=TEST_PARAMS, lookback_bars=80, universe_version="v1", now=NOW
        )
    except (M.ShortSignalSourceError, M51.LiveSignalSourceError):
        raised = True
    assert raised is True


def test_fetch_live_short_technical_regime_end_to_end_with_fake_client():
    df = _multiindex_bars("AAPL", n=100, pattern=PATTERN_MILD_BEARISH)
    client = _FakeBarsClient(df_to_return=df)
    end = df.index.get_level_values("timestamp")[-1].to_pydatetime() + timedelta(hours=1)
    regime = M.fetch_live_short_technical_regime(
        "AAPL", bars_client=client, params=TEST_PARAMS, lookback_bars=80, universe_version="v1", now=end
    )
    assert regime.symbol == "AAPL"
    assert regime.status in M51.TECHNICAL_STATUSES


# ============================================================================
# 12. Governance / short-only invariant
# ============================================================================


def test_module_has_no_long_or_bullish_scoring_path():
    """AST-based, not a raw substring search -- mirrors `.51`'s own
    governance test exactly, direction-flipped. This module's docstring
    legitimately discusses `.51`'s long/bullish vocabulary in prose (it
    explains the mirror relationship), which would false-positive a raw
    grep the same way `.51`'s own docstring did for "short"/"bearish".
    """
    source = Path(M.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    lowered = {n.lower() for n in names}
    banned_fragments = ("bullish",)
    offenders = {n for n in lowered for frag in banned_fragments if frag in n}
    assert not offenders, f"unexpected bullish-named identifier(s) in module: {offenders}"
    # "long" itself is a common substring (e.g. none expected here, but a
    # bare "long" fragment check would false-positive on words like
    # "belong"/"prolong" if they ever appeared) -- checked as a whole
    # identifier match instead, not a substring, for this one fragment.
    assert not any(n == "long" or n.startswith("long_") or n.endswith("_long") for n in lowered)


def test_module_has_no_order_or_execution_function():
    source = Path(M.__file__).read_text(encoding="utf-8")
    banned_substrings = ("submit_order", "place_order", "execute_trade", "cancel_order", "send_order")
    for token in banned_substrings:
        assert token not in source


def test_short_technical_weight_rejects_negative():
    bars = _make_bars(80, pattern=PATTERN_MILD_BEARISH, volume_spike_last=True)
    regime = M.build_short_technical_regime_from_bars("AAPL", bars, params=TEST_PARAMS, universe_version="v1", now=bars.index[-1] + timedelta(hours=1))
    ev = ENGINE.build_candidate_evidence("AAPL", short_technical_regime=regime, now=bars.index[-1] + timedelta(hours=1))
    with pytest.raises(ENGINE.DecisionEngineError):
        ENGINE.compute_base_rank_score(
            ev, sentiment_weight=1.0, wave_weight=1.0, technical_weight=0.0, short_technical_weight=-0.1
        )


def test_short_technical_term_never_adds_regardless_of_weight_magnitude():
    """The core governance invariant of this entire milestone: for ANY
    non-negative weight, a usable short-technical regime's contribution
    to the base rank score is <= 0. There is no weight value a caller
    could supply that would make `.52`'s term positive.
    """
    bars = _make_bars(80, pattern=PATTERN_MILD_BEARISH, volume_spike_last=True)
    regime = M.build_short_technical_regime_from_bars("AAPL", bars, params=TEST_PARAMS, universe_version="v1", now=bars.index[-1] + timedelta(hours=1))
    assert regime.status in ("CONFIRMING", "CONFIRMED")
    ev = ENGINE.build_candidate_evidence("AAPL", short_technical_regime=regime, now=bars.index[-1] + timedelta(hours=1))
    for w in (0.0, 0.5, 1.0, 5.0, 100.0):
        score = ENGINE.compute_base_rank_score(
            ev, sentiment_weight=0.0, wave_weight=0.0, technical_weight=0.0, short_technical_weight=w
        )
        assert score <= 0.0
