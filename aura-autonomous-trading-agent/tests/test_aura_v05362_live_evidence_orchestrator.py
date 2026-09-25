#!/usr/bin/env python3
"""Contract + unit tests for AURA v0.5.3.62 Live Evidence Orchestrator.

`.46`/`.47`/`.48`/`.350`/`.359`/`.360` are loaded into `sys.modules` under
their canonical names BEFORE `.362` is loaded, mirroring `.360`'s own test
convention, so `.362`'s internal `import aura_v05346_...`/`aura_v05360_...`
resolve to the SAME module objects these fixtures are built against.

NO LIVE ALPACA ACCESS IS REQUIRED OR ATTEMPTED BY ANY TEST IN THIS FILE.
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


NEWS = _load("aura_v05346_news_ingestion_classification", ROOT / "aura_v05346_news_ingestion_classification.py")
SENTIMENT = _load("aura_v05347_market_sentiment_scoring", ROOT / "aura_v05347_market_sentiment_scoring.py")
WAVE = _load("aura_v05348_elliott_wave_research", ROOT / "aura_v05348_elliott_wave_research.py")
ENGINE = _load("aura_v05350_decision_engine", ROOT / "aura_v05350_decision_engine.py")
ROTATION = _load("aura_v05359_sector_rotation", ROOT / "aura_v05359_sector_rotation.py")
BUILDER = _load("aura_v05360_research_full_evidence_builder", ROOT / "aura_v05360_research_full_evidence_builder.py")
ORCH = _load("aura_v05362_live_evidence_orchestrator", ROOT / "aura_v05362_live_evidence_orchestrator.py")

NOW = datetime(2026, 9, 25, 12, 0, 0, tzinfo=timezone.utc)


# ============================================================================
# Fixtures
# ============================================================================


class FakeArticle:
    def __init__(self, id, headline, summary="", url=None, author="Staff",
                 created_at=None, updated_at=None, symbols=None, source="Benzinga"):
        self.id = id
        self.headline = headline
        self.summary = summary
        self.url = url
        self.author = author
        self.created_at = created_at or NOW
        self.updated_at = updated_at or NOW
        self.symbols = symbols if symbols is not None else []
        self.source = source


class FakeNewsSet:
    def __init__(self, articles):
        self.data = {"news": list(articles)}


class FakeNewsClient:
    """Duck-typed stand-in for alpaca-py's `NewsClient` -- matches `.46`'s
    own test fixture exactly (`.get_news(request) -> object with .data`)."""

    def __init__(self, articles=None, raise_exc=None):
        self._articles = articles or []
        self._raise_exc = raise_exc
        self.calls: list[Any] = []

    def get_news(self, request_params):
        self.calls.append(request_params)
        if self._raise_exc is not None:
            raise self._raise_exc
        return FakeNewsSet(self._articles)


def make_bars_df(n=60, start_price=100.0) -> pd.DataFrame:
    idx = pd.date_range(end=NOW, periods=n, freq="D", tz="UTC")
    closes = [start_price + i * 0.5 for i in range(n)]
    return pd.DataFrame(
        {
            "open": [c - 0.2 for c in closes], "high": [c + 0.3 for c in closes],
            "low": [c - 0.3 for c in closes], "close": closes,
            "volume": [1_000_000 + i * 100 for i in range(n)],
        },
        index=idx,
    )


def flat_bar(ts, price):
    return {"timestamp": ts, "open": price, "high": price, "low": price, "close": price, "volume": 1000.0}


def bars_from_closes(prices):
    return [flat_bar(f"t{i}", p) for i, p in enumerate(prices)]


# ============================================================================
# bars_df_to_research_dicts
# ============================================================================


def test_bars_df_to_research_dicts_none_returns_empty():
    assert ORCH.bars_df_to_research_dicts(None) == []


def test_bars_df_to_research_dicts_empty_frame_returns_empty():
    assert ORCH.bars_df_to_research_dicts(pd.DataFrame()) == []


def test_bars_df_to_research_dicts_converts_all_rows_and_columns():
    df = make_bars_df(n=5, start_price=200.0)
    records = ORCH.bars_df_to_research_dicts(df)
    assert len(records) == 5
    first = records[0]
    assert set(["timestamp", "open", "high", "low", "close", "volume"]) <= set(first.keys())
    assert first["close"] == pytest.approx(200.0)
    assert isinstance(first["timestamp"], str)


def test_bars_df_to_research_dicts_never_mutates_input():
    df = make_bars_df(n=5)
    original = df.copy(deep=True)
    ORCH.bars_df_to_research_dicts(df)
    pd.testing.assert_frame_equal(df, original)


# ============================================================================
# fetch_universe_news_events
# ============================================================================


def test_fetch_universe_news_events_ingests_and_reads_back(tmp_path):
    articles = [FakeArticle(id=1, headline="Acme beats estimates", symbols=["AAPL"], source="Reuters")]
    client = FakeNewsClient(articles=articles)
    state_dir = tmp_path / "news_state"
    events, status = ORCH.fetch_universe_news_events(
        client, state_dir, symbols=("AAPL", "SPY"), start=None, end=None, now=NOW,
    )
    assert len(events) == 1
    assert events[0]["symbols"] == ["AAPL"]
    assert status["fetch_status"]["status"] == "SUCCESS"


def test_fetch_universe_news_events_fails_open_and_preserves_prior_ledger(tmp_path):
    state_dir = tmp_path / "news_state"
    good_client = FakeNewsClient(articles=[FakeArticle(id=1, headline="Good article", symbols=["AAPL"])])
    events_first, _ = ORCH.fetch_universe_news_events(
        good_client, state_dir, symbols=("AAPL",), start=None, end=None, now=NOW,
    )
    assert len(events_first) == 1

    failing_client = FakeNewsClient(raise_exc=RuntimeError("network down"))
    events_second, status_second = ORCH.fetch_universe_news_events(
        failing_client, state_dir, symbols=("AAPL",), start=None, end=None, now=NOW,
    )
    assert status_second["fetch_status"]["status"] == "FAILED"
    # the previously-good ledger is NOT wiped by the failed fetch
    assert len(events_second) == 1


# ============================================================================
# build_live_evidence_for_universe
# ============================================================================


def test_build_live_evidence_computes_sentiment_and_wave_per_symbol():
    bars_by_symbol = {
        "AAPL": bars_from_closes([105, 100, 110, 104, 130, 120, 140, 125, 135, 115, 120]),
    }
    news_events = [
        {"symbols": ["AAPL"], "headline": "Acme beats estimates", "summary": "", "origin_source": "Reuters",
         "created_at": NOW.isoformat(), "external_id": "e1"},
        {"symbols": ["AAPL"], "headline": "Acme guidance raised", "summary": "", "origin_source": "Bloomberg",
         "created_at": NOW.isoformat(), "external_id": "e2"},
    ]
    results = ORCH.build_live_evidence_for_universe(bars_by_symbol, news_events, now=NOW)
    assert "AAPL" in results
    ev = results["AAPL"]
    assert ev.sentiment_regime is not None
    assert ev.wave_result is not None
    # no SPY/GLD/SLV bars supplied -> sector rotation skipped for the whole cycle
    assert ev.sector_rotation_regime is None
    assert ev.build_error == "SECTOR_ROTATION_SKIPPED_BENCHMARK_OR_SAFE_HAVEN_BARS_UNAVAILABLE"


def test_build_live_evidence_computes_sector_rotation_when_universe_complete():
    bars_by_symbol = {
        "AAPL": bars_from_closes([100, 102, 104, 106, 108, 110, 112, 114, 116, 118, 120]),
        "SPY": bars_from_closes([100, 100.5, 101, 101.5, 102, 102.5, 103, 103.5, 104, 104.5, 105]),
        "GLD": bars_from_closes([100] * 11),
        "SLV": bars_from_closes([100] * 11),
    }
    results = ORCH.build_live_evidence_for_universe(bars_by_symbol, [], now=NOW)
    for symbol in bars_by_symbol:
        assert results[symbol].sector_rotation_regime is not None, symbol
        assert results[symbol].build_error is None


def test_build_live_evidence_symbol_with_no_bars_gets_none_wave_not_an_error():
    results = ORCH.build_live_evidence_for_universe({"AAPL": []}, [], now=NOW)
    assert results["AAPL"].wave_result is None
    # sentiment is still computed (it does not depend on bars)
    assert results["AAPL"].sentiment_regime is not None


def test_symbol_live_evidence_to_dict_shape():
    results = ORCH.build_live_evidence_for_universe(
        {"AAPL": bars_from_closes([105, 100, 110, 104, 130, 120, 140, 125, 135, 115, 120])}, [], now=NOW,
    )
    d = results["AAPL"].to_dict()
    assert set(["symbol", "sentiment", "wave", "sector_rotation", "build_error"]) <= set(d.keys())
    assert d["symbol"] == "AAPL"


# ============================================================================
# Configuration constants -- confirm the Martin-approved values are exactly
# what was agreed (AskUserQuestion, 2026-09-24/25), not silently drifted.
# ============================================================================


def test_live_evidence_decide_kwargs_matches_confirmed_decision():
    kw = ORCH.LIVE_EVIDENCE_DECIDE_KWARGS
    assert kw["sentiment_weight"] == 1.0
    assert kw["wave_weight"] == 1.0
    assert kw["technical_weight"] == 1.0
    assert kw["short_technical_weight"] == 1.0
    assert kw["sector_rotation_weight"] == 0.0  # compute + log only
    # generic thresholds reused verbatim from .054's already-approved frozen record
    assert kw["decision_threshold"] == 0.1
    assert kw["ai_penalty_per_concern"] == 0.2
    assert kw["critic_penalty_per_issue"] == 0.15
    # never silently smuggles proposal_module/llm_client/now -- callers supply those
    assert "proposal_module" not in kw
    assert "llm_client" not in kw
    assert "now" not in kw


def test_sentiment_wave_params_match_confirmed_decision():
    assert ORCH.SENTIMENT_WAVE_PARAMS == {
        "reversal_pct": 3.0, "decay_window_hours": 48.0, "min_source_count": 2,
    }


def test_sector_rotation_config_matches_confirmed_decision():
    assert ORCH.BENCHMARK_SYMBOL == "SPY"
    assert ORCH.SAFE_HAVEN_SYMBOLS == ("GLD", "SLV")
    assert ORCH.SECTOR_ROTATION_PARAMS == {
        "lookback_bars": 20, "top_n": 5, "defensive_ma_period": 20, "score_scale": 10.0,
    }
