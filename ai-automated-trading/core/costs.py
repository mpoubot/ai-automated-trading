"""
core/costs.py

Cost model: trading fees, slippage, and perpetual-swap funding.

Ported (re-implemented, not imported) from mexc_bot's funding-rate lookup
and fee/slippage constants (backtester.py::_funding_rate_at,
config.py::BACKTEST_TAKER_FEE_PCT / BACKTEST_SLIPPAGE_PCT /
FUNDING_RATE_FALLBACK / FUNDING_INTERVAL_HOURS). Funding is NEVER silently
skipped: `funding_rate_at()` always returns a rate (real if available, the
documented fallback constant otherwise) AND a flag saying which one was
used, so callers can propagate a per-bar "was this funding rate real or
fallback" fact into the result record -- per the Phase 5 requirement that
"where funding history is unavailable, the result must explicitly record
that limitation."
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pandas as pd


@dataclass(frozen=True)
class CostModel:
    fee_pct: float = 0.0005          # taker fee per fill (mexc_bot default: BACKTEST_TAKER_FEE_PCT)
    slippage_pct: float = 0.0005     # assumed slippage per fill (mexc_bot default: BACKTEST_SLIPPAGE_PCT)
    funding_interval_hours: float = 8.0
    funding_rate_fallback: float = 0.0001   # used only when no real rate is available at ts
    funding_df: Optional[pd.DataFrame] = None  # columns: timestamp, funding_rate (may be None/empty)

    def fee(self, price: float, quantity: float) -> float:
        return price * quantity * self.fee_pct

    def slippage(self, price: float, quantity: float) -> float:
        return price * quantity * self.slippage_pct

    def funding_rate_at(self, ts: pd.Timestamp) -> tuple[float, bool]:
        """Returns (rate, is_real). is_real=False means the fallback
        constant was used because no real rate was available at/before
        `ts` -- this flag must be threaded through to the result record,
        never silently dropped."""
        if self.funding_df is None or len(self.funding_df) == 0:
            return self.funding_rate_fallback, False
        prior = self.funding_df[self.funding_df["timestamp"] <= ts]
        if prior.empty:
            return self.funding_rate_fallback, False
        return float(prior.iloc[-1]["funding_rate"]), True

    def funding_pnl(self, side: str, notional: float, ts: pd.Timestamp,
                     intervals: int) -> tuple[float, bool]:
        """Signed funding P&L for holding `notional` on `side` across
        `intervals` funding periods as of `ts`. Positive rate: longs pay,
        shorts receive (matches mexc_bot convention). Returns
        (funding_pnl, is_real_rate)."""
        rate, is_real = self.funding_rate_at(ts)
        sign = -1.0 if side == "long" else 1.0
        return sign * rate * notional * intervals, is_real
