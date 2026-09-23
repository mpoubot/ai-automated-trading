"""
tests/_fixtures.py

Shared synthetic-data helpers for unit tests. Deliberately NOT named
test_*.py so pytest does not collect it as a test module.

Per the Phase 5 command ("use existing validated MEXC data fixtures where
practical, no large optimization"), tests that specifically need to prove
the pipeline works against the REAL frozen dataset (test_dataset_loading.py,
test_dataset_validation.py) read the real dataset via conftest.py's
`dataset_root` fixture. Everything else (strategy interface, signal
generation, backtest mechanics, costs, risk, portfolio, reproducibility)
uses small, fast, deterministic synthetic OHLCV/funding data generated here
-- real-dataset-sized runs are not needed to prove those mechanics are
correct, and keeping unit tests fast matters for a suite that will be run
repeatedly.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def make_synthetic_ohlcv(num_bars: int = 300, start_price: float = 100.0,
                          seed: int = 42, start_ts: str = "2024-01-01",
                          drift: float = 0.0002, vol: float = 0.01) -> pd.DataFrame:
    """Deterministic (seeded) synthetic 1h OHLCV series. Not real market
    data -- used only to exercise pipeline mechanics in unit tests."""
    rng = np.random.default_rng(seed)
    returns = rng.normal(loc=drift, scale=vol, size=num_bars)
    close = start_price * np.cumprod(1 + returns)
    open_ = np.empty(num_bars)
    open_[0] = start_price
    open_[1:] = close[:-1]
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.003, num_bars)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.003, num_bars)))
    volume = rng.integers(100, 10000, num_bars)
    timestamps = pd.date_range(start=start_ts, periods=num_bars, freq="1h")
    return pd.DataFrame({
        "timestamp": timestamps,
        "open": open_, "high": high, "low": low, "close": close,
        "volume": volume,
    })


def make_synthetic_funding(ohlcv: pd.DataFrame, rate: float = 0.0001,
                            interval_hours: int = 8) -> pd.DataFrame:
    """Deterministic synthetic funding-rate history spanning `ohlcv`'s
    timestamp range at a fixed rate and interval."""
    ts = pd.date_range(start=ohlcv["timestamp"].iloc[0], end=ohlcv["timestamp"].iloc[-1],
                        freq=f"{interval_hours}h")
    return pd.DataFrame({
        "timestamp": ts,
        "funding_rate": [rate] * len(ts),
        "collect_cycle_hours": [interval_hours] * len(ts),
    })
