"""
core/dataset.py

Loads the frozen, versioned MEXC dataset
(mexc_bot/data/native_v2_full720day_20260917/) through its manifest, with
sha256 verification against manifest.json before any data is used. This is
the ONLY module in the research core allowed to touch the raw dataset
files; everything downstream consumes the DataFrames this module returns.

Per the Phase 5 command: this module MUST NOT rerun the 720-day
acquisition, and MUST NOT introduce CCXT as an alternate data path. It only
reads the already-frozen, already-validated parquet files and their
manifest -- the MEXC adapter (mexc_bot/core/mexc_native.py) stays untouched
and is not imported here.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pandas as pd

from core.instrument import Instrument


class DatasetIntegrityError(RuntimeError):
    """Raised when a parquet file's sha256 does not match manifest.json, a
    symbol's recorded validation_status is not 'PASS', or a requested
    symbol/file is missing. A research run must never silently proceed on
    unverified or invalid data."""


@dataclass(frozen=True)
class DatasetHandle:
    """Identifies exactly which frozen dataset snapshot a research run
    used -- required for the reproducibility guarantee (result fields
    dataset_id / dataset_version)."""
    root: Path
    acquisition_id: str
    fetched_at_utc: str
    manifest_sha256: str

    @property
    def dataset_id(self) -> str:
        return self.acquisition_id

    @property
    def dataset_version(self) -> str:
        return self.manifest_sha256[:12]


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def open_dataset(root: str | Path) -> DatasetHandle:
    """Reads manifest.json, hashes it, and (when manifest_sha256.txt is
    present alongside it) cross-checks that recorded hash too. Returns a
    DatasetHandle identifying this exact dataset snapshot by its manifest
    hash -- this is what makes a later run able to prove it used the exact
    same dataset."""
    root = Path(root)
    manifest_path = root / "manifest.json"
    if not manifest_path.exists():
        raise DatasetIntegrityError(f"manifest.json not found under {root}")
    manifest = json.loads(manifest_path.read_text())

    computed = _sha256_file(manifest_path)
    manifest_hash_path = root / "manifest_sha256.txt"
    if manifest_hash_path.exists():
        recorded = manifest_hash_path.read_text().strip().split()[0]
        if recorded != computed:
            raise DatasetIntegrityError(
                f"manifest.json sha256 mismatch: file hashes to {computed}, "
                f"manifest_sha256.txt records {recorded}")

    return DatasetHandle(
        root=root,
        acquisition_id=manifest["acquisition_id"],
        fetched_at_utc=manifest["fetched_at_utc"],
        manifest_sha256=computed,
    )


def _load_manifest(handle: DatasetHandle) -> dict:
    return json.loads((handle.root / "manifest.json").read_text())


def list_symbols(handle: DatasetHandle) -> list[str]:
    """Native MEXC symbols present in this dataset snapshot, e.g. ['ADA_USDT', 'AVAX_USDT', ...]."""
    manifest = _load_manifest(handle)
    return sorted(manifest["symbols"].keys())


def load_ohlcv(handle: DatasetHandle, instrument: Instrument) -> pd.DataFrame:
    """Loads one symbol's OHLCV parquet, verifying its sha256 against the
    manifest and its recorded validation_status == 'PASS' before returning
    anything. Raises DatasetIntegrityError on any mismatch or non-PASS
    status."""
    manifest = _load_manifest(handle)
    native = instrument.native_symbol
    if native not in manifest["symbols"]:
        raise DatasetIntegrityError(f"{native} not present in dataset manifest at {handle.root}")
    entry = manifest["symbols"][native]
    if entry.get("validation_status") != "PASS":
        raise DatasetIntegrityError(
            f"{native} validation_status={entry.get('validation_status')!r}, expected 'PASS'")

    path = handle.root / "ohlcv" / f"{native}_1h.parquet"
    if not path.exists():
        raise DatasetIntegrityError(f"OHLCV parquet not found: {path}")
    computed = _sha256_file(path)
    expected = entry["ohlcv_parquet_sha256"]
    if computed != expected:
        raise DatasetIntegrityError(
            f"{native} OHLCV parquet sha256 mismatch: file hashes to {computed}, "
            f"manifest records {expected}")

    df = pd.read_parquet(path)
    df = df.sort_values("timestamp").reset_index(drop=True)
    return df


def load_funding(handle: DatasetHandle, instrument: Instrument) -> tuple[pd.DataFrame, dict]:
    """Loads one symbol's funding-rate-history parquet with the same
    sha256 verification, plus a metadata dict surfacing the known funding
    coverage limitation documented in manifest.json's
    method_notes.funding_retention_finding: 8 of 11 symbols' funding
    history starts only 2025-03-27, not the full 720-day OHLCV window; TIA
    and COTI have non-standard collection cycles. Callers MUST propagate
    this metadata into result records rather than silently treating
    pre-coverage bars as zero funding."""
    manifest = _load_manifest(handle)
    native = instrument.native_symbol
    if native not in manifest["symbols"]:
        raise DatasetIntegrityError(f"{native} not present in dataset manifest at {handle.root}")
    entry = manifest["symbols"][native]

    path = handle.root / "funding" / f"{native}_funding.parquet"
    if not path.exists():
        raise DatasetIntegrityError(f"funding parquet not found: {path}")
    computed = _sha256_file(path)
    expected = entry["funding_parquet_sha256"]
    if computed != expected:
        raise DatasetIntegrityError(
            f"{native} funding parquet sha256 mismatch: file hashes to {computed}, "
            f"manifest records {expected}")

    df = pd.read_parquet(path)
    df = df.sort_values("timestamp").reset_index(drop=True)

    ohlcv_first_ts = pd.Timestamp(entry["ohlcv_first_ts"])
    has_funding = entry.get("funding_rows", 0) > 0
    funding_first_ts: Optional[pd.Timestamp] = pd.Timestamp(entry["funding_first_ts"]) if has_funding else None
    coverage_gap = bool(has_funding and funding_first_ts > ohlcv_first_ts)

    meta = {
        "funding_data_available": has_funding,
        "funding_coverage_start": str(funding_first_ts) if funding_first_ts is not None else None,
        "funding_coverage_gap_vs_ohlcv": coverage_gap,
        "funding_rows": entry.get("funding_rows", 0),
        "note": (
            "Funding history does not cover the full OHLCV window for this symbol "
            "(known MEXC platform retention limitation, see manifest.json "
            "method_notes.funding_retention_finding); bars before "
            f"{funding_first_ts} have no real funding rate available."
            if coverage_gap else
            "Funding history covers the full OHLCV window for this symbol."
            if has_funding else
            "No funding history available for this symbol."
        ),
    }
    return df, meta
