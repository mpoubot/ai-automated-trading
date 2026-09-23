"""
Offline verification of core/mexc_native.py's parsing logic.

Two fixture sets, both real MEXC data — no network call is made by this
test itself:

1. KLINE_FIXTURE / FUNDING_FIXTURE — captured verbatim from ccxt==4.5.78's
   own mexc.py docstrings (Step 2 evidence).
2. data/native/raw/.../pilot_live_2026-09-17*.json — captured live from
   contract.mexc.com on 2026-09-17 via Martin's Windows machine (the
   pre-download pilot for BTC_USDT, Min60 + funding history), saved
   verbatim as part of "preserve enough raw information to reproduce the
   dataset later."

Run: python3 tests/test_mexc_native.py  (from the mexc_bot/ directory)
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
from core.mexc_native import (
    to_native_symbol, kline_response_to_df, funding_response_to_df,
)

REPO_ROOT = Path(__file__).parent.parent

# --- Fixture 1: real kline response, verbatim from ccxt mexc.py L1902-1915 ---
KLINE_FIXTURE = {
    "success": True,
    "code": 0,
    "data": {
        "time": [1634052300, 1634052360, 1634052420],
        "open": [3492.2, 3491.3, 3495.65],
        "close": [3491.3, 3495.65, 3495.2],
        "high": [3495.85, 3496.55, 3499.4],
        "low": [3491.15, 3490.9, 3494.2],
        "vol": [1740.0, 351.0, 314.0],
        "amount": [60793.623, 12260.4885, 10983.1375],
        # real* deliberately absent, exactly as in the captured example —
        # tests that our parser tolerates a response with no real* fields.
    },
}

# --- Fixture 2: real funding-history response, verbatim from ccxt mexc.py
#     L4412-4433 (fetch_funding_rate_history docstring) ---
FUNDING_FIXTURE = {
    "success": True,
    "code": 0,
    "data": {
        "pageSize": 2,
        "totalCount": 21,
        "totalPage": 11,
        "currentPage": 1,
        "resultList": [
            {"symbol": "BTC_USDT", "fundingRate": 0.000266, "settleTime": 1609804800000},
            {"symbol": "BTC_USDT", "fundingRate": 0.00029, "settleTime": 1609776000000},
        ],
    },
}


def test_symbol_mapping():
    assert to_native_symbol("BTC/USDT:USDT") == "BTC_USDT"
    assert to_native_symbol("SUI/USDT:USDT") == "SUI_USDT"
    assert to_native_symbol("BTC_USDT") == "BTC_USDT"  # idempotent passthrough
    print("PASS: symbol mapping")


def test_kline_parsing():
    df = kline_response_to_df(KLINE_FIXTURE)
    assert len(df) == 3
    assert list(df.columns) == ["timestamp", "open", "high", "low", "close",
                                 "volume", "amount", "real_open", "real_high",
                                 "real_low", "real_close"]
    # verified fact #1: native time is SECONDS -> must land on the correct
    # real-world UTC instant once interpreted as seconds, not ms.
    # Timestamps are tz-naive by design (matches data_fetcher.py) — see
    # module docstring.
    assert df["timestamp"].dt.tz is None
    expected_ts = pd.Timestamp(1634052300, unit="s")
    assert df["timestamp"].iloc[0] == expected_ts, (df["timestamp"].iloc[0], expected_ts)
    assert df["timestamp"].iloc[0].year == 2021 and df["timestamp"].iloc[0].month == 10
    # verified fact #2: parser must carry the PLAIN close, not real_close
    assert df["close"].iloc[0] == 3491.3
    assert pd.isna(df["real_close"].iloc[0])  # absent in fixture -> NaN, not crash
    # monotonic increasing timestamps, 60s apart (Min1 interval in the fixture)
    assert (df["timestamp"].diff().dropna().dt.total_seconds() == 60).all()
    print("PASS: kline parsing (time units, plain vs real* fields, schema)")


def test_funding_parsing():
    df = funding_response_to_df(FUNDING_FIXTURE)
    assert len(df) == 2
    # verified fact #3: settleTime is MILLISECONDS; tz-naive by design
    assert df["timestamp"].dt.tz is None
    expected_ts0 = pd.Timestamp(1609804800000, unit="ms")
    assert df["timestamp"].iloc[0] == expected_ts0
    assert df["timestamp"].iloc[0].year == 2021 and df["timestamp"].iloc[0].month == 1
    # resultList is newest-first in the raw fixture (as in the real API) —
    # confirm our parser preserves that order (sorting happens one level up,
    # in fetch_funding_history_native, not in funding_response_to_df).
    assert df["timestamp"].iloc[0] > df["timestamp"].iloc[1]
    assert (df["timestamp"].iloc[0] - df["timestamp"].iloc[1]).total_seconds() == 8 * 3600
    print("PASS: funding parsing (settleTime units, newest-first order preserved)")


def test_live_pilot_ohlcv():
    """BTC_USDT / Min60, live-fetched 2026-09-17 via the Windows device
    bridge browser pane (3-day window, 72 candles)."""
    raw = json.loads((REPO_ROOT / "data/native/raw/ohlcv/BTC_USDT/pilot_live_2026-09-17.json").read_text())
    df = kline_response_to_df(raw)
    assert len(df) == 72
    assert df["timestamp"].dt.tz is None
    assert df["timestamp"].is_monotonic_increasing
    assert df["timestamp"].duplicated().sum() == 0
    assert not df[["open", "high", "low", "close", "volume"]].isna().any().any()
    gaps = df["timestamp"].diff().dropna()
    assert (gaps == pd.Timedelta(hours=1)).all(), f"non-1h gaps found: {gaps.unique()}"
    assert df["timestamp"].iloc[0] == pd.Timestamp("2026-09-14 08:00:00")
    assert df["timestamp"].iloc[-1] == pd.Timestamp("2026-09-17 07:00:00")
    # real* vs plain fields are close but genuinely distinct on live data —
    # confirms verified fact #2 was a real choice, not a formality.
    assert (df["open"] != df["real_open"]).any()
    print("PASS: live pilot OHLCV (72 rows, no gaps, no dupes, no nulls, tz-naive, chronological)")


def test_live_pilot_funding():
    """BTC_USDT funding history, live-fetched 2026-09-17, page_num=1,
    page_size=5."""
    raw = json.loads((REPO_ROOT / "data/native/raw/funding/BTC_USDT/pilot_live_2026-09-17_page1_size5.json").read_text())
    df = funding_response_to_df(raw)
    assert len(df) == 5
    assert df["timestamp"].dt.tz is None
    assert df["timestamp"].is_monotonic_decreasing  # raw API order: newest-first
    assert df["timestamp"].duplicated().sum() == 0
    assert (df["collect_cycle_hours"] == 8).all()
    spacing = df["timestamp"].diff().dropna().abs().unique()
    assert list(spacing) == [pd.Timedelta(hours=8)]
    print("PASS: live pilot funding (5 rows, newest-first, 8h spacing, no dupes)")


def test_backtester_compatibility():
    """Mirrors the EXACT comparison backtester.py:78 performs
    (`funding_df[funding_df["timestamp"] <= ts]`) using our own live pilot
    data, and confirms the dtype family matches core/data_fetcher.py's
    existing (tz-naive) convention exactly."""
    kline_raw = json.loads((REPO_ROOT / "data/native/raw/ohlcv/BTC_USDT/pilot_live_2026-09-17.json").read_text())
    funding_raw = json.loads((REPO_ROOT / "data/native/raw/funding/BTC_USDT/pilot_live_2026-09-17_page1_size5.json").read_text())
    ohlcv_df = kline_response_to_df(kline_raw)
    funding_df = funding_response_to_df(funding_raw)

    ts = ohlcv_df["timestamp"].iloc[-1]
    prior = funding_df[funding_df["timestamp"] <= ts]  # would raise TypeError if tz mismatch
    assert len(prior) == 5

    data_fetcher_style_ts = pd.to_datetime(1789628400 * 1000, unit="ms")  # no utc=True, as in data_fetcher.py
    assert data_fetcher_style_ts.tz is None
    assert data_fetcher_style_ts == ts
    print("PASS: backtester.py:78-style comparison succeeds; dtype matches data_fetcher.py exactly")


if __name__ == "__main__":
    test_symbol_mapping()
    test_kline_parsing()
    test_funding_parsing()
    test_live_pilot_ohlcv()
    test_live_pilot_funding()
    test_backtester_compatibility()
    print("\nAll tests passed (ccxt-captured fixtures + live 2026-09-17 pilot data).")
