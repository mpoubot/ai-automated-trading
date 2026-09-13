#!/usr/bin/env python3
"""Contract + unit tests for AURA v0.5.3.46 News Ingestion + Classification.

Uses an injectable fake Alpaca news client (no real network call, no real
credentials, matching this repo's established `.45`/`.43` convention).
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
NEWS_PATH = ROOT / "aura_v05346_news_ingestion_classification.py"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


NEWS = _load("aura_v05346_news_ingestion_classification", NEWS_PATH)

NOW = datetime(2026, 9, 13, 12, 0, 0, tzinfo=timezone.utc)


class FakeArticle:
    def __init__(self, id, headline, summary="", url=None, author="Staff",
                 created_at=None, updated_at=None, symbols=None):
        self.id = id
        self.headline = headline
        self.summary = summary
        self.url = url
        self.author = author
        self.created_at = created_at or NOW
        self.updated_at = updated_at or NOW
        self.symbols = symbols if symbols is not None else []


class FakeNewsSet:
    def __init__(self, articles):
        self.data = {"news": list(articles)}


class FakeNewsClient:
    def __init__(self, articles=None, raise_exc=None):
        self._articles = articles or []
        self._raise_exc = raise_exc
        self.calls: list[Any] = []  # type: ignore[name-defined]

    def get_news(self, request_params):
        self.calls.append(request_params)
        if self._raise_exc is not None:
            raise self._raise_exc
        return FakeNewsSet(self._articles)


def _tmp_state_dir():
    d = tempfile.mkdtemp()
    return Path(d) / "state" / "news_ledger"


# ------------------------------------------------------------------------
# classify_news_item
# ------------------------------------------------------------------------

def test_classify_earnings_headline():
    categories, evidence = NEWS.classify_news_item("Acme Corp beats estimates in Q3 earnings", "")
    assert "EARNINGS" in categories
    assert evidence["EARNINGS"]


def test_classify_multiple_categories_non_exclusive():
    categories, evidence = NEWS.classify_news_item(
        "Acme Corp CEO resigns after SEC investigation into merger", "",
    )
    assert "MANAGEMENT_CHANGE" in categories
    assert "REGULATORY" in categories
    assert "MERGER_ACQUISITION" in categories
    assert len(categories) >= 3


def test_classify_uncategorized_when_nothing_matches():
    categories, evidence = NEWS.classify_news_item("A quiet afternoon in the market", "Nothing notable happened.")
    assert categories == ("UNCATEGORIZED",)
    assert evidence == {}


def test_classify_is_case_insensitive():
    categories, _ = NEWS.classify_news_item("ACME UPGRADES OUTLOOK AFTER STRONG DIVIDEND HIKE", "")
    assert "ANALYST_ACTION" in categories or "GUIDANCE" in categories
    assert "DIVIDEND" in categories


def test_classify_never_raises_on_empty_input():
    categories, evidence = NEWS.classify_news_item("", "")
    assert categories == ("UNCATEGORIZED",)
    assert evidence == {}


def test_classify_evidence_shows_matched_patterns():
    _, evidence = NEWS.classify_news_item("Company announces dividend increase", "")
    assert "DIVIDEND" in evidence
    assert isinstance(evidence["DIVIDEND"], tuple)
    assert len(evidence["DIVIDEND"]) >= 1


def test_classify_method_constant_is_deterministic_label():
    assert NEWS.CLASSIFICATION_METHOD == "DETERMINISTIC_KEYWORD_RULES"


# ------------------------------------------------------------------------
# fetch_alpaca_news
# ------------------------------------------------------------------------

def test_fetch_not_configured_when_client_is_none():
    items, status = NEWS.fetch_alpaca_news(None)
    assert items == []
    assert status.status == "NOT_CONFIGURED"
    assert status.error is None


def test_fetch_success_maps_fields_and_classifies():
    article = FakeArticle(
        id=12345, headline="Acme Corp beats Q3 earnings estimates", summary="Strong quarter.",
        url="https://example.com/a", author="Jane Reporter", symbols=["ACME"],
    )
    client = FakeNewsClient(articles=[article])
    items, status = NEWS.fetch_alpaca_news(client, symbols=("ACME",), limit=10, fetched_at=NOW.isoformat())

    assert status.status == "SUCCESS"
    assert status.items_fetched == 1
    assert len(items) == 1
    item = items[0]
    assert item.source == "ALPACA"
    assert item.external_id == "12345"
    assert item.headline == "Acme Corp beats Q3 earnings estimates"
    assert item.symbols == ("ACME",)
    assert "EARNINGS" in item.categories
    assert item.classification_method == "DETERMINISTIC_KEYWORD_RULES"
    assert item.content_hash
    assert item.fetched_at == NOW.isoformat()


def test_fetch_failure_returns_failed_status_not_empty_success():
    client = FakeNewsClient(raise_exc=ConnectionError("simulated network failure"))
    items, status = NEWS.fetch_alpaca_news(client)
    assert items == []
    assert status.status == "FAILED"
    assert "ConnectionError" in status.error


def test_fetch_handles_missing_optional_fields_without_fabricating():
    article = FakeArticle(id=1, headline="X", summary="", url=None, symbols=[])
    client = FakeNewsClient(articles=[article])
    items, status = NEWS.fetch_alpaca_news(client)
    assert status.status == "SUCCESS"
    assert items[0].url is None
    assert items[0].symbols == ()


def test_fetch_passes_symbol_filter_into_request():
    client = FakeNewsClient(articles=[])
    NEWS.fetch_alpaca_news(client, symbols=("AAPL", "MSFT"), limit=5)
    assert len(client.calls) == 1
    req = client.calls[0]
    assert getattr(req, "symbols") == "AAPL,MSFT"
    assert getattr(req, "limit") == 5


def test_fetch_empty_result_is_still_success():
    client = FakeNewsClient(articles=[])
    items, status = NEWS.fetch_alpaca_news(client)
    assert items == []
    assert status.status == "SUCCESS"
    assert status.items_fetched == 0


# ------------------------------------------------------------------------
# ledger persistence
# ------------------------------------------------------------------------

def test_load_ledger_empty_when_no_file():
    state_dir = _tmp_state_dir()
    assert NEWS.load_news_ledger(state_dir) == []


def test_append_ledger_persists_new_items():
    state_dir = _tmp_state_dir()
    article = FakeArticle(id=1, headline="Acme announces dividend", summary="")
    items, _ = NEWS.fetch_alpaca_news(FakeNewsClient(articles=[article]))
    new_count, dup_count = NEWS.append_news_ledger(state_dir, items)
    assert new_count == 1
    assert dup_count == 0

    ledger = NEWS.load_news_ledger(state_dir)
    assert len(ledger) == 1
    assert ledger[0]["external_id"] == "1"
    assert ledger[0]["categories"] == ["DIVIDEND"]


def test_append_ledger_dedups_by_source_and_external_id():
    state_dir = _tmp_state_dir()
    article = FakeArticle(id=42, headline="Acme merger announced", summary="")
    items, _ = NEWS.fetch_alpaca_news(FakeNewsClient(articles=[article]))

    first_new, first_dup = NEWS.append_news_ledger(state_dir, items)
    second_new, second_dup = NEWS.append_news_ledger(state_dir, items)

    assert first_new == 1 and first_dup == 0
    assert second_new == 0 and second_dup == 1
    assert len(NEWS.load_news_ledger(state_dir)) == 1


def test_append_ledger_allows_same_headline_different_id():
    """Two distinct articles that happen to share text are NOT deduped --
    dedup keys strictly on (source, external_id), never on content_hash,
    per the module's documented design (a source may legitimately reissue
    similar wording under a new id)."""
    state_dir = _tmp_state_dir()
    a1 = FakeArticle(id=1, headline="Acme announces dividend", summary="")
    a2 = FakeArticle(id=2, headline="Acme announces dividend", summary="")
    items, _ = NEWS.fetch_alpaca_news(FakeNewsClient(articles=[a1, a2]))
    new_count, dup_count = NEWS.append_news_ledger(state_dir, items)
    assert new_count == 2
    assert dup_count == 0


def test_ledger_state_hash_present_and_stable_for_same_content():
    state_dir = _tmp_state_dir()
    article = FakeArticle(id=7, headline="Fed raises interest rates", summary="")
    items, _ = NEWS.fetch_alpaca_news(FakeNewsClient(articles=[article]))
    NEWS.append_news_ledger(state_dir, items)

    path = NEWS._news_ledger_path(state_dir)
    with path.open() as f:
        body = json.load(f)
    assert "state_hash" in body
    recomputed = NEWS.sha256_text(NEWS.stable_json({"events": body["events"]}))
    assert body["state_hash"] == recomputed


def test_ledger_write_is_atomic_no_tmp_file_left_behind():
    state_dir = _tmp_state_dir()
    article = FakeArticle(id=9, headline="Acme launches new product", summary="")
    items, _ = NEWS.fetch_alpaca_news(FakeNewsClient(articles=[article]))
    NEWS.append_news_ledger(state_dir, items)
    tmp_path = NEWS._news_ledger_path(state_dir).with_suffix(".json.tmp")
    assert not tmp_path.exists()


# ------------------------------------------------------------------------
# ingest_and_classify_news (orchestration)
# ------------------------------------------------------------------------

def test_ingest_success_path_reports_new_and_persists():
    state_dir = _tmp_state_dir()
    article = FakeArticle(id=100, headline="Acme beats earnings estimates", summary="")
    client = FakeNewsClient(articles=[article])

    result = NEWS.ingest_and_classify_news(client, state_dir, fetched_at=NOW.isoformat())

    assert result.fetch_status.status == "SUCCESS"
    assert result.items_fetched == 1
    assert result.items_new == 1
    assert result.items_duplicate == 0
    assert Path(result.ledger_path).is_file()


def test_ingest_second_call_with_same_article_reports_duplicate():
    state_dir = _tmp_state_dir()
    article = FakeArticle(id=200, headline="Acme sued in court", summary="")
    client = FakeNewsClient(articles=[article])

    NEWS.ingest_and_classify_news(client, state_dir, fetched_at=NOW.isoformat())
    result2 = NEWS.ingest_and_classify_news(client, state_dir, fetched_at=NOW.isoformat())

    assert result2.items_fetched == 1
    assert result2.items_new == 0
    assert result2.items_duplicate == 1


def test_ingest_failed_fetch_does_not_touch_existing_ledger():
    state_dir = _tmp_state_dir()
    good_article = FakeArticle(id=300, headline="Acme wins lawsuit settlement", summary="")
    good_client = FakeNewsClient(articles=[good_article])
    NEWS.ingest_and_classify_news(good_client, state_dir, fetched_at=NOW.isoformat())
    ledger_before = NEWS.load_news_ledger(state_dir)
    assert len(ledger_before) == 1

    failing_client = FakeNewsClient(raise_exc=ConnectionError("boom"))
    result = NEWS.ingest_and_classify_news(failing_client, state_dir, fetched_at=NOW.isoformat())

    assert result.fetch_status.status == "FAILED"
    assert result.items_new == 0
    ledger_after = NEWS.load_news_ledger(state_dir)
    assert ledger_after == ledger_before


def test_ingest_not_configured_client_does_not_write_ledger():
    state_dir = _tmp_state_dir()
    result = NEWS.ingest_and_classify_news(None, state_dir)
    assert result.fetch_status.status == "NOT_CONFIGURED"
    assert result.items_new == 0
    assert not NEWS._news_ledger_path(state_dir).is_file()


def test_ingest_multiple_items_mixed_new_and_duplicate():
    state_dir = _tmp_state_dir()
    a1 = FakeArticle(id=1, headline="Acme beats earnings estimates", summary="")
    a2 = FakeArticle(id=2, headline="Acme announces dividend", summary="")
    client1 = FakeNewsClient(articles=[a1])
    NEWS.ingest_and_classify_news(client1, state_dir, fetched_at=NOW.isoformat())

    client2 = FakeNewsClient(articles=[a1, a2])
    result = NEWS.ingest_and_classify_news(client2, state_dir, fetched_at=NOW.isoformat())

    assert result.items_fetched == 2
    assert result.items_new == 1
    assert result.items_duplicate == 1
    assert len(NEWS.load_news_ledger(state_dir)) == 2


# ------------------------------------------------------------------------
# Governance / non-goal invariants -- these tests exist specifically to
# lock in the scoping decisions so a future edit cannot silently expand
# `.46` past its authorized scope.
# ------------------------------------------------------------------------

def test_module_has_no_sentiment_scoring_function():
    assert not hasattr(NEWS, "score_sentiment")
    assert not hasattr(NEWS, "compute_sentiment")


def test_module_has_no_trade_or_order_function():
    for name in dir(NEWS):
        lowered = name.lower()
        assert "submit_order" not in lowered
        assert "place_trade" not in lowered
        assert "authorize" not in lowered


def test_module_has_no_earnings_blackout_function():
    assert not hasattr(NEWS, "compute_blackout_window")
    assert not hasattr(NEWS, "is_earnings_blackout")


def test_ingestion_result_carries_no_direction_or_size_fields():
    result = NEWS.IngestionResult(
        fetch_status=NEWS.NewsFetchStatus(source="ALPACA", status="SUCCESS", error=None,
                                           fetched_at=NOW.isoformat(), items_fetched=0),
        items_fetched=0, items_new=0, items_duplicate=0, ledger_path="x",
    )
    d = result.to_dict()
    assert "direction" not in d
    assert "size" not in d
    assert "signal" not in d
