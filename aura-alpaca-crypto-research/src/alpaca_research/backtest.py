from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import pandas as pd

from .strategy import Strategy


@dataclass(frozen=True)
class BacktestConfig:
    initial_equity: float = 100_000.0
    fee_bps: float = 0.0
    slippage_bps: float = 0.0


def run_backtest(
    bars_by_symbol: dict[str, pd.DataFrame],
    strategy: Strategy,
    config: BacktestConfig,
) -> pd.DataFrame:
    """Deterministic signal replay skeleton.

    Execution accounting is intentionally minimal until the exact competition
    strategy and cost model are frozen. The output is a signal ledger, not a
    claim of profitability.
    """
    records: list[dict] = []
    for symbol in sorted(bars_by_symbol):
        bars = bars_by_symbol[symbol]
        if bars.empty:
            continue
        signal = strategy.evaluate(bars, symbol)
        records.append(
            {
                "timestamp": signal.timestamp,
                "symbol": signal.symbol,
                "action": signal.action,
                "score": signal.score,
                "strategy_version": signal.strategy_version,
            }
        )
    return pd.DataFrame(records)
