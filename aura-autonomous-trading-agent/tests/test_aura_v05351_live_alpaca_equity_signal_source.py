#!/usr/bin/env python3
"""Contract + unit tests for AURA v0.5.3.51 Live Alpaca equity/ETF
technical signal source.

Two complementary test strategies are used deliberately:

  1. `evaluate_current_bar` is tested against SYNTHETIC, hand-controlled
     feature DataFrames (via `_synthetic_features`) -- this gives exact,
     reproducible control over which status (EARLY/CONFIRMING/CONFIRMED/
     FAILED) and boundary condition (RSI band edges, rel_volume
     threshold, whipsaw trigger, score clamp) each test exercises,
     without depending on reverse-engineering a realistic price path
     that happens to land exactly on a given score.
  2. `build_technical_features`/`build_technical_regime_from_bars` are
     tested end-to-end against REALISTIC, deterministically-generated
     price series (via `_make_bars`) -- these exercise the real
     feature-computation pipeline (no-look-ahead, reproducibility,
     freshness, `.50` integration) where the exact resulting status is
     empirically verified once and asserted, not hand-picked.

`.50` (`aura_v05350_decision_engine.py`) is loaded dynamically for the
`.51` -> `.50` integration tests, mirroring `.50`'s own test convention
of loading its dependencies under their canonical module names first.
"""
from __future__ import annotations

import ast
import dataclasses
import importlib.util
import json
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


M = _load("aura_v05351_live_alpaca_equity_signal_source", ROOT / "aura_v05351_live_alpaca_equity_signal_source.py")

NOW = datetime(2026, 9, 13, 12, 0, 0, tzinfo=timezone.utc)
NOW_ISO = NOW.isoformat()


# ============================================================================
# Shared fixtures
# ============================================================================


TEST_PARAMS = M.TechnicalScoringParams(
    min_bars_required=55,
    max_bar_age_seconds=3 * 86400.0,
    ema3_cross_ema8_points=15.0,
    ema3_above_ema8_points=5.0,
    ema8_above_ema21_points=15.0,
    ema21_above_ema50_points=10.0,
    macd_bullish_points=10.0,
    rsi_constructive_low=50.0,
    rsi_constructive_high=70.0,
    rsi_constructive_points=10.0,
    rsi_extended_threshold=75.0,
    rsi_extended_penalty_points=15.0,
    rel_volume_threshold=1.20,
    rel_volume_points=15.0,
    price_acceleration_points=5.0,
    persistence_lookback_bars=3,
    persistence_min_bars_for_bonus=3,
    persistence_points=5.0,
    whipsaw_prior_move_threshold=0.01,
    whipsaw_now_move_threshold=0.005,
    whipsaw_penalty_points=20.0,
    score_floor=0.0,
    score_ceiling=100.0,
    early_status_max_persistence=1,
    confirmed_score_threshold=70.0,
    confirmed_min_persistence=2,
    confirming_score_threshold=45.0,
)


def _synthetic_features(n=60, *, overrides_last=None, ema38_tail=None):
    """A hand-controlled features-shaped DataFrame -- every column
    `evaluate_current_bar` reads is present with neutral defaults, and
    the last row (plus optionally the EMA3/EMA8 tail, for persistence
    control) can be overridden precisely.
    """
    idx = pd.date_range("2026-01-01", periods=n, freq="D", tz="UTC")
    data = {
        "close": [100.0] * n,
        "high": [100.5] * n,
        "low": [99.5] * n,
        "cross_3_8": [False] * n,
        "ema_3": [100.0] * n,
        "ema_8": [99.0] * n,  # ema_3 > ema_8 for every row by default
        "ema_21": [98.0] * n,
        "ema_50": [97.0] * n,
        "macd": [0.5] * n,
        "macd_signal": [0.3] * n,
        "rsi_14": [60.0] * n,
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


PATTERN_MILD_BULLISH = [0.004, 0.003, 0.003, -0.0015, 0.003, 0.004]
PATTERN_FLAT = None  # handled specially
PATTERN_DOWN = [-0.004, -0.003, -0.003, 0.0015, -0.003, -0.004]


def _make_bars(n=80, *, pattern=None, flat=False, base_price=100.0, volume=1_000_000, volume_spike_last=False, start=None):
    """Deterministic (no randomness), realistic-shaped OHLCV bars for
    end-to-end feature/regime tests. `pattern` is a cyclic list of daily
    returns.
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


# ============================================================================
# 1. Current-bar evaluation (synthetic, exact status control)
# ============================================================================


def test_evaluate_current_bar_confirmed():
    feats = _synthetic_features(overrides_last={"cross_3_8": True})
    result = M.evaluate_current_bar(feats, params=TEST_PARAMS)
    # cross(15) + ema3>8(5) + ema8>21(15) + ema21>50(10) + macd(10) + rsi(10) + persistence(5) = 70
    assert result["signal_score"] == pytest.approx(70.0)
    assert result["status"] == "CONFIRMED"
    assert result["persistence_bars"] == 3


def test_evaluate_current_bar_early_when_fresh_crossover_low_persistence():
    # ema3>ema8 only on the very last bar -> persistence == 1 <= early_status_max_persistence
    feats = _synthetic_features(overrides_last={"cross_3_8": True}, ema38_tail=[(99.0, 100.0), (99.0, 100.0), (100.0, 99.0)])
    result = M.evaluate_current_bar(feats, params=TEST_PARAMS)
    assert result["persistence_bars"] == 1
    assert result["status"] == "EARLY"


def test_evaluate_current_bar_confirming_below_confirmed_threshold():
    feats = _synthetic_features(overrides_last={"cross_3_8": False, "rel_volume": 0.5})
    # ema3>8(5) + ema8>21(15) + ema21>50(10) + macd(10) + rsi(10) + persistence(5) = 55 -> CONFIRMING (>=45, <70)
    result = M.evaluate_current_bar(feats, params=TEST_PARAMS)
    assert result["signal_score"] == pytest.approx(55.0)
    assert result["status"] == "CONFIRMING"


def test_evaluate_current_bar_failed_when_score_low():
    feats = _synthetic_features(
        overrides_last={
            "cross_3_8": False,
            "ema_3": 90.0, "ema_8": 95.0, "ema_21": 100.0, "ema_50": 105.0,  # every ">" comparison false
            "macd": 0.2, "macd_signal": 0.5,  # not bullish
            "rsi_14": 40.0,  # below constructive band, below extended threshold -- no bonus, no penalty
            "rel_volume": 0.5,
            "price_acceleration": -0.01,
        },
    )
    result = M.evaluate_current_bar(feats, params=TEST_PARAMS)
    assert result["status"] == "FAILED"
    assert result["signal_score"] < TEST_PARAMS.confirming_score_threshold


def test_evaluate_current_bar_rsi_extended_penalty_applied():
    feats = _synthetic_features(overrides_last={"cross_3_8": False, "rel_volume": 0.5, "rsi_14": 90.0})
    result = M.evaluate_current_bar(feats, params=TEST_PARAMS)
    assert "RSI extremely extended" in result["reasons"]
    # same as CONFIRMING test but RSI penalty replaces RSI bonus: 5+15+10+10+5-15=30
    assert result["signal_score"] == pytest.approx(30.0)


def test_evaluate_current_bar_whipsaw_penalty():
    feats = _synthetic_features(overrides_last={"cross_3_8": False, "rel_volume": 0.5})
    feats.iloc[-4, feats.columns.get_loc("close")] = 100.0
    feats.iloc[-3, feats.columns.get_loc("close")] = 100.0
    feats.iloc[-2, feats.columns.get_loc("close")] = 102.0  # prev_move = +2% > 1% threshold
    feats.iloc[-1, feats.columns.get_loc("close")] = 101.0  # now_move = -0.98% < -0.5% threshold
    result = M.evaluate_current_bar(feats, params=TEST_PARAMS)
    assert "Possible momentum reversal / whipsaw" in result["reasons"]


def test_evaluate_current_bar_score_clamped_to_ceiling():
    tight_params = dataclasses.replace(TEST_PARAMS, score_ceiling=60.0)
    feats = _synthetic_features(overrides_last={"cross_3_8": True})
    result = M.evaluate_current_bar(feats, params=tight_params)
    assert result["signal_score"] == pytest.approx(60.0)


def test_evaluate_current_bar_score_clamped_to_floor():
    tight_params = dataclasses.replace(TEST_PARAMS, score_floor=5.0)
    feats = _synthetic_features(
        overrides_last={
            "cross_3_8": False,
            "ema_3": 90.0, "ema_8": 95.0, "ema_21": 100.0, "ema_50": 105.0,  # every ">" comparison false
            "macd": 0.2, "macd_signal": 0.5,  # not bullish
            "rsi_14": 90.0,  # extended -> -15 penalty, the only nonzero term
            "rel_volume": 0.5,
            "price_acceleration": -0.01,
        },
    )
    # raw score = -15 (RSI extended penalty only) -> clamped up to score_floor=5.0
    result = M.evaluate_current_bar(feats, params=tight_params)
    assert result["signal_score"] == pytest.approx(5.0)


# ============================================================================
# 2. Deterministic repeatability
# ============================================================================


def test_evaluate_current_bar_deterministic_same_input_same_output():
    feats = _synthetic_features(overrides_last={"cross_3_8": True})
    r1 = M.evaluate_current_bar(feats, params=TEST_PARAMS)
    r2 = M.evaluate_current_bar(feats, params=TEST_PARAMS)
    assert r1 == r2


def test_build_technical_regime_from_bars_deterministic():
    bars = _make_bars(80, pattern=PATTERN_MILD_BULLISH, volume_spike_last=True)
    r1 = M.build_technical_regime_from_bars("AAPL", bars, params=TEST_PARAMS, universe_version="v1", now=NOW)
    r2 = M.build_technical_regime_from_bars("AAPL", bars, params=TEST_PARAMS, universe_version="v1", now=NOW)
    assert r1 == r2


# ============================================================================
# 3. Missing / invalid / incomplete OHLCV
# ============================================================================


def test_build_technical_regime_raises_on_none_bars():
    with pytest.raises(M.LiveSignalSourceError):
        M.build_technical_regime_from_bars("AAPL", None, params=TEST_PARAMS, universe_version="v1", now=NOW)


def test_build_technical_regime_raises_on_missing_columns():
    bad = pd.DataFrame({"close": [1.0, 2.0]}, index=pd.date_range("2026-01-01", periods=2, freq="D", tz="UTC"))
    with pytest.raises(M.LiveSignalSourceError):
        M.build_technical_regime_from_bars("AAPL", bad, params=TEST_PARAMS, universe_version="v1", now=NOW)


def test_build_technical_regime_insufficient_data_on_empty_frame():
    empty = pd.DataFrame(
        {c: [] for c in M.REQUIRED_BAR_COLUMNS}, index=pd.DatetimeIndex([], tz="UTC")
    )
    regime = M.build_technical_regime_from_bars("AAPL", empty, params=TEST_PARAMS, universe_version="v1", now=NOW)
    assert regime.status == "INSUFFICIENT_DATA"
    assert regime.bars_used == 0


def test_build_technical_regime_insufficient_data_on_too_few_bars():
    bars = _make_bars(TEST_PARAMS.min_bars_required - 1, pattern=PATTERN_MILD_BULLISH)
    regime = M.build_technical_regime_from_bars("AAPL", bars, params=TEST_PARAMS, universe_version="v1", now=bars.index[-1] + timedelta(hours=1))
    assert regime.status == "INSUFFICIENT_DATA"


def test_evaluate_current_bar_insufficient_data_on_nan_required_field():
    feats = _synthetic_features()
    feats.iloc[-1, feats.columns.get_loc("rsi_14")] = float("nan")
    result = M.evaluate_current_bar(feats, params=TEST_PARAMS)
    assert result["status"] == "INSUFFICIENT_DATA"


def test_evaluate_current_bar_insufficient_data_below_min_bars():
    feats = _synthetic_features(n=10)
    result = M.evaluate_current_bar(feats, params=TEST_PARAMS)
    assert result["status"] == "INSUFFICIENT_DATA"


# ============================================================================
# 4. Stale data
# ============================================================================


def test_build_technical_regime_stale_when_last_bar_too_old():
    bars = _make_bars(80, pattern=PATTERN_MILD_BULLISH)
    far_future_now = bars.index[-1].to_pydatetime() + timedelta(days=30)
    regime = M.build_technical_regime_from_bars("AAPL", bars, params=TEST_PARAMS, universe_version="v1", now=far_future_now)
    assert regime.status == "STALE_DATA"


def test_build_technical_regime_stale_when_bar_is_future_dated():
    bars = _make_bars(80, pattern=PATTERN_MILD_BULLISH)
    past_now = bars.index[-1].to_pydatetime() - timedelta(days=5)
    regime = M.build_technical_regime_from_bars("AAPL", bars, params=TEST_PARAMS, universe_version="v1", now=past_now)
    assert regime.status == "STALE_DATA"
    assert "future-dated" in regime.reasons[0]


def test_build_technical_regime_fresh_at_exact_boundary():
    bars = _make_bars(80, pattern=PATTERN_MILD_BULLISH)
    boundary_now = bars.index[-1].to_pydatetime() + timedelta(seconds=TEST_PARAMS.max_bar_age_seconds)
    regime = M.build_technical_regime_from_bars("AAPL", bars, params=TEST_PARAMS, universe_version="v1", now=boundary_now)
    assert regime.status != "STALE_DATA"


def test_build_technical_regime_stale_just_past_boundary():
    bars = _make_bars(80, pattern=PATTERN_MILD_BULLISH)
    just_past_now = bars.index[-1].to_pydatetime() + timedelta(seconds=TEST_PARAMS.max_bar_age_seconds + 1)
    regime = M.build_technical_regime_from_bars("AAPL", bars, params=TEST_PARAMS, universe_version="v1", now=just_past_now)
    assert regime.status == "STALE_DATA"


# ============================================================================
# 5. Boundary conditions
# ============================================================================


def test_build_technical_regime_exactly_min_bars_required_is_evaluated():
    bars = _make_bars(TEST_PARAMS.min_bars_required, pattern=PATTERN_MILD_BULLISH)
    regime = M.build_technical_regime_from_bars("AAPL", bars, params=TEST_PARAMS, universe_version="v1", now=bars.index[-1] + timedelta(hours=1))
    assert regime.status != "INSUFFICIENT_DATA"
    assert regime.bars_used == TEST_PARAMS.min_bars_required


def test_evaluate_current_bar_rsi_exactly_at_constructive_band_edges():
    feats_low = _synthetic_features(overrides_last={"rsi_14": TEST_PARAMS.rsi_constructive_low})
    feats_high = _synthetic_features(overrides_last={"rsi_14": TEST_PARAMS.rsi_constructive_high})
    r_low = M.evaluate_current_bar(feats_low, params=TEST_PARAMS)
    r_high = M.evaluate_current_bar(feats_high, params=TEST_PARAMS)
    assert "RSI constructive" in r_low["reasons"]
    assert "RSI constructive" in r_high["reasons"]


def test_evaluate_current_bar_rel_volume_exactly_at_threshold_counts():
    feats = _synthetic_features(overrides_last={"rel_volume": TEST_PARAMS.rel_volume_threshold})
    result = M.evaluate_current_bar(feats, params=TEST_PARAMS)
    assert any("Relative volume" in r for r in result["reasons"])


def test_technical_scoring_params_rejects_invalid_fields():
    with pytest.raises(M.LiveSignalSourceError):
        dataclasses.replace(TEST_PARAMS, min_bars_required=0)
    with pytest.raises(M.LiveSignalSourceError):
        dataclasses.replace(TEST_PARAMS, max_bar_age_seconds=0)
    with pytest.raises(M.LiveSignalSourceError):
        dataclasses.replace(TEST_PARAMS, rsi_constructive_low=80.0, rsi_constructive_high=20.0)
    with pytest.raises(M.LiveSignalSourceError):
        dataclasses.replace(TEST_PARAMS, score_floor=90.0, score_ceiling=10.0)
    with pytest.raises(M.LiveSignalSourceError):
        dataclasses.replace(TEST_PARAMS, persistence_lookback_bars=0)


# ============================================================================
# 6. No look-ahead / no repainting
# ============================================================================


def test_no_look_ahead_appending_future_bars_does_not_change_past_row():
    full = _make_bars(120, pattern=PATTERN_MILD_BULLISH, volume_spike_last=True)
    truncated = full.iloc[:80]

    feats_full = M.build_technical_features(full)
    feats_truncated = M.build_technical_features(truncated)

    shared_row_label = truncated.index[-1]
    row_from_truncated = feats_truncated.loc[shared_row_label]
    row_from_full = feats_full.loc[shared_row_label]

    for col in feats_truncated.columns:
        a, b = row_from_truncated[col], row_from_full[col]
        if isinstance(a, float) and a != a:  # NaN
            assert b != b
        else:
            assert a == b, f"look-ahead detected in column {col}: {a} != {b}"


def test_no_repainting_evaluation_of_a_past_bar_is_stable():
    full = _make_bars(120, pattern=PATTERN_MILD_BULLISH, volume_spike_last=False)
    truncated = full.iloc[:80]
    regime_from_truncated = M.build_technical_regime_from_bars(
        "AAPL", truncated, params=TEST_PARAMS, universe_version="v1", now=truncated.index[-1] + timedelta(hours=1)
    )
    # Re-evaluate the SAME historical point using a longer series truncated
    # back to the identical cutoff -- must reproduce the identical regime.
    same_cutoff_again = full.iloc[:80]
    regime_again = M.build_technical_regime_from_bars(
        "AAPL", same_cutoff_again, params=TEST_PARAMS, universe_version="v1", now=truncated.index[-1] + timedelta(hours=1)
    )
    assert regime_from_truncated == regime_again


# ============================================================================
# 7. Evidence provenance
# ============================================================================


def test_technical_regime_carries_provenance_fields():
    bars = _make_bars(80, pattern=PATTERN_MILD_BULLISH, volume_spike_last=True)
    regime = M.build_technical_regime_from_bars("AAPL", bars, params=TEST_PARAMS, universe_version="v1", now=bars.index[-1] + timedelta(hours=1))
    assert regime.symbol == "AAPL"
    assert regime.universe_version == "v1"
    assert regime.bars_used == 80
    assert regime.last_bar_timestamp is not None
    assert len(regime.scoring_params_hash) == 64  # sha256 hex digest


def test_scoring_params_hash_changes_when_params_change():
    bars = _make_bars(80, pattern=PATTERN_MILD_BULLISH, volume_spike_last=True)
    r1 = M.build_technical_regime_from_bars("AAPL", bars, params=TEST_PARAMS, universe_version="v1", now=bars.index[-1] + timedelta(hours=1))
    other_params = dataclasses.replace(TEST_PARAMS, rel_volume_threshold=1.5)
    r2 = M.build_technical_regime_from_bars("AAPL", bars, params=other_params, universe_version="v1", now=bars.index[-1] + timedelta(hours=1))
    assert r1.scoring_params_hash != r2.scoring_params_hash


def test_technical_regime_rejects_invalid_status():
    with pytest.raises(M.LiveSignalSourceError):
        M.TechnicalRegime(
            symbol="AAPL", as_of=NOW_ISO, last_bar_timestamp=NOW_ISO, status="BULLISH_EXTREME",
            signal_score=0.0, persistence_bars=0, rsi_14=None, rel_volume=None, atr_pct=None,
            price_acceleration=None, reasons=(), bars_used=0, universe_version="v1", scoring_params_hash="h",
        )


# ============================================================================
# 8. `.51` -> `.50` integration
# ============================================================================

ENGINE = _load("aura_v05350_decision_engine", ROOT / "aura_v05350_decision_engine.py")


def test_technical_regime_integrates_into_50_candidate_evidence():
    bars = _make_bars(80, pattern=PATTERN_MILD_BULLISH, volume_spike_last=True)
    regime = M.build_technical_regime_from_bars("AAPL", bars, params=TEST_PARAMS, universe_version="v1", now=bars.index[-1] + timedelta(hours=1))
    assert regime.status in ("CONFIRMING", "CONFIRMED")  # empirically verified bullish pattern -> usable

    ev = ENGINE.build_candidate_evidence("AAPL", technical_regime=regime, now=bars.index[-1] + timedelta(hours=1))
    assert ev.technical_usable is True
    assert "TECHNICAL" in ev.sources_present
    assert ENGINE.is_shortlist_eligible(ev) is True


def test_technical_regime_score_flows_into_50_base_rank_score():
    bars = _make_bars(80, pattern=PATTERN_MILD_BULLISH, volume_spike_last=True)
    regime = M.build_technical_regime_from_bars("AAPL", bars, params=TEST_PARAMS, universe_version="v1", now=bars.index[-1] + timedelta(hours=1))
    ev = ENGINE.build_candidate_evidence("AAPL", technical_regime=regime, now=bars.index[-1] + timedelta(hours=1))
    score = ENGINE.compute_base_rank_score(ev, sentiment_weight=1.0, wave_weight=1.0, technical_weight=2.0)
    assert score == pytest.approx(2.0 * (regime.signal_score / 100.0))
    assert score > 0  # LONG-only: a usable technical regime never produces a negative contribution


def test_failed_technical_regime_does_not_reach_50_shortlist():
    bars = _make_bars(80, flat=True)
    regime = M.build_technical_regime_from_bars("AAPL", bars, params=TEST_PARAMS, universe_version="v1", now=bars.index[-1] + timedelta(hours=1))
    assert regime.status not in ("CONFIRMING", "CONFIRMED")  # empirically verified flat pattern -> not usable

    ev = ENGINE.build_candidate_evidence("AAPL", technical_regime=regime, now=bars.index[-1] + timedelta(hours=1))
    assert ev.technical_usable is False
    assert ENGINE.is_shortlist_eligible(ev) is False


# ============================================================================
# 9. Behavior when technical evidence is absent
# ============================================================================


def test_50_build_candidate_evidence_unaffected_when_technical_absent():
    ev = ENGINE.build_candidate_evidence("BTC/USDT:USDT", now=NOW)
    assert ev.technical_regime is None
    assert ev.technical_usable is False
    assert "TECHNICAL" not in ev.sources_present
    # sentiment/wave-only evidence is untouched by .51's existence
    assert ENGINE.is_shortlist_eligible(ev) is False  # no source at all here


# ============================================================================
# 10. Pinned universe -- loaded from the real, verified config file
# ============================================================================


def test_load_pinned_universe_from_real_config_file():
    universe = M.load_pinned_universe()
    assert universe.version == "v1"
    assert len(universe.symbols) == 27
    assert len(set(universe.symbols)) == 27  # no duplicates
    for expected in ("AAPL", "MSFT", "SPY", "QQQ", "GLD", "SLV"):
        assert expected in universe.symbols


def test_load_pinned_universe_fails_closed_on_missing_file(tmp_path):
    with pytest.raises(M.LiveSignalSourceError):
        M.load_pinned_universe(tmp_path / "does_not_exist.json")


def test_load_pinned_universe_fails_closed_on_malformed_json(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(M.LiveSignalSourceError):
        M.load_pinned_universe(bad)


def test_load_pinned_universe_fails_closed_on_empty_symbols(tmp_path):
    bad = tmp_path / "empty.json"
    bad.write_text(json.dumps({"version": "v1", "symbols": []}), encoding="utf-8")
    with pytest.raises(M.LiveSignalSourceError):
        M.load_pinned_universe(bad)


def test_load_pinned_universe_fails_closed_on_duplicate_symbols(tmp_path):
    bad = tmp_path / "dupes.json"
    bad.write_text(json.dumps({"version": "v1", "symbols": ["AAPL", "AAPL"]}), encoding="utf-8")
    with pytest.raises(M.LiveSignalSourceError):
        M.load_pinned_universe(bad)


def test_load_pinned_universe_fails_closed_on_missing_version(tmp_path):
    bad = tmp_path / "noversion.json"
    bad.write_text(json.dumps({"symbols": ["AAPL"]}), encoding="utf-8")
    with pytest.raises(M.LiveSignalSourceError):
        M.load_pinned_universe(bad)


# ============================================================================
# 11. Live fetch layer -- fake client, no network/credentials required
# ============================================================================


class _FakeBarsResponse:
    def __init__(self, df):
        self.df = df


class _FakeBarsClient:
    def __init__(self, df_to_return=None, exception=None):
        self._df = df_to_return
        self._exception = exception
        self.last_request = None

    def get_stock_bars(self, request):
        self.last_request = request
        if self._exception is not None:
            raise self._exception
        return _FakeBarsResponse(self._df)


def _multiindex_bars(symbol, n=80, pattern=None):
    single = _make_bars(n, pattern=pattern or PATTERN_MILD_BULLISH)
    single = single.reset_index().rename(columns={"index": "timestamp"})
    single["symbol"] = symbol
    return single.set_index(["symbol", "timestamp"])


def test_fetch_recent_bars_trims_to_lookback_and_handles_multiindex():
    df = _multiindex_bars("AAPL", n=100)
    client = _FakeBarsClient(df_to_return=df)
    result = M.fetch_recent_bars("AAPL", client=client, lookback_bars=80, end=datetime(2026, 6, 1, tzinfo=timezone.utc))
    assert len(result) == 80
    assert list(result.columns) == list(M.REQUIRED_BAR_COLUMNS)


def test_fetch_recent_bars_raises_on_empty_response():
    client = _FakeBarsClient(df_to_return=pd.DataFrame())
    with pytest.raises(M.LiveSignalSourceError):
        M.fetch_recent_bars("AAPL", client=client, lookback_bars=80)


def test_fetch_recent_bars_rejects_non_positive_lookback():
    client = _FakeBarsClient(df_to_return=_multiindex_bars("AAPL"))
    with pytest.raises(M.LiveSignalSourceError):
        M.fetch_recent_bars("AAPL", client=client, lookback_bars=0)


def test_fetch_live_technical_regime_wraps_fetch_exceptions_fail_closed():
    client = _FakeBarsClient(exception=RuntimeError("connection reset"))
    with pytest.raises(M.LiveSignalSourceError):
        M.fetch_live_technical_regime(
            "AAPL", bars_client=client, params=TEST_PARAMS, lookback_bars=80, universe_version="v1", now=NOW
        )


def test_fetch_live_technical_regime_never_silently_falls_back_on_failure():
    """Unlike the AegisAlpha anti-pattern rejected in this module's
    reuse-first audit, a fetch failure must propagate, never silently
    substitute a stale/cached/default result.
    """
    client = _FakeBarsClient(exception=RuntimeError("boom"))
    try:
        M.fetch_live_technical_regime(
            "AAPL", bars_client=client, params=TEST_PARAMS, lookback_bars=80, universe_version="v1", now=NOW
        )
        raised = False
    except M.LiveSignalSourceError:
        raised = True
    assert raised is True


def test_fetch_live_technical_regime_end_to_end_with_fake_client():
    df = _multiindex_bars("AAPL", n=100, pattern=PATTERN_MILD_BULLISH)
    client = _FakeBarsClient(df_to_return=df)
    end = df.index.get_level_values("timestamp")[-1].to_pydatetime() + timedelta(hours=1)
    regime = M.fetch_live_technical_regime(
        "AAPL", bars_client=client, params=TEST_PARAMS, lookback_bars=80, universe_version="v1", now=end
    )
    assert regime.symbol == "AAPL"
    assert regime.status in M.TECHNICAL_STATUSES


# ============================================================================
# 12. Governance / long-only invariant
# ============================================================================


def test_module_has_no_short_or_bearish_scoring_path():
    """AST-based, not a raw substring search -- the module's own
    docstring legitimately discusses `.50`'s SHORT_LEANING/DECIDE_SHORT
    vocabulary in prose (explaining why `.51` never produces it), so a
    literal grep would false-positive on that prose exactly like the
    known `.49` test-bug pattern this project already fixed once. This
    checks only actual function/class/variable NAMES defined in the
    module, which is what a real short/bearish code path would need.
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
    banned_fragments = ("short", "bearish")
    offenders = {n for n in lowered for frag in banned_fragments if frag in n}
    assert not offenders, f"unexpected short/bearish-named identifier(s) in module: {offenders}"


def test_module_has_no_order_or_execution_function():
    source = Path(M.__file__).read_text(encoding="utf-8")
    banned_substrings = ("submit_order", "place_order", "execute_trade", "cancel_order", "send_order")
    for token in banned_substrings:
        assert token not in source
