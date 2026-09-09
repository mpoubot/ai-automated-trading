#!/usr/bin/env python3
"""
Run AURA v0.5.3.23 (Execution Specification Builder) against the latest
v0.5.3.21 runtime cycle.

Reads regime_output/runtime/runtime_state.json (written by
aura_v05321_alpaca_paper_runtime.py) for the most recent cycle's directory,
then runs aura_v05323_execution_specification_builder.py against that
cycle's signal_decision.json (.13) + execution_safety.json (.19), writing
execution_specification.json into the SAME cycle directory.

This script does not decide anything itself. It only wires .21's own
established cycle boundary into .23's input. If --sizing-config is not
supplied, .23 fails closed with MISSING_QUANTITY_SOURCE for every symbol
-- no default quantity is invented here either. See
docs/AURA_V0531622_EXECUTION_SPECIFICATION_BRIDGE_2026-09-02.md for the
full rationale (direction is allowlist-only, quantity is never fabricated).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_STATE = ROOT / "regime_output" / "runtime" / "runtime_state.json"
BUILDER = ROOT / "aura_v05323_execution_specification_builder.py"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sizing-config",
        type=Path,
        default=None,
        help="Optional JSON file of {symbol: positive quantity}. Omitted by "
        "default -- every symbol then fails closed with "
        "MISSING_QUANTITY_SOURCE rather than executing with a guessed size.",
    )
    args = parser.parse_args()

    if not RUNTIME_STATE.exists():
        print(f"FAIL-CLOSED: no runtime state at {RUNTIME_STATE}", file=sys.stderr)
        return 1

    state = json.loads(RUNTIME_STATE.read_text(encoding="utf-8"))
    cycle_dir_raw = state.get("cycle_directory")
    if not cycle_dir_raw:
        print("FAIL-CLOSED: runtime_state.json has no cycle_directory", file=sys.stderr)
        return 1

    cycle_dir = Path(cycle_dir_raw)
    signal_input = cycle_dir / "signal_decision.json"
    safety_input = cycle_dir / "execution_safety.json"
    output = cycle_dir / "execution_specification.json"

    if not signal_input.exists() or not safety_input.exists():
        print(
            f"FAIL-CLOSED: missing {signal_input} or {safety_input} "
            "(did the .21 cycle complete?)",
            file=sys.stderr,
        )
        return 1

    cmd = [
        sys.executable,
        str(BUILDER),
        "--signal-input",
        str(signal_input),
        "--safety-input",
        str(safety_input),
        "--output",
        str(output),
    ]
    if args.sizing_config is not None:
        cmd += ["--sizing-config", str(args.sizing_config)]

    result = subprocess.run(cmd, cwd=ROOT)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
