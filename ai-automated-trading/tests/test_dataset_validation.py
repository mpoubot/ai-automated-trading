"""
tests/test_dataset_validation.py

Dataset validation/integrity guarantees: sha256 mismatches and non-PASS
validation_status are refused rather than silently used, the manifest's own
hash is cross-checked when manifest_sha256.txt is present, and the known
funding-history coverage gap (documented in manifest.json's
method_notes.funding_retention_finding: 8 of 11 symbols' funding starts
2025-03-27, not the full 720-day window; TIA/COTI have non-standard
collection cycles) is surfaced through load_funding()'s metadata rather than
silently ignored.
"""
import json

import pytest

from core import dataset as ds
from core.instrument import Instrument


def test_all_symbols_report_validation_status_pass(dataset_root):
    """The frozen dataset's own manifest must show every symbol PASS --
    this is a fact about the dataset, asserted here so a future re-freeze
    that regresses validation is caught immediately."""
    manifest = json.loads((dataset_root / "manifest.json").read_text())
    for native, entry in manifest["symbols"].items():
        assert entry["validation_status"] == "PASS", f"{native}: {entry['validation_status']}"
        assert entry["gap_count"] == 0, f"{native}: gap_count={entry['gap_count']}"
        assert entry["duplicate_timestamps"] == 0, f"{native}: duplicate_timestamps"


def test_load_ohlcv_rejects_sha256_mismatch(dataset_root, tmp_path):
    """Corrupt a copy of one OHLCV parquet file and confirm load_ohlcv()
    refuses it rather than silently loading corrupted data."""
    handle = ds.open_dataset(dataset_root)
    src = dataset_root / "ohlcv" / "BTC_USDT_1h.parquet"

    # Build a tampered dataset directory: copy the manifest + all files,
    # then corrupt just the BTC ohlcv parquet.
    tampered_root = tmp_path / "tampered_dataset"
    (tampered_root / "ohlcv").mkdir(parents=True)
    (tampered_root / "funding").mkdir(parents=True)
    (tampered_root / "manifest.json").write_bytes((dataset_root / "manifest.json").read_bytes())
    for f in (dataset_root / "ohlcv").glob("*.parquet"):
        (tampered_root / "ohlcv" / f.name).write_bytes(f.read_bytes())
    for f in (dataset_root / "funding").glob("*.parquet"):
        (tampered_root / "funding" / f.name).write_bytes(f.read_bytes())

    corrupted = tampered_root / "ohlcv" / "BTC_USDT_1h.parquet"
    original_bytes = corrupted.read_bytes()
    corrupted.write_bytes(original_bytes[:-1] + bytes([original_bytes[-1] ^ 0xFF]))

    tampered_handle = ds.open_dataset(tampered_root)
    with pytest.raises(ds.DatasetIntegrityError):
        ds.load_ohlcv(tampered_handle, Instrument.mexc_swap("BTC"))


def test_load_ohlcv_rejects_non_pass_status(dataset_root, tmp_path):
    """A manifest claiming a non-PASS status for a symbol must block
    loading that symbol, even if the parquet file itself is byte-identical
    to the real, validated one."""
    tampered_root = tmp_path / "bad_status_dataset"
    (tampered_root / "ohlcv").mkdir(parents=True)
    (tampered_root / "funding").mkdir(parents=True)
    manifest = json.loads((dataset_root / "manifest.json").read_text())
    manifest["symbols"]["BTC_USDT"]["validation_status"] = "FAIL"
    (tampered_root / "manifest.json").write_text(json.dumps(manifest))
    (tampered_root / "ohlcv" / "BTC_USDT_1h.parquet").write_bytes(
        (dataset_root / "ohlcv" / "BTC_USDT_1h.parquet").read_bytes())

    handle = ds.open_dataset(tampered_root)
    with pytest.raises(ds.DatasetIntegrityError):
        ds.load_ohlcv(handle, Instrument.mexc_swap("BTC"))


def test_funding_coverage_gap_flagged_for_known_short_history_symbols(dataset_root):
    """BTC_USDT's OHLCV window starts 2024-09-27 but its funding history
    (per the manifest's documented platform-retention limitation) starts
    2025-03-27 -- load_funding() must flag this gap, not silently treat
    early bars as zero funding."""
    handle = ds.open_dataset(dataset_root)
    _df, meta = ds.load_funding(handle, Instrument.mexc_swap("BTC"))
    assert meta["funding_data_available"] is True
    assert meta["funding_coverage_gap_vs_ohlcv"] is True
    assert "retention" in meta["note"].lower() or "limitation" in meta["note"].lower()


def test_funding_metadata_present_for_every_symbol(dataset_root):
    handle = ds.open_dataset(dataset_root)
    for native in ds.list_symbols(handle):
        inst = Instrument.from_native_mexc(native)
        _df, meta = ds.load_funding(handle, inst)
        assert isinstance(meta["funding_data_available"], bool)
        assert isinstance(meta["funding_coverage_gap_vs_ohlcv"], bool)
