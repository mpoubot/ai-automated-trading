#!/usr/bin/env python3
"""Unit tests for `verify_new_etf_shortability.py` -- the read-only
shortability-verification script for the 12 ETF symbols added to the
pinned universe by the 2026-09-29 ETF curation pass.

Only the pure/dependency-injected functions (`verify_symbol_shortability`,
`build_report`) are exercised directly with a fake Alpaca client. `main()`
itself is not exercised here -- it requires real Alpaca credentials and a
real (or realistically faked) `TradingClient`/`Assets` API round trip,
exactly like `.335`'s own CLI `main()` is excluded from this repo's unit
suite for the same reason (see that module's own test file). No live
Alpaca access is required or attempted by any test in this file.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


ADAPTER = _load("aura_v05335_alpaca_equity_execution_adapter", ROOT / "aura_v05335_alpaca_equity_execution_adapter.py")
M = _load("verify_new_etf_shortability", ROOT / "verify_new_etf_shortability.py")


# ============================================================================
# Fakes -- mirrors .335's own test fixture shape (_FakeAsset/_FakeReadOnlyClient)
# ============================================================================


class _FakeEnumField:
    def __init__(self, value):
        self.value = value


class _FakeAsset:
    def __init__(self, **kwargs):
        self.symbol = kwargs.get("symbol")
        self.asset_class = _FakeEnumField(kwargs.get("asset_class", "us_equity"))
        self.exchange = _FakeEnumField(kwargs.get("exchange", "ARCA"))
        self.status = _FakeEnumField(kwargs.get("status", "active"))
        self.tradable = kwargs.get("tradable", True)
        self.shortable = kwargs.get("shortable", True)
        self.easy_to_borrow = kwargs.get("easy_to_borrow", True)
        self.fractionable = kwargs.get("fractionable", True)
        self.marginable = kwargs.get("marginable", True)
        self.min_order_size = kwargs.get("min_order_size")
        self.min_trade_increment = kwargs.get("min_trade_increment")
        self.price_increment = kwargs.get("price_increment")


class _FakeTradingClient:
    def __init__(self, assets_by_symbol):
        self._assets_by_symbol = assets_by_symbol
        self.calls = []

    def get_asset(self, symbol):
        self.calls.append(symbol)
        if symbol not in self._assets_by_symbol:
            raise KeyError(f"no fake asset configured for {symbol}")
        return self._assets_by_symbol[symbol]


# ============================================================================
# 1. NEW_ETF_SYMBOLS -- exactly the 12 symbols the 2026-09-29 curation
#    pass added, no more, no fewer.
# ============================================================================


def test_new_etf_symbols_is_exactly_the_twelve_added_symbols():
    assert M.NEW_ETF_SYMBOLS == (
        "DIA",
        "XLK", "XLF", "XLE", "XLV", "XLI", "XLP", "XLY", "XLB", "XLRE", "XLU", "XLC",
    )
    assert len(M.NEW_ETF_SYMBOLS) == 12
    assert len(set(M.NEW_ETF_SYMBOLS)) == 12  # no duplicates


def test_new_etf_symbols_are_all_present_in_the_real_v2_pinned_universe():
    technical_module = _load(
        "aura_v05351_live_alpaca_equity_signal_source",
        ROOT / "aura_v05351_live_alpaca_equity_signal_source.py",
    )
    universe = technical_module.load_pinned_universe()
    for symbol in M.NEW_ETF_SYMBOLS:
        assert symbol in universe.symbols


# ============================================================================
# 2. verify_symbol_shortability -- read-only, per-symbol, never raises
# ============================================================================


def test_verify_symbol_shortability_shortable_case():
    client = _FakeTradingClient({"DIA": _FakeAsset(symbol="DIA", shortable=True, easy_to_borrow=True)})
    result = M.verify_symbol_shortability("DIA", trading_client=client, execution_adapter_module=ADAPTER)
    assert result["ok"] is True
    assert result["symbol"] == "DIA"
    assert result["shortability_status"] == "SHORTABLE"
    assert result["shortable"] is True
    assert result["easy_to_borrow"] is True
    assert client.calls == ["DIA"]


def test_verify_symbol_shortability_not_shortable_case():
    client = _FakeTradingClient({"XLRE": _FakeAsset(symbol="XLRE", shortable=False, easy_to_borrow=False)})
    result = M.verify_symbol_shortability("XLRE", trading_client=client, execution_adapter_module=ADAPTER)
    assert result["ok"] is True
    assert result["shortability_status"] == "NOT_SHORTABLE"


def test_verify_symbol_shortability_unknown_case_hard_to_borrow():
    # shortable=True but easy_to_borrow=False -> conservative UNKNOWN,
    # exactly mirroring .335's own alpaca_asset_to_shortability_status
    # rule (never treated as SHORTABLE just because shortable=True).
    client = _FakeTradingClient({"XLU": _FakeAsset(symbol="XLU", shortable=True, easy_to_borrow=False)})
    result = M.verify_symbol_shortability("XLU", trading_client=client, execution_adapter_module=ADAPTER)
    assert result["ok"] is True
    assert result["shortability_status"] == "UNKNOWN"


def test_verify_symbol_shortability_lookup_failure_is_recorded_not_raised():
    client = _FakeTradingClient({})  # no assets configured -> get_asset raises KeyError
    result = M.verify_symbol_shortability("XLB", trading_client=client, execution_adapter_module=ADAPTER)
    assert result["ok"] is False
    assert result["symbol"] == "XLB"
    assert "error" in result
    assert "KeyError" in result["error"]


def test_verify_symbol_shortability_never_calls_a_submission_method():
    # The fake client below has no submit_order at all -- if
    # verify_symbol_shortability ever tried to call one, this would raise
    # AttributeError, proving read-only-only behaviour structurally.
    client = _FakeTradingClient({"XLK": _FakeAsset(symbol="XLK")})
    assert not hasattr(client, "submit_order")
    result = M.verify_symbol_shortability("XLK", trading_client=client, execution_adapter_module=ADAPTER)
    assert result["ok"] is True


# ============================================================================
# 3. build_report -- aggregation, never raises, honest summary buckets
# ============================================================================


def test_build_report_buckets_results_by_shortability_status():
    client = _FakeTradingClient({
        "DIA": _FakeAsset(symbol="DIA", shortable=True, easy_to_borrow=True),
        "XLF": _FakeAsset(symbol="XLF", shortable=False, easy_to_borrow=False),
        "XLE": _FakeAsset(symbol="XLE", shortable=True, easy_to_borrow=False),
    })
    results = tuple(
        M.verify_symbol_shortability(s, trading_client=client, execution_adapter_module=ADAPTER)
        for s in ("DIA", "XLF", "XLE")
    )
    report = M.build_report(results)
    assert report["all_lookups_ok"] is True
    assert report["summary"]["SHORTABLE"] == ["DIA"]
    assert report["summary"]["NOT_SHORTABLE"] == ["XLF"]
    assert report["summary"]["UNKNOWN"] == ["XLE"]
    assert report["summary"]["LOOKUP_FAILED"] == []
    assert report["paper"] is True
    assert report["live"] is False
    assert report["universe_version_this_verifies"] == "v2"


def test_build_report_all_lookups_ok_is_false_when_any_lookup_failed():
    client = _FakeTradingClient({"DIA": _FakeAsset(symbol="DIA")})
    results = (
        M.verify_symbol_shortability("DIA", trading_client=client, execution_adapter_module=ADAPTER),
        M.verify_symbol_shortability("NOPE", trading_client=client, execution_adapter_module=ADAPTER),
    )
    report = M.build_report(results)
    assert report["all_lookups_ok"] is False
    assert report["summary"]["LOOKUP_FAILED"] == ["NOPE"]


def test_build_report_includes_every_symbol_checked_regardless_of_outcome():
    client = _FakeTradingClient({s: _FakeAsset(symbol=s) for s in M.NEW_ETF_SYMBOLS})
    results = tuple(
        M.verify_symbol_shortability(s, trading_client=client, execution_adapter_module=ADAPTER)
        for s in M.NEW_ETF_SYMBOLS
    )
    report = M.build_report(results)
    assert len(report["results"]) == 12
    assert report["symbols_checked"] == list(M.NEW_ETF_SYMBOLS)
