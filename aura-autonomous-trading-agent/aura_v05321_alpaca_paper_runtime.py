#!/usr/bin/env python3
"""AURA v0.5.3.21 — controlled Alpaca paper runtime supervisor.

This supervisor runs the validated chain against CLOSED Alpaca market data:
.21 data -> .12 -> .13 -> .14 -> .15 -> .16 -> .17 -> .20 -> .18 -> .19
-> .25 (position sizing) -> .23 (execution specification) -> .26 (paper
order submission glue, ENTRIES ONLY).

Important: v0.5.3.16-.19 are intentionally non-authorizing contracts, and
the .25/.23/.26 tail added for Phase 5 does not change that -- .23's own
VALIDATED_LONG_ENTRY_REGIME_LABELS / VALIDATED_SHORT_ENTRY_REGIME_LABELS
remain whatever .23 itself has them set to (empty as of this change), so
every cycle still reports NO_VALIDATED_EXECUTION_DIRECTION for every
symbol until that's a separate, deliberate, reviewed decision. Even once
populated, this runtime NEVER actually submits a paper order unless BOTH:
    1. this runtime is invoked with --submit-paper (off by default), AND
    2. AURA_ALPACA_PAPER_ORDERS_ENABLED=true is set in the environment
       (.26's own, independent gate on top of .22's own identical gate).
Without --submit-paper, .25/.23/.26 still run every cycle -- so you can
see exactly what WOULD be sized/built/attempted -- but .26 only ever
reaches VALIDATED_NO_SUBMISSION, and no Alpaca order-submission
credentials are required for that path. .20 supplies deterministic
simulation evidence so the reconciliation and kill-switch path (.18/.19)
can be exercised without inventing execution; that is unrelated to and
unaffected by the .25/.23/.26 entries tail.

Exits are OUT OF SCOPE for this runtime: .24 (Phase 3's exit-policy
tracker) is not wired in here, and .26 never builds or submits a closing
order for an existing position.

Use --once for a single cycle. Use --loop-seconds for controlled recurring
paper validation. Ctrl+C stops the loop. Every cycle is archived under
regime_output/runtime/cycles/<cycle-id>/ and the latest state is persisted.
"""
from __future__ import annotations
import argparse, json, os, subprocess, sys, time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "regime_output" / "runtime"
STATE = OUT / "runtime_state.json"
DATA = ROOT / "data" / "prospective_alpaca" / "alpaca_1h_closed_bars.csv"
SCRIPTS = [
    ("12", ROOT / "aura_v05312_market_state_engine.py", "--input", DATA, "--output"),
    ("13", ROOT / "aura_v05313_signal_decision_engine.py", "--input", OUT / "market_state.json", "--output"),
    ("14", ROOT / "aura_v05314_risk_gate.py", "--input", OUT / "signal_decision.json", "--output"),
    ("15", ROOT / "aura_v05315_position_state.py", "--input", OUT / "risk_gate.json", "--output"),
    ("16", ROOT / "aura_v05316_paper_execution.py", "--input", OUT / "position_state.json", "--output"),
    ("17", ROOT / "aura_v05317_decision_execution_ledger.py", "--input", OUT / "paper_execution.json", "--output"),
]


def run(cmd: list[str]) -> None:
    print("\n$ " + " ".join(f'"{x}"' if " " in x else x for x in cmd))
    rc = subprocess.run(cmd, cwd=ROOT).returncode
    if rc != 0:
        raise RuntimeError(f"step failed with exit code {rc}")


def write_state(payload: dict) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def cycle(submit_paper: bool = False) -> int:
    ts = datetime.now(timezone.utc)
    cycle_id = ts.strftime("%Y%m%dT%H%M%SZ")
    cycle_dir = OUT / "cycles" / cycle_id
    cycle_dir.mkdir(parents=True, exist_ok=False)

    state = {"runtime_version": "AURA v0.5.3.21", "cycle_id": cycle_id,
             "started_at": ts.isoformat(), "mode": "ALPACA_PAPER_DATA_PLUS_LOCAL_SIMULATION",
             "exchange": "ALPACA", "live_execution": False, "submit_paper_requested": submit_paper,
             "status": "RUNNING"}
    write_state(state)

    try:
        run([sys.executable, str(ROOT / "aura_v05321_alpaca_market_data.py"), "--output", str(DATA)])

        # Use cycle-local artifacts as the handoff boundary. This prevents a
        # partially written output from a previous cycle becoming an input.
        outputs = {
            "12": cycle_dir / "market_state.json",
            "13": cycle_dir / "signal_decision.json",
            "14": cycle_dir / "risk_gate.json",
            "15": cycle_dir / "position_state.json",
            "16": cycle_dir / "paper_execution.json",
            "17": cycle_dir / "decision_execution_ledger.json",
            "20": cycle_dir / "observed_execution.json",
            "18": cycle_dir / "reconciliation.json",
            "19": cycle_dir / "execution_safety.json",
            "25": cycle_dir / "position_sizing.json",
            "25_sizing_config": cycle_dir / "sizing_config.json",
            "23": cycle_dir / "execution_specification.json",
            "26": cycle_dir / "paper_order_submission.json",
        }
        run([sys.executable, str(ROOT / "aura_v05312_market_state_engine.py"), "--input", str(DATA), "--output", str(outputs["12"])])
        run([sys.executable, str(ROOT / "aura_v05313_signal_decision_engine.py"), "--input", str(outputs["12"]), "--output", str(outputs["13"])])
        run([sys.executable, str(ROOT / "aura_v05314_risk_gate.py"), "--input", str(outputs["13"]), "--output", str(outputs["14"])])
        run([sys.executable, str(ROOT / "aura_v05315_position_state.py"), "--input", str(outputs["14"]), "--output", str(outputs["15"])])
        run([sys.executable, str(ROOT / "aura_v05316_paper_execution.py"), "--input", str(outputs["15"]), "--output", str(outputs["16"])])
        run([sys.executable, str(ROOT / "aura_v05317_decision_execution_ledger.py"), "--input", str(outputs["16"]), "--output", str(outputs["17"])])

        # .20 writes explicit simulated observation evidence; it never calls
        # Alpaca and never claims that a real paper position exists.
        run([sys.executable, str(ROOT / "aura_v05320_paper_fill_simulator.py"), "--input", str(outputs["17"]), "--output", str(outputs["20"])])
        run([sys.executable, str(ROOT / "aura_v05318_reconciliation.py"), "--ledger-input", str(outputs["17"]), "--observed-input", str(outputs["20"]), "--output", str(outputs["18"])])
        run([sys.executable, str(ROOT / "aura_v05319_execution_safety.py"), "--input", str(outputs["18"]), "--output", str(outputs["19"])])

        # --- Phase 5 tail: sizing -> execution spec -> submission glue. ---
        # Runs every cycle regardless of --submit-paper, so what WOULD be
        # sized/built/attempted is always visible. Only .26's own
        # --submit-paper (forwarded here) can ever lead to a real order,
        # and only if AURA_ALPACA_PAPER_ORDERS_ENABLED=true is also set --
        # .26/.22's own independent env-var gate is untouched by this flag.
        run([sys.executable, str(ROOT / "aura_v05325_position_sizing.py"),
             "--bars-input", str(DATA), "--output", str(outputs["25"]),
             "--sizing-config-output", str(outputs["25_sizing_config"])])
        run([sys.executable, str(ROOT / "aura_v05323_execution_specification_builder.py"),
             "--signal-input", str(outputs["13"]), "--safety-input", str(outputs["19"]),
             "--sizing-config", str(outputs["25_sizing_config"]), "--output", str(outputs["23"])])
        submission_cmd = [sys.executable, str(ROOT / "aura_v05326_paper_order_submission.py"),
                           "--execution-spec-input", str(outputs["23"]), "--output", str(outputs["26"]),
                           "--work-dir", str(cycle_dir / "paper_order_submission_work")]
        if submit_paper:
            submission_cmd.append("--submit-paper")
        run(submission_cmd)

        final = json.loads(outputs["19"].read_text(encoding="utf-8"))
        submission = json.loads(outputs["26"].read_text(encoding="utf-8"))
        state.update({"status": "PASS", "completed_at": datetime.now(timezone.utc).isoformat(),
                      "execution_authorized": final.get("execution_authorized", False),
                      "paper_execution_authorized": final.get("paper_execution_authorized", False),
                      "live_execution_authorized": final.get("live_execution_authorized", False),
                      "kill_switch_active": final.get("kill_switch_active", True),
                      "ready_symbol_count": submission.get("ready_symbol_count", 0),
                      "submitted_count": submission.get("submitted_count", 0),
                      "validated_only_count": submission.get("validated_only_count", 0),
                      "cycle_directory": str(cycle_dir)})
        write_state(state)
        print("\nAURA v0.5.3.21 CYCLE: PASS")
        print("ALPACA MARKET DATA: INGESTED")
        print("PAPER EXECUTION (.16 chain): SIMULATED ONLY")
        print(f"ENTRIES READY / SUBMITTED / VALIDATED-ONLY: "
              f"{submission.get('ready_symbol_count', 0)} / "
              f"{submission.get('submitted_count', 0)} / "
              f"{submission.get('validated_only_count', 0)}")
        print(f"SUBMIT-PAPER REQUESTED: {submit_paper}")
        print("LIVE EXECUTION: FALSE")
        print(f"CYCLE: {cycle_dir}")
        return 0
    except Exception as exc:
        state.update({"status": "FAIL_CLOSED", "completed_at": datetime.now(timezone.utc).isoformat(),
                      "execution_authorized": False, "paper_execution_authorized": False,
                      "live_execution_authorized": False, "error": f"{type(exc).__name__}: {exc}",
                      "cycle_directory": str(cycle_dir)})
        write_state(state)
        print(f"\nFAIL-CLOSED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--once", action="store_true")
    p.add_argument("--loop-seconds", type=int, default=0,
                   help="Recurring interval; 0 means one cycle. Minimum 60 seconds.")
    p.add_argument("--submit-paper", action="store_true",
                   help="Forward --submit-paper to .26 each cycle. Still requires "
                        "AURA_ALPACA_PAPER_ORDERS_ENABLED=true (.26/.22's own "
                        "independent gate) before any order is actually submitted. "
                        "Off by default: without this flag, entries are sized and "
                        "built every cycle but only ever reach VALIDATED_NO_SUBMISSION.")
    args = p.parse_args()
    if args.loop_seconds and args.loop_seconds < 60:
        p.error("--loop-seconds must be >= 60")
    if args.once:
        return cycle(args.submit_paper)
    if not args.loop_seconds:
        return cycle(args.submit_paper)
    while True:
        rc = cycle(args.submit_paper)
        if rc != 0:
            return rc
        time.sleep(args.loop_seconds)

if __name__ == "__main__":
    raise SystemExit(main())
