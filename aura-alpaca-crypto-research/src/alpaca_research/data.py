from __future__ import annotations

from datetime import datetime
from typing import Sequence

import pandas as pd
from alpaca.data.historical import CryptoHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit


def parse_timeframe(value: str) -> TimeFrame:
    value = value.strip().lower()
    if value.endswith("hour") or value.endswith("h"):
        n = int(value.replace("hour", "").replace("h", "") or "1")
        return TimeFrame(n, TimeFrameUnit.Hour)
    if value.endswith("min") or value.endswith("t"):
        n = int(value.replace("min", "").replace("t", "") or "1")
        return TimeFrame(n, TimeFrameUnit.Minute)
    if value in {"1d", "1day", "day"}:
        return TimeFrame.Day
    raise ValueError(f"Unsupported timeframe: {value}")


def fetch_crypto_bars(
    symbols: Sequence[str],
    start: datetime,
    end: datetime,
    timeframe: str = "1Hour",
) -> pd.DataFrame:
    """Fetch historical Alpaca crypto bars for deterministic research replay."""
    client = CryptoHistoricalDataClient()
    request = CryptoBarsRequest(
        symbol_or_symbols=list(symbols),
        timeframe=parse_timeframe(timeframe),
        start=start,
        end=end,
    )
    bars = client.get_crypto_bars(request)
    frame = bars.df.copy()
    if frame.empty:
        return frame
    return frame.sort_index()
