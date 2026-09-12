#!/usr/bin/env python3
"""
AURA v0.5.3.42 — Crypto short-side signal research (MEXC perpetual futures).

RESEARCH ONLY. NEVER PLACES ORDERS. NEVER TOUCHES LIVE/PAPER EXECUTION.
Does not read or write anything under `.19`/`.22`/`.23`'s live execution
path, and does not change `VALIDATED_LONG_ENTRY_REGIME_LABELS` /
`VALIDATED_SHORT_ENTRY_REGIME_LABELS` in
`aura_v05323_execution_specification_builder.py` -- those stay empty
exactly as they are today. This script does not call `.39`'s promotion-gate
API either: its output is a research finding, not evidence submitted for
promotion. Promoting anything into a live signal remains a separate,
deliberate, human decision, made only after the full post-`.53` crypto
validation campaign Martin has separately commissioned (walk-forward across
a wider re-check, permutation/randomization null tests, multiple-testing
correction, regime segmentation) -- none of which this script performs.

Why this exists (v0.5.5 Final Implementation Baseline, milestone `.42`)
------------------------------------------------------------------------
The roadmap's one-line description for `.42` ("nothing structural blocks
this -- `.33`/`.27` already support OPEN_SHORT on MEXC") covers execution
readiness only. A pre-implementation audit (2026-09-12) found the actual
signal/research side was unbuilt for MEXC specifically: the live scheduled
pipeline (`.12`/`.13`/`.23`) runs entirely on Alpaca crypto-SPOT data
(BTC/USD, ETH/USD) via `aura_regime_backtest_v2.py` /
`aura_exit_policy_backtest.py`, and no code anywhere ran AURA's regime logic
against real MEXC perpetual-futures data or accounted for MEXC's perpetual-
specific costs (funding). Per Martin's explicit decision (2026-09-12), this
milestone is merged with the parallel "crypto backtest infrastructure"
workstream he separately commissioned: this ONE script both (a) discharges
`.42`'s "produce a short-side research signal for MEXC" requirement and
(b) is the first real piece of that shared MEXC data/backtest track.

What this script reuses, unchanged (same-signal-path discipline)
------------------------------------------------------------------------
Entry (candidate) detection is NOT reimplemented here. It is imported by
file from `aura_exit_policy_backtest.py` (`EPB`), which itself imports
`aura_regime_backtest_v2.py` (`V2`), which itself imports `.12`
(`aura_v05312_market_state_engine.py`, `MSE`) for `wilder_atr`/`build_4h`.
This script calls `V2.compute_regime_series()` (unchanged) and
`EPB.build_entries()` (unchanged) exactly as the existing Alpaca-based
scripts do -- the only two things that differ are (1) the DATA fed into
that pipeline (MEXC perpetual OHLCV instead of Alpaca crypto-spot OHLCV)
and (2) the trade SIMULATION layer (fill timing + cost model), described
below. `EPB.trade_summary`, `V2.bootstrap_mean_ci`, `V2.block_boundaries`,
and `V2.slice_by_range` are reused directly, unmodified -- none of them are
data-source-specific.

Candidate -> direction mapping (SIMULATION ONLY, same convention EPB
already established -- see EPB's own docstring for why):
  FROZEN_CANDIDATE ("BEAR x LOW x POSITIVE")            -> LONG
  MIRROR_CANDIDATE ("BULL_OR_NEUTRAL x LOW x NON_POSITIVE") -> SHORT
`.42` is specifically about the SHORT (MIRROR) side; both are still run and
reported so the short-side result can be read against its long-side sibling
under identical data/cost treatment, not in isolation.

What is genuinely NEW in this script (the two things `.42`'s audit found
missing, plus a `.40` Backtest-Readiness-Audit blocker fixed along the way)
------------------------------------------------------------------------
1. **Real MEXC perpetual data, pinned.** `aura_regime_backtest_v2.py` /
   `aura_exit_policy_backtest.py` fetch Alpaca crypto-SPOT bars live, fresh,
   from wall-clock "now" on every run (Backtest Readiness Audit blocker
   #3, HIGH: "no pinned/persisted historical dataset"). This script instead
   reads `AURA_CRYPTO/bars_1h.csv` and `AURA_CRYPTO/funding_history.csv` --
   real MEXC-perpetual OHLCV and real MEXC funding-rate history for
   BTC_USDT/ETH_USDT, 2026-02-27 through 2026-08-26 (six months), already
   committed to this git repository (not gitignored) by an earlier research
   run (`AURA_CRYPTO/research_run.json`, `experiment: "C0"`,
   `venue: "MEXC_CONTRACT"`). Being git-tracked, the dataset's reproducibility
   anchor is the git blob hash of these two files at the commit this script
   is checkpointed at -- `load_pinned_dataset()` below also computes and
   reports a SHA-256 of each file plus row counts and the exact date range,
   so a re-run can positively confirm it read the identical bytes without
   needing network access. IMPORTANT LIMITATION, disclosed rather than
   hidden: this session's sandboxed network egress policy blocks every
   MEXC/market-data domain (confirmed via the outbound proxy's own status
   endpoint, which reports a 403 policy denial, not a transient failure) --
   this script could not itself re-fetch or extend this dataset even if
   asked to. It reads what is already on disk; it does not go get more.
   Extending the pinned window, or adding symbols beyond BTC_USDT/ETH_USDT,
   requires a fetch from an environment with MEXC network access (this
   script's own loader function is written so that swapping in a longer or
   wider CSV pair, in the same schema, requires no other code change).

2. **MEXC perpetual cost model: funding + fees/slippage, not just a flat
   bps guess.** Neither existing backtest script models perpetual funding
   at all (Backtest Readiness Audit blocker #1, HIGH), and both use an
   unsourced flat `DEFAULT_COST_BPS = 10.0` (blocker #2, HIGH). This script
   adds real, direction-aware funding-cost accrual from
   `funding_history.csv` (every funding settlement strictly after entry
   fill and at/before exit is summed and signed by direction -- MEXC's
   perpetual funding convention, shared with essentially every major
   perpetual venue: a positive funding rate means longs pay shorts, a
   negative rate means shorts pay longs) and a documented, NOT independently
   re-verified, fee/slippage assumption: `FEE_BPS_ASSUMPTION = 1.0` and
   `SLIPPAGE_BPS_ASSUMPTION = 5.0`, inherited byte-for-byte from this
   project's own prior MEXC research run config (`AURA_CRYPTO/
   research_run.json`: `"fee_bps": 1.0, "slippage_bps": 5.0`). These are
   flagged, not silently trusted: this script could not reach mexc.com to
   check them against MEXC's current published fee schedule (same network
   restriction as above). Martin should re-verify both constants before any
   result here is used for more than a preliminary read.

3. **Complete per-trade ledger, persisted.** A second, independent
   Backtest-Readiness-Audit finding (2026-09-12 follow-up pass) is that
   `simulate_trades_for_entries()`'s per-trade DataFrame -- which already
   carries entry/exit timestamp+price, exit reason, MFE/MAE -- is built in
   memory and then discarded before reaching any persisted output; only a
   per-(symbol,candidate) AGGREGATE row is ever written to disk. This
   script writes the full per-trade ledger to CSV (every trade, every
   field, including the fee/funding/slippage breakdown and the regime
   snapshot at entry), in addition to the aggregate summary CSV.

4. **Next-bar-open fills**, not same-bar-close (Backtest Readiness Audit
   blocker #4, MEDIUM: "optimistic same-bar-close fills"). The existing
   scripts fill at the entry (signal) bar's own close -- the instant the
   regime condition is first observably true, which a real order could not
   have been filled at. This script fills at the OPEN of the bar
   immediately following the signal bar, which is the earliest realistic
   execution point given hourly bars.

What is explicitly NOT done here (deferred to the post-`.53` campaign,
per Martin's own instruction not to present in-sample results as validated)
------------------------------------------------------------------------
Walk-forward validation IS included below (`walk_forward_policy_mexc`) --
it is cheap to include (the machinery already exists, tested, in
`aura_regime_backtest_v2.py`/`aura_exit_policy_backtest.py`, and reusing it
here is a straightforward parameterization, not new research risk) and it
only strengthens the honesty of what's reported. What is NOT included, and
is explicitly reserved for the full post-`.53` "AURA Crypto Strategy
Validation Report": permutation / randomization null-distribution tests,
multiple-testing correction (this script tests 2 symbols x 2 candidates x
a policy grid -- a real multiple-comparisons exposure that is not
corrected for here), and regime-segmented performance reporting. A
`PRELIMINARY_NOT_VALIDATED` banner is printed and written into every output
artifact for this reason -- a PROMISING verdict from this script is a lead
for the full campaign to re-check, never a promotion input on its own.

Output
------
research/mexc_crypto_signal_research_v05342/mexc_crypto_signal_trades.csv
    -- full per-trade ledger, every field.
research/mexc_crypto_signal_research_v05342/mexc_crypto_signal_summary.csv
    -- one row per (symbol, candidate), aggregate stats + verdict.
research/mexc_crypto_signal_research_v05342/mexc_crypto_signal_report.txt
    -- human-readable report: dataset manifest (pinning), cost-model
    assumptions and their provenance, and the results table.

No credentials required -- this script never makes a network call; it only
reads the two pinned CSVs already committed under `AURA_CRYPTO/`.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
DEFAULT_DATA_DIR = ROOT / "AURA_CRYPTO"
DEFAULT_OUTDIR = ROOT / "research" / "mexc_crypto_signal_research_v05342"

PRELIMINARY_BANNER = (
    "PRELIMINARY_NOT_VALIDATED -- in-sample/single-split research result. "
    "No permutation test, no multiple-testing correction, no regime "
    "segmentation. NOT a promotion input. See module docstring."
)


def _load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Reuse the exact, already-tested entry-detection / statistics machinery --
# see module docstring "What this script reuses, unchanged".
EPB = _load_module("aura_exit_policy_backtest", "aura_exit_policy_backtest.py")
V2 = EPB.V2
SDE = EPB.SDE

CANDIDATES = EPB.CANDIDATES  # ("FROZEN", "MIRROR")
CANDIDATE_LABEL = EPB.CANDIDATE_LABEL
CANDIDATE_DIRECTION = EPB.CANDIDATE_DIRECTION  # {"FROZEN": "LONG", "MIRROR": "SHORT"}
FROZEN_ATR_THRESHOLD_PCT = EPB.FROZEN_ATR_THRESHOLD_PCT

MEXC_SYMBOLS = ("BTC_USDT", "ETH_USDT")  # matches AURA_CRYPTO/universe.csv (CRYPTO_C0_FROZEN)

TRAIN_FRACTION = V2.TRAIN_FRACTION  # 0.70, same split convention as v2/EPB

# Cost-model constants -- see module docstring item 2 for full provenance
# and the disclosed limitation (not independently re-verified against
# MEXC's current published schedule; network access is blocked in this
# sandboxed session). Inherited byte-for-byte from AURA_CRYPTO/research_run.json.
FEE_BPS_ASSUMPTION = 1.0
SLIPPAGE_BPS_ASSUMPTION = 5.0
FEE_SLIPPAGE_PROVENANCE = (
    "AURA_CRYPTO/research_run.json (config.fee_bps=1.0, config.slippage_bps=5.0), "
    "an earlier MEXC-venue research run in this repo. NOT independently re-verified "
    "against MEXC's current published fee schedule -- this session's network egress "
    "policy blocks mexc.com (confirmed 403 at the outbound proxy, not a transient "
    "failure). Re-verify before treating any result here as more than preliminary."
)

DEFAULT_TRAILING_STOPS_PCT = EPB.DEFAULT_TRAILING_STOPS_PCT
DEFAULT_TAKE_PROFITS_PCT = EPB.DEFAULT_TAKE_PROFITS_PCT
DEFAULT_MAX_HOLD_HOURS = EPB.DEFAULT_MAX_HOLD_HOURS
BASELINE_HORIZON_HOURS = EPB.BASELINE_HORIZON_HOURS

MIN_TRAIN_TRADES_FOR_SEARCH = EPB.MIN_TRAIN_TRADES_FOR_SEARCH
MIN_TRAIN_TRADES_VERDICT = EPB.MIN_TRAIN_TRADES_VERDICT
MIN_TEST_TRADES_VERDICT = EPB.MIN_TEST_TRADES_VERDICT
MIN_TEST_TRADES_WALKFORWARD = EPB.MIN_TEST_TRADES_WALKFORWARD
MIN_WALKFORWARD_FOLDS_CONSIDERED = EPB.MIN_WALKFORWARD_FOLDS_CONSIDERED
WALKFORWARD_MAJORITY_FRACTION = EPB.WALKFORWARD_MAJORITY_FRACTION
BOOTSTRAP_P_POSITIVE_THRESHOLD = EPB.BOOTSTRAP_P_POSITIVE_THRESHOLD

DEFAULT_N_BOOT = V2.DEFAULT_N_BOOT
DEFAULT_ALPHA = V2.DEFAULT_ALPHA
DEFAULT_K_FOLDS = V2.DEFAULT_K_FOLDS
DEFAULT_SEED = V2.DEFAULT_SEED

REQUIRED_BAR_COLUMNS = ("symbol", "timestamp", "open", "high", "low", "close", "volume")
REQUIRED_FUNDING_COLUMNS = ("symbol", "settle_time", "funding_rate")


# ------------------------------------------------------------------------
# Pinned dataset loading + reproducibility manifest.
# ------------------------------------------------------------------------

def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_pinned_dataset(data_dir: Path = DEFAULT_DATA_DIR) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    """Load the pinned MEXC bars + funding history from disk. Fails closed
    (raises) if either file is missing or malformed -- this script never
    silently substitutes a shorter/different dataset. Returns
    (bars_df, funding_df, manifest) where manifest records the exact bytes
    read (SHA-256 per file) plus row counts and date range per symbol, so
    every output artifact can cite precisely what data produced it."""
    bars_path = data_dir / "bars_1h.csv"
    funding_path = data_dir / "funding_history.csv"

    for p in (bars_path, funding_path):
        if not p.is_file():
            raise FileNotFoundError(
                f"Pinned MEXC dataset file missing: {p}. This script does not fetch data "
                f"(and could not reach MEXC from this session even if it tried -- see module "
                f"docstring). Supply the pinned CSV or point --data-dir at a directory that has it."
            )

    bars = pd.read_csv(bars_path)
    funding = pd.read_csv(funding_path)

    missing_bar_cols = [c for c in REQUIRED_BAR_COLUMNS if c not in bars.columns]
    if missing_bar_cols:
        raise ValueError(f"{bars_path} is missing required columns: {missing_bar_cols}")
    missing_funding_cols = [c for c in REQUIRED_FUNDING_COLUMNS if c not in funding.columns]
    if missing_funding_cols:
        raise ValueError(f"{funding_path} is missing required columns: {missing_funding_cols}")

    bars["timestamp"] = pd.to_datetime(bars["timestamp"], utc=True, errors="coerce")
    funding["settle_time"] = pd.to_datetime(funding["settle_time"], utc=True, errors="coerce")
    if bars["timestamp"].isna().any():
        raise ValueError(f"{bars_path} has unparseable timestamps")
    if funding["settle_time"].isna().any():
        raise ValueError(f"{funding_path} has unparseable settle_time values")

    manifest: dict[str, Any] = {
        "bars_path": str(bars_path),
        "bars_sha256": _sha256_file(bars_path),
        "bars_rows": int(len(bars)),
        "funding_path": str(funding_path),
        "funding_sha256": _sha256_file(funding_path),
        "funding_rows": int(len(funding)),
        "symbols": {},
    }
    for sym in sorted(bars["symbol"].unique()):
        s = bars[bars["symbol"] == sym].sort_values("timestamp")
        manifest["symbols"][sym] = {
            "bars_n": int(len(s)),
            "bars_start": s["timestamp"].iloc[0].isoformat(),
            "bars_end": s["timestamp"].iloc[-1].isoformat(),
        }

    return bars, funding, manifest


# ------------------------------------------------------------------------
# Funding-cost accrual.
# ------------------------------------------------------------------------

def funding_cost_pct(
    funding_df: pd.DataFrame, symbol: str, entry_ts: pd.Timestamp, exit_ts: pd.Timestamp, direction: str
) -> float:
    """Sum every funding settlement strictly after entry_ts and at/before
    exit_ts for this symbol, sign-adjusted by direction. MEXC (like every
    major perpetual venue) settles funding = position_notional * rate; as a
    percent of notional that is simply rate * 100 per event, so summing
    rates and scaling once is equivalent to summing per-event payments.
    Convention: a POSITIVE funding_rate means LONG pays SHORT; a NEGATIVE
    rate means SHORT pays LONG. Returned value is a COST (positive = paid
    by this trade's holder, negative = received)."""
    events = funding_df[
        (funding_df["symbol"] == symbol)
        & (funding_df["settle_time"] > entry_ts)
        & (funding_df["settle_time"] <= exit_ts)
    ]
    total_rate = float(events["funding_rate"].sum()) if len(events) else 0.0
    if direction == "LONG":
        return total_rate * 100.0
    return -total_rate * 100.0


# ------------------------------------------------------------------------
# Trade simulation -- next-bar-open fill, funding + fee/slippage cost.
# Exit logic (trailing stop / take-profit / no-look-ahead / same-bar
# stop-wins-collision / TIMEOUT) is otherwise identical to
# EPB.simulate_trade -- see that function's docstring for the exit-logic
# rationale, unchanged here.
# ------------------------------------------------------------------------

def simulate_trade_mexc(
    g_full: pd.DataFrame,
    funding_df: pd.DataFrame,
    symbol: str,
    signal_ts: pd.Timestamp,
    direction: str,
    trailing_stop_pct: float,
    take_profit_pct: float | None,
    max_hold_bars: int,
    fee_slippage_pct: float,
) -> dict[str, Any]:
    """Fill at the OPEN of the bar immediately after signal_ts (the bar
    whose close first satisfied the regime match) -- see module docstring
    item 4. signal_ts itself is never used as the fill timestamp."""
    pos = int(g_full["timestamp"].searchsorted(signal_ts, side="right"))
    if pos >= len(g_full):
        return {
            "exit_reason": "NO_DATA_AFTER_SIGNAL",
            "entry_ts": None,
            "entry_price": None,
            "bars_held": 0,
            "gross_return_pct": None,
            "fee_slippage_cost_pct": None,
            "funding_cost_pct": None,
            "net_return_pct": None,
            "mfe_pct": None,
            "mae_pct": None,
            "exit_ts": signal_ts,  # never blocks a later entry
            "exit_price": None,
        }

    entry_bar = g_full.iloc[pos]
    entry_ts = entry_bar["timestamp"]
    entry_price = float(entry_bar["open"])
    post = g_full.iloc[pos: pos + max_hold_bars]

    running_extreme = entry_price
    worst_excursion = entry_price
    exit_reason: str | None = None
    exit_price: float | None = None
    exit_ts: pd.Timestamp | None = None
    bars_held = 0

    for _, bar in post.iterrows():
        bars_held += 1
        if direction == "LONG":
            stop_level = running_extreme * (1.0 - trailing_stop_pct / 100.0)
            target_level = entry_price * (1.0 + take_profit_pct / 100.0) if take_profit_pct is not None else None
            stop_hit = bar["low"] <= stop_level
            target_hit = target_level is not None and bar["high"] >= target_level
        else:  # SHORT
            stop_level = running_extreme * (1.0 + trailing_stop_pct / 100.0)
            target_level = entry_price * (1.0 - take_profit_pct / 100.0) if take_profit_pct is not None else None
            stop_hit = bar["high"] >= stop_level
            target_hit = target_level is not None and bar["low"] <= target_level

        if stop_hit:
            exit_reason, exit_price, exit_ts = "STOP", stop_level, bar["timestamp"]
        elif target_hit:
            exit_reason, exit_price, exit_ts = "TARGET", target_level, bar["timestamp"]

        if direction == "LONG":
            worst_excursion = min(worst_excursion, bar["low"])
        else:
            worst_excursion = max(worst_excursion, bar["high"])

        if exit_reason is not None:
            break

        running_extreme = max(running_extreme, bar["high"]) if direction == "LONG" else min(running_extreme, bar["low"])

    if exit_reason is None:
        exit_reason = "TIMEOUT"
        exit_price = float(post.iloc[-1]["close"])
        exit_ts = post.iloc[-1]["timestamp"]

    if direction == "LONG":
        gross_return_pct = 100.0 * (exit_price / entry_price - 1.0)
        mfe_pct = 100.0 * (running_extreme / entry_price - 1.0)
        mae_pct = 100.0 * (worst_excursion / entry_price - 1.0)
    else:
        gross_return_pct = 100.0 * (entry_price / exit_price - 1.0)
        mfe_pct = 100.0 * (entry_price / running_extreme - 1.0)
        mae_pct = 100.0 * (entry_price / worst_excursion - 1.0)

    fcost = funding_cost_pct(funding_df, symbol, entry_ts, exit_ts, direction)
    net_return_pct = gross_return_pct - fee_slippage_pct - fcost

    return {
        "exit_reason": exit_reason,
        "entry_ts": entry_ts,
        "entry_price": entry_price,
        "bars_held": bars_held,
        "gross_return_pct": gross_return_pct,
        "fee_slippage_cost_pct": fee_slippage_pct,
        "funding_cost_pct": fcost,
        "net_return_pct": net_return_pct,
        "mfe_pct": mfe_pct,
        "mae_pct": mae_pct,
        "exit_ts": exit_ts,
        "exit_price": exit_price,
    }


def simulate_trades_for_entries_mexc(
    g_full: pd.DataFrame,
    funding_df: pd.DataFrame,
    symbol: str,
    entries: pd.DataFrame,
    direction: str,
    policy: tuple[float, float | None],
    max_hold_bars: int,
    fee_slippage_pct: float,
) -> tuple[pd.DataFrame, int]:
    """Sequential, single-position account -- identical discipline to
    EPB.simulate_trades_for_entries (an entry that fires while a prior
    trade under this same policy is still open is skipped, not stacked).
    Every returned row also carries the regime snapshot at the SIGNAL bar
    (trend_bear / atr14_pct / bar2_positive) for the ledger."""
    ts, tp = policy
    rows: list[dict[str, Any]] = []
    skipped = 0
    blocked_until: pd.Timestamp | None = None
    for _, row in entries.sort_values("timestamp").iterrows():
        signal_ts = row["timestamp"]
        if blocked_until is not None and signal_ts < blocked_until:
            skipped += 1
            continue
        trade = simulate_trade_mexc(
            g_full, funding_df, symbol, signal_ts, direction, ts, tp, max_hold_bars, fee_slippage_pct
        )
        trade["symbol"] = symbol
        trade["signal_ts"] = signal_ts
        trade["direction"] = direction
        trade["trend_bear_at_signal"] = bool(row["trend_bear"])
        trade["bar2_positive_at_signal"] = bool(row["bar2_positive"])
        trade["atr14_pct_at_signal"] = float(row["atr14_pct"])
        rows.append(trade)
        blocked_until = trade.get("exit_ts", signal_ts)
    return pd.DataFrame(rows), skipped


def trade_summary_mexc(trades: pd.DataFrame) -> dict[str, Any]:
    """Same shape/semantics as EPB.trade_summary, reused as-is where
    possible; kept as a thin wrapper here only because this ledger's column
    set is a superset (fee/funding broken out) -- EPB.trade_summary only
    reads net_return_pct/exit_reason/bars_held, all present here too."""
    return EPB.trade_summary(trades)


# ------------------------------------------------------------------------
# TRAIN-only policy search + walk-forward (mirrors EPB exactly, adapted to
# call the MEXC-cost-aware simulator above instead of EPB's flat-cost one).
# ------------------------------------------------------------------------

def search_best_policy_mexc(
    g_full: pd.DataFrame,
    funding_df: pd.DataFrame,
    symbol: str,
    train_entries: pd.DataFrame,
    direction: str,
    grid: list[tuple[float, float | None]],
    max_hold_bars: int,
    fee_slippage_pct: float,
) -> tuple[tuple[float, float | None] | None, dict[str, Any] | None]:
    if len(train_entries) < MIN_TRAIN_TRADES_FOR_SEARCH:
        return None, None

    best_policy, best_summary = None, None
    for policy in grid:
        trades, skipped = simulate_trades_for_entries_mexc(
            g_full, funding_df, symbol, train_entries, direction, policy, max_hold_bars, fee_slippage_pct
        )
        summary = trade_summary_mexc(trades)
        if summary["n_trades"] < MIN_TRAIN_TRADES_FOR_SEARCH or summary["mean_net_return_pct"] is None:
            continue
        summary["entries_skipped_overlap"] = skipped
        if best_summary is None or summary["mean_net_return_pct"] > best_summary["mean_net_return_pct"]:
            best_policy, best_summary = policy, summary

    return best_policy, best_summary


def walk_forward_policy_mexc(
    g: pd.DataFrame,
    g_full: pd.DataFrame,
    funding_df: pd.DataFrame,
    symbol: str,
    valid_full: pd.DataFrame,
    candidate: str,
    direction: str,
    grid: list[tuple[float, float | None]],
    *,
    k_folds: int,
    max_hold_bars: int,
    fee_slippage_pct: float,
) -> dict[str, Any]:
    boundaries = V2.block_boundaries(valid_full, k_folds)
    folds: list[dict[str, Any]] = []

    for k in range(2, k_folds + 1):
        train_end = boundaries[k - 2]
        test_end = boundaries[k - 1]
        fold: dict[str, Any] = {"fold": k - 1, "train_end": train_end.isoformat(), "test_end": test_end.isoformat()}

        train_g = V2.slice_by_range(g, start_ts=None, end_ts=train_end)
        test_g = V2.slice_by_range(g, start_ts=train_end, end_ts=test_end)
        fold_train_entries = EPB.build_entries(train_g, candidate, FROZEN_ATR_THRESHOLD_PCT)

        chosen, chosen_summary = search_best_policy_mexc(
            g_full, funding_df, symbol, fold_train_entries, direction, grid, max_hold_bars, fee_slippage_pct
        )
        if chosen is None:
            fold["status"] = "NO_ELIGIBLE_POLICY_ON_TRAIN"
            folds.append(fold)
            continue

        fold["chosen_policy"] = EPB.policy_id(chosen)
        fold["train_trades"] = chosen_summary["n_trades"]

        fold_test_entries = EPB.build_entries(test_g, candidate, FROZEN_ATR_THRESHOLD_PCT)
        test_trades, test_skipped = simulate_trades_for_entries_mexc(
            g_full, funding_df, symbol, fold_test_entries, direction, chosen, max_hold_bars, fee_slippage_pct
        )
        test_summary = trade_summary_mexc(test_trades)
        fold["test_trades"] = test_summary["n_trades"]
        fold["test_mean_net_return_pct"] = test_summary["mean_net_return_pct"]

        if test_summary["n_trades"] >= MIN_TEST_TRADES_WALKFORWARD and test_summary["mean_net_return_pct"] is not None:
            fold["status"] = "EVALUATED"
        else:
            fold["status"] = "TOO_FEW_TEST_TRADES"
        folds.append(fold)

    considered = [f for f in folds if f.get("status") == "EVALUATED"]
    positive = [f for f in considered if f["test_mean_net_return_pct"] > 0]
    return {"folds": folds, "folds_considered": len(considered), "folds_positive": len(positive)}


# ------------------------------------------------------------------------
# Per-symbol, per-candidate orchestration + verdict (same methodology and
# thresholds as EPB.backtest_symbol_candidate; verdict field is deliberately
# named preliminary_verdict, not verdict, so it can never be mistaken for a
# `.39` promotion-gate outcome downstream).
# ------------------------------------------------------------------------

def backtest_symbol_candidate_mexc(
    symbol: str,
    candidate: str,
    g: pd.DataFrame,
    funding_df: pd.DataFrame,
    valid_full: pd.DataFrame,
    train_boundary_ts: pd.Timestamp,
    *,
    n_boot: int,
    alpha: float,
    k_folds: int,
    max_hold_hours: int,
    fee_slippage_pct: float,
    grid: list[tuple[float, float | None]],
    rng: np.random.Generator,
) -> tuple[dict[str, Any], pd.DataFrame]:
    direction = CANDIDATE_DIRECTION[candidate]
    g_full = g.sort_values("timestamp").reset_index(drop=True)

    result: dict[str, Any] = {
        "symbol": symbol,
        "candidate": candidate,
        "candidate_label": CANDIDATE_LABEL[candidate],
        "direction": direction,
    }

    train_g = V2.slice_by_range(g, end_ts=train_boundary_ts)
    test_g = V2.slice_by_range(g, start_ts=train_boundary_ts)
    train_entries = EPB.build_entries(train_g, candidate, FROZEN_ATR_THRESHOLD_PCT)
    test_entries = EPB.build_entries(test_g, candidate, FROZEN_ATR_THRESHOLD_PCT)

    chosen, chosen_train_summary = search_best_policy_mexc(
        g_full, funding_df, symbol, train_entries, direction, grid, max_hold_hours, fee_slippage_pct
    )
    if chosen is None:
        result.update(preliminary_verdict="INSUFFICIENT_DATA", reason="NO_POLICY_HAD_ENOUGH_TRAIN_TRADES")
        return result, pd.DataFrame()

    result["chosen_policy"] = EPB.policy_id(chosen)
    result["chosen_trailing_stop_pct"], result["chosen_take_profit_pct"] = chosen
    result["train_trades"] = chosen_train_summary["n_trades"]
    result["train_mean_net_return_pct"] = chosen_train_summary["mean_net_return_pct"]

    test_trades, test_entries_skipped_overlap = simulate_trades_for_entries_mexc(
        g_full, funding_df, symbol, test_entries, direction, chosen, max_hold_hours, fee_slippage_pct
    )
    test_summary = trade_summary_mexc(test_trades)
    for key, value in test_summary.items():
        result[f"test_{key}"] = value
    result["test_entries_total"] = int(len(test_entries))
    result["test_entries_skipped_overlap"] = test_entries_skipped_overlap
    result["test_mean_funding_cost_pct"] = (
        float(test_trades["funding_cost_pct"].mean()) if len(test_trades) and test_trades["funding_cost_pct"].notna().any() else None
    )

    baseline_col = f"fwd_{BASELINE_HORIZON_HOURS}h"
    baseline_vals = (
        test_entries[baseline_col].dropna().to_numpy(dtype=float) - fee_slippage_pct if len(test_entries) else np.array([])
    )
    result["test_baseline_24h_hold_mean_net_return_pct"] = float(baseline_vals.mean()) if len(baseline_vals) else None
    result["excess_vs_baseline"] = (
        result["test_mean_net_return_pct"] - result["test_baseline_24h_hold_mean_net_return_pct"]
        if result["test_mean_net_return_pct"] is not None and result["test_baseline_24h_hold_mean_net_return_pct"] is not None
        else None
    )

    test_net_vals = test_trades["net_return_pct"].dropna().to_numpy(dtype=float) if len(test_trades) else np.array([])
    boot = V2.bootstrap_mean_ci(test_net_vals, n_boot=n_boot, alpha=alpha, rng=rng)
    result["bootstrap_n"] = boot["n"]
    result["bootstrap_ci_low"] = boot["ci_low"]
    result["bootstrap_ci_high"] = boot["ci_high"]
    result["bootstrap_p_positive"] = boot["p_positive"]

    wf = walk_forward_policy_mexc(
        g, g_full, funding_df, symbol, valid_full, candidate, direction, grid,
        k_folds=k_folds, max_hold_bars=max_hold_hours, fee_slippage_pct=fee_slippage_pct,
    )
    result["walkforward_folds_considered"] = wf["folds_considered"]
    result["walkforward_folds_positive"] = wf["folds_positive"]
    result["walkforward_fold_detail"] = json.dumps(wf["folds"])

    train_trades_n = result["train_trades"]
    test_trades_n = result["test_n_trades"]

    if (
        train_trades_n < MIN_TRAIN_TRADES_VERDICT
        or test_trades_n < MIN_TEST_TRADES_VERDICT
        or wf["folds_considered"] < MIN_WALKFORWARD_FOLDS_CONSIDERED
    ):
        result["preliminary_verdict"] = "INSUFFICIENT_DATA"
        result["reason"] = (
            f"train_trades={train_trades_n}<{MIN_TRAIN_TRADES_VERDICT} or "
            f"test_trades={test_trades_n}<{MIN_TEST_TRADES_VERDICT} or "
            f"walkforward_folds_considered={wf['folds_considered']}<{MIN_WALKFORWARD_FOLDS_CONSIDERED}"
        )
        return result, test_trades

    passes_direction = result["test_mean_net_return_pct"] is not None and result["test_mean_net_return_pct"] > 0
    passes_excess = result["excess_vs_baseline"] is not None and result["excess_vs_baseline"] > 0
    passes_significance = boot["p_positive"] is not None and boot["p_positive"] >= BOOTSTRAP_P_POSITIVE_THRESHOLD
    wf_fraction = (wf["folds_positive"] / wf["folds_considered"]) if wf["folds_considered"] else 0.0
    passes_walkforward = wf_fraction >= WALKFORWARD_MAJORITY_FRACTION
    result["walkforward_positive_fraction"] = wf_fraction

    if passes_direction and passes_excess and passes_significance and passes_walkforward:
        result["preliminary_verdict"] = "PROMISING"
        result["reason"] = "positive_excess_significant_and_stable_across_walkforward_folds"
    elif passes_direction and passes_excess and (passes_significance or passes_walkforward):
        result["preliminary_verdict"] = "WEAK"
        result["reason"] = "positive_excess_but_not_both_significant_and_stable"
    else:
        result["preliminary_verdict"] = "NOT_PROMISING"
        result["reason"] = "no_positive_significant_stable_edge_over_24h_hold_baseline"

    return result, test_trades


def backtest_symbol_mexc(
    symbol: str,
    raw: pd.DataFrame,
    funding_df: pd.DataFrame,
    *,
    n_boot: int,
    alpha: float,
    k_folds: int,
    max_hold_hours: int,
    fee_slippage_pct: float,
    grid: list[tuple[float, float | None]],
    rng: np.random.Generator,
) -> tuple[list[dict[str, Any]], pd.DataFrame]:
    g = V2.compute_regime_series(raw)
    if g is None:
        return (
            [
                {
                    "symbol": symbol, "candidate": c, "candidate_label": CANDIDATE_LABEL[c],
                    "direction": CANDIDATE_DIRECTION[c],
                    "preliminary_verdict": "INSUFFICIENT_DATA",
                    "reason": f"FEWER_THAN_{V2.MIN_1H_BARS}_USABLE_1H_BARS_OR_NO_4H_WARMUP",
                }
                for c in CANDIDATES
            ],
            pd.DataFrame(),
        )

    valid_full = g[g["_valid_row"]].reset_index(drop=True)
    if len(valid_full) < V2.MIN_1H_BARS:
        return (
            [
                {
                    "symbol": symbol, "candidate": c, "candidate_label": CANDIDATE_LABEL[c],
                    "direction": CANDIDATE_DIRECTION[c],
                    "preliminary_verdict": "INSUFFICIENT_DATA", "reason": "TOO_FEW_VALID_REGIME_BARS_AFTER_WARMUP",
                }
                for c in CANDIDATES
            ],
            pd.DataFrame(),
        )

    split_idx = int(len(valid_full) * TRAIN_FRACTION)
    train_valid = valid_full.iloc[:split_idx]
    test_valid = valid_full.iloc[split_idx:]
    if train_valid.empty or test_valid.empty:
        return (
            [
                {
                    "symbol": symbol, "candidate": c, "candidate_label": CANDIDATE_LABEL[c],
                    "direction": CANDIDATE_DIRECTION[c],
                    "preliminary_verdict": "INSUFFICIENT_DATA", "reason": "EMPTY_TRAIN_OR_TEST_SPLIT",
                }
                for c in CANDIDATES
            ],
            pd.DataFrame(),
        )

    train_boundary_ts = train_valid["timestamp"].max()
    max_hold_bars = int(max_hold_hours)

    results = []
    all_trades = []
    for candidate in CANDIDATES:
        result, trades = backtest_symbol_candidate_mexc(
            symbol, candidate, g, funding_df, valid_full, train_boundary_ts,
            n_boot=n_boot, alpha=alpha, k_folds=k_folds, max_hold_hours=max_hold_bars,
            fee_slippage_pct=fee_slippage_pct, grid=grid, rng=rng,
        )
        results.append(result)
        if len(trades):
            t = trades.copy()
            t["candidate"] = candidate
            t["candidate_label"] = CANDIDATE_LABEL[candidate]
            all_trades.append(t)

    trades_df = pd.concat(all_trades, ignore_index=True) if all_trades else pd.DataFrame()
    return results, trades_df


# ------------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR, help="Directory containing the pinned bars_1h.csv / funding_history.csv.")
    p.add_argument("--outdir", type=Path, default=DEFAULT_OUTDIR)
    p.add_argument("--symbols", nargs="*", default=None, help="Explicit MEXC symbol list (e.g. BTC_USDT). Defaults to every symbol present in the pinned dataset.")
    p.add_argument("--fee-bps", type=float, default=FEE_BPS_ASSUMPTION)
    p.add_argument("--slippage-bps", type=float, default=SLIPPAGE_BPS_ASSUMPTION)
    p.add_argument("--n-boot", type=int, default=DEFAULT_N_BOOT)
    p.add_argument("--alpha", type=float, default=DEFAULT_ALPHA, help="Bootstrap CI significance level (0.10 = 90%% CI).")
    p.add_argument("--k-folds", type=int, default=DEFAULT_K_FOLDS)
    p.add_argument("--seed", type=int, default=DEFAULT_SEED)
    p.add_argument("--trailing-stops", type=float, nargs="*", default=list(DEFAULT_TRAILING_STOPS_PCT))
    p.add_argument("--take-profits", type=float, nargs="*", default=list(DEFAULT_TAKE_PROFITS_PCT))
    p.add_argument("--max-hold-hours", type=int, default=DEFAULT_MAX_HOLD_HOURS)
    args = p.parse_args()

    print(PRELIMINARY_BANNER)
    print("MODE: RESEARCH ONLY -- NO ORDERS -- NO NETWORK CALLS (pinned dataset)")
    print()

    try:
        bars, funding, manifest = load_pinned_dataset(args.data_dir)
    except (FileNotFoundError, ValueError) as exc:
        print(f"FAIL-CLOSED: {exc}", file=sys.stderr)
        return 2

    symbols = args.symbols if args.symbols else sorted(bars["symbol"].unique())
    fee_slippage_pct = (args.fee_bps + args.slippage_bps) / 100.0
    grid = EPB.policy_grid(tuple(args.trailing_stops), tuple(args.take_profits))
    rng = np.random.default_rng(args.seed)

    print(f"Dataset manifest: {json.dumps(manifest, indent=2)}")
    print(f"Fee/slippage assumption: fee_bps={args.fee_bps} slippage_bps={args.slippage_bps} -- provenance: {FEE_SLIPPAGE_PROVENANCE}")
    print(f"Symbols: {symbols}  n_boot={args.n_boot} alpha={args.alpha} k_folds={args.k_folds} seed={args.seed}")
    print()

    rows: list[dict[str, Any]] = []
    trade_frames: list[pd.DataFrame] = []
    for i, symbol in enumerate(symbols, start=1):
        raw = bars[bars["symbol"] == symbol].sort_values("timestamp").reset_index(drop=True)
        symbol_results, symbol_trades = backtest_symbol_mexc(
            symbol, raw, funding,
            n_boot=args.n_boot, alpha=args.alpha, k_folds=args.k_folds,
            max_hold_hours=args.max_hold_hours, fee_slippage_pct=fee_slippage_pct,
            grid=grid, rng=rng,
        )
        rows.extend(symbol_results)
        if len(symbol_trades):
            trade_frames.append(symbol_trades)
        for r in symbol_results:
            print(f"[{i:02d}/{len(symbols)}] {symbol} {r['candidate']}: {r['preliminary_verdict']} ({r.get('reason', '')})")

    summary_df = pd.DataFrame(rows)
    trades_df = pd.concat(trade_frames, ignore_index=True) if trade_frames else pd.DataFrame()

    args.outdir.mkdir(parents=True, exist_ok=True)
    summary_path = args.outdir / "mexc_crypto_signal_summary.csv"
    trades_path = args.outdir / "mexc_crypto_signal_trades.csv"
    summary_df.to_csv(summary_path, index=False)
    trades_df.to_csv(trades_path, index=False)

    report_path = args.outdir / "mexc_crypto_signal_report.txt"
    with report_path.open("w", encoding="utf-8") as f:
        f.write("=" * 100 + "\n")
        f.write("AURA v0.5.3.42 -- MEXC CRYPTO SHORT-SIDE SIGNAL RESEARCH\n")
        f.write(f"{PRELIMINARY_BANNER}\n")
        f.write("RESEARCH ONLY -- NO ORDERS PLACED -- NO NETWORK CALLS (pinned dataset)\n")
        f.write("=" * 100 + "\n\n")
        f.write("Dataset manifest (pinning / reproducibility anchor):\n")
        f.write(json.dumps(manifest, indent=2) + "\n\n")
        f.write(f"Fee/slippage assumption: fee_bps={args.fee_bps} slippage_bps={args.slippage_bps}\n")
        f.write(f"Provenance / limitation: {FEE_SLIPPAGE_PROVENANCE}\n\n")
        f.write(f"Symbols: {symbols}\n")
        f.write(f"n_boot={args.n_boot}  alpha={args.alpha}  k_folds={args.k_folds}  seed={args.seed}\n")
        f.write(f"Policy grid: trailing_stops={args.trailing_stops}  take_profits={args.take_profits}  (+ trail-only variant per trailing-stop level)\n\n")

        for verdict in ("PROMISING", "WEAK", "NOT_PROMISING", "INSUFFICIENT_DATA"):
            subset = [r for r in rows if r.get("preliminary_verdict") == verdict]
            f.write(f"{verdict}: {len(subset)}\n")
            for row in subset:
                extra = ""
                if verdict in ("PROMISING", "WEAK", "NOT_PROMISING"):
                    extra = (
                        f" | policy={row.get('chosen_policy')}"
                        f" test_mean_net={row.get('test_mean_net_return_pct')}"
                        f" test_mean_funding_cost_pct={row.get('test_mean_funding_cost_pct')}"
                        f" excess_vs_24h_hold={row.get('excess_vs_baseline')}"
                        f" win_rate={row.get('test_win_rate')}"
                        f" profit_factor={row.get('test_profit_factor')}"
                        f" max_dd={row.get('test_max_drawdown_pct')}"
                        f" bootstrap_p_positive={row.get('bootstrap_p_positive')}"
                        f" walkforward={row.get('walkforward_folds_positive')}/{row.get('walkforward_folds_considered')}"
                        f" entries_skipped_overlap={row.get('test_entries_skipped_overlap')}/{row.get('test_entries_total')}"
                    )
                f.write(f"  {row['symbol']} {row['candidate']} ({row['direction']}): {row.get('reason', '')}{extra}\n")
            f.write("\n")

        f.write("-" * 100 + "\n")
        f.write("Deferred to the post-.53 full crypto validation campaign (NOT performed here):\n")
        f.write("permutation/randomization null tests, multiple-testing correction across the\n")
        f.write("2 symbols x 2 candidates x policy-grid comparisons run above, regime-segmented\n")
        f.write("performance reporting. See module docstring.\n")

    print()
    print(f"SUMMARY: {summary_path.resolve()}")
    print(f"TRADES:  {trades_path.resolve()}")
    print(f"REPORT:  {report_path.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
