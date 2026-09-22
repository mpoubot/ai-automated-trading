"""
core/results.py

ResultRecord: the machine-readable result every research run must produce
per instrument, covering the Phase 5 command's explicit minimum field list
(run ID, strategy ID/version, dataset ID/version, instrument, timeframe,
start/end, number of bars, trades, wins, losses, gross P&L, fees, funding,
net P&L, return, max drawdown, exposure, profit factor, expectancy,
validation status).

reconstruct_trades()/compute_drawdown()/compute_metrics() are ported
(re-implemented, not imported) from mexc_bot/core/trade_metrics.py, adapted
to this package's own event-log schema (core/backtest_engine.py), which
keeps gross PnL, fees, and funding as separate running totals per trade
instead of merging them into one pnl number -- needed so gross_pnl / fees /
funding / net_pnl can each be reported individually, as the command
requires.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

import pandas as pd


@dataclass
class ResultRecord:
    run_id: str
    strategy_id: str
    strategy_version: str
    is_research_fixture: bool
    dataset_id: str
    dataset_version: str
    instrument: str          # canonical_id, e.g. "MEXC:BTC_USDT:swap"
    timeframe: str
    start: str
    end: str
    num_bars: int
    num_trades: int
    wins: int
    losses: int
    gross_pnl: float
    fees: float
    funding: float
    net_pnl: float
    return_pct: float
    max_drawdown: float
    max_drawdown_pct: float
    exposure_pct: float
    profit_factor: float
    expectancy: float
    validation_status: str
    config_hash: str
    starting_capital: float
    funding_data_available: bool
    funding_coverage_gap_vs_ohlcv: bool
    # REMEDIATION (PHASE5_INDEPENDENT_AUDIT_2026-09-22.md finding #3): the
    # run's final RiskState (see core/risk.py) and the reason string that
    # produced it were computed (BacktestRunResult.final_risk_state /
    # final_risk_reason, core/backtest_engine.py) but never persisted here --
    # an observability gap. A reader of a result JSON previously had no way
    # to tell whether a symbol ended ACTIVE, RESTRICTED, or HALTED, or why,
    # without re-running the backtest. Populated from
    # BacktestRunResult.final_risk_state.value / final_risk_reason.
    final_risk_state: str = "active"
    state_reason: str = "ok"
    notes: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def reconstruct_trades(event_log: list[dict]) -> pd.DataFrame:
    """Groups a flat event log (entry/partial_tp/exit_stop/exit_time_stop/
    exit_final/funding events, in chronological order) into one row per
    full trade, with gross_pnl, fees, funding_pnl kept as separate running
    totals (unlike mexc_bot/core/trade_metrics.py, which merges fee into
    pnl)."""
    trades = []
    current: Optional[dict] = None

    for row in event_log:
        if row["type"] == "entry":
            if current is not None:
                trades.append(current)
            current = {
                "instrument": row["instrument"], "entry_time": row["timestamp"],
                "side": row.get("side"), "gross_pnl": 0.0, "fees": 0.0,
                "funding_pnl": 0.0, "num_events": 0, "candles_held": 0,
            }
            continue
        if current is None:
            continue
        if row["type"] == "funding":
            current["funding_pnl"] += row.get("pnl", 0.0)
            continue
        current["gross_pnl"] += row.get("gross_pnl", 0.0)
        current["fees"] += row.get("fee", 0.0)
        current["num_events"] += 1
        if row["type"] in ("exit_stop", "exit_time_stop", "exit_final"):
            current["exit_type"] = row["type"]
            current["exit_time"] = row["timestamp"]
            current["candles_held"] = row.get("candles_held", 0)

    if current is not None:
        trades.append(current)

    df = pd.DataFrame(trades)
    if not df.empty:
        df["net_pnl"] = df["gross_pnl"] - df["fees"] + df["funding_pnl"]
    return df


def compute_drawdown(trades_df: pd.DataFrame, starting_capital: float) -> dict:
    """
    Max drawdown measured on the EQUITY CURVE (starting_capital + cumulative
    net PnL), both in currency and as a percent of the running equity peak.

    NOTE on a defect found and fixed here during Phase 5 post-run
    validation: an earlier version of this function (faithfully porting
    mexc_bot/core/trade_metrics.py::compute_drawdown's formula) measured
    drawdown-% against the running peak of CUMULATIVE TRADE PNL alone, not
    against equity. When cumulative PnL hovers near zero (small allocated
    capital, thin edge or no edge -- exactly the fixture's situation), that
    peak-of-PnL denominator can be a few cents, producing a nonsensical
    percentage (observed: 99.9972% on a run where currency drawdown was a
    small fraction of starting capital). Using the equity curve
    (starting_capital + cumulative PnL) as both the peak and the
    denominator fixes this and matches how core/risk.py's
    SimpleResearchRiskModel itself measures drawdown (against equity, not
    against PnL alone) -- so a reported max_drawdown_pct near this run's
    risk_state=HALTED is now directly comparable to the configured
    max_drawdown_limit_pct threshold, as it should be.
    """
    if trades_df.empty:
        return {"max_drawdown": 0.0, "max_drawdown_pct": 0.0}
    df = trades_df.sort_values("entry_time")
    equity_curve = starting_capital + df["net_pnl"].cumsum()
    running_peak = equity_curve.cummax()
    drawdown = equity_curve - running_peak
    max_dd = float(drawdown.min()) if len(drawdown) else 0.0
    peak_at_trough = float(running_peak.iloc[drawdown.idxmin()]) if len(drawdown) else 0.0
    dd_pct = (abs(max_dd) / peak_at_trough * 100) if peak_at_trough > 0 else 0.0
    return {"max_drawdown": round(max_dd, 6), "max_drawdown_pct": round(dd_pct, 4)}


def compute_metrics(event_log: list[dict], starting_capital: float, num_bars: int) -> dict:
    trades_df = reconstruct_trades(event_log)

    if trades_df.empty:
        return {
            "num_trades": 0, "wins": 0, "losses": 0, "gross_pnl": 0.0, "fees": 0.0,
            "funding": 0.0, "net_pnl": 0.0, "return_pct": 0.0, "max_drawdown": 0.0,
            "max_drawdown_pct": 0.0, "exposure_pct": 0.0, "profit_factor": 0.0,
            "expectancy": 0.0,
        }

    wins = trades_df[trades_df["net_pnl"] > 0]
    losses = trades_df[trades_df["net_pnl"] <= 0]
    gross_profit = wins["net_pnl"].sum() if not wins.empty else 0.0
    gross_loss = abs(losses["net_pnl"].sum()) if not losses.empty else 0.0
    net_pnl = trades_df["net_pnl"].sum()
    dd = compute_drawdown(trades_df, starting_capital)
    total_held = trades_df["candles_held"].fillna(0).sum() if "candles_held" in trades_df.columns else 0

    return {
        "num_trades": int(len(trades_df)),
        "wins": int(len(wins)),
        "losses": int(len(losses)),
        "gross_pnl": round(float(trades_df["gross_pnl"].sum()), 6),
        "fees": round(float(trades_df["fees"].sum()), 6),
        "funding": round(float(trades_df["funding_pnl"].sum()), 6),
        "net_pnl": round(float(net_pnl), 6),
        "return_pct": round(float(net_pnl) / starting_capital * 100, 4) if starting_capital else 0.0,
        "max_drawdown": dd["max_drawdown"],
        "max_drawdown_pct": dd["max_drawdown_pct"],
        "exposure_pct": round(float(total_held) / num_bars * 100, 4) if num_bars else 0.0,
        "profit_factor": round(float(gross_profit / gross_loss), 4) if gross_loss > 0 else float("inf"),
        "expectancy": round(float(net_pnl) / len(trades_df), 6),
    }


def config_hash(payload: dict) -> str:
    """Deterministic hash of a run's strategy/dataset/config identity --
    used to verify that two separate runs (same run_id space, different
    wall-clock run_id) are actually reproductions of the same
    configuration. Distinct from run_id, which is wall-clock/human
    distinguishing only, never part of the reproducibility check."""
    canonical = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def write_result(result: ResultRecord, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result.to_dict(), indent=2, sort_keys=True))
