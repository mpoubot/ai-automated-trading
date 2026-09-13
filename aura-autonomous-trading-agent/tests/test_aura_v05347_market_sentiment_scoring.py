#!/usr/bin/env python3
"""Contract + unit tests for AURA v0.5.3.47 Market Sentiment Scoring.

Builds real `.46` NewsItem event dicts (via `.46`'s own fetch/classify
path against an injected fake client, not hand-typed dicts) wherever
practical, and exercises `.47`'s pure scoring functions on them.
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NEWS_PATH = ROOT / "aura_v05346_news_ingestion_classification.py"
SENT_PATH = ROOT / "aura_v05347_market_sentiment_scoring.py"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


NEWS = _load("aura_v05346_news_ingestion_classification", NEWS_PATH)
SENT = _load("aura_v05347_market_sentiment_scoring", SENT_PATH)

NOW = datetime(2026, 9, 13, 12, 0, 0, tzinfo=timezone.utc)


class FakeArticle:
    def __init__(self, id, headline, summary="", symbols=None, source="Benzinga", created_at=None):
        self.id = id
        self.headline = headline
        self.summary = summary
        self.url = None
        self.author = "Staff"
        self.created_at = created_at or NOW
        self.updated_at = created_at or NOW
        self.symbols = symbols if symbols is not None else []
        self.source = source


class FakeNewsSet:
    def __init__(self, articles):
        self.data = {"news": list(articles)}


class FakeNewsClient:
    def __init__(self, articles):
        self._articles = articles

    def get_news(self, request_params):
        return FakeNewsSet(self._articles)


def _events_from(articles):
    items, _status = NEWS.fetch_alpaca_news(FakeNewsClient(articles), fetched_at=NOW.isoformat())
    return [i.to_dict() for i in items]


def _hours_ago(h):
    return NOW - timedelta(hours=h)


# ------------------------------------------------------------------------
# classify_sentiment_direction
# ------------------------------------------------------------------------

def test_bullish_keyword_detected():
    direction, evidence = SENT.classify_sentiment_direction("Acme beats estimates and raises guidance", "")
    assert direction == "BULLISH"
    assert "BULLISH" in evidence


def test_bearish_keyword_detected():
    direction, evidence = SENT.classify_sentiment_direction("Acme misses estimates, shares plunge", "")
    assert direction == "BEARISH"
    assert "BEARISH" in evidence


def test_neutral_when_no_cues_match():
    direction, evidence = SENT.classify_sentiment_direction("Acme to present at investor conference", "")
    assert direction == "NEUTRAL"
    assert evidence == {}


def test_mixed_when_both_cues_present():
    direction, evidence = SENT.classify_sentiment_direction(
        "Acme beats estimates but warns of weak demand ahead", "",
    )
    assert direction == "MIXED"
    assert "BULLISH" in evidence and "BEARISH" in evidence


def test_classify_direction_never_raises_on_empty_input():
    direction, evidence = SENT.classify_sentiment_direction("", "")
    assert direction == "NEUTRAL"
    assert evidence == {}


# ------------------------------------------------------------------------
# score_symbol_sentiment -- required-parameter enforcement
# ------------------------------------------------------------------------

def test_decay_window_is_required_zero_rejected():
    events = _events_from([FakeArticle(1, "Acme beats estimates", symbols=["ACME"])])
    try:
        SENT.score_symbol_sentiment(events, "ACME", decay_window_hours=0, min_source_count=1, now=NOW)
        assert False, "expected SentimentScoringError"
    except SENT.SentimentScoringError:
        pass


def test_decay_window_none_rejected():
    events = _events_from([FakeArticle(1, "Acme beats estimates", symbols=["ACME"])])
    try:
        SENT.score_symbol_sentiment(events, "ACME", decay_window_hours=None, min_source_count=1, now=NOW)
        assert False, "expected SentimentScoringError"
    except SENT.SentimentScoringError:
        pass


def test_min_source_count_zero_rejected():
    events = _events_from([FakeArticle(1, "Acme beats estimates", symbols=["ACME"])])
    try:
        SENT.score_symbol_sentiment(events, "ACME", decay_window_hours=48, min_source_count=0, now=NOW)
        assert False, "expected SentimentScoringError"
    except SENT.SentimentScoringError:
        pass


def test_module_documents_but_never_reads_example_window():
    """EXAMPLE_RESEARCH_DECAY_WINDOW_HOURS exists as documentation only --
    no function in the module may reference it as a default."""
    assert SENT.EXAMPLE_RESEARCH_DECAY_WINDOW_HOURS == 48.0
    import inspect
    src = inspect.getsource(SENT.score_symbol_sentiment) + inspect.getsource(SENT.score_all_symbols)
    assert "EXAMPLE_RESEARCH_DECAY_WINDOW_HOURS" not in src


# ------------------------------------------------------------------------
# score_symbol_sentiment -- data quality / no-data / recency
# ------------------------------------------------------------------------

def test_no_data_status_when_symbol_has_no_events():
    events = _events_from([FakeArticle(1, "Acme beats estimates", symbols=["ACME"])])
    regime = SENT.score_symbol_sentiment(events, "MSFT", decay_window_hours=48, min_source_count=1, now=NOW)
    assert regime.corroboration_status == "NO_DATA"
    assert regime.items_considered == 0
    assert regime.raw_score is None
    assert regime.promotable_score is None


def test_events_outside_decay_window_excluded():
    old_article = FakeArticle(1, "Acme beats estimates", symbols=["ACME"], created_at=_hours_ago(100))
    events = _events_from([old_article])
    regime = SENT.score_symbol_sentiment(events, "ACME", decay_window_hours=48, min_source_count=1, now=NOW)
    assert regime.items_considered == 0
    assert regime.corroboration_status == "NO_DATA"


def test_events_without_symbol_tag_excluded():
    events = _events_from([FakeArticle(1, "Acme beats estimates", symbols=[])])
    regime = SENT.score_symbol_sentiment(events, "ACME", decay_window_hours=48, min_source_count=1, now=NOW)
    assert regime.items_considered == 0


def test_future_timestamp_clock_skew_treated_as_zero_age_not_negative():
    future_article = FakeArticle(1, "Acme beats estimates", symbols=["ACME"], created_at=NOW + timedelta(hours=1))
    events = _events_from([future_article])
    regime = SENT.score_symbol_sentiment(events, "ACME", decay_window_hours=48, min_source_count=1, now=NOW)
    assert regime.items_considered == 1
    assert regime.raw_score == 1.0  # full weight, not excluded or negative-weighted


# ------------------------------------------------------------------------
# score_symbol_sentiment -- corroboration / source diversity
# ------------------------------------------------------------------------

def test_single_source_insufficient_corroboration_hides_score():
    events = _events_from([
        FakeArticle(1, "Acme beats estimates", symbols=["ACME"], source="Benzinga"),
        FakeArticle(2, "Acme raises guidance", symbols=["ACME"], source="Benzinga"),
        FakeArticle(3, "Acme beats estimates again", symbols=["ACME"], source="Benzinga"),
    ])
    regime = SENT.score_symbol_sentiment(events, "ACME", decay_window_hours=48, min_source_count=2, now=NOW)
    assert regime.items_considered == 3
    assert regime.source_count == 1
    assert regime.corroboration_status == "INSUFFICIENT"
    assert regime.promotable_score is None
    assert regime.raw_score is not None  # still computed/visible for audit


def test_multi_source_meets_threshold_promotes_score():
    events = _events_from([
        FakeArticle(1, "Acme beats estimates", symbols=["ACME"], source="Benzinga"),
        FakeArticle(2, "Acme raises guidance", symbols=["ACME"], source="GlobeNewswire"),
    ])
    regime = SENT.score_symbol_sentiment(events, "ACME", decay_window_hours=48, min_source_count=2, now=NOW)
    assert regime.source_count == 2
    assert regime.corroboration_status == "SUFFICIENT"
    assert regime.promotable_score is not None
    assert regime.promotable_score == regime.raw_score


def test_ten_articles_one_wire_never_counted_as_ten_sources():
    articles = [FakeArticle(i, "Acme beats estimates", symbols=["ACME"], source="Benzinga") for i in range(10)]
    events = _events_from(articles)
    regime = SENT.score_symbol_sentiment(events, "ACME", decay_window_hours=48, min_source_count=2, now=NOW)
    assert regime.items_considered == 10
    assert regime.source_count == 1
    assert regime.corroboration_status == "INSUFFICIENT"


def test_none_origin_source_not_counted_as_a_distinct_source():
    events = _events_from([FakeArticle(1, "Acme beats estimates", symbols=["ACME"])])
    events[0]["origin_source"] = None
    regime = SENT.score_symbol_sentiment(events, "ACME", decay_window_hours=48, min_source_count=1, now=NOW)
    assert regime.source_count == 0
    assert regime.corroboration_status == "INSUFFICIENT"


# ------------------------------------------------------------------------
# score_symbol_sentiment -- score math / recency weighting
# ------------------------------------------------------------------------

def test_bullish_and_bearish_items_partially_offset():
    events = _events_from([
        FakeArticle(1, "Acme beats estimates", symbols=["ACME"], source="A"),
        FakeArticle(2, "Acme misses estimates", symbols=["ACME"], source="B"),
    ])
    regime = SENT.score_symbol_sentiment(events, "ACME", decay_window_hours=48, min_source_count=1, now=NOW)
    assert regime.bullish_count == 1
    assert regime.bearish_count == 1
    assert regime.raw_score == 0.0


def test_recency_weighting_favors_fresher_item():
    events = _events_from([
        FakeArticle(1, "Acme beats estimates", symbols=["ACME"], source="A", created_at=_hours_ago(1)),
        FakeArticle(2, "Acme misses estimates", symbols=["ACME"], source="B", created_at=_hours_ago(47)),
    ])
    regime = SENT.score_symbol_sentiment(events, "ACME", decay_window_hours=48, min_source_count=1, now=NOW)
    # fresh bullish item outweighs the nearly-decayed-to-zero bearish item
    assert regime.raw_score is not None
    assert regime.raw_score > 0.0


def test_neutral_and_mixed_items_counted_but_contribute_zero():
    events = _events_from([
        FakeArticle(1, "Acme to present at investor conference", symbols=["ACME"], source="A"),
        FakeArticle(2, "Acme beats estimates but warns of weak demand", symbols=["ACME"], source="B"),
    ])
    regime = SENT.score_symbol_sentiment(events, "ACME", decay_window_hours=48, min_source_count=1, now=NOW)
    assert regime.neutral_count == 1
    assert regime.mixed_count == 1
    assert regime.raw_score == 0.0


def test_score_is_deterministic_reproducible_across_calls():
    events = _events_from([
        FakeArticle(1, "Acme beats estimates", symbols=["ACME"], source="A"),
        FakeArticle(2, "Acme raises guidance", symbols=["ACME"], source="B"),
    ])
    r1 = SENT.score_symbol_sentiment(events, "ACME", decay_window_hours=48, min_source_count=1, now=NOW)
    r2 = SENT.score_symbol_sentiment(events, "ACME", decay_window_hours=48, min_source_count=1, now=NOW)
    assert r1.to_dict() == r2.to_dict()


def test_input_event_ids_recoverable_for_audit():
    events = _events_from([
        FakeArticle(101, "Acme beats estimates", symbols=["ACME"], source="A"),
        FakeArticle(202, "Acme raises guidance", symbols=["ACME"], source="B"),
    ])
    regime = SENT.score_symbol_sentiment(events, "ACME", decay_window_hours=48, min_source_count=1, now=NOW)
    assert set(regime.input_event_ids) == {"101", "202"}


def test_missing_created_at_excludes_item_rather_than_assuming_fresh():
    events = _events_from([FakeArticle(1, "Acme beats estimates", symbols=["ACME"])])
    events[0]["created_at"] = None
    regime = SENT.score_symbol_sentiment(events, "ACME", decay_window_hours=48, min_source_count=1, now=NOW)
    assert regime.items_considered == 0
    assert regime.corroboration_status == "NO_DATA"


# ------------------------------------------------------------------------
# score_all_symbols
# ------------------------------------------------------------------------

def test_score_all_symbols_discovers_every_tagged_symbol():
    events = _events_from([
        FakeArticle(1, "Acme beats estimates", symbols=["ACME"], source="A"),
        FakeArticle(2, "Widget Co misses estimates", symbols=["WIDGET"], source="B"),
    ])
    regimes = SENT.score_all_symbols(events, decay_window_hours=48, min_source_count=1, now=NOW)
    assert set(regimes.keys()) == {"ACME", "WIDGET"}
    assert regimes["ACME"].raw_score == 1.0
    assert regimes["WIDGET"].raw_score == -1.0


def test_score_all_symbols_excludes_macro_unsymbolized_items():
    events = _events_from([
        FakeArticle(1, "Fed raises interest rates", symbols=[]),
        FakeArticle(2, "Acme beats estimates", symbols=["ACME"], source="A"),
    ])
    regimes = SENT.score_all_symbols(events, decay_window_hours=48, min_source_count=1, now=NOW)
    assert set(regimes.keys()) == {"ACME"}


def test_score_all_symbols_empty_ledger_returns_empty_dict():
    regimes = SENT.score_all_symbols([], decay_window_hours=48, min_source_count=1, now=NOW)
    assert regimes == {}


def test_multi_symbol_item_contributes_to_each_tagged_symbol():
    events = _events_from([
        FakeArticle(1, "Acme and Widget Co announce merger", symbols=["ACME", "WIDGET"], source="A"),
    ])
    regimes = SENT.score_all_symbols(events, decay_window_hours=48, min_source_count=1, now=NOW)
    assert set(regimes.keys()) == {"ACME", "WIDGET"}
    assert regimes["ACME"].items_considered == 1
    assert regimes["WIDGET"].items_considered == 1


# ------------------------------------------------------------------------
# Governance / non-goal invariants
# ------------------------------------------------------------------------

def test_module_has_no_trade_or_order_function():
    for name in dir(SENT):
        lowered = name.lower()
        assert "submit_order" not in lowered
        assert "place_trade" not in lowered
        assert "authorize" not in lowered
        assert "execute" not in lowered


def test_module_has_no_market_wide_aggregate_function():
    assert not hasattr(SENT, "score_market_wide")
    assert not hasattr(SENT, "score_macro")


def test_regime_to_dict_carries_no_direction_or_size_for_execution():
    regime = SENT.SentimentRegime(
        symbol="ACME", as_of=NOW.isoformat(), decay_window_hours=48, min_source_count=1,
        items_considered=0, input_event_ids=(), distinct_origin_sources=(), source_count=0,
        corroboration_status="NO_DATA", bullish_count=0, bearish_count=0, neutral_count=0,
        mixed_count=0, raw_score=None, promotable_score=None,
    )
    d = regime.to_dict()
    assert "order_direction" not in d
    assert "position_size" not in d
    assert "signal" not in d
