"""
core/portfolio.py

PortfolioAllocator: explicit, fixed, reproducible per-symbol capital
reservation -- the structural fix for the shared-equity-pool bug found
during this engagement's earlier work on aura_v054_backtest.py (AURA's
frozen v0.5.4 backtest engine): BTC_USDT got 0 trades in an 11-symbol
combined run sharing one $100,000 equity pool, vs. 69 trades when run
alone, because symbols shared one shrinking pool and earlier symbols in
iteration order could starve later ones.

CORRECTED ATTRIBUTION (PHASE5_INDEPENDENT_AUDIT_2026-09-22.md finding #2):
an earlier version of this docstring, and other new-project documentation,
incorrectly attributed this finding to mexc_bot/run_broad_backtest.py.
Direct, fresh re-inspection of that file confirms it already implements
independent per-symbol equity/circuit-breaker state (a fresh equity_tracker
dict and CircuitBreaker instance per symbol inside its loop) and documents
this design choice explicitly in its own docstring -- it was never the
source of the shared-pool bug. The true source was aura_v054_backtest.py,
per mexc_bot_V054_MEXC_BASELINE_PHASE2_REPORT_2026-09-19.md. This
PortfolioAllocator remains valid, deliberate capital-conservation design
regardless of which engine the original finding came from -- only the
historical justification text was wrong, and only in this new project;
mexc_bot itself was never modified.

Design: total research capital is split into N equal, fixed reservations
(one per symbol) up front. Each symbol's backtest runs against its OWN
reservation only -- no symbol's sizing or entry eligibility can be affected
by another symbol's trades, drawdown, or iteration order. This is
deliberately simple (equal-weight, fixed at run start), matching the Phase 5
instruction: "can be simple, no sophisticated portfolio optimization yet." A
later phase can replace equal-weight with a real allocator behind this same
interface.

OUT OF SCOPE, DOCUMENTED DELIBERATELY (PHASE5_INDEPENDENT_AUDIT_2026-09-22.md
finding #5): fixing each symbol's capital independently bounds any ONE
symbol's maximum loss, but this allocator does NOT cap how many symbols may
hold an open position AT THE SAME TIME across the whole portfolio -- there
is no concurrent-position or aggregate-notional limit here. See
docs/README.md ("Known scope gap: no cross-symbol concurrent-position cap")
for the full rationale. This must be resolved before real multi-symbol or
live execution; it is not something this class already handles.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CapitalReservation:
    instrument_id: str
    allocated_capital: float


class PortfolioAllocator:
    """Fixed, equal-weight, non-shrinking capital allocation across
    symbols. Reservations are computed once, at construction, from the
    FULL symbol list and total capital -- never recomputed mid-run as
    trades close, so no symbol's allocation can be affected by another
    symbol's results or by the order symbols are processed in."""

    def __init__(self, total_capital: float, instrument_ids: list[str]):
        if total_capital <= 0:
            raise ValueError(f"total_capital must be > 0, got {total_capital}")
        if not instrument_ids:
            raise ValueError("instrument_ids must be non-empty")
        if len(set(instrument_ids)) != len(instrument_ids):
            raise ValueError(f"instrument_ids must be unique, got {instrument_ids}")

        self.total_capital = total_capital
        self.instrument_ids = list(instrument_ids)
        per_symbol = total_capital / len(instrument_ids)
        self._reservations = {
            iid: CapitalReservation(instrument_id=iid, allocated_capital=per_symbol)
            for iid in self.instrument_ids
        }

    def reservation_for(self, instrument_id: str) -> CapitalReservation:
        if instrument_id not in self._reservations:
            raise KeyError(
                f"{instrument_id!r} was not in the allocator's fixed symbol list "
                f"{self.instrument_ids} -- reservations are fixed at construction "
                f"and cannot be created ad hoc mid-run.")
        return self._reservations[instrument_id]

    def all_reservations(self) -> list[CapitalReservation]:
        return [self._reservations[iid] for iid in self.instrument_ids]
