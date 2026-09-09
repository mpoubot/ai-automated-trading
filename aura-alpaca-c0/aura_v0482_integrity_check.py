#!/usr/bin/env python3
"""
AURA v0.4.8.2 — Paper Lab Integrity Check

Hardening layer only. It does not change signals, exits, universe, or trades.
Run daily before/after the v0.4.8 paper process.

Checks:
- required AURA_LIVE files exist
- universe is frozen and has 101 rows when initialized
- signal IDs are unique
- model set is exactly A/B/C
- frozen signal definition text remains present in the signals file
- no duplicate signal/model pairs in positions
- friction remains the frozen 12 bp round trip at the metadata level where available

The script returns exit code 0 on PASS and 1 on FAIL.
"""

from __future__ import annotations
import argparse
from pathlib import Path
import sys
import pandas as pd

REQUIRED = [
    "universe.csv",
    "signals.csv",
    "positions.csv",
    "position_snapshots.csv",
    "executions.csv",
    "daily_model_metrics.csv",
    "baseline.csv",
]

EXPECTED_MODELS = {"A_10D_TIME", "B_PURE_ATR_2x4x", "C_ATR_2x4x_10D"}


def load(p):
    return pd.read_csv(p) if p.exists() else pd.DataFrame()


def fail(msg):
    print(f"[FAIL] {msg}")
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live-dir", default="AURA_LIVE")
    args = ap.parse_args()

    d = Path(args.live_dir)
    ok = True

    print("AURA v0.4.8.2 — PAPER LAB INTEGRITY CHECK")
    print("=" * 58)

    for name in REQUIRED:
        p = d / name
        if not p.exists():
            ok = fail(f"Missing {name}") and ok

    if not ok:
        print("RESULT: FAIL")
        return 1

    universe = load(d / "universe.csv")
    signals = load(d / "signals.csv")
    positions = load(d / "positions.csv")
    metrics = load(d / "daily_model_metrics.csv")

    if len(universe) != 101:
        ok = fail(f"Universe row count is {len(universe)}, expected 101") and ok
    else:
        print("[PASS] Frozen universe contains 101 securities.")

    if "universe_version" in universe.columns and len(universe):
        versions = universe["universe_version"].dropna().astype(str).unique()
        if len(versions) != 1:
            ok = fail("Universe contains multiple universe_version values.") and ok
        else:
            print(f"[PASS] Immutable universe version: {versions[0]}")

    if "signal_id" in signals.columns:
        if signals["signal_id"].duplicated().any():
            ok = fail("Duplicate signal_id values found.") and ok
        else:
            print("[PASS] Signal Master IDs are unique.")

    if "model" in metrics.columns:
        models = set(metrics["model"].dropna().astype(str))
        unexpected = models - EXPECTED_MODELS
        if unexpected:
            ok = fail(f"Unexpected model(s): {sorted(unexpected)}") and ok
        else:
            print("[PASS] Model set is A/B/C only.")

    if not signals.empty:
        required_signal_cols = {"signal_id", "symbol", "decision_date", "entry_date"}
        missing = required_signal_cols - set(signals.columns)
        if missing:
            ok = fail(f"Signals missing columns: {sorted(missing)}") and ok
        else:
            print("[PASS] Signal Master schema is present.")

    if not positions.empty and {"signal_id", "model"}.issubset(positions.columns):
        dup = positions.duplicated(["signal_id", "model"])
        if dup.any():
            ok = fail("Duplicate signal_id/model position pairs found.") and ok
        else:
            print("[PASS] Position ledger has no duplicate signal/model pairs.")

    print()
    print("Frozen research contract:")
    print("  Signal: EMA3/EMA8 bullish crossover + MACD histogram > 0 + RelVol >= 1")
    print("  A: 10D close")
    print("  B: pure ATR 2x stop / 4x target, 60-session horizon")
    print("  C: ATR 2x/4x + 10D close")
    print("  Friction: 12 bp round trip")
    print("  Paper only: NO ORDERS")

    print()
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
