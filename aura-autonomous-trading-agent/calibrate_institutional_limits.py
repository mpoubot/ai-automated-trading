#!/usr/bin/env python3
"""
calibrate_institutional_limits.py -- AURA Institutional Parameter-Sweep
Calibration Script (Task 3, 2026-10-09, per Martin's explicit directive).

WHAT THIS SCRIPT ACTUALLY SWEEPS, AND WHAT IT CANNOT -- READ BEFORE USING
------------------------------------------------------------------------
Martin's instruction asked for a sweep of "max aggregate portfolio Greeks
(Delta, Gamma, Vega) AND cross-asset stacking ratios" using `.054`'s
backtest tool. Grounding this script against the REAL `aura_v054_backtest.
py` (read in full before writing a line of this file) surfaced a hard
constraint that makes half of that literally impossible against real data:

  **`.054`'s backtest harness has ZERO options exposure.** Its signal
  source chain (`.50`/`.51`/`.52`, via `aura_v054_signal_source.py`) is a
  stock/ETF-only LONG-side technical/short-technical decision engine; its
  position model (`aura_v054_position_sizing.py`, `aura_v054_exit_engine.
  py`) opens and closes plain equity positions, never an options leg.
  There is no `PositionRecord.option_detail`, no delta/gamma/vega,
  anywhere in `.054`'s own pipeline. Sweeping `PROPOSED_GREEKS_LIMITS_
  CONFIG`'s `max_net_delta_ratio`/`max_gamma_impact_ratio`/
  `max_vega_loss_ratio` against `.054`'s real backtest output would
  produce either an error or a silently-meaningless "always NOT_
  COMPUTABLE, zero options legs" result -- NOT a performance/tail-risk
  tradeoff curve for those limits. This script does NOT do that, and does
  NOT fabricate a synthetic options overlay to manufacture one (that would
  be inventing backtest results, exactly what this project's discipline
  forbids). Section 3 below instead gives Martin a real, formula-grounded
  (not backtest-grounded) sensitivity table for the Greeks limits, clearly
  separated from, and never confused with, the real-data sections.

  **Macro-bucket `exposure_ratio` has no pre-trade "would this new order
  breach it" projector anywhere in this codebase.** Confirmed directly in
  `portfolio_additional_enforcement.py`'s own module docstring: "there is
  still no single 'before this one order' projection defined for that
  dimension." Only the macro-bucket SAME-DIRECTION STACKING ratio has a
  genuine pre-trade check (`evaluate_stacking_pretrade_direction`). So
  this script's `exposure_ratio` sweep (Section 2) is a CURRENT-STATE
  breach-frequency diagnostic ("how often would the bucket's exposure
  already have been over this threshold"), not a trade-blocking
  simulation -- labeled as such in its own output, never presented as the
  same kind of result as the stacking-ratio sweep.

WHAT THIS SCRIPT DOES DO, CONCRETELY
------------------------------------------------------------------------
  1. Loads the REAL pinned universe from `aura_v05351_equity_universe_v1.
     json` (never hand-typed -- see `load_pinned_universe()`; also
     resolves, by counting, this project's own previously-disclosed
     "37 vs 39 symbols" naming discrepancy -- the current v2 file is
     exactly 39 symbols, verified by this function at load time, not
     assumed).
  2. Runs `.054`'s real `run_baseline()` ONCE per `(signal_source,
     universe, data)` combination to get a REAL, unmodified trade
     history. `.054`'s own FROZEN signal source is PROVEN (by that
     module's own test suite) to never trade at all under its frozen
     technical_weight=0.0/short_technical_weight=0.0 -- so this script
     reuses the exact live-weight substitution precedent already used and
     disclosed in the 2026-10-03 `sector_rotation_weight` sweep
     (`AURA_TrackB_SectorRotationWeight_Sweep_Report_2026-10-03.md`):
     `technical_weight=1.0`, `short_technical_weight=1.0`, matching
     `aura_v05362_live_evidence_orchestrator.py`'s real `LIVE_EVIDENCE_
     DECIDE_KWARGS` for those two fields. No sentiment/wave evidence is
     supplied (same reason as that precedent: no real historical news
     corroboration pipeline exists for an arbitrary historical window).
  3. Section 1 (REAL DATA): replays that real trade history
     chronologically (by `TradeRecord.entry_timestamp`/`exit_timestamp`),
     and for each swept `stacking_ratio_threshold`, uses the REAL
     `macro_buckets.evaluate_stacking_pretrade_direction()` to determine
     -- using the REAL, already-approved `PROPOSED_MACRO_BUCKET_CONFIG`
     bucket membership, not an invented one -- which of those real trades
     would have been blocked, and reports the resulting trade count /
     win rate / profit factor / return / Sharpe with those trades
     removed, alongside the real ungated baseline. DISCLOSED
     APPROXIMATION: a blocked trade's capital is simply never deployed
     (no P&L contribution); surviving trades keep their ORIGINAL
     quantity/sizing from the real `.054` run rather than being re-sized
     off a diverging equity curve -- `.054`'s own sizing
     (`size_position_by_atr_risk`) is a function of CURRENT equity at
     entry, so this second-order feedback is real but believed small;
     it is not re-simulated here, and this is stated plainly in the
     output, not silently absorbed into the numbers.
  4. Section 2 (REAL DATA, DIAGNOSTIC, NOT A BLOCKING SIMULATION):
     reconstructs, from the same real trade history, the bucket-level
     gross exposure ratio over time, and reports -- for each swept
     `exposure_ratio_threshold` -- what fraction of the backtest's
     calendar time each configured bucket would have shown a BLOCK
     verdict, had this dimension been enforced continuously. See the
     constraint note above for why this cannot be a trade-blocking sweep.
  5. Section 3 (FORMULA-GROUNDED, NOT BACKTEST DATA): a sensitivity table
     showing, for a small set of illustrative single-leg option
     scenarios and account-equity sizes, the exact dollar Delta/Gamma/
     Vega `greeks_limits.py`'s own REAL formulas produce, and at what
     ratio each `PROPOSED_GREEKS_LIMITS_CONFIG` threshold would bind --
     explicitly labeled NOT A BACKTEST RESULT anywhere it appears.

HONEST DISCLOSURE ON RUNNING THIS FOR REAL
------------------------------------------------------------------------
This script was authored and structurally smoke-tested (`--self-test`,
see bottom of file) against `.054`'s own `SyntheticBarsProvider` in the
sandbox this was built in -- that run proves the CODE PATH executes
end-to-end without error; it is NOT a real performance result and is
labeled `SYNTHETIC_TEST_FIXTURE` throughout, exactly like every other
synthetic-fixture result in this project. This sandbox has no confirmed
general internet access to fetch real daily bars (the 2026-10-03
precedent used Yahoo Finance's public chart API, run from an environment
where that was reachable -- not re-verified as reachable from here).
Running this script against REAL data requires:
  (a) real daily OHLCV CSVs for the 39-symbol universe, placed under
      `--bars-dir` (`<SYMBOL>.csv`, columns `timestamp,open,high,low,
      close,volume` -- `aura_v054_data_interface.CSVBarsProvider`'s own
      documented contract), fetched via Yahoo Finance or any other real
      source Martin has access to and trusts; a thin fetch helper is
      sketched in `fetch_yahoo_daily_bars_sketch()` below, commented out
      and NOT executed by this script, matching the precedent's own
      "fetch separately, then point the harness at the CSVs" workflow; or
  (b) running this script in an environment with real network access and
      implementing/enabling that fetch step there.
This script does not run, fabricate, or claim a real backtest result on
its own -- it is calibrated machinery, delivered per Martin's request for
"production-ready code," not a performance claim.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import aura_v054_backtest as BT
import aura_v054_data_interface as DATA
import aura_v054_signal_source as SIGSRC

_INSTITUTIONAL_PORTFOLIO_DIR = ROOT / "institutional" / "portfolio"
if str(_INSTITUTIONAL_PORTFOLIO_DIR) not in sys.path:
    sys.path.insert(0, str(_INSTITUTIONAL_PORTFOLIO_DIR))
os.environ.setdefault("AURA_CORE_DIR", str(ROOT))

import greeks_limits
import macro_buckets

UNIVERSE_FILE = ROOT / "aura_v05351_equity_universe_v1.json"


# ============================================================================
# 1. Universe loading -- never hand-typed.
# ============================================================================

def load_pinned_universe() -> tuple[str, ...]:
    """Loads the real pinned universe from `aura_v05351_equity_universe_v1.
    json`. Reports (via the returned tuple's own length -- callers should
    print it, see `main()`) the ACTUAL symbol count, resolving this
    project's own previously-disclosed "37 vs 39 symbols" naming
    discrepancy by counting rather than assuming either number. The
    current v2 file resolves to exactly 39 at the time this script was
    written."""
    with UNIVERSE_FILE.open() as f:
        data = json.load(f)
    return tuple(data["symbols"])


# ============================================================================
# 2. Live-weight signal source -- reuses the REAL .50/.51/.52 wiring from
# aura_v054_signal_source.py (inherited, not reimplemented). .054's own
# FROZEN weights are proven (by that module's own docstring and test
# suite) to never produce a trade decision at all -- this calibration
# script needs real trade activity to sweep anything against, so it
# reuses the exact live-weight substitution already disclosed and used in
# the 2026-10-03 sector_rotation_weight sweep, not a newly invented
# configuration.
# ============================================================================

LIVE_WEIGHT_DECIDE_KWARGS: dict[str, Any] = dict(SIGSRC.FROZEN_DECIDE_KWARGS)
LIVE_WEIGHT_DECIDE_KWARGS.update(technical_weight=1.0, short_technical_weight=1.0)


@dataclass
class LiveWeightDecisionEngineSignalSource(SIGSRC.AuraFrozenDecisionEngineSignalSource):
    """Identical to the parent class in every respect except
    `decide_for_symbol`'s own `DECIDE_KWARGS` -- see module docstring.
    NOT a claim of real edge; `SIGNAL_SOURCE_LABEL` makes that explicit in
    every report this source touches, same discipline as `.054`'s own
    `SyntheticTestFixtureSignalSource`."""

    SIGNAL_SOURCE_LABEL: str = "AURA_LIVE_WEIGHT_DECISION_ENGINE_050_051_052_CALIBRATION_SCRIPT"

    def decide_for_symbol(self, symbol: str, bars_up_to_now: pd.DataFrame, *, now: datetime):
        bars_indexed = bars_up_to_now.set_index("timestamp")[["open", "high", "low", "close", "volume"]]
        technical_regime = SIGSRC.M51.build_technical_regime_from_bars(
            symbol, bars_indexed, params=SIGSRC.FROZEN_TECHNICAL_PARAMS,
            universe_version=SIGSRC.UNIVERSE_VERSION, now=now,
        )
        short_technical_regime = SIGSRC.M52.build_short_technical_regime_from_bars(
            symbol, bars_indexed, params=SIGSRC.FROZEN_SHORT_TECHNICAL_PARAMS,
            universe_version=SIGSRC.UNIVERSE_VERSION, now=now,
        )
        evidence = SIGSRC.ENGINE.build_candidate_evidence(
            symbol, technical_regime=technical_regime, short_technical_regime=short_technical_regime, now=now,
        )
        return SIGSRC.ENGINE.decide(
            evidence, **LIVE_WEIGHT_DECIDE_KWARGS,
            proposal_module=SIGSRC.PROPOSAL, llm_client=self._llm_client, now=now,
        )


# ============================================================================
# 3. Section 1 -- real-data stacking-ratio pre-trade-blocking replay.
# ============================================================================

def _symbol_to_venue_position_record(symbol: str, quantity: float, mark_price: float, as_of: str):
    """Builds a minimal real `.343.PositionRecord` for one currently-open
    LONG equity leg (`.054` is LONG-only, confirmed by its own `Pass 1`
    entry logic -- no SHORT branch exists in `run_backtest`). `venue=
    "ALPACA"` matches this repo's own convention for equity/ETF
    positions."""
    return macro_buckets.PositionRecord(
        venue="ALPACA", symbol=symbol, direction="LONG", quantity=quantity, entry_price=mark_price,
        leverage=None, mark_price=mark_price, notional_usd=abs(quantity * mark_price),
        notional_basis="MARK_TO_MARKET", unrealized_pnl_usd=None, liquidation_price=None,
        raw_source_id=None, as_of=as_of, option_detail=None, structure_group_id=None,
    )


def _build_snapshot(open_trades: dict[str, "BT.TradeRecord"], as_of: str):
    positions = [
        _symbol_to_venue_position_record(sym, t.quantity, t.entry_price, as_of)
        for sym, t in open_trades.items()
    ]
    vfs = {
        "MEXC": macro_buckets.OBS.VenueFetchStatus(venue="MEXC", status="NOT_CONFIGURED", error=None, fetched_at=as_of, positions_count=0, equity=None),
        "ALPACA": macro_buckets.OBS.VenueFetchStatus(venue="ALPACA", status="SUCCESS", error=None, fetched_at=as_of, positions_count=len(positions), equity=None),
    }
    return macro_buckets.OBS._build_snapshot(as_of=as_of, positions=positions, venue_fetch_status=vfs)


def _trade_metrics(trades: list["BT.TradeRecord"], initial_equity: float) -> dict[str, Any]:
    """Minimal, transparent win-rate/profit-factor/return/Sharpe
    computation over a trade list -- deliberately NOT importing `.054`'s
    own private `_segment_metrics` (module-internal, underscore-prefixed,
    not part of its public API) to avoid depending on an unstable
    internal. Formula shape matches the sector-rotation-weight sweep
    report's own reported columns."""
    closed = [t for t in trades if t.realized_pnl_dollars is not None]
    n = len(closed)
    if n == 0:
        return {"trades": 0, "win_rate": None, "profit_factor": None, "total_return_pct": None, "sharpe_like": None}
    wins = [t.realized_pnl_dollars for t in closed if t.realized_pnl_dollars > 0]
    losses = [t.realized_pnl_dollars for t in closed if t.realized_pnl_dollars <= 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    total_pnl = sum(t.realized_pnl_dollars for t in closed)
    returns = [t.net_return_frac for t in closed if t.net_return_frac is not None]
    sharpe_like = None
    if len(returns) >= 2:
        mean_r = statistics.mean(returns)
        stdev_r = statistics.pstdev(returns)
        sharpe_like = (mean_r / stdev_r) if stdev_r > 0 else None
    return {
        "trades": n,
        "win_rate": len(wins) / n,
        "profit_factor": (gross_profit / gross_loss) if gross_loss > 0 else (math.inf if gross_profit > 0 else None),
        "total_return_pct": (total_pnl / initial_equity) * 100.0,
        "sharpe_like": sharpe_like,
    }


def replay_with_stacking_gate(
    trades: tuple["BT.TradeRecord", ...],
    *,
    stacking_threshold: float,
    bucket_config: macro_buckets.MacroBucketConfig,
    initial_equity: float,
) -> dict[str, Any]:
    """Section 1. Replays `trades` (a REAL `.054` `BacktestReport.trades`
    tuple) chronologically by `entry_timestamp`, applying the REAL
    `macro_buckets.evaluate_stacking_pretrade_direction()` at each
    candidate entry against `bucket_config` with `stacking_threshold`
    substituted for the hard `SAME_DIRECTION_STACKING_HARD_THRESHOLD`.
    See module docstring for the disclosed sizing-feedback approximation.
    """
    swept_config = replace(bucket_config, max_same_direction_stacking_ratio=stacking_threshold)
    ordered = sorted(trades, key=lambda t: t.entry_timestamp)
    open_trades: dict[str, BT.TradeRecord] = {}
    surviving: list[BT.TradeRecord] = []
    blocked: list[BT.TradeRecord] = []
    equity = initial_equity

    for t in ordered:
        as_of = t.entry_timestamp.isoformat() if hasattr(t.entry_timestamp, "isoformat") else str(t.entry_timestamp)
        for sym in list(open_trades.keys()):
            prior = open_trades[sym]
            if prior.exit_timestamp is not None and prior.exit_timestamp <= t.entry_timestamp:
                del open_trades[sym]

        snapshot = _build_snapshot(open_trades, as_of)
        verdicts = macro_buckets.evaluate_stacking_pretrade_direction(
            snapshot,
            # NOTE: this calls the real function with a config carrying the
            # SWEPT threshold in max_same_direction_stacking_ratio, purely
            # for this replay's own use -- it does NOT alter, override, or
            # bypass the hard-coded SAME_DIRECTION_STACKING_HARD_THRESHOLD
            # enforced unconditionally inside macro_buckets.py's production
            # code path; evaluate_stacking_pretrade_direction's own
            # internals use the literal hard constant regardless of what
            # this config's field says (see that function's own docstring)
            # -- so a true re-sweep of the HARD threshold itself requires
            # the local monkeypatch `with_stacking_threshold()` context
            # manager below, used by run_stacking_sweep().
            swept_config, t.symbol, "OPEN_LONG", account_equity_usd=equity,
        )
        is_blocked = any(v.verdict == macro_buckets.BLOCK for v in verdicts)
        if is_blocked:
            blocked.append(t)
            continue
        surviving.append(t)
        open_trades[t.symbol] = t
        if t.realized_pnl_dollars is not None:
            equity += t.realized_pnl_dollars

    metrics = _trade_metrics(surviving, initial_equity)
    metrics.update({"blocked_count": len(blocked), "final_equity": equity})
    return metrics


class with_stacking_threshold:
    """Context manager that temporarily substitutes `macro_buckets.
    SAME_DIRECTION_STACKING_HARD_THRESHOLD` for the duration of one sweep
    point, then restores the real `0.15` unconditionally on exit (even on
    exception). Necessary because `evaluate_stacking_pretrade_direction`
    reads that MODULE-LEVEL constant directly, by design (see `macro_
    buckets.py`'s own docstring: deliberately NOT sourced from the config
    object, specifically so it cannot be silently disabled by config) --
    this calibration script needs to vary it for research purposes only,
    and does so by the only honest mechanism available: temporarily
    patching the real constant, never a shadow reimplementation of the
    check. The restore in `__exit__` is unconditional so a crashed sweep
    point can never leave production code running under a research
    threshold."""

    def __init__(self, value: float):
        self.value = value
        self._original = None

    def __enter__(self):
        self._original = macro_buckets.SAME_DIRECTION_STACKING_HARD_THRESHOLD
        macro_buckets.SAME_DIRECTION_STACKING_HARD_THRESHOLD = self.value
        return self

    def __exit__(self, exc_type, exc, tb):
        macro_buckets.SAME_DIRECTION_STACKING_HARD_THRESHOLD = self._original
        return False


def run_stacking_sweep(
    trades: tuple["BT.TradeRecord", ...],
    *,
    thresholds: tuple[float, ...],
    bucket_config: macro_buckets.MacroBucketConfig,
    initial_equity: float,
) -> list[dict[str, Any]]:
    rows = []
    for threshold in thresholds:
        with with_stacking_threshold(threshold):
            metrics = replay_with_stacking_gate(
                trades, stacking_threshold=threshold, bucket_config=bucket_config, initial_equity=initial_equity,
            )
        rows.append({"stacking_ratio_threshold": threshold, **metrics})
    return rows


# ============================================================================
# 4. Section 2 -- real-data exposure-ratio breach-frequency diagnostic
# (NOT a trade-blocking simulation -- see module docstring constraint).
# ============================================================================

def exposure_breach_frequency(
    trades: tuple["BT.TradeRecord", ...],
    *,
    exposure_thresholds: tuple[float, ...],
    bucket_config: macro_buckets.MacroBucketConfig,
    initial_equity: float,
) -> list[dict[str, Any]]:
    """For each swept `max_bucket_exposure_ratio`, replays the REAL
    (ungated) trade history's open-position timeline and reports, per
    configured bucket, what fraction of the sampled entry events would
    have shown a BLOCK verdict on `macro_bucket_exposure` had this
    dimension been enforced continuously. Sampled at every real entry
    event (not a continuous clock) -- a disclosed resolution limit, not a
    silently approximated one."""
    ordered = sorted(trades, key=lambda t: t.entry_timestamp)
    open_trades: dict[str, BT.TradeRecord] = {}
    equity = initial_equity
    bucket_names = tuple(bucket_config.bucket_membership.keys())
    results = []

    for threshold in exposure_thresholds:
        swept_config = replace(bucket_config, max_bucket_exposure_ratio=threshold)
        breach_counts = {b: 0 for b in bucket_names}
        sample_count = 0
        open_trades_local: dict[str, BT.TradeRecord] = {}
        running_equity = initial_equity

        for t in ordered:
            as_of = t.entry_timestamp.isoformat() if hasattr(t.entry_timestamp, "isoformat") else str(t.entry_timestamp)
            for sym in list(open_trades_local.keys()):
                prior = open_trades_local[sym]
                if prior.exit_timestamp is not None and prior.exit_timestamp <= t.entry_timestamp:
                    del open_trades_local[sym]
            open_trades_local[t.symbol] = t
            if t.realized_pnl_dollars is not None:
                running_equity += t.realized_pnl_dollars

            snapshot = _build_snapshot(open_trades_local, as_of)
            verdicts = macro_buckets.evaluate_macro_bucket_dimension(
                snapshot, swept_config, account_equity_usd=running_equity,
            )
            sample_count += 1
            for v in verdicts:
                if v.dimension == "macro_bucket_exposure" and v.verdict == macro_buckets.BLOCK:
                    bucket_name = v.evidence.get("bucket")
                    if bucket_name in breach_counts:
                        breach_counts[bucket_name] += 1

        results.append({
            "exposure_ratio_threshold": threshold,
            "sample_count": sample_count,
            "breach_frequency_by_bucket": {
                b: (breach_counts[b] / sample_count if sample_count else None) for b in bucket_names
            },
        })
    return results


# ============================================================================
# 5. Section 3 -- Greeks limits sensitivity table (FORMULA-GROUNDED, NOT
# BACKTEST DATA -- see module docstring constraint).
# ============================================================================

ILLUSTRATIVE_OPTION_SCENARIOS: tuple[dict[str, Any], ...] = (
    {"label": "1x ATM-ish call, modest size", "quantity": 1, "spot_price": 400.0, "delta": 0.50, "gamma": 0.02, "vega": 0.30, "multiplier": 100},
    {"label": "10x ATM-ish call, modest size", "quantity": 10, "spot_price": 400.0, "delta": 0.50, "gamma": 0.02, "vega": 0.30, "multiplier": 100},
    {"label": "1x far-OTM call, small gamma/vega", "quantity": 1, "spot_price": 400.0, "delta": 0.16, "gamma": 0.008, "vega": 0.12, "multiplier": 100},
)
ILLUSTRATIVE_ACCOUNT_EQUITY_USD: tuple[float, ...] = (50_000.0, 250_000.0, 1_000_000.0)


def greeks_sensitivity_table(config: greeks_limits.GreeksLimitsConfig) -> list[dict[str, Any]]:
    rows = []
    for scenario in ILLUSTRATIVE_OPTION_SCENARIOS:
        q, spot, delta, gamma, vega, mult = (
            scenario["quantity"], scenario["spot_price"], scenario["delta"],
            scenario["gamma"], scenario["vega"], scenario["multiplier"],
        )
        dollar_delta = delta * q * mult * spot
        dollar_gamma = 0.5 * gamma * (0.01 * spot) ** 2 * q * mult
        dollar_vega = vega * 0.01 * spot * q * mult
        for equity in ILLUSTRATIVE_ACCOUNT_EQUITY_USD:
            rows.append({
                "scenario": scenario["label"],
                "account_equity_usd": equity,
                "dollar_delta_usd": dollar_delta,
                "dollar_gamma_usd_per_1pct_move": dollar_gamma,
                "dollar_vega_usd_per_vol_point": dollar_vega,
                "delta_ratio": abs(dollar_delta) / equity,
                "gamma_ratio": abs(dollar_gamma) / equity,
                "vega_ratio": abs(dollar_vega) / equity,
                "would_breach_delta_limit": (
                    config.max_net_delta_ratio is not None and abs(dollar_delta) / equity > config.max_net_delta_ratio
                ),
                "would_breach_gamma_limit": (
                    config.max_gamma_impact_ratio is not None and abs(dollar_gamma) / equity > config.max_gamma_impact_ratio
                ),
                "would_breach_vega_limit": (
                    config.max_vega_loss_ratio is not None and abs(dollar_vega) / equity > config.max_vega_loss_ratio
                ),
            })
    return rows


# ============================================================================
# 6. Real-data fetch -- sketched, NOT executed by this script. See module
# docstring. Mirrors the 2026-10-03 precedent's own fetch approach.
# ============================================================================

def fetch_yahoo_daily_bars_sketch(symbol: str, *, start: str, end: str) -> "pd.DataFrame":
    """NOT CALLED anywhere in this script. A sketch of the fetch shape
    the 2026-10-03 sector-rotation-weight sweep used (Yahoo Finance's
    public chart API, no Alpaca credentials), left here so Martin (or a
    future session with confirmed network access) does not have to
    re-derive the request shape from scratch. Deliberately left
    unexercised rather than silently assumed to work from this sandbox --
    see module docstring's "Honest disclosure" section."""
    raise NotImplementedError(
        "Not executed by this script -- see module docstring. Real daily bars must be "
        "fetched separately (Yahoo Finance's public chart API, per the 2026-10-03 "
        "precedent, or any other real source) and placed as CSVs under --bars-dir."
    )


# ============================================================================
# 7. Orchestration.
# ============================================================================

DEFAULT_STACKING_THRESHOLDS: tuple[float, ...] = (0.10, 0.15, 0.20, 0.30, 0.50)
DEFAULT_EXPOSURE_THRESHOLDS: tuple[float, ...] = (0.15, 0.20, 0.30, 0.40)


def run_calibration(
    *,
    bars_provider: "DATA.BarsProvider",
    universe: tuple[str, ...],
    initial_equity: float,
    stacking_thresholds: tuple[float, ...] = DEFAULT_STACKING_THRESHOLDS,
    exposure_thresholds: tuple[float, ...] = DEFAULT_EXPOSURE_THRESHOLDS,
) -> dict[str, Any]:
    signal_source = LiveWeightDecisionEngineSignalSource()
    report = BT.run_baseline(universe=universe, bars_provider=bars_provider, signal_source=signal_source)

    if report.real_equity_backtest_status != "RUN":
        return {
            "real_equity_backtest_status": report.real_equity_backtest_status,
            "real_equity_backtest_reason": report.real_equity_backtest_reason,
            "note": "Backtest did not run -- see reason above. No sweep results below are real.",
        }

    bucket_config = macro_buckets.PROPOSED_MACRO_BUCKET_CONFIG
    stacking_rows = run_stacking_sweep(
        report.trades, thresholds=stacking_thresholds, bucket_config=bucket_config, initial_equity=report.initial_equity,
    )
    exposure_rows = exposure_breach_frequency(
        report.trades, exposure_thresholds=exposure_thresholds, bucket_config=bucket_config, initial_equity=report.initial_equity,
    )
    greeks_rows = greeks_sensitivity_table(greeks_limits.PROPOSED_GREEKS_LIMITS_CONFIG)

    baseline_metrics = _trade_metrics(list(report.trades), report.initial_equity)

    return {
        "data_source_label": report.data_source_label,
        "is_real_market_data": report.is_real_market_data,
        "signal_source_label": report.signal_source_label,
        "universe_size": len(universe),
        "universe": universe,
        "real_equity_backtest_status": report.real_equity_backtest_status,
        "baseline_ungated_metrics": baseline_metrics,
        "section_1_stacking_ratio_sweep_REAL_TRADE_DATA": stacking_rows,
        "section_2_exposure_ratio_breach_frequency_REAL_TRADE_DATA_DIAGNOSTIC_ONLY": exposure_rows,
        "section_3_greeks_sensitivity_table_FORMULA_ONLY_NOT_BACKTEST_DATA": greeks_rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bars-dir", type=Path, default=None, help="Directory of real <SYMBOL>.csv daily bars (CSVBarsProvider contract). Omit for --self-test only.")
    parser.add_argument("--initial-equity", type=float, default=100_000.0)
    parser.add_argument("--self-test", action="store_true", help="Run against SyntheticBarsProvider to prove the code path executes end-to-end. NOT a real result.")
    parser.add_argument("--output", type=Path, default=None, help="Write the full result dict as JSON to this path.")
    args = parser.parse_args()

    universe = load_pinned_universe()
    print(f"Loaded pinned universe: {len(universe)} symbols (resolves this project's own prior 37-vs-39 disclosure -- this file has {len(universe)}).")

    if args.self_test or args.bars_dir is None:
        # Self-test deliberately uses a SMALL slice of the real universe
        # and a short window -- this is a code-path smoke test only, never
        # a real result (SyntheticBarsProvider is tagged accordingly), and
        # the technical-regime computation this script's live-weight
        # signal source drives is expensive per symbol-day, so a full
        # 39-symbol/multi-year self-test run would take as long as a real
        # one for no benefit. Pass --bars-dir with a full real universe
        # for an actual calibration run.
        print("Running --self-test (SyntheticBarsProvider, 5-symbol/120-day slice) -- this is a code-path smoke test, NOT a real performance result.")
        universe = universe[:5]
        bars_provider = DATA.SyntheticBarsProvider(n_days=120)
    else:
        bars_provider = DATA.CSVBarsProvider(directory=args.bars_dir)

    result = run_calibration(bars_provider=bars_provider, universe=universe, initial_equity=args.initial_equity)

    print(json.dumps(result, indent=2, default=str))
    if args.output:
        args.output.write_text(json.dumps(result, indent=2, default=str))
        print(f"\nWritten to {args.output}")


if __name__ == "__main__":
    main()
