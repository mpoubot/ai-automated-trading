#!/usr/bin/env python3
"""Offline contract tests for scripts/mexc_live_calibration_observer.py.

No real MEXC credentials, no real network call, anywhere in this file --
matching every other test file's discipline in this repo. Since the
calibration script itself is meant to run against a real live account,
these tests exist to catch bugs in its logic (pagination, classification
reuse, discrepancy flagging, the read-only guard) BEFORE it is ever
pointed at a real account, using fakes throughout.
"""
from __future__ import annotations

import importlib.util
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "mexc_live_calibration_observer.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("mexc_live_calibration_observer", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


MOD = _load_module()


def expect(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)
    print(f"PASS: {name}")


def raw_order(**overrides) -> dict:
    base = {
        "orderId": "mexc-order-1",
        "symbol": "BTC_USDT",
        "positionId": "0",
        "price": "50000",
        "vol": "1",
        "dealAvgPrice": "50000",
        "dealVol": "1",
        "state": "3",
        "externalOid": "aura-calib-1",
        "createTime": "1700000000000",
        "updateTime": "1700000001000",
    }
    base.update(overrides)
    return base


def ccxt_order(info: dict, **overrides) -> dict:
    base = {
        "id": info.get("orderId"),
        "info": info,
        "status": "closed",
        "filled": info.get("dealVol"),
        "amount": info.get("vol"),
        "clientOrderId": None,
        "timestamp": int(info.get("updateTime")) if info.get("updateTime") else None,
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------- #
# Read-only guard
# --------------------------------------------------------------------- #

class _FakeRealExchange:
    def __init__(self):
        self.rateLimit = 50
        self.has = {"fetchOpenOrders": True, "fetchMyTrades": True}
        self.create_order_called = False

    def fetch_orders(self, symbol, since=None, limit=None, params=None):
        return []

    def create_order(self, *a, **kw):
        self.create_order_called = True
        return {"id": "should-never-happen"}

    def cancel_order(self, *a, **kw):
        raise AssertionError("cancel_order should never be reachable through the guard")

    def set_leverage(self, *a, **kw):
        raise AssertionError("set_leverage should never be reachable through the guard")


def test_readonly_guard_allows_read_methods_and_blocks_writes() -> None:
    real = _FakeRealExchange()
    guarded = MOD._ReadOnlyExchangeGuard(real)

    expect("guard: allows fetch_orders through to the real exchange", guarded.fetch_orders("BTC/USDT:USDT") == [])
    expect("guard: allows rateLimit attribute through", guarded.rateLimit == 50)
    expect("guard: allows has dict through", guarded.has.get("fetchOpenOrders") is True)

    for method_name in ("create_order", "cancel_order", "set_leverage"):
        try:
            getattr(guarded, method_name)
            raise AssertionError(f"guard: {method_name} should have been refused but was returned")
        except RuntimeError as exc:
            expect(f"guard: {method_name} refused with READ_ONLY_GUARD_REFUSED_METHOD", "READ_ONLY_GUARD_REFUSED_METHOD" in str(exc))

    expect("guard: create_order on the underlying real exchange was never invoked", real.create_order_called is False)

    try:
        guarded.some_new_attribute = "x"
        raise AssertionError("guard: attribute assignment should have been refused")
    except RuntimeError as exc:
        expect("guard: attribute assignment refused", "READ_ONLY_GUARD_REFUSED_ATTRIBUTE_WRITE" in str(exc))


# --------------------------------------------------------------------- #
# Pagination
# --------------------------------------------------------------------- #

class _FakePaginatingExchange:
    """Serves fetch_orders() in fixed-size pages advancing by timestamp,
    mirroring how a real exchange's since-cursor pagination behaves."""

    def __init__(self, all_orders: list[dict], page_limit: int = 2):
        self.rateLimit = 10
        self._all = sorted(all_orders, key=lambda o: o["timestamp"])
        self._page_limit = page_limit
        self.calls: list[tuple] = []

    def fetch_orders(self, symbol, since=None, limit=None, params=None):
        self.calls.append((symbol, since, limit))
        eligible = [o for o in self._all if o["timestamp"] >= since]
        return eligible[:limit]


def test_pagination_advances_cursor_and_dedupes() -> None:
    orders = []
    for i in range(5):
        info = raw_order(orderId=f"order-{i}", externalOid=f"aura-{i}", updateTime=str(1700000000000 + i * 1000))
        orders.append(ccxt_order(info, timestamp=1700000000000 + i * 1000))

    fake = _FakePaginatingExchange(orders, page_limit=2)
    result, meta = MOD.paginated_fetch_orders(fake, "BTC/USDT:USDT", since_ms=1700000000000, page_limit=2, max_pages=10)

    expect("pagination: all 5 orders eventually collected across pages", len(result) == 5)
    expect("pagination: page count is more than one (actually paginated, not one lucky call)", meta["pages_fetched"] >= 3)
    expect("pagination: did not hit the page cap", meta["hit_page_cap"] is False)

    ids = {o["info"]["orderId"] for o in result}
    expect("pagination: no duplicate orders across page boundaries", ids == {f"order-{i}" for i in range(5)})


def test_pagination_respects_max_pages_cap() -> None:
    # An exchange that always returns a full page at the SAME timestamp --
    # cursor never advances meaningfully in a naive implementation, so
    # this also exercises the "no forward progress" early-exit path.
    orders = [ccxt_order(raw_order(orderId="stuck-order", updateTime="1700000000000"), timestamp=1700000000000)]
    fake = _FakePaginatingExchange(orders, page_limit=1)
    result, meta = MOD.paginated_fetch_orders(fake, "BTC/USDT:USDT", since_ms=1700000000000, page_limit=1, max_pages=3)
    expect("pagination: stalled cursor does not spin forever (stops via no-forward-progress or page cap)", meta["pages_fetched"] <= 3)
    expect("pagination: still returns the one real order despite the stall", len(result) == 1)


def test_pagination_empty_history() -> None:
    fake = _FakePaginatingExchange([], page_limit=100)
    result, meta = MOD.paginated_fetch_orders(fake, "BTC/USDT:USDT", since_ms=1700000000000, page_limit=100, max_pages=5)
    expect("pagination: empty history returns empty list, not an error", result == [])
    expect("pagination: empty history takes exactly one page", meta["pages_fetched"] == 1)


# --------------------------------------------------------------------- #
# observe_symbol: classification reuse + discrepancy flagging
# --------------------------------------------------------------------- #

class _FakeObserveExchange:
    def __init__(self, orders, positions=None, open_orders=None, my_trades=None,
                 supports_open_orders=True, supports_my_trades=True):
        self.rateLimit = 10
        self.has = {"fetchOpenOrders": supports_open_orders, "fetchMyTrades": supports_my_trades}
        self._orders = orders
        self._positions = positions or []
        self._open_orders = open_orders or []
        self._my_trades = my_trades or []

    def fetch_orders(self, symbol, since=None, limit=None, params=None):
        return self._orders

    def fetch_open_orders(self, symbol):
        return self._open_orders

    def fetch_my_trades(self, symbol, since=None, limit=None):
        return self._my_trades

    def fetch_positions(self, symbols=None):
        return self._positions

    def market(self, symbol):
        return {"id": symbol.split(":")[0].replace("/", "_")}


def test_observe_symbol_clean_fill_no_flags() -> None:
    info = raw_order(orderId="clean-1", externalOid="aura-clean-1", state="3", dealVol="1", vol="1", updateTime="1700000001000")
    order = ccxt_order(info, timestamp=1700000001000, status="closed", filled="1", amount="1")
    fake = _FakeObserveExchange([order])

    result = MOD.observe_symbol(fake, "BTC/USDT:USDT", lookback_days=90)

    expect("observe: order_count is 1", result["order_count"] == 1)
    expect("observe: no discrepancy flags on a clean fill", result["discrepancy_flags"] == [])
    expect("observe: .28 classification reached FILLED", result["orders"][0]["classified_order_status_via_28"] == "FILLED")
    expect("observe: .30 terminal classification reached FILLED_CLEAN", result["orders"][0]["classified_terminal_order_via_30"] == "FILLED_CLEAN")


def test_observe_symbol_flags_partial_fill_then_cancel() -> None:
    info = raw_order(orderId="partial-cancel-1", externalOid="aura-partial-1", state="4", dealVol="0.4", vol="1", updateTime="1700000001000")
    order = ccxt_order(info, timestamp=1700000001000, status="canceled", filled="0.4", amount="1")
    fake = _FakeObserveExchange([order])

    result = MOD.observe_symbol(fake, "BTC/USDT:USDT", lookback_days=90)

    flag_types = {f["type"] for f in result["discrepancy_flags"]}
    expect("observe: PARTIAL_FILL_THEN_CANCEL flagged", "PARTIAL_FILL_THEN_CANCEL" in flag_types)
    expect("observe: .30 terminal classification reached PARTIAL_ON_TERMINAL_ORDER", result["orders"][0]["classified_terminal_order_via_30"] == "PARTIAL_ON_TERMINAL_ORDER")


def test_observe_symbol_flags_unmapped_raw_state() -> None:
    info = raw_order(orderId="unmapped-1", externalOid="aura-unmapped-1", state="1", dealVol="0", vol="1")
    order = ccxt_order(info, timestamp=1700000001000, status="open", filled="0", amount="1")
    fake = _FakeObserveExchange([order])

    result = MOD.observe_symbol(fake, "BTC/USDT:USDT", lookback_days=90)

    flag_types = [f["type"] for f in result["discrepancy_flags"]]
    expect("observe: UNMAPPED_RAW_STATE flagged for raw state '1'", "UNMAPPED_RAW_STATE" in flag_types)
    expect("observe: raw_state_tally records the unmapped state", result["raw_state_tally"].get("1") == 1)


def test_observe_symbol_flags_ccxt_dealvol_mismatch() -> None:
    info = raw_order(orderId="mismatch-1", externalOid="aura-mismatch-1", state="3", dealVol="1", vol="1")
    # ccxt's own 'filled' field disagrees with the raw dealVol -- exactly
    # the item-7 discrepancy class this script exists to surface.
    order = ccxt_order(info, timestamp=1700000001000, status="closed", filled="0.5", amount="1")
    fake = _FakeObserveExchange([order])

    result = MOD.observe_symbol(fake, "BTC/USDT:USDT", lookback_days=90)

    flag_types = {f["type"] for f in result["discrepancy_flags"]}
    expect("observe: CCXT_FILLED_VS_RAW_DEALVOL_MISMATCH flagged", "CCXT_FILLED_VS_RAW_DEALVOL_MISMATCH" in flag_types)


def test_observe_symbol_records_position_and_capability_probes() -> None:
    info = raw_order(orderId="pos-1", externalOid="aura-pos-1", state="3", dealVol="1", vol="1")
    order = ccxt_order(info, timestamp=1700000001000)
    position_info = {"symbol": "BTC_USDT", "holdVol": "2", "positionId": "pos-abc"}
    fake = _FakeObserveExchange([order], positions=[{"info": position_info}])

    result = MOD.observe_symbol(fake, "BTC/USDT:USDT", lookback_days=90)

    expect("observe: position_exists True when holdVol > 0", result["current_position"]["position_exists"] is True)
    expect("observe: open_orders_endpoint_supported recorded True", result["open_orders_endpoint_supported"] is True)
    expect("observe: my_trades_endpoint_supported recorded True", result["my_trades_endpoint_supported"] is True)


def test_observe_symbol_no_orders_is_not_an_error() -> None:
    fake = _FakeObserveExchange([])
    result = MOD.observe_symbol(fake, "BTC/USDT:USDT", lookback_days=90)
    expect("observe: zero orders is a valid, non-error result", result["order_count"] == 0 and result["discrepancy_flags"] == [])


# --------------------------------------------------------------------- #
# AURA-reachability labeling (the critical distinction Martin required)
# --------------------------------------------------------------------- #

def test_aura_reachable_flag_is_asset_based_and_narrow() -> None:
    manifest = {
        "pinned_universe": [
            {"symbol": s, "currently_reachable_by_aura": s.split("/")[0] in MOD.AURA_CHAIN_REACHABLE_UNDERLYINGS}
            for s in ["BTC/USDT:USDT", "ETH/USDT:USDT", "SUI/USDT:USDT", "DOGE/USDT:USDT"]
        ]
    }
    reachable = {e["symbol"] for e in manifest["pinned_universe"] if e["currently_reachable_by_aura"]}
    expect("aura-reachable: BTC and ETH marked reachable", reachable == {"BTC/USDT:USDT", "ETH/USDT:USDT"})
    expect("aura-reachable: SUI and DOGE marked NOT reachable", "SUI/USDT:USDT" not in reachable and "DOGE/USDT:USDT" not in reachable)


# --------------------------------------------------------------------- #
# Credential handling -- fails closed, never falls back silently
# --------------------------------------------------------------------- #

def test_build_authenticated_exchange_fails_closed_without_credentials() -> None:
    import os
    saved_key = os.environ.pop("MEXC_API_KEY", None)
    saved_secret = os.environ.pop("MEXC_API_SECRET", None)
    try:
        try:
            MOD.build_authenticated_exchange()
            raise AssertionError("build_authenticated_exchange: should have failed closed with no credentials set")
        except RuntimeError as exc:
            expect("build_authenticated_exchange: fails closed with MISSING_MEXC_CREDENTIALS", "MISSING_MEXC_CREDENTIALS" in str(exc))
    finally:
        if saved_key is not None:
            os.environ["MEXC_API_KEY"] = saved_key
        if saved_secret is not None:
            os.environ["MEXC_API_SECRET"] = saved_secret


# --------------------------------------------------------------------- #
# select_live_scanner_universe: the real mexc_bot indicator/strategy code
# (imported, not reimplemented -- see module docstring), exercised
# against a fake public exchange. This is the least-tested, most
# mechanically complex part of the script (cross-repo dynamic import,
# sys.path manipulation, two-stage filter) so it gets its own explicit
# coverage rather than being trusted on the strength of the other tests.
# --------------------------------------------------------------------- #

class _FakeScannerExchange:
    """Public-data-only fake matching what mexc_bot's data_fetcher.py
    actually calls: load_markets(), fetch_tickers(), fetch_ohlcv()."""

    def __init__(self, markets: dict, tickers: dict, ohlcv_by_symbol: dict):
        self.rateLimit = 5
        self._markets = markets
        self._tickers = tickers
        self._ohlcv = ohlcv_by_symbol

    def load_markets(self):
        return self._markets

    def fetch_tickers(self):
        return self._tickers

    def fetch_ohlcv(self, symbol, timeframe=None, limit=None, since=None):
        return self._ohlcv.get(symbol, [])


def _synthetic_ohlcv(n: int, base_price: float, pct_swing: float) -> list[list[float]]:
    """A simple deterministic sawtooth series -- pct_swing controls how
    much each candle's range is, so ATR% comes out predictably above or
    below the filter thresholds without needing real market data."""
    candles = []
    ts = 1700000000000
    price = base_price
    for i in range(n):
        direction = 1 if i % 2 == 0 else -1
        high = price * (1 + pct_swing)
        low = price * (1 - pct_swing)
        close = price * (1 + direction * pct_swing * 0.5)
        candles.append([ts + i * 3_600_000, price, high, low, close, 1000.0])
        price = close
    return candles


def test_select_live_scanner_universe_end_to_end_with_fake_public_data() -> None:
    markets = {
        "BTC/USDT:USDT": {"active": True, "quote": "USDT", "swap": True},
        "ETH/USDT:USDT": {"active": True, "quote": "USDT", "swap": True},
        "DEAD/USDT:USDT": {"active": True, "quote": "USDT", "swap": True},   # will fail: near-zero ATR%
        "INACTIVE/USDT:USDT": {"active": False, "quote": "USDT", "swap": True},  # excluded: inactive
        "BTC/USDC:USDC": {"active": True, "quote": "USDC", "swap": True},   # excluded: wrong quote asset
    }
    tickers = {
        "BTC/USDT:USDT": {"quoteVolume": 50_000_000},
        "ETH/USDT:USDT": {"quoteVolume": 30_000_000},
        "DEAD/USDT:USDT": {"quoteVolume": 10_000_000},
        "INACTIVE/USDT:USDT": {"quoteVolume": 100_000_000},
        "BTC/USDC:USDC": {"quoteVolume": 100_000_000},
    }
    ohlcv = {
        "BTC/USDT:USDT": _synthetic_ohlcv(300, 50000, 0.03),   # ~3% swings -> passes ATR band
        "ETH/USDT:USDT": _synthetic_ohlcv(300, 3000, 0.03),
        "DEAD/USDT:USDT": _synthetic_ohlcv(300, 1.0, 0.0001),  # near-zero volatility -> fails ATR_PCT_MIN
    }
    fake = _FakeScannerExchange(markets, tickers, ohlcv)

    original_builder = MOD.build_public_exchange
    MOD.build_public_exchange = lambda: fake
    try:
        manifest = MOD.select_live_scanner_universe(top_n=20, max_scanned=60)
    finally:
        MOD.build_public_exchange = original_builder

    pinned_symbols = {e["symbol"] for e in manifest["pinned_universe"]}
    expect("scanner: BTC and ETH pass and are pinned", {"BTC/USDT:USDT", "ETH/USDT:USDT"} <= pinned_symbols)
    expect("scanner: DEAD excluded for failing the ATR%% band", "DEAD/USDT:USDT" not in pinned_symbols)
    expect("scanner: inactive market excluded entirely from candidates", "INACTIVE/USDT:USDT" not in pinned_symbols)
    expect("scanner: wrong-quote-asset market excluded entirely from candidates", "BTC/USDC:USDC" not in pinned_symbols)

    btc_entry = next(e for e in manifest["pinned_universe"] if e["symbol"] == "BTC/USDT:USDT")
    eth_entry = next(e for e in manifest["pinned_universe"] if e["symbol"] == "ETH/USDT:USDT")
    expect("scanner: BTC marked currently_reachable_by_aura", btc_entry["currently_reachable_by_aura"] is True)
    expect("scanner: ETH marked currently_reachable_by_aura", eth_entry["currently_reachable_by_aura"] is True)

    expect("scanner: manifest records scanner criteria", manifest["scanner_criteria"] == MOD.SCANNER_CRITERIA)
    expect("scanner: candidates_considered_count excludes inactive/wrong-quote before filtering", manifest["candidates_considered_count"] == 3)


def test_select_live_scanner_universe_respects_top_n() -> None:
    markets = {f"SYM{i}/USDT:USDT": {"active": True, "quote": "USDT", "swap": True} for i in range(5)}
    tickers = {f"SYM{i}/USDT:USDT": {"quoteVolume": 10_000_000 - i * 100} for i in range(5)}
    ohlcv = {f"SYM{i}/USDT:USDT": _synthetic_ohlcv(300, 100, 0.03) for i in range(5)}
    fake = _FakeScannerExchange(markets, tickers, ohlcv)

    original_builder = MOD.build_public_exchange
    MOD.build_public_exchange = lambda: fake
    try:
        manifest = MOD.select_live_scanner_universe(top_n=2, max_scanned=60)
    finally:
        MOD.build_public_exchange = original_builder

    expect("scanner: top_n=2 pins exactly 2 symbols out of 5 passing candidates", manifest["pinned_universe_count"] == 2)
    expect("scanner: pinned symbols are the two highest by quote volume", {e["symbol"] for e in manifest["pinned_universe"]} == {"SYM0/USDT:USDT", "SYM1/USDT:USDT"})


# --------------------------------------------------------------------- #
# CLI end-to-end: --universe-only, against a monkeypatched public builder
# --------------------------------------------------------------------- #

def test_run_calibration_universe_only_writes_expected_files() -> None:
    markets = {
        "BTC/USDT:USDT": {"active": True, "quote": "USDT", "swap": True},
        "ETH/USDT:USDT": {"active": True, "quote": "USDT", "swap": True},
    }
    tickers = {
        "BTC/USDT:USDT": {"quoteVolume": 50_000_000},
        "ETH/USDT:USDT": {"quoteVolume": 30_000_000},
    }
    ohlcv = {
        "BTC/USDT:USDT": _synthetic_ohlcv(300, 50000, 0.03),
        "ETH/USDT:USDT": _synthetic_ohlcv(300, 3000, 0.03),
    }
    fake = _FakeScannerExchange(markets, tickers, ohlcv)

    original_builder = MOD.build_public_exchange
    MOD.build_public_exchange = lambda: fake
    try:
        with tempfile.TemporaryDirectory() as d:
            out_dir = Path(d) / "calibration_output"
            MOD.run_calibration(top_n=20, lookback_days=90, max_scanned=60, output_dir=out_dir,
                                 symbols_override=None, universe_only=True)
            run_dirs = list(out_dir.iterdir())
            expect("cli: exactly one run directory created", len(run_dirs) == 1)
            manifest_path = run_dirs[0] / "calibration_run_manifest.json"
            expect("cli: manifest file written", manifest_path.exists())
            expect("cli: no per-symbol history files written in --universe-only mode",
                   not any(p.name not in ("calibration_run_manifest.json",) for p in run_dirs[0].iterdir()))
    finally:
        MOD.build_public_exchange = original_builder


def test_run_calibration_with_symbols_override_skips_scanner() -> None:
    with tempfile.TemporaryDirectory() as d:
        out_dir = Path(d) / "calibration_output"
        MOD.run_calibration(top_n=20, lookback_days=90, max_scanned=60, output_dir=out_dir,
                             symbols_override=["BTC/USDT:USDT", "SUI/USDT:USDT"], universe_only=True)
        run_dirs = list(out_dir.iterdir())
        manifest = (run_dirs[0] / "calibration_run_manifest.json").read_text(encoding="utf-8")
        expect("cli: symbols override present in manifest", "SUI/USDT:USDT" in manifest)
        expect("cli: override manifest marks selected_by_live_scanner False", '"selected_by_live_scanner": false' in manifest)


if __name__ == "__main__":
    tests = [obj for name, obj in list(globals().items()) if name.startswith("test_")]
    for t in tests:
        t()
    print("MEXC CALIBRATION OBSERVER CONTRACT: ALL PASS")
