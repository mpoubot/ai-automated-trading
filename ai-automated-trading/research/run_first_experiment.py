"""
research/run_first_experiment.py

The ONE controlled research experiment required by the Phase 5 command:
frozen dataset, PipelineValidationFixture (a synthetic, explicitly-labelled
fixture -- NOT a trading strategy), deterministic config, no parameter
sweep, no optimization. Proves
DATA -> CANONICAL INSTRUMENT -> STRATEGY -> SIGNAL -> BACKTEST ->
COST/FUNDING -> RISK -> PORTFOLIO -> REPRODUCIBLE RESULT
works end-to-end against the real, frozen, sha256-verified 720-day MEXC
dataset (mexc_bot/data/native_v2_full720day_20260917/).

Run:
    python3 research/run_first_experiment.py

Dataset location: by default this script looks for the frozen dataset as a
sibling of this repo (../mexc_bot/data/native_v2_full720day_20260917),
which is where it lives on Martin's machine once this folder sits next to
mexc_bot/. Override with the AURA_DATASET_ROOT environment variable (used
in the cloud sandbox during development/testing, where the dataset was
staged to a different path).

Every result is labelled clearly as pipeline-validation output, never as
evidence of a trading edge, per Martin's explicit instruction.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core import dataset as ds
from core.backtest_engine import ExitConfig, run_backtest
from core.costs import CostModel
from core.indicators import atr as compute_atr
from core.instrument import Instrument
from core.portfolio import PortfolioAllocator
from core.results import ResultRecord, compute_metrics, config_hash, write_result
from core.risk import RiskConfig
from strategies.pipeline_fixture import PipelineValidationFixture

# --- Deterministic configuration (no sweep, no optimization) ---------------
DATASET_ROOT = Path(os.environ.get(
    "AURA_DATASET_ROOT",
    str(REPO_ROOT.parent / "mexc_bot" / "data" / "native_v2_full720day_20260917"),
))
TOTAL_CAPITAL = 500.0        # mirrors mexc_bot/config.py BACKTEST_STARTING_EQUITY
TIMEFRAME = "1h"
FIXTURE_INTERVAL_BARS = 48   # deterministic; see strategies/pipeline_fixture.py docstring
RISK_CFG = RiskConfig()      # defaults mirror mexc_bot/config.py risk constants
EXIT_CFG = ExitConfig()      # defaults mirror mexc_bot/config.py exit constants
FEE_PCT = 0.0005
SLIPPAGE_PCT = 0.0005
FUNDING_INTERVAL_HOURS = 8.0
FUNDING_RATE_FALLBACK = 0.0001

RESULTS_DIR = REPO_ROOT / "results"


def main() -> dict:
    run_id = "run_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    handle = ds.open_dataset(DATASET_ROOT)
    symbols = ds.list_symbols(handle)
    instruments = [Instrument.from_native_mexc(sym) for sym in symbols]
    canonical_ids = [inst.canonical_id for inst in instruments]

    allocator = PortfolioAllocator(total_capital=TOTAL_CAPITAL, instrument_ids=canonical_ids)

    config_payload = {
        "dataset_id": handle.dataset_id, "dataset_version": handle.dataset_version,
        "strategy_id": PipelineValidationFixture.strategy_id,
        "strategy_version": PipelineValidationFixture.version,
        "fixture_interval_bars": FIXTURE_INTERVAL_BARS,
        "total_capital": TOTAL_CAPITAL, "risk_cfg": RISK_CFG.__dict__,
        "exit_cfg": EXIT_CFG.__dict__, "fee_pct": FEE_PCT, "slippage_pct": SLIPPAGE_PCT,
        "funding_interval_hours": FUNDING_INTERVAL_HOURS,
        "funding_rate_fallback": FUNDING_RATE_FALLBACK, "symbols": sorted(canonical_ids),
    }
    run_config_hash = config_hash(config_payload)

    results: list[ResultRecord] = []
    print(f"=== Phase 5 controlled research run: {run_id} (config_hash={run_config_hash}) ===")
    print(f"Dataset: {handle.dataset_id} @ {handle.root}")
    print(f"Symbols ({len(canonical_ids)}): {canonical_ids}")
    print(f"Strategy: {PipelineValidationFixture.strategy_id} v{PipelineValidationFixture.version} "
          f"({'FIXTURE -- NOT A TRADING STRATEGY' if PipelineValidationFixture.is_research_fixture else 'REAL STRATEGY'})")

    for instrument in instruments:
        native = instrument.native_symbol
        ohlcv = ds.load_ohlcv(handle, instrument)
        funding_df, funding_meta = ds.load_funding(handle, instrument)
        ohlcv = ohlcv.copy()
        ohlcv["atr"] = compute_atr(ohlcv, period=14)

        cost_model = CostModel(
            fee_pct=FEE_PCT, slippage_pct=SLIPPAGE_PCT,
            funding_interval_hours=FUNDING_INTERVAL_HOURS,
            funding_rate_fallback=FUNDING_RATE_FALLBACK, funding_df=funding_df,
        )
        reservation = allocator.reservation_for(instrument.canonical_id)
        strategy = PipelineValidationFixture(interval_bars=FIXTURE_INTERVAL_BARS)

        run_result = run_backtest(
            strategy=strategy, instrument=instrument, ohlcv=ohlcv,
            cost_model=cost_model, risk_cfg=RISK_CFG,
            starting_capital=reservation.allocated_capital, exit_config=EXIT_CFG,
        )

        metrics = compute_metrics(run_result.event_log, reservation.allocated_capital,
                                   run_result.bars_processed)

        record = ResultRecord(
            run_id=run_id, strategy_id=strategy.strategy_id, strategy_version=strategy.version,
            is_research_fixture=strategy.is_research_fixture,
            dataset_id=handle.dataset_id, dataset_version=handle.dataset_version,
            instrument=instrument.canonical_id, timeframe=TIMEFRAME,
            start=str(ohlcv["timestamp"].iloc[0]), end=str(ohlcv["timestamp"].iloc[-1]),
            num_bars=len(ohlcv), num_trades=metrics["num_trades"], wins=metrics["wins"],
            losses=metrics["losses"], gross_pnl=metrics["gross_pnl"], fees=metrics["fees"],
            funding=metrics["funding"], net_pnl=metrics["net_pnl"], return_pct=metrics["return_pct"],
            max_drawdown=metrics["max_drawdown"], max_drawdown_pct=metrics["max_drawdown_pct"],
            exposure_pct=metrics["exposure_pct"], profit_factor=metrics["profit_factor"],
            expectancy=metrics["expectancy"],
            validation_status="PIPELINE_FIXTURE_RUN_OK" if strategy.is_research_fixture else "UNVALIDATED",
            config_hash=run_config_hash, starting_capital=reservation.allocated_capital,
            funding_data_available=funding_meta["funding_data_available"],
            funding_coverage_gap_vs_ohlcv=funding_meta["funding_coverage_gap_vs_ohlcv"],
            final_risk_state=run_result.final_risk_state.value,
            state_reason=run_result.final_risk_reason,
            notes=(
                "PIPELINE VALIDATION FIXTURE -- NOT A TRADING STRATEGY. This result proves the "
                "research pipeline executes end-to-end; it must not be interpreted as evidence "
                "of trading edge. " + funding_meta["note"]
            ),
        )
        results.append(record)
        write_result(record, RESULTS_DIR / run_id / f"{native}.json")
        print(f"  {native}: {metrics['num_trades']} trades, net_pnl={metrics['net_pnl']:.2f}, "
              f"risk_state={run_result.final_risk_state.value}, "
              f"funding_gap={funding_meta['funding_coverage_gap_vs_ohlcv']}")

    summary = {
        "run_id": run_id, "config_hash": run_config_hash,
        "label": "PIPELINE VALIDATION FIXTURE RUN -- NOT A TRADING STRATEGY EVALUATION",
        "dataset_id": handle.dataset_id, "dataset_version": handle.dataset_version,
        "dataset_root": str(handle.root),
        "num_instruments": len(results),
        "total_trades": sum(r.num_trades for r in results),
        "total_net_pnl": round(sum(r.net_pnl for r in results), 6),
        "instruments_with_funding_gap": [r.instrument for r in results if r.funding_coverage_gap_vs_ohlcv],
        "instruments_with_zero_trades": [r.instrument for r in results if r.num_trades == 0],
        "per_instrument": [r.to_dict() for r in results],
    }
    summary_path = RESULTS_DIR / run_id / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True, default=str))
    print(f"\n=== Run complete. {summary['total_trades']} total trades across "
          f"{summary['num_instruments']} instruments. ===")
    print(f"Summary written to {summary_path}")
    if summary["instruments_with_zero_trades"]:
        print(f"NOTE: zero trades for: {summary['instruments_with_zero_trades']} "
              f"-- see per-symbol result for risk_state.")
    return summary


if __name__ == "__main__":
    main()
