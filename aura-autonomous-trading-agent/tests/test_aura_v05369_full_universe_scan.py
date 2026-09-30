#!/usr/bin/env python3
"""Unit tests for AURA v0.5.3.69 full tradable-universe scan. Every test
in this file is offline -- `assets_fetch_fn` is always a fake, never a
real `alpaca-py` client call -- matching this repo's universal
dependency-injected-network-call testing convention (see `.67`'s own
test file's identical `http_get` injection)."""
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


M = _load("aura_v05369_full_universe_scan", ROOT / "aura_v05369_full_universe_scan.py")
EQUITY_CLI = _load("aura_v05356_stage3_live_equity_cli", ROOT / "aura_v05356_stage3_live_equity_cli.py")


# ============================================================================
# Fakes
# ============================================================================


def fake_asset(symbol, *, tradable=True, exchange="NASDAQ", status="active", asset_class="us_equity"):
    return SimpleNamespace(symbol=symbol, tradable=tradable, exchange=exchange, status=status, asset_class=asset_class)


def fake_assets_fetch_fn_factory(assets, *, capture=None):
    def fake_assets_fetch_fn(client):
        if capture is not None:
            capture.append(client)
        return assets

    return fake_assets_fetch_fn


# ============================================================================
# fetch_tradable_us_equity_assets -- tradable=True filtering, nothing else
# ============================================================================


def test_fetch_tradable_us_equity_assets_filters_untradable_only():
    assets = [
        fake_asset("AAPL", tradable=True),
        fake_asset("DELISTEDCO", tradable=False),
        fake_asset("MSFT", tradable=True),
    ]
    result = M.fetch_tradable_us_equity_assets(
        alpaca_client=object(), assets_fetch_fn=fake_assets_fetch_fn_factory(assets),
    )
    symbols = {a.symbol for a in result}
    assert symbols == {"AAPL", "MSFT"}
    assert all(a.tradable for a in result)


def test_fetch_tradable_us_equity_assets_applies_no_price_or_exchange_filter():
    """Martin's explicit, final confirmed choice (2026-09-30, AskUserQuestion
    'tradable=True only, no other filtering') -- an OTC-exchange, low-symbol
    name is included exactly like a major-exchange one, as long as it's
    tradable. This test exists specifically to catch a future change that
    silently reintroduces exchange/price filtering."""
    assets = [
        fake_asset("BIGCO", tradable=True, exchange="NASDAQ"),
        fake_asset("OTCCO", tradable=True, exchange="OTC"),
    ]
    result = M.fetch_tradable_us_equity_assets(
        alpaca_client=object(), assets_fetch_fn=fake_assets_fetch_fn_factory(assets),
    )
    assert {a.symbol for a in result} == {"BIGCO", "OTCCO"}


def test_fetch_tradable_us_equity_assets_skips_missing_symbol():
    assets = [fake_asset(None, tradable=True), fake_asset("AAPL", tradable=True)]
    result = M.fetch_tradable_us_equity_assets(
        alpaca_client=object(), assets_fetch_fn=fake_assets_fetch_fn_factory(assets),
    )
    assert {a.symbol for a in result} == {"AAPL"}


# ============================================================================
# classify_asset_class
# ============================================================================


def test_classify_asset_class_known_etf_vs_default_stock():
    known = frozenset({"SPY", "QQQ"})
    assert M.classify_asset_class("SPY", known_etf_symbols=known) == "ETF"
    assert M.classify_asset_class("AAPL", known_etf_symbols=known) == "STOCK"


# ============================================================================
# Cache / refresh-if-stale -- mirrors .67's own cache tests.
# ============================================================================


def test_refresh_full_universe_if_stale_fetches_fresh_when_no_cache(tmp_path):
    assets = [fake_asset("AAPL"), fake_asset("MSFT")]
    now = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
    state = M.refresh_full_universe_if_stale(
        alpaca_client=object(), state_dir=tmp_path, now=now,
        assets_fetch_fn=fake_assets_fetch_fn_factory(assets),
    )
    assert state.status == "OK"
    assert state.source == "FETCHED_FRESH"
    assert set(state.symbols) == {"AAPL", "MSFT"}
    assert (tmp_path / M.DEFAULT_STATE_FILENAME).exists()


def test_refresh_full_universe_if_stale_reuses_fresh_cache_zero_network_calls(tmp_path):
    assets = [fake_asset("AAPL")]
    now = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
    M.refresh_full_universe_if_stale(
        alpaca_client=object(), state_dir=tmp_path, now=now,
        assets_fetch_fn=fake_assets_fetch_fn_factory(assets),
    )

    calls = []

    def failing_fetch_fn(client):
        calls.append(client)
        raise AssertionError("should not be called -- cache is fresh")

    state = M.refresh_full_universe_if_stale(
        alpaca_client=object(), state_dir=tmp_path, now=now + timedelta(hours=1),
        assets_fetch_fn=failing_fetch_fn,
    )
    assert state.status == "OK"
    assert state.source == "CACHE_REUSED"
    assert state.symbols == ("AAPL",)
    assert calls == []


def test_refresh_full_universe_if_stale_refetches_after_max_age(tmp_path):
    now = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
    M.refresh_full_universe_if_stale(
        alpaca_client=object(), state_dir=tmp_path, now=now,
        assets_fetch_fn=fake_assets_fetch_fn_factory([fake_asset("AAPL")]),
    )
    later = now + timedelta(hours=25)
    state = M.refresh_full_universe_if_stale(
        alpaca_client=object(), state_dir=tmp_path, now=later, max_age_hours=24.0,
        assets_fetch_fn=fake_assets_fetch_fn_factory([fake_asset("AAPL"), fake_asset("MSFT")]),
    )
    assert state.source == "FETCHED_FRESH"
    assert set(state.symbols) == {"AAPL", "MSFT"}


def test_refresh_full_universe_if_stale_fails_closed_no_usable_cache(tmp_path):
    def failing_fetch_fn(client):
        raise RuntimeError("simulated network failure")

    now = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
    state = M.refresh_full_universe_if_stale(
        alpaca_client=object(), state_dir=tmp_path, now=now, assets_fetch_fn=failing_fetch_fn,
    )
    assert state.status == "UNAVAILABLE"
    assert state.source == "FETCH_FAILED_NO_USABLE_CACHE"
    assert state.symbols == ()
    assert "RuntimeError" in state.error


def test_refresh_full_universe_if_stale_falls_back_to_stale_cache_on_refetch_failure(tmp_path):
    """A stale-but-present cache should NOT be silently reused as fresh
    data (that would violate the 24h freshness contract), but a refetch
    failure with NO usable cache at all is the only case that reports
    UNAVAILABLE with an empty symbol tuple -- this test documents that a
    present-but-stale cache, when refetch fails, still correctly reports
    UNAVAILABLE (fail-closed, matching .67's identical choice) rather than
    silently serving the stale data as if it were fresh."""
    now = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
    M.refresh_full_universe_if_stale(
        alpaca_client=object(), state_dir=tmp_path, now=now,
        assets_fetch_fn=fake_assets_fetch_fn_factory([fake_asset("AAPL")]),
    )

    def failing_fetch_fn(client):
        raise RuntimeError("simulated network failure")

    later = now + timedelta(hours=25)
    state = M.refresh_full_universe_if_stale(
        alpaca_client=object(), state_dir=tmp_path, now=later, max_age_hours=24.0, assets_fetch_fn=failing_fetch_fn,
    )
    assert state.status == "UNAVAILABLE"
    assert state.symbols == ()


# ============================================================================
# build_symbol_requests_from_full_universe
# ============================================================================


def test_build_symbol_requests_from_full_universe_classifies_and_auto_sizes():
    state = M.FullUniverseScanState(
        status="OK", source="FETCHED_FRESH", symbols=("AAPL", "SPY", "MSFT"), generated_at="2026-09-30T12:00:00+00:00",
    )
    requests = M.build_symbol_requests_from_full_universe(state, equity_cli_module=EQUITY_CLI)
    by_symbol = {r.symbol: r for r in requests}
    assert by_symbol["SPY"].asset_class == "ETF"
    assert by_symbol["AAPL"].asset_class == "STOCK"
    assert by_symbol["MSFT"].asset_class == "STOCK"
    assert all(r.quantity is None for r in requests)


def test_build_symbol_requests_from_full_universe_empty_state_yields_no_requests():
    state = M.FullUniverseScanState(status="UNAVAILABLE", source="FETCH_FAILED_NO_USABLE_CACHE", symbols=())
    requests = M.build_symbol_requests_from_full_universe(state, equity_cli_module=EQUITY_CLI)
    assert requests == ()


# ============================================================================
# FullUniverseScanState.to_dict()
# ============================================================================


def test_full_universe_scan_state_to_dict_shape():
    state = M.FullUniverseScanState(
        status="OK", source="FETCHED_FRESH", symbols=("AAPL", "MSFT"), generated_at="2026-09-30T12:00:00+00:00",
    )
    d = state.to_dict()
    assert d["status"] == "OK"
    assert d["symbol_count"] == 2
    assert d["symbols"] == ["AAPL", "MSFT"]
    assert d["filter_applied"].startswith("tradable=True")
