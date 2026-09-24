#!/usr/bin/env python3
"""Contract + unit tests for AURA v0.5.3.60 Research-track full-evidence
builder.

`.47`/`.48`/`.350`/`.359` are loaded into `sys.modules` under their
canonical names BEFORE `.360` is loaded, mirroring `.350`'s own test
convention, so `.360`'s internal `import aura_v05347_...` etc. resolve to
the SAME module objects these fixtures are built against.
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


SENTIMENT = _load("aura_v05347_market_sentiment_scoring", ROOT / "aura_v05347_market_sentiment_scoring.py")
WAVE = _load("aura_v05348_elliott_wave_research", ROOT / "aura_v05348_elliott_wave_research.py")
ENGINE = _load("aura_v05350_decision_engine", ROOT / "aura_v05350_decision_engine.py")
ROTATION = _load("aura_v05359_sector_rotation", ROOT / "aura_v05359_sector_rotation.py")
BUILDER = _load("aura_v05360_research_full_evidence_builder", ROOT / "aura_v05360_research_full_evidence_builder.py")

NOW = datetime(2026, 9, 24, 12, 0, 0, tzinfo=timezone.utc)
NOW_ISO = NOW.isoformat()


# ============================================================================
# Fixtures
# ============================================================================


def news_event(symbol, headline, *, origin="Reuters", hours_ago=1.0, external_id="e1"):
    created = (NOW - timedelta(hours=hours_ago)).isoformat()
    return {
        "symbols": [symbol],
        "headline": headline,
        "summary": "",
        "origin_source": origin,
        "created_at": created,
        "external_id": external_id,
    }


def flat_bar(ts, price):
    return {"timestamp": ts, "open": price, "high": price, "low": price, "close": price}


def bars_from_closes(prices):
    return [flat_bar(f"t{i}", p) for i, p in enumerate(prices)]


# Verified fixture from `.48`'s own test suite: 11 flat-bars -> 9 confirmed
# swings, exactly 1 VALID_CANDIDATE (SINGLE_VALID_CANDIDATE, direction UP).
SINGLE_VALID_WAVE_PRICES = [105, 100, 110, 104, 130, 120, 140, 125, 135, 115, 120]


def sector_rotation_inputs(**overrides):
    bench = bars_from_closes([100, 100, 100, 100, 100])
    haven = {"SHV": bars_from_closes([100.0, 100.1, 100.2, 100.3, 100.4])}
    universe = {"AAA": bars_from_closes([100, 105, 110, 115, 120])}
    kwargs = dict(
        symbol_bars=universe["AAA"],
        benchmark_symbol="SPY",
        benchmark_bars=bench,
        universe_bars=universe,
        safe_haven_symbols=(),
        safe_haven_bars=haven,
        defensive_ratio_pair=None,
        defensive_ratio_bars=None,
        lookback_bars=4,
        top_n=1,
        defensive_ma_period=3,
        score_scale=5.0,
    )
    kwargs.update(overrides)
    return BUILDER.SectorRotationInputs(**kwargs)


# ============================================================================
# _validate_regime_shape (Decision-Seam Validation Rule)
# ============================================================================


def test_validate_regime_shape_none_regime_passes():
    BUILDER._validate_regime_shape("X", None, score_attr="score", score_range=(-1.0, 1.0))


def test_validate_regime_shape_missing_score_attr_treated_as_not_usable():
    regime = SimpleNamespace(score=None)
    BUILDER._validate_regime_shape("X", regime, score_attr="score", score_range=(-1.0, 1.0))


def test_validate_regime_shape_valid_numeric_in_range_passes():
    regime = SimpleNamespace(score=0.5)
    BUILDER._validate_regime_shape("X", regime, score_attr="score", score_range=(-1.0, 1.0))


def test_validate_regime_shape_rejects_non_numeric_score():
    regime = SimpleNamespace(score="high")
    with pytest.raises(BUILDER.ResearchEvidenceError):
        BUILDER._validate_regime_shape("X", regime, score_attr="score", score_range=(-1.0, 1.0))


def test_validate_regime_shape_rejects_bool_score():
    """bool is a subclass of int in Python -- must be explicitly excluded,
    exactly the class of untyped-boundary bug this rule exists to catch."""
    regime = SimpleNamespace(score=True)
    with pytest.raises(BUILDER.ResearchEvidenceError):
        BUILDER._validate_regime_shape("X", regime, score_attr="score", score_range=(-1.0, 1.0))


def test_validate_regime_shape_rejects_out_of_range_score():
    regime = SimpleNamespace(score=1.5)
    with pytest.raises(BUILDER.ResearchEvidenceError):
        BUILDER._validate_regime_shape("X", regime, score_attr="score", score_range=(-1.0, 1.0))


# ============================================================================
# build_full_evidence_for_symbol -- basic wiring
# ============================================================================


def test_build_full_evidence_minimal_no_news_no_bars_no_rotation():
    result = BUILDER.build_full_evidence_for_symbol(
        "AAPL", [], [], reversal_pct=1.0, decay_window_hours=48.0, min_source_count=2, now=NOW
    )
    assert result.symbol == "AAPL"
    assert result.news_events_considered == 0
    assert result.sentiment_regime is not None
    assert result.sentiment_regime.corroboration_status == "NO_DATA"
    assert result.wave_result is None
    assert result.sector_rotation_regime is None
    assert result.evidence.symbol == "AAPL"


def test_build_full_evidence_sentiment_usable_with_corroborated_bullish_news():
    events = [
        news_event("AAPL", "AAPL beats estimates on strong quarter", origin="Reuters", external_id="e1"),
        news_event("AAPL", "AAPL surges after earnings", origin="Bloomberg", external_id="e2"),
    ]
    result = BUILDER.build_full_evidence_for_symbol(
        "AAPL", [], events, reversal_pct=1.0, decay_window_hours=48.0, min_source_count=2, now=NOW
    )
    assert result.sentiment_regime.corroboration_status == "SUFFICIENT"
    assert result.evidence.sentiment_usable is True
    assert result.news_events_considered == 2


def test_news_events_considered_counts_all_tagged_events_even_outside_decay_window():
    """`.360`'s own `news_events_considered` counts every event tagged to
    the symbol (raw ledger volume for audit), independent of `.47`'s own
    decay-windowed `items_considered` -- these are deliberately different
    numbers measuring different things."""
    stale_event = news_event("AAPL", "AAPL beats estimates", hours_ago=1000.0, external_id="stale")
    result = BUILDER.build_full_evidence_for_symbol(
        "AAPL", [], [stale_event], reversal_pct=1.0, decay_window_hours=48.0, min_source_count=1, now=NOW
    )
    assert result.news_events_considered == 1  # counted by .360 regardless of decay
    assert result.sentiment_regime.items_considered == 0  # excluded by .47's own decay window


def test_build_full_evidence_wave_usable_with_valid_single_candidate_bars():
    bars = bars_from_closes(SINGLE_VALID_WAVE_PRICES)
    result = BUILDER.build_full_evidence_for_symbol(
        "AAPL", bars, [], reversal_pct=1.0, decay_window_hours=48.0, min_source_count=1, now=NOW
    )
    assert result.wave_result is not None
    assert result.wave_result.ambiguity_status == "SINGLE_VALID_CANDIDATE"
    assert result.evidence.wave_usable is True


def test_build_full_evidence_technical_and_short_technical_pass_through_unchanged():
    fake_technical = SimpleNamespace(status="CONFIRMED", signal_score=80.0, as_of=NOW_ISO)
    fake_short = SimpleNamespace(status="CONFIRMED", signal_score=70.0, as_of=NOW_ISO)
    result = BUILDER.build_full_evidence_for_symbol(
        "AAPL", [], [], reversal_pct=1.0, decay_window_hours=48.0, min_source_count=1,
        technical_regime=fake_technical, short_technical_regime=fake_short, now=NOW,
    )
    assert result.evidence.technical_regime is fake_technical
    assert result.evidence.short_technical_regime is fake_short


# ============================================================================
# build_full_evidence_for_symbol -- sector rotation wiring
# ============================================================================


def test_build_full_evidence_without_sector_rotation_inputs_leaves_it_absent():
    result = BUILDER.build_full_evidence_for_symbol(
        "AAPL", [], [], reversal_pct=1.0, decay_window_hours=48.0, min_source_count=1, now=NOW
    )
    assert result.sector_rotation_regime is None
    assert result.evidence.sector_rotation_regime is None
    assert result.evidence.sector_rotation_usable is False
    assert "SECTOR_ROTATION" not in result.evidence.sources_present


def test_build_full_evidence_with_sector_rotation_inputs_wires_into_evidence():
    sri = sector_rotation_inputs()
    result = BUILDER.build_full_evidence_for_symbol(
        "AAA", sri.symbol_bars, [], reversal_pct=1.0, decay_window_hours=48.0, min_source_count=1,
        sector_rotation_inputs=sri, now=NOW,
    )
    assert result.sector_rotation_regime is not None
    assert result.sector_rotation_regime.rotation_tier == "LEADER"
    assert result.evidence.sector_rotation_usable is True
    assert "SECTOR_ROTATION" in result.evidence.sources_present


def test_build_full_evidence_sector_rotation_not_usable_when_not_applicable():
    sri = sector_rotation_inputs(symbol_bars=bars_from_closes([100, 101]))  # insufficient for lookback=4
    result = BUILDER.build_full_evidence_for_symbol(
        "ZZZ", sri.symbol_bars, [], reversal_pct=1.0, decay_window_hours=48.0, min_source_count=1,
        sector_rotation_inputs=sri, now=NOW,
    )
    assert result.sector_rotation_regime.rotation_tier == "NOT_APPLICABLE"
    assert result.evidence.sector_rotation_usable is False
    # present but not usable -- recorded for audit, same discipline `.51` established for TECHNICAL
    assert "SECTOR_ROTATION" in result.evidence.sources_present


# ============================================================================
# build_full_evidence_for_symbol -- feeds directly into `.350`'s decide()
# ============================================================================


def test_evidence_from_360_is_a_real_350_candidate_evidence_usable_by_decide():
    sri = sector_rotation_inputs()
    result = BUILDER.build_full_evidence_for_symbol(
        "AAA", sri.symbol_bars, [], reversal_pct=1.0, decay_window_hours=48.0, min_source_count=1,
        sector_rotation_inputs=sri, now=NOW,
    )
    assert isinstance(result.evidence, ENGINE.CandidateEvidence)
    score = ENGINE.compute_base_rank_score(
        result.evidence, sentiment_weight=1.0, wave_weight=1.0, technical_weight=0.0,
        short_technical_weight=0.0, sector_rotation_weight=2.0,
    )
    assert score > 0  # LEADER tier contributes a positive score term


def test_full_evidence_build_result_to_dict_handles_all_none_sources():
    result = BUILDER.build_full_evidence_for_symbol(
        "AAPL", [], [], reversal_pct=1.0, decay_window_hours=48.0, min_source_count=1, now=NOW
    )
    d = result.to_dict()
    assert d["symbol"] == "AAPL"
    assert d["wave"] is None
    assert d["sector_rotation"] is None
    assert d["sentiment"] is not None  # sentiment regime always computed, even if NO_DATA


def test_full_evidence_build_result_to_dict_includes_sector_rotation_when_present():
    sri = sector_rotation_inputs()
    result = BUILDER.build_full_evidence_for_symbol(
        "AAA", sri.symbol_bars, [], reversal_pct=1.0, decay_window_hours=48.0, min_source_count=1,
        sector_rotation_inputs=sri, now=NOW,
    )
    d = result.to_dict()
    assert d["sector_rotation"]["rotation_tier"] == "LEADER"


# ============================================================================
# Required, no-default research parameters -- never invented
# ============================================================================


def test_reversal_pct_and_decay_params_forwarded_verbatim_not_invented():
    """Structural proof: `.360` never hardcodes `.47`/`.48` research
    parameters -- an invalid one raises from the underlying module, not
    silently defaulted by `.360`."""
    with pytest.raises(SENTIMENT.SentimentScoringError):
        BUILDER.build_full_evidence_for_symbol(
            "AAPL", [], [], reversal_pct=1.0, decay_window_hours=0, min_source_count=1, now=NOW
        )
    with pytest.raises(WAVE.ElliottWaveError):
        BUILDER.build_full_evidence_for_symbol(
            "AAPL", bars_from_closes(SINGLE_VALID_WAVE_PRICES), [], reversal_pct=0,
            decay_window_hours=48.0, min_source_count=1, now=NOW,
        )
