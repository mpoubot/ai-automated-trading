"""
tests/test_dataset_loading.py

Dataset loading against the REAL frozen dataset
(mexc_bot/data/native_v2_full720day_20260917/): manifest opens, sha256
verification passes on real files, all 11 symbols load, and loaded
OHLCV/funding frames have the expected shape and dtypes. Uses the real
dataset (via the `dataset_root` fixture) rather than a synthetic one,
because this is specifically the module responsible for reading it.
"""
import pandas as pd

from core import dataset as ds
from core.instrument import Instrument

EXPECTED_SYMBOLS = {
    "ADA_USDT", "AVAX_USDT", "BNB_USDT", "BTC_USDT", "COTI_USDT", "DOGE_USDT",
    "DOT_USDT", "ETH_USDT", "SUI_USDT", "TIA_USDT", "XRP_USDT",
}


def test_open_dataset_reads_manifest(dataset_root):
    handle = ds.open_dataset(dataset_root)
    assert handle.acquisition_id  # non-empty
    assert handle.dataset_id == handle.acquisition_id
    assert len(handle.dataset_version) == 12
    assert len(handle.manifest_sha256) == 64


def test_list_symbols_matches_expected_eleven(dataset_root):
    handle = ds.open_dataset(dataset_root)
    symbols = ds.list_symbols(handle)
    assert set(symbols) == EXPECTED_SYMBOLS
    assert len(symbols) == 11


def test_load_ohlcv_btc_shape_and_dtypes(dataset_root):
    handle = ds.open_dataset(dataset_root)
    df = ds.load_ohlcv(handle, Instrument.mexc_swap("BTC"))
    assert len(df) > 0
    for col in ("timestamp", "open", "high", "low", "close", "volume"):
        assert col in df.columns
    assert pd.api.types.is_datetime64_any_dtype(df["timestamp"])
    assert df["timestamp"].is_monotonic_increasing


def test_load_ohlcv_all_symbols_load_without_error(dataset_root):
    handle = ds.open_dataset(dataset_root)
    for native in ds.list_symbols(handle):
        inst = Instrument.from_native_mexc(native)
        df = ds.load_ohlcv(handle, inst)
        assert len(df) > 0, f"{native}: empty OHLCV frame"


def test_load_funding_returns_frame_and_metadata(dataset_root):
    handle = ds.open_dataset(dataset_root)
    df, meta = ds.load_funding(handle, Instrument.mexc_swap("BTC"))
    assert "funding_rate" in df.columns
    assert "funding_data_available" in meta
    assert "funding_coverage_gap_vs_ohlcv" in meta
    assert "note" in meta


def test_load_ohlcv_unknown_symbol_raises(dataset_root):
    import pytest
    handle = ds.open_dataset(dataset_root)
    with pytest.raises(ds.DatasetIntegrityError):
        ds.load_ohlcv(handle, Instrument.mexc_swap("NOTREAL"))
