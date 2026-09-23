#!/usr/bin/env python3
"""
AURA v0.5.4 -- Baseline backtest runner

Wires together: `aura_v054_data_interface.BarsProvider` (pluggable data
source) -> `aura_v054_atr.wilder_atr_from_bars` -> a pluggable
`aura_v054_signal_source.SignalSource` -> `aura_v054_exit_engine.
simulate_atr_trailing_trade` -> `aura_v054_position_sizing.
size_position_by_atr_risk` -> `aura_v054_portfolio_risk` (limits +
planned-risk ledger) -> `aura_v054_cost_model`.

REAL_EQUITY_BACKTEST STATUS (Martin's explicit requirement, 2026-09-15)
-------------------------------------------------------------------------
This sandbox has zero real S&P 100 daily OHLCV data available (confirmed:
no general internet access, no real Alpaca credentials, no pinned
dataset -- see `aura_v054_data_interface.py`'s module docstring). Calling
`run_backtest(...)` with `bars_provider=UnavailableRealEquityDataSource()`
(the default) raises `RealEquityDataNotAvailableError` immediately and
`run_baseline(...)` (the top-level entry point) catches this and returns
a `BacktestReport` with:
    real_equity_backtest_status = "NOT_RUN"
    real_equity_backtest_reason = "No real S&P 100 daily OHLCV dataset
                                    available in current environment."
and NO performance numbers -- never a fabricated or substituted result.

Supplying a real dataset later requires ONLY constructing a
`CSVBarsProvider` (or a future live-Alpaca-backed provider satisfying
`BarsProvider`) and passing it as `bars_provider=` -- no change to the
strategy, risk, exit, or sizing modules, and no change to the frozen
parameters below.

SYNTHETIC VALIDATION MODE
--------------------------
Passing `bars_provider=SyntheticBarsProvider(...)` runs the full pipeline
against deterministic synthetic data purely to prove the wiring is
correct. Every such report is tagged
`data_source_label="SYNTHETIC_TEST_FIXTURE"` and
`is_real_market_data=False`; `run_baseline`'s caller (and this session's
final report) must never present numbers from this mode as real trading
performance.

Multi-symbol simplification (disclosed, not hidden)
-----------------------------------------------------
All symbols in one backtest run are assumed to share an identical
trading-day calendar (true of `SyntheticBarsProvider`'s output by
construction; a real multi-symbol equities dataset can have ragged
per-symbol calendars -- e.g. IPOs, delistings, halts -- which this
baseline does not yet reconcile). Portfolio state (open positions,
gross exposure, portfolio risk) is tracked using each symbol's own
integer bar index as a shared clock. This is flagged as a KNOWN
LIMITATION for the real-data run, not silently assumed away.

Equity marks to market only at each trade's close (not on a continuous
daily basis across all open positions) -- a documented simplification
of this baseline's equity-curve/drawdown/Sharpe computation, not a
claim of full daily mark-to-market accounting.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import pandas as pd

import aura_v054_atr as ATR
import aura_v054_cost_model as COST
import aura_v054_data_interface as DATA
import aura_v054_exit_engine as EXIT
import aura_v054_portfolio_risk as RISK
import aura_v054_position_sizing as SIZING

VERSION = "AURA v0.5.4"

DEFAULT_INITIAL_EQUITY = 100_000.0
HOLDOUT_FRACTION = 0.20  # trailing 20% of available chronological observations, computed mechanically
MIN_WARMUP_BARS = 60  # >= .51's min_bars_required (55) + a small margin, so the signal source is never asked to score on too little history


class BacktestError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class TradeRecord:
    symbol: str
    entry_bar_index: int
    entry_timestamp: pd.Timestamp
    entry_price: float
    quantity: int
    exit_bar_index: int | None
    exit_timestamp: pd.Timestamp | None
    exit_reason: str | None
    gross_return_frac: float | None
    net_return_frac: float | None
    realized_pnl_dollars: float | None
    planned_risk_dollars: float
    market_value: float
    period: str  # "RESEARCH" or "HOLDOUT" -- by the ENTRY bar's segment


@dataclass(frozen=True, slots=True)
class SegmentMetrics:
    period: str
    n_trades: int
    n_wins: int
    n_losses: int
    win_rate: float | None
    profit_factor: float | None
    avg_trade_return_frac: float | None
    total_return_frac: float | None
    cagr: float | None
    max_drawdown_frac: float | None
    sharpe_trade_level: float | None
    sortino_trade_level: float | None
    avg_holding_period_bars: float | None
    turnover_frac: float | None
    total_transaction_costs_dollars: float
    largest_win_frac: float | None
    largest_loss_frac: float | None
    max_consecutive_losses: int
    n_signals_total: int
    n_rejected_by_risk_gate: int
    n_rejected_by_exposure_limit: int
    n_rejected_by_position_limit: int


@dataclass
class BacktestReport:
    real_equity_backtest_status: str  # "NOT_RUN" | "RUN"
    real_equity_backtest_reason: str | None
    data_source_label: str
    is_real_market_data: bool
    signal_source_label: str
    holdout_boundary_bar_index: int | None
    holdout_boundary_timestamp: str | None
    universe: tuple[str, ...]
    initial_equity: float
    final_equity: float | None
    trades: list[TradeRecord] = field(default_factory=list)
    research_metrics: SegmentMetrics | None = None
    holdout_metrics: SegmentMetrics | None = None
    notes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    """Every value here is either CONFIRMED (universe/version), or
    EXPLICITLY_SELECTED / BASELINE_RESEARCH_HYPOTHESIS per Martin's .54
    spec (2026-09-15) -- none is optimized, none is claimed best."""

    trail_atr_mult: float = EXIT.TRAIL_ATR_MULT
    take_profit_pct: float | None = EXIT.TAKE_PROFIT_PCT
    max_hold_bars: int = EXIT.MAX_HOLD_BARS
    risk_fraction_per_trade: float = SIZING.RISK_FRACTION_PER_TRADE
    cost_model: COST.CostModel = field(default_factory=COST.FlatRoundTripCostModel)
    portfolio_limits: RISK.PortfolioRiskLimits = field(default_factory=RISK.PortfolioRiskLimits)
    initial_equity: float = DEFAULT_INITIAL_EQUITY
    atr_period: int = ATR.ATR_PERIOD
    holdout_fraction: float = HOLDOUT_FRACTION
    warmup_bars: int = MIN_WARMUP_BARS


def compute_holdout_boundary(n_bars_by_symbol: dict[str, int], *, holdout_fraction: float) -> int:
    """Mechanical, chronological holdout boundary -- trailing
    `holdout_fraction` of the ACTUAL dataset used (never a hardcoded
    prior-estimate date), computed from the shortest available series
    (so every symbol has a genuine, fully-populated holdout segment).
    Never randomly sampled; always the last N observations.
    """
    if not n_bars_by_symbol:
        raise BacktestError("EMPTY_UNIVERSE:cannot compute a holdout boundary with zero symbols")
    if not (0.0 < holdout_fraction < 1.0):
        raise BacktestError("INVALID_HOLDOUT_FRACTION:must be in (0, 1)")
    min_n = min(n_bars_by_symbol.values())
    boundary = int(math.floor(min_n * (1.0 - holdout_fraction)))
    return boundary


def _max_drawdown(equity_curve: list[float]) -> float | None:
    if len(equity_curve) < 2:
        return None
    peak = equity_curve[0]
    max_dd = 0.0
    for e in equity_curve:
        peak = max(peak, e)
        dd = (e - peak) / peak if peak > 0 else 0.0
        max_dd = min(max_dd, dd)
    return abs(max_dd)


def _sharpe_like(returns: list[float]) -> float | None:
    if len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    std = math.sqrt(var)
    if std == 0:
        return None
    return mean / std


def _sortino_like(returns: list[float]) -> float | None:
    if len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    downside = [min(0.0, r) for r in returns]
    dvar = sum(d ** 2 for d in downside) / (len(returns) - 1)
    dstd = math.sqrt(dvar)
    if dstd == 0:
        return None
    return mean / dstd


def _segment_metrics(period: str, trades: list[TradeRecord], initial_equity: float, signal_counts: dict[str, int]) -> SegmentMetrics:
    closed = [t for t in trades if t.net_return_frac is not None]
    returns = [t.net_return_frac for t in closed]
    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r <= 0]

    equity_curve = [initial_equity]
    running = initial_equity
    for t in sorted(closed, key=lambda t: (t.exit_bar_index if t.exit_bar_index is not None else 0)):
        running += t.realized_pnl_dollars or 0.0
        equity_curve.append(running)

    total_return_frac = (equity_curve[-1] - initial_equity) / initial_equity if len(equity_curve) > 1 else (0.0 if closed == [] else None)

    max_consec_losses = 0
    cur = 0
    for t in sorted(closed, key=lambda t: (t.exit_bar_index if t.exit_bar_index is not None else 0)):
        if (t.net_return_frac or 0.0) <= 0:
            cur += 1
            max_consec_losses = max(max_consec_losses, cur)
        else:
            cur = 0

    gross_profit = sum(r for r in returns if r > 0)
    gross_loss = -sum(r for r in returns if r <= 0)
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (None if gross_profit == 0 else float("inf"))

    return SegmentMetrics(
        period=period,
        n_trades=len(closed),
        n_wins=len(wins),
        n_losses=len(losses),
        win_rate=(len(wins) / len(closed)) if closed else None,
        profit_factor=profit_factor,
        avg_trade_return_frac=(sum(returns) / len(returns)) if returns else None,
        total_return_frac=total_return_frac,
        cagr=None,  # requires a real calendar span; left None for synthetic/pipeline-validation runs (documented limitation)
        max_drawdown_frac=_max_drawdown(equity_curve),
        sharpe_trade_level=_sharpe_like(returns),
        sortino_trade_level=_sortino_like(returns),
        avg_holding_period_bars=(sum(t.exit_bar_index - t.entry_bar_index for t in closed if t.exit_bar_index is not None) / len(closed)) if closed else None,
        turnover_frac=(sum(t.market_value for t in closed) / initial_equity) if closed else None,
        total_transaction_costs_dollars=sum((t.market_value * (t.gross_return_frac - t.net_return_frac if t.gross_return_frac is not None and t.net_return_frac is not None else 0.0)) for t in closed),
        largest_win_frac=max(returns) if returns else None,
        largest_loss_frac=min(returns) if returns else None,
        max_consecutive_losses=max_consec_losses,
        n_signals_total=signal_counts.get("total", 0),
        n_rejected_by_risk_gate=signal_counts.get("rejected_risk_gate", 0),
        n_rejected_by_exposure_limit=signal_counts.get("rejected_exposure", 0),
        n_rejected_by_position_limit=signal_counts.get("rejected_position_limit", 0),
    )


def _decision_is_long(decision: Any) -> bool:
    outcome = decision.outcome if hasattr(decision, "outcome") else decision.get("outcome")
    return outcome == "DECIDE_LONG"


def run_backtest(
    *,
    universe: tuple[str, ...],
    bars_provider: DATA.BarsProvider,
    signal_source: Any,
    config: BacktestConfig = BacktestConfig(),
) -> BacktestReport:
    """Runs the full .54 pipeline against whatever `bars_provider` /
    `signal_source` are supplied. Raises `RealEquityDataNotAvailableError`
    if `bars_provider` cannot supply bars (propagated to the caller,
    `run_baseline`, which converts it into the REAL_EQUITY_BACKTEST=
    NOT_RUN report status)."""
    bars_by_symbol: dict[str, pd.DataFrame] = {}
    atr_by_symbol: dict[str, pd.Series] = {}
    for symbol in universe:
        df = bars_provider.get_daily_bars(symbol)
        DATA.validate_bars_frame(df, symbol=symbol)
        bars_by_symbol[symbol] = df
        atr_result = ATR.wilder_atr_from_bars(df, period=config.atr_period)
        atr_by_symbol[symbol] = atr_result.atr

    n_bars_by_symbol = {s: len(df) for s, df in bars_by_symbol.items()}
    holdout_boundary = compute_holdout_boundary(n_bars_by_symbol, holdout_fraction=config.holdout_fraction)
    boundary_ts = bars_by_symbol[universe[0]]["timestamp"].iloc[holdout_boundary] if holdout_boundary < n_bars_by_symbol[universe[0]] else None

    # ---- Pass 1: scan each symbol day-by-day (no look-ahead: decisions
    # only ever see bars up to and including the current index) for
    # candidate LONG entries, skipping while a same-symbol position is
    # still open (mirrors aura_exit_policy_backtest.py's own documented
    # "SEQUENTIAL, SINGLE-POSITION ACCOUNT" discipline). ----
    candidate_events: list[tuple[int, str]] = []  # (entry_bar_index, symbol), later merged chronologically
    signal_counts = {"total": 0, "rejected_risk_gate": 0, "rejected_exposure": 0, "rejected_position_limit": 0}
    precomputed_exits: dict[tuple[str, int], EXIT.ATRTrailingExitResult] = {}

    for symbol in universe:
        df = bars_by_symbol[symbol]
        atr = atr_by_symbol[symbol]
        highs = df["high"].tolist()
        lows = df["low"].tolist()
        closes = df["close"].tolist()
        atr_list = atr.tolist()
        n = len(df)

        i = config.warmup_bars
        while i < n:
            bars_up_to_now = df.iloc[: i + 1]
            now_ts = df["timestamp"].iloc[i].to_pydatetime()
            atr_i = atr_list[i]
            if atr_i is None or (isinstance(atr_i, float) and math.isnan(atr_i)):
                i += 1
                continue

            decision = signal_source.decide_for_symbol(symbol, bars_up_to_now, now=now_ts)
            signal_counts["total"] += 1

            if _decision_is_long(decision):
                entry_price = float(closes[i])
                try:
                    initial_distance = EXIT.initial_stop_distance_long(entry_price, atr_i, trail_atr_mult=config.trail_atr_mult)
                except EXIT.ExitEngineError:
                    i += 1
                    continue
                exit_result = EXIT.simulate_atr_trailing_trade(
                    highs=highs,
                    lows=lows,
                    closes=closes,
                    atr_values=atr_list,
                    entry_idx=i,
                    entry_price=entry_price,
                    direction="LONG",
                    trail_atr_mult=config.trail_atr_mult,
                    take_profit_pct=config.take_profit_pct,
                    max_hold_bars=config.max_hold_bars,
                    cost_pct=config.cost_model.cost_pct if hasattr(config.cost_model, "cost_pct") else 0.0,
                )
                precomputed_exits[(symbol, i)] = exit_result
                candidate_events.append((i, symbol))
                # Skip forward past this simulated trade's own lifetime
                # before scanning for the NEXT entry on this symbol --
                # never stacks a second simulated position on one symbol.
                if exit_result.exit_bar_index is not None:
                    i = exit_result.exit_bar_index + 1
                else:
                    i += 1
                continue
            i += 1

    # ---- Pass 2: process candidate entries in global chronological
    # order, applying portfolio-level risk gating and a shared,
    # realized-P&L-tracked equity curve. ----
    candidate_events.sort(key=lambda e: (e[0], e[1]))
    ledger = RISK.PlannedRiskLedger()
    equity = config.initial_equity
    trades: list[TradeRecord] = []
    open_until: dict[str, int] = {}  # symbol -> exit_bar_index of its currently tracked open position

    def _release_closed_positions(up_to_bar_index: int) -> None:
        # Realized P&L is already credited to `equity` at the point a
        # trade's outcome is known (see below) -- this only removes the
        # position from the ledger once its exit bar has passed, so
        # later portfolio-risk checks stop counting its exposure/risk.
        for sym in list(open_until.keys()):
            if open_until[sym] < up_to_bar_index:
                ledger.close_position(sym)
                del open_until[sym]

    for entry_idx, symbol in candidate_events:
        _release_closed_positions(entry_idx)

        exit_result = precomputed_exits[(symbol, entry_idx)]
        entry_price = exit_result.entry_price
        initial_distance = exit_result.initial_stop_distance

        if symbol in open_until:
            signal_counts["rejected_position_limit"] += 1
            continue

        sizing = SIZING.size_position_by_atr_risk(
            equity=equity,
            entry_price=entry_price,
            planned_stop_distance=initial_distance,
            risk_fraction_per_trade=config.risk_fraction_per_trade,
        )
        if sizing.quantity <= 0:
            signal_counts["rejected_risk_gate"] += 1
            continue

        verdict = RISK.evaluate_candidate_position(
            ledger=ledger,
            limits=config.portfolio_limits,
            equity=equity,
            candidate_market_value=sizing.market_value,
            candidate_planned_risk_dollars=sizing.planned_risk_dollars,
        )
        if not verdict.allowed:
            if any(r.startswith("MAX_POSITIONS_EXCEEDED") for r in verdict.reasons):
                signal_counts["rejected_position_limit"] += 1
            elif any(r.startswith("MAX_GROSS_EXPOSURE_EXCEEDED") for r in verdict.reasons):
                signal_counts["rejected_exposure"] += 1
            else:
                signal_counts["rejected_risk_gate"] += 1
            continue

        ledger.open_position(
            RISK.PlannedPositionRisk(
                symbol=symbol,
                entry_bar_index=entry_idx,
                quantity=sizing.quantity,
                entry_price=entry_price,
                planned_stop_distance=initial_distance,
                planned_risk_dollars=sizing.planned_risk_dollars,
                market_value=sizing.market_value,
            )
        )
        if exit_result.exit_bar_index is not None:
            open_until[symbol] = exit_result.exit_bar_index

        realized_pnl = None
        if exit_result.net_return_frac is not None:
            realized_pnl = sizing.market_value * exit_result.net_return_frac
            equity += realized_pnl

        period = "RESEARCH" if entry_idx < holdout_boundary else "HOLDOUT"
        df = bars_by_symbol[symbol]
        trades.append(
            TradeRecord(
                symbol=symbol,
                entry_bar_index=entry_idx,
                entry_timestamp=df["timestamp"].iloc[entry_idx],
                entry_price=entry_price,
                quantity=sizing.quantity,
                exit_bar_index=exit_result.exit_bar_index,
                exit_timestamp=df["timestamp"].iloc[exit_result.exit_bar_index] if exit_result.exit_bar_index is not None else None,
                exit_reason=exit_result.exit_reason,
                gross_return_frac=exit_result.gross_return_frac,
                net_return_frac=exit_result.net_return_frac,
                realized_pnl_dollars=realized_pnl,
                planned_risk_dollars=sizing.planned_risk_dollars,
                market_value=sizing.market_value,
                period=period,
            )
        )
        if exit_result.exit_bar_index is None:
            # NO_DATA_AFTER_ENTRY -- never actually resolved; remove from ledger immediately, no P&L.
            ledger.close_position(symbol)
            del open_until[symbol]

    research_trades = [t for t in trades if t.period == "RESEARCH"]
    holdout_trades = [t for t in trades if t.period == "HOLDOUT"]

    return BacktestReport(
        real_equity_backtest_status="RUN",
        real_equity_backtest_reason=None,
        data_source_label=getattr(bars_provider, "DATA_SOURCE_LABEL", "UNKNOWN"),
        is_real_market_data=getattr(bars_provider, "IS_REAL_MARKET_DATA", False),
        signal_source_label=getattr(signal_source, "SIGNAL_SOURCE_LABEL", "UNKNOWN"),
        holdout_boundary_bar_index=holdout_boundary,
        holdout_boundary_timestamp=str(boundary_ts) if boundary_ts is not None else None,
        universe=universe,
        initial_equity=config.initial_equity,
        final_equity=equity,
        trades=trades,
        research_metrics=_segment_metrics("RESEARCH", research_trades, config.initial_equity, signal_counts),
        holdout_metrics=_segment_metrics("HOLDOUT", holdout_trades, config.initial_equity, signal_counts),
        notes=(
            "Multi-symbol calendar assumed aligned across the universe (see module docstring).",
            "Equity marks to market only at each trade's close, not continuously daily.",
            "CAGR intentionally left None -- requires a real calendar span, not meaningful for a synthetic/pipeline-validation run.",
        ),
    )


def run_baseline(
    *,
    universe: tuple[str, ...],
    bars_provider: DATA.BarsProvider,
    signal_source: Any,
    config: BacktestConfig = BacktestConfig(),
) -> BacktestReport:
    """Top-level entry point. Converts `RealEquityDataNotAvailableError`
    into an honest `REAL_EQUITY_BACKTEST=NOT_RUN` report rather than
    letting the caller crash or, worse, silently falling back to a
    different data source."""
    try:
        return run_backtest(universe=universe, bars_provider=bars_provider, signal_source=signal_source, config=config)
    except DATA.RealEquityDataNotAvailableError as exc:
        return BacktestReport(
            real_equity_backtest_status="NOT_RUN",
            real_equity_backtest_reason=exc.reason,
            data_source_label=getattr(bars_provider, "DATA_SOURCE_LABEL", "UNKNOWN"),
            is_real_market_data=getattr(bars_provider, "IS_REAL_MARKET_DATA", False),
            signal_source_label=getattr(signal_source, "SIGNAL_SOURCE_LABEL", "UNKNOWN"),
            holdout_boundary_bar_index=None,
            holdout_boundary_timestamp=None,
            universe=universe,
            initial_equity=config.initial_equity,
            final_equity=None,
            trades=[],
            research_metrics=None,
            holdout_metrics=None,
            notes=("REAL_EQUITY_BACKTEST=NOT_RUN: " + exc.reason,),
        )
