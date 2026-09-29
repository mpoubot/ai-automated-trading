#!/usr/bin/env python3
"""Unit tests for AURA v0.5.3.67 earnings-calendar ingestion (Financial
Modeling Prep). Every test in this file is offline -- `http_get` is always
a fake, never `requests.get` -- matching this repo's universal
dependency-injected-network-call testing convention (see e.g. `.46`'s own
test file, or `verify_new_etf_shortability.py`'s tests)."""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


M = _load("aura_v05367_earnings_calendar_ingestion", ROOT / "aura_v05367_earnings_calendar_ingestion.py")


# ============================================================================
# Fakes
# ============================================================================


class FakeResponse:
    def __init__(self, *, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else []
        self.text = text

    def json(self):
        return self._payload


def fake_http_get_factory(*, status_code=200, payload=None, text="", capture=None):
    def fake_http_get(url, *, params, timeout):
        if capture is not None:
            capture.append({"url": url, "params": params, "timeout": timeout})
        return FakeResponse(status_code=status_code, payload=payload, text=text)

    return fake_http_get


FMP_ROW_AAPL_TODAY = {
    "date": "2026-09-23", "symbol": "AAPL", "eps": None, "epsEstimated": 1.5,
    "time": "amc", "revenue": None, "revenueEstimated": 90000000000, "fiscalDateEnding": "2026-09-30",
}
FMP_ROW_MSFT_FUTURE = {"date": "2026-10-05", "symbol": "MSFT", "epsEstimated": 3.1, "time": "bmo"}


# ============================================================================
# load_fmp_api_key
# ============================================================================


def test_load_fmp_api_key_missing_raises(monkeypatch):
    monkeypatch.delenv(M.FMP_API_KEY_ENV, raising=False)
    with pytest.raises(M.EarningsCalendarIngestionError):
        M.load_fmp_api_key()


def test_load_fmp_api_key_present(monkeypatch):
    monkeypatch.setenv(M.FMP_API_KEY_ENV, "test-key-123")
    assert M.load_fmp_api_key() == "test-key-123"


# ============================================================================
# fetch_earnings_calendar_raw
# ============================================================================


def test_fetch_earnings_calendar_raw_happy_path():
    captured = []
    http_get = fake_http_get_factory(payload=[FMP_ROW_AAPL_TODAY], capture=captured)
    rows = M.fetch_earnings_calendar_raw(
        "key", from_date=date(2026, 9, 20), to_date=date(2026, 10, 20), http_get=http_get,
    )
    assert rows == [FMP_ROW_AAPL_TODAY]
    assert captured[0]["params"] == {"from": "2026-09-20", "to": "2026-10-20", "apikey": "key"}
    assert captured[0]["url"] == M.FMP_EARNINGS_CALENDAR_URL


def test_fetch_earnings_calendar_raw_non_200_raises():
    http_get = fake_http_get_factory(status_code=429, text="rate limited")
    with pytest.raises(M.EarningsCalendarIngestionError, match="HTTP_429"):
        M.fetch_earnings_calendar_raw("key", from_date=date(2026, 9, 20), to_date=date(2026, 10, 20), http_get=http_get)


def test_fetch_earnings_calendar_raw_unexpected_shape_raises():
    http_get = fake_http_get_factory(payload={"error": "not a list"})
    with pytest.raises(M.EarningsCalendarIngestionError, match="UNEXPECTED_RESPONSE_SHAPE"):
        M.fetch_earnings_calendar_raw("key", from_date=date(2026, 9, 20), to_date=date(2026, 10, 20), http_get=http_get)


# ============================================================================
# parse_earnings_calendar_entries -- tolerant of missing/malformed rows
# ============================================================================


def test_parse_entries_happy_path():
    entries = M.parse_earnings_calendar_entries([FMP_ROW_AAPL_TODAY, FMP_ROW_MSFT_FUTURE])
    assert len(entries) == 2
    assert entries[0].symbol == "AAPL"
    assert entries[0].report_date == date(2026, 9, 23)
    assert entries[0].time_hint == "amc"
    assert entries[1].symbol == "MSFT"
    assert entries[1].report_date == date(2026, 10, 5)


def test_parse_entries_skips_rows_missing_symbol_or_date():
    rows = [
        {"date": "2026-09-23"},  # no symbol
        {"symbol": "AAPL"},  # no date
        {"symbol": "AAPL", "date": "not-a-date"},  # unparseable date
        "not-a-dict",
        FMP_ROW_AAPL_TODAY,
    ]
    entries = M.parse_earnings_calendar_entries(rows)
    assert len(entries) == 1
    assert entries[0].symbol == "AAPL"


def test_parse_entries_uppercases_symbol():
    entries = M.parse_earnings_calendar_entries([{"date": "2026-09-23", "symbol": "aapl"}])
    assert entries[0].symbol == "AAPL"


# ============================================================================
# build_calendar_by_symbol -- universe filtering
# ============================================================================


def test_build_calendar_by_symbol_filters_to_universe_and_sorts_dates():
    entries = M.parse_earnings_calendar_entries([
        FMP_ROW_AAPL_TODAY, FMP_ROW_MSFT_FUTURE,
        {"date": "2026-09-01", "symbol": "AAPL"},  # second AAPL row, earlier date
        {"date": "2026-11-01", "symbol": "TSLA"},  # not in universe
    ])
    calendar = M.build_calendar_by_symbol(entries, universe_symbols=("AAPL", "MSFT"))
    assert set(calendar.keys()) == {"AAPL", "MSFT"}
    assert calendar["AAPL"] == (date(2026, 9, 1), date(2026, 9, 23))
    assert "TSLA" not in calendar


def test_build_calendar_by_symbol_non_reporting_symbol_absent_not_empty_tuple():
    entries = M.parse_earnings_calendar_entries([FMP_ROW_AAPL_TODAY])
    # DIA never appears in FMP's data at all (it's an index ETF) -- confirm
    # it is simply absent as a key, not present with an empty tuple, since
    # `.68` distinguishes "absent" (not blacked out) from data-unavailable
    # by a different signal entirely (calendar_state.status).
    calendar = M.build_calendar_by_symbol(entries, universe_symbols=("AAPL", "DIA"))
    assert "DIA" not in calendar
    assert "AAPL" in calendar


# ============================================================================
# Cache round-trip + freshness
# ============================================================================


def test_save_and_load_calendar_cache_round_trip(tmp_path):
    entries = M.parse_earnings_calendar_entries([FMP_ROW_AAPL_TODAY, FMP_ROW_MSFT_FUTURE])
    M.save_calendar_cache(
        tmp_path, entries=entries, generated_at="2026-09-23T08:00:00+00:00",
        from_date=date(2026, 9, 20), to_date=date(2026, 10, 20),
    )
    cache = M.load_calendar_cache(tmp_path)
    assert cache is not None
    assert cache["generated_at"] == "2026-09-23T08:00:00+00:00"
    reparsed = M._parse_cached_entries(cache)
    assert len(reparsed) == 2
    assert reparsed[0].symbol == "AAPL"


def test_load_calendar_cache_missing_file_returns_none(tmp_path):
    assert M.load_calendar_cache(tmp_path) is None


def test_load_calendar_cache_corrupt_file_returns_none(tmp_path):
    path = tmp_path / M.DEFAULT_STATE_FILENAME
    path.write_text("not valid json{{{", encoding="utf-8")
    assert M.load_calendar_cache(tmp_path) is None


def test_is_cache_fresh_within_window():
    now = datetime(2026, 9, 23, 12, 0, 0, tzinfo=timezone.utc)
    generated_at = "2026-09-23T00:00:00+00:00"  # 12h old
    assert M.is_cache_fresh(generated_at, now=now, max_age_hours=24.0) is True


def test_is_cache_fresh_stale_beyond_window():
    now = datetime(2026, 9, 23, 12, 0, 0, tzinfo=timezone.utc)
    generated_at = "2026-09-20T00:00:00+00:00"  # 3.5 days old
    assert M.is_cache_fresh(generated_at, now=now, max_age_hours=24.0) is False


def test_is_cache_fresh_future_timestamp_is_not_fresh():
    # Negative age (cache claims to be from the future) must never be
    # treated as fresh -- fail-closed, not "somehow even fresher".
    now = datetime(2026, 9, 23, 12, 0, 0, tzinfo=timezone.utc)
    generated_at = "2026-09-24T00:00:00+00:00"
    assert M.is_cache_fresh(generated_at, now=now, max_age_hours=24.0) is False


def test_is_cache_fresh_malformed_timestamp_is_not_fresh():
    now = datetime(2026, 9, 23, 12, 0, 0, tzinfo=timezone.utc)
    assert M.is_cache_fresh("not-a-timestamp", now=now, max_age_hours=24.0) is False


# ============================================================================
# refresh_earnings_calendar_if_stale -- the orchestration function callers
# actually use.
# ============================================================================


NOW = datetime(2026, 9, 23, 12, 0, 0, tzinfo=timezone.utc)


def test_refresh_fetches_fresh_when_no_cache(tmp_path):
    captured = []
    http_get = fake_http_get_factory(payload=[FMP_ROW_AAPL_TODAY], capture=captured)
    state = M.refresh_earnings_calendar_if_stale(
        api_key="key", state_dir=tmp_path, universe_symbols=("AAPL",), now=NOW, http_get=http_get,
    )
    assert state.status == "OK"
    assert state.source == "FETCHED_FRESH"
    assert state.calendar_by_symbol == {"AAPL": (date(2026, 9, 23),)}
    assert len(captured) == 1
    assert M.load_calendar_cache(tmp_path) is not None  # cache was written


def test_refresh_reuses_fresh_cache_with_zero_network_calls(tmp_path):
    entries = M.parse_earnings_calendar_entries([FMP_ROW_AAPL_TODAY])
    M.save_calendar_cache(
        tmp_path, entries=entries, generated_at="2026-09-23T08:00:00+00:00",
        from_date=date(2026, 9, 20), to_date=date(2026, 10, 20),
    )
    captured = []
    http_get = fake_http_get_factory(payload=[], capture=captured)  # would prove a call happened
    state = M.refresh_earnings_calendar_if_stale(
        api_key="key", state_dir=tmp_path, universe_symbols=("AAPL",), now=NOW, http_get=http_get,
    )
    assert state.status == "OK"
    assert state.source == "CACHE_REUSED"
    assert state.calendar_by_symbol == {"AAPL": (date(2026, 9, 23),)}
    assert captured == []  # no network call made


def test_refresh_refetches_when_cache_stale(tmp_path):
    entries = M.parse_earnings_calendar_entries([FMP_ROW_AAPL_TODAY])
    M.save_calendar_cache(
        tmp_path, entries=entries, generated_at="2026-09-19T00:00:00+00:00",  # >24h old
        from_date=date(2026, 9, 15), to_date=date(2026, 10, 15),
    )
    captured = []
    http_get = fake_http_get_factory(payload=[FMP_ROW_MSFT_FUTURE], capture=captured)
    state = M.refresh_earnings_calendar_if_stale(
        api_key="key", state_dir=tmp_path, universe_symbols=("AAPL", "MSFT"), now=NOW, http_get=http_get,
    )
    assert len(captured) == 1  # a real refetch happened
    assert state.source == "FETCHED_FRESH"
    assert "AAPL" not in state.calendar_by_symbol  # stale cache's AAPL row is gone -- freshly fetched data only
    assert state.calendar_by_symbol == {"MSFT": (date(2026, 10, 5),)}


def test_refresh_fetch_failure_with_no_cache_is_fail_closed_unavailable(tmp_path):
    http_get = fake_http_get_factory(status_code=500, text="server error")
    state = M.refresh_earnings_calendar_if_stale(
        api_key="key", state_dir=tmp_path, universe_symbols=("AAPL",), now=NOW, http_get=http_get,
    )
    assert state.status == "UNAVAILABLE"
    assert state.source == "FETCH_FAILED_NO_USABLE_CACHE"
    assert state.calendar_by_symbol == {}
    assert "HTTP_500" in state.error


def test_refresh_fetch_failure_with_stale_cache_is_also_fail_closed_unavailable(tmp_path):
    # Martin's explicit choice (AskUserQuestion, 2026-09-29): a STALE cache
    # does not get reused as a fallback when refetch fails -- fail closed
    # for everyone, not "better than nothing".
    entries = M.parse_earnings_calendar_entries([FMP_ROW_AAPL_TODAY])
    M.save_calendar_cache(
        tmp_path, entries=entries, generated_at="2026-09-19T00:00:00+00:00",
        from_date=date(2026, 9, 15), to_date=date(2026, 10, 15),
    )
    http_get = fake_http_get_factory(status_code=500, text="server error")
    state = M.refresh_earnings_calendar_if_stale(
        api_key="key", state_dir=tmp_path, universe_symbols=("AAPL",), now=NOW, http_get=http_get,
    )
    assert state.status == "UNAVAILABLE"
    assert state.calendar_by_symbol == {}


def test_refresh_never_raises_for_network_failure(tmp_path):
    def raising_http_get(url, *, params, timeout):
        raise ConnectionError("dns resolution failed")

    state = M.refresh_earnings_calendar_if_stale(
        api_key="key", state_dir=tmp_path, universe_symbols=("AAPL",), now=NOW, http_get=raising_http_get,
    )
    assert state.status == "UNAVAILABLE"
    assert "ConnectionError" in state.error


def test_refresh_correctly_filters_a_universe_that_grew_since_the_cache_was_built(tmp_path):
    # Cache was built when the universe was just AAPL; a newly-added ETF
    # symbol (never in the fetched entries, but also never invalidates the
    # cache) must simply be absent from calendar_by_symbol, not force a
    # refetch and not raise.
    entries = M.parse_earnings_calendar_entries([FMP_ROW_AAPL_TODAY])
    M.save_calendar_cache(
        tmp_path, entries=entries, generated_at="2026-09-23T08:00:00+00:00",
        from_date=date(2026, 9, 20), to_date=date(2026, 10, 20),
    )
    captured = []
    http_get = fake_http_get_factory(payload=[], capture=captured)
    state = M.refresh_earnings_calendar_if_stale(
        api_key="key", state_dir=tmp_path, universe_symbols=("AAPL", "XLK"), now=NOW, http_get=http_get,
    )
    assert captured == []  # cache still fresh, no refetch triggered by universe growth
    assert "XLK" not in state.calendar_by_symbol
    assert "AAPL" in state.calendar_by_symbol
