#!/usr/bin/env python3
"""
AURA v0.5.4 -- Track B 90-trading-day real-Alpaca backtest + permutation
test runner.

Built 2026-10-08 per Martin's request ("perform 90 days of backtesting of
the whole solution") and four rounds of AskUserQuestion clarification:

  1. SCOPE: Track B (Alpaca equities) pipeline only -- `aura_v054_backtest
     .run_backtest`'s data -> signal -> sizing -> exit -> portfolio-risk
     chain. Not Track A/MEXC (paused per Martin's own earlier direction),
     not the separate v0.53.x live/paper-trading stack.

  2. SIGNAL SOURCE: `SyntheticTestFixtureSignalSource` (a disclosed,
     NON-RESEARCHED 20-day-momentum rule), NOT
     `AuraFrozenDecisionEngineSignalSource`. Re-verified 2026-10-08, same
     day, directly from the real `aura_v054_signal_source.py` on Martin's
     machine (not assumed from an earlier session note): FROZEN_DECIDE_
     KWARGS still has technical_weight=0.0/short_technical_weight=0.0, so
     base_rank_score is mathematically 0.0 for every symbol/bar and the
     real decision engine still always ABSTAINs -- it would produce ZERO
     trades regardless of data window, so Martin chose the pipeline-
     exercise fixture instead once this was surfaced.
     *** SyntheticTestFixtureSignalSource IS NOT A TRADING STRATEGY ***
     Every result below is tagged SIGNAL_SOURCE_LABEL=
     "SYNTHETIC_TEST_FIXTURE_RULE" and must never be presented as .50/
     .51/.52 performance or as evidence of any real trading edge.

  3. DATA SOURCE: real Alpaca daily OHLCV via the new
     `AlpacaEquityBarsProvider` (aura_v054_alpaca_bars_provider.py), not
     synthetic -- requires ALPACA_EQUITY_PAPER_API_KEY/
     ALPACA_EQUITY_PAPER_SECRET_KEY in `.env`, which exist only on
     Martin's machine. This script does nothing without them.

  4. WINDOW: "90 days" = ~90 REAL TRADING DAYS of live signal-scanning,
     not 90 raw calendar days of fetched data. `run_backtest`'s 60-bar
     warmup floor means 90 calendar days (~63 trading days) would leave
     almost no bars ever scanned -- a near-empty, meaningless result.
     The provider's DEFAULT_LOOKBACK_BARS=160 gives ~60 bars warmup +
     ~100 bars of live scanning (68 RESEARCH + 32 HOLDOUT at the default
     20% holdout split) -- see that module's docstring for the exact math.

  UNIVERSE: the existing pinned 39-symbol
  `aura_v05351_equity_universe_v1.json` list (Martin's choice) -- not
  re-selected here.

WHAT THIS DOES
---------------
  1. Fetches real Alpaca daily bars for the universe (prefetch, batched).
  2. Runs `aura_v054_backtest.run_baseline` (plain backtest: trades,
     equity curve, RESEARCH/HOLDOUT metrics).
  3. Runs `aura_v054_permutation_test.run_permutation_test` on the same
     data/signal source (real-vs-random-timing, empirical p-value) --
     Martin's choice over a plain-backtest-only run, since this is
     exactly the validation engine built and tested earlier this session.
  4. Prints a summary and writes the full results to a timestamped JSON
     file next to this script.

HOW TO RUN
-----------
  On the machine where the real `.env` lives (not in any cloud sandbox):

      python run_track_b_alpaca_90day_backtest.py

  Takes a few minutes: ~160 trading days x 39 symbols fetched from
  Alpaca, then 1 real backtest + 200 permutation-test control backtests
  (DEFAULT_N_ITERATIONS in aura_v054_permutation_test.py), each scanning
  the same ~160-bar universe.

SIGNAL SOURCE (added 2026-10-08, after the frozen-vs-live-weights
correction -- see aura_v054_live_weight_signal_source.py's own module
docstring for the full story)
---------------------------------------------------------------------------
Two signal sources are selectable via --signal-source:

  synthetic (default) -- SyntheticTestFixtureSignalSource, the disclosed
    20-day-momentum rule. NOT a real strategy, never researched/validated.
    Pipeline-exercise only.

  live_weight -- LiveWeightDecisionEngineSignalSource
    (aura_v054_live_weight_signal_source.py). The REAL .50/.51/.52
    decision engine, with the REAL, currently-confirmed live decision
    weights (technical_weight=1.0, short_technical_weight=1.0, matching
    aura_v05362_live_evidence_orchestrator.py's LIVE_EVIDENCE_DECIDE_
    KWARGS) -- NOT .054's own deliberately-frozen 0.0/0.0
    FROZEN_DECIDE_KWARGS, which is untouched by this change. Still does
    NOT include sentiment/wave/sector-rotation evidence (none of that is
    available for historical backtesting in this project -- see that
    module's own docstring for exactly why) -- the decision is driven
    entirely by real .51/.52 technical evidence, same as what actually
    moves the needle in live trading today.

      python run_track_b_alpaca_90day_backtest.py --signal-source live_weight
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

import aura_v054_backtest as BT
import aura_v054_permutation_test as PERM
import aura_v054_signal_source as SIG
from aura_v054_alpaca_bars_provider import AlpacaEquityBarsProvider
from aura_v054_live_weight_signal_source import LiveWeightDecisionEngineSignalSource

REPO_ROOT = Path(__file__).resolve().parent
UNIVERSE_FILE = REPO_ROOT / "aura_v05351_equity_universe_v1.json"


def _load_universe() -> tuple[str, ...]:
    data = json.loads(UNIVERSE_FILE.read_text())
    return tuple(data["symbols"])


def _trade_count(trades, period: str) -> int:
    return len([t for t in trades if t.period == period])


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--signal-source",
        choices=("synthetic", "live_weight"),
        default="synthetic",
        help=(
            "synthetic (default): SyntheticTestFixtureSignalSource, the disclosed 20-day-momentum "
            "pipeline-exercise rule -- NOT a real strategy. "
            "live_weight: the real .50/.51/.52 decision engine with the real, confirmed live "
            "decision weights (technical_weight=1.0, short_technical_weight=1.0) -- see "
            "aura_v054_live_weight_signal_source.py's module docstring for exactly what this does "
            "and does not reproduce from the real live path."
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    load_dotenv()
    api_key = os.getenv("ALPACA_EQUITY_PAPER_API_KEY")
    secret_key = os.getenv("ALPACA_EQUITY_PAPER_SECRET_KEY")
    if not api_key or not secret_key:
        raise RuntimeError(
            "Missing ALPACA_EQUITY_PAPER_API_KEY or ALPACA_EQUITY_PAPER_SECRET_KEY in .env -- "
            "this is the dedicated equity/ETF paper credential pair (see .env.example), "
            "deliberately NOT the crypto ALPACA_PAPER_API_KEY/ALPACA_PAPER_SECRET_KEY pair."
        )

    universe = _load_universe()
    print(f"Universe ({len(universe)} symbols): {', '.join(universe)}")

    provider = AlpacaEquityBarsProvider(api_key=api_key, secret_key=secret_key)
    print(f"\nFetching ~{provider.lookback_bars} trading days of real Alpaca daily bars per symbol ...")
    provider.prefetch(universe)
    n_ok = len(provider._cache)
    n_missing = len(provider._fetch_errors)
    print(f"Fetched {n_ok}/{len(universe)} symbols.")
    if n_missing:
        print(f"  Missing/failed ({n_missing}): {provider._fetch_errors}")
    if n_ok == 0:
        print("No symbols fetched -- aborting before running the backtest.")
        return 1

    fetched_universe = tuple(s for s in universe if s in provider._cache)

    if args.signal_source == "live_weight":
        signal_source = LiveWeightDecisionEngineSignalSource()
        print(
            f"\nSignal source: {signal_source.SIGNAL_SOURCE_LABEL} "
            f"-- the REAL .50/.51/.52 decision engine with the REAL live decision weights "
            f"(technical_weight=1.0, short_technical_weight=1.0). Still no sentiment/wave/"
            f"sector-rotation evidence (see aura_v054_live_weight_signal_source.py docstring) -- "
            f"driven entirely by real .51/.52 technical evidence."
        )
    else:
        signal_source = SIG.SyntheticTestFixtureSignalSource()
        print(
            f"\nSignal source: {signal_source.SIGNAL_SOURCE_LABEL} "
            f"-- NOT A TRADING STRATEGY (see aura_v054_signal_source.py docstring). "
            f"Pipeline-exercise rule only."
        )

    print("\n=== Plain backtest (run_baseline) ===")
    report = BT.run_baseline(universe=fetched_universe, bars_provider=provider, signal_source=signal_source)

    print(f"real_equity_backtest_status = {report.real_equity_backtest_status}")
    if report.real_equity_backtest_status != "RUN":
        print(f"reason: {report.real_equity_backtest_reason}")
        return 1

    print(f"data_source_label = {report.data_source_label}  is_real_market_data = {report.is_real_market_data}")
    print(
        f"holdout_boundary_bar_index = {report.holdout_boundary_bar_index} "
        f"({report.holdout_boundary_timestamp})"
    )
    print(f"initial_equity = {report.initial_equity:,.2f}   final_equity = {report.final_equity:,.2f}")
    print(
        f"total trades = {len(report.trades)}  "
        f"(RESEARCH={_trade_count(report.trades, 'RESEARCH')}, "
        f"HOLDOUT={_trade_count(report.trades, 'HOLDOUT')})"
    )
    for label, m in (("RESEARCH", report.research_metrics), ("HOLDOUT", report.holdout_metrics)):
        if m is None:
            continue
        print(
            f"\n{label}: n_trades={m.n_trades}  win_rate={m.win_rate}  "
            f"total_return_frac={m.total_return_frac}  profit_factor={m.profit_factor}  "
            f"sharpe_trade_level={m.sharpe_trade_level}  max_drawdown_frac={m.max_drawdown_frac}"
        )

    perm_result = None
    perm_error = None
    print("\n=== Permutation test (real entry timing vs. random timing) ===")
    try:
        perm_result = PERM.run_permutation_test(
            universe=fetched_universe,
            bars_provider=provider,
            real_signal_source=signal_source,
        )
        print(
            f"metric={perm_result.metric_name}  real_value={perm_result.real_value}  "
            f"p_value={perm_result.p_value}  exceedances={perm_result.exceedances}/"
            f"{perm_result.n_iterations}"
        )
    except PERM.PermutationTestError as exc:
        perm_error = str(exc)
        print(f"Permutation test not run: {perm_error}")

    out = {
        "run_at_utc": datetime.now(timezone.utc).isoformat(),
        "universe_requested": list(universe),
        "universe_fetched": list(fetched_universe),
        "fetch_errors": dict(provider._fetch_errors),
        "data_source_label": report.data_source_label,
        "is_real_market_data": report.is_real_market_data,
        "signal_source_label": report.signal_source_label,
        "NOTE": (
            (
                "signal_source_label=SYNTHETIC_TEST_FIXTURE_RULE is NOT a real trading strategy -- "
                "see aura_v054_signal_source.py. These results are a pipeline exercise against real "
                "~90-trading-day Alpaca data, not evidence of any trading edge."
            )
            if args.signal_source != "live_weight"
            else (
                "signal_source_label=AURA_LIVE_WEIGHT_050_051_052 -- the REAL .50/.51/.52 decision "
                "engine with the REAL live decision weights (technical_weight=1.0, "
                "short_technical_weight=1.0, matching aura_v05362_live_evidence_orchestrator.py's "
                "LIVE_EVIDENCE_DECIDE_KWARGS), NOT .054's own deliberately-frozen 0.0/0.0 config. "
                "Still does NOT include sentiment/wave/sector-rotation evidence -- see "
                "aura_v054_live_weight_signal_source.py's module docstring for exactly why and what "
                "that means for how representative this is of the real live path."
            )
        ),
        "holdout_boundary_bar_index": report.holdout_boundary_bar_index,
        "initial_equity": report.initial_equity,
        "final_equity": report.final_equity,
        "n_trades_total": len(report.trades),
        "n_trades_research": _trade_count(report.trades, "RESEARCH"),
        "n_trades_holdout": _trade_count(report.trades, "HOLDOUT"),
        # SegmentMetrics is a frozen(slots=True) dataclass -- it has no __dict__,
        # so dataclasses.asdict() (field-introspection based) is required here,
        # not .__dict__ (which only works on ordinary, non-slotted dataclasses).
        "research_metrics": dataclasses.asdict(report.research_metrics) if report.research_metrics else None,
        "holdout_metrics": dataclasses.asdict(report.holdout_metrics) if report.holdout_metrics else None,
        "trades": BT.trade_records_to_dataframe(report.trades).to_dict(orient="records"),
        "permutation_test": (
            {
                "metric_name": perm_result.metric_name,
                "real_value": perm_result.real_value,
                "p_value": perm_result.p_value,
                "exceedances": perm_result.exceedances,
                "n_iterations": perm_result.n_iterations,
                "k_by_symbol": perm_result.k_by_symbol,
            }
            if perm_result is not None
            else {"error": perm_error}
        ),
    }
    out_file = REPO_ROOT / f"track_b_90day_alpaca_backtest_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}.json"
    out_file.write_text(json.dumps(out, indent=2, default=str))
    print(f"\nFull results written to {out_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
