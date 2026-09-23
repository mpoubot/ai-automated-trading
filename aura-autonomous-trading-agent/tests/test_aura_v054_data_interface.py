"""AURA v0.5.4 tests -- pluggable data-ingestion interface."""

from __future__ import annotations

import pandas as pd
import pytest

import aura_v054_data_interface as DATA


def test_synthetic_provider_produces_valid_schema():
    provider = DATA.SyntheticBarsProvider(n_days=50)
    df = provider.get_daily_bars("AAPL")
    assert list(df.columns) == list(DATA.BARS_SCHEMA)
    assert len(df) == 50
    assert df["timestamp"].is_monotonic_increasing
    DATA.validate_bars_frame(df, symbol="AAPL")  # must not raise


def test_synthetic_provider_labels_itself_non_real():
    provider = DATA.SyntheticBarsProvider()
    assert provider.DATA_SOURCE_LABEL == "SYNTHETIC_TEST_FIXTURE"
    assert provider.IS_REAL_MARKET_DATA is False


def test_synthetic_provider_deterministic_across_calls():
    p1 = DATA.SyntheticBarsProvider(n_days=40)
    p2 = DATA.SyntheticBarsProvider(n_days=40)
    df1 = p1.get_daily_bars("AAPL")
    df2 = p2.get_daily_bars("AAPL")
    pd.testing.assert_frame_equal(df1, df2)


def test_unavailable_real_equity_data_source_raises_with_documented_reason():
    src = DATA.UnavailableRealEquityDataSource()
    with pytest.raises(DATA.RealEquityDataNotAvailableError) as exc_info:
        src.get_daily_bars("AAPL")
    assert exc_info.value.reason == DATA.REAL_EQUITY_BACKTEST_NOT_RUN_REASON


def test_csv_provider_reads_real_data_path(tmp_path):
    csv_path = tmp_path / "AAPL.csv"
    csv_path.write_text(
        "timestamp,open,high,low,close,volume\n"
        "2024-01-02,100,101,99,100.5,1000000\n"
        "2024-01-03,100.5,102,100,101.5,1100000\n"
    )
    provider = DATA.CSVBarsProvider(directory=tmp_path)
    df = provider.get_daily_bars("AAPL")
    assert provider.IS_REAL_MARKET_DATA is True
    assert len(df) == 2
    assert df["close"].iloc[-1] == pytest.approx(101.5)


def test_csv_provider_missing_file_raises_not_available():
    provider = DATA.CSVBarsProvider(directory="/nonexistent/path/xyz")
    with pytest.raises(DATA.RealEquityDataNotAvailableError):
        provider.get_daily_bars("ZZZZ")


def test_validate_bars_frame_rejects_duplicate_timestamps():
    df = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(["2024-01-02", "2024-01-02"], utc=True),
            "open": [1.0, 1.0], "high": [2.0, 2.0], "low": [0.5, 0.5], "close": [1.5, 1.5], "volume": [100, 100],
        }
    )
    with pytest.raises(DATA.DataInterfaceError):
        DATA.validate_bars_frame(df, symbol="X")


def test_validate_bars_frame_rejects_inconsistent_ohlc():
    df = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(["2024-01-02"], utc=True),
            "open": [1.0], "high": [0.5], "low": [2.0], "close": [1.5], "volume": [100],  # high < low
        }
    )
    with pytest.raises(DATA.DataInterfaceError):
        DATA.validate_bars_frame(df, symbol="X")
