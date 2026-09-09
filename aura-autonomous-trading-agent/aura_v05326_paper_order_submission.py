#!/usr/bin/env python3
"""
AURA v0.5.3.26 — Paper Order Submission Glue (Phase 5, entries only)

Purpose
--------
v0.5.3.23 (Execution Specification Builder) produces one fully-formed,
per-symbol execution specification for every symbol it judges
EXECUTION_SPEC_READY. v0.5.3.22 (the isolated Alpaca PAPER execution
adapter) accepts exactly ONE such specification per invocation and,
depending on --submit-paper and the AURA_ALPACA_PAPER_ORDERS_ENABLED
environment variable, either validates it only or actually submits it to
Alpaca's paper endpoint. Nothing before this module turns .23's
multi-symbol output into the one-call-per-symbol shape .22 expects. This
module is that glue -- nothing more.

Scope (Martin's explicit choice, confirmed before building)
-------------------------------------------------------------
- ENTRIES ONLY. This module never looks at v0.5.3.24's (Phase 3) exit
  decisions and never builds or submits a closing/SELL order for an
  existing position. That remains out of scope until Martin asks for it.
- v0.5.3.23's VALIDATED_LONG_ENTRY_REGIME_LABELS /
  VALIDATED_SHORT_ENTRY_REGIME_LABELS allowlists are UNTOUCHED by this
  module and remain whatever v0.5.3.23 itself has them set to (empty by
  default, as of this module's introduction). This module will forward
  whatever .23 already decided; it does not, and cannot, grant a symbol
  a direction .23 didn't already validate.
- OFF BY DEFAULT. Real submission requires --submit-paper on THIS
  module (explicit, not inherited from any config), which is forwarded
  to v0.5.3.22 for that call only. v0.5.3.22's own independent gate
  (AURA_ALPACA_PAPER_ORDERS_ENABLED=true) still applies unchanged on top
  of that -- two explicit, separate switches, neither one alone enough.
  Without --submit-paper, every ready symbol is still built, still sent
  through v0.5.3.22, but only ever reaches VALIDATED_NO_SUBMISSION --
  nothing is submitted, and no Alpaca credentials are even required for
  that path (v0.5.3.22 itself never touches the network in validate-only
  mode).

This layer deliberately does NOT:
- decide direction, risk, or quantity -- those remain entirely
  v0.5.3.13/.14/.19/.23's job; this module only relays what .23 already
  decided was EXECUTION_SPEC_READY;
- reimplement v0.5.3.22's own validation, idempotency, or submission
  logic -- it is invoked as a subprocess, unmodified, exactly as the
  runtime supervisor already invokes every other numbered stage;
- retry a failed adapter call, suppress its error, or invent a result
  when the adapter process itself fails to produce output -- that
  symbol is reported ADAPTER_FAILED with the adapter's own stderr
  attached, never silently dropped;
- touch anything BLOCKED by .23 -- a symbol .23 didn't mark
  EXECUTION_SPEC_READY is never submitted, never even built into a
  request.

Default input:
    regime_output/execution_specification/execution_specification.json  (v0.5.3.23)

Default output:
    regime_output/paper_order_submission/paper_order_submission.json

Per-symbol adapter input/output files are written under
regime_output/paper_order_submission/work/<symbol>/, one pair per
symbol, so every v0.5.3.22 invocation and its raw response stays on disk
for audit.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent

VERSION = "AURA v0.5.3.26"
EXPECTED_EXEC_SPEC_VERSION = "AURA v0.5.3.23"
EXPECTED_EXEC_SPEC_ENGINE = "EXECUTION_SPECIFICATION_BUILDER"

DEFAULT_EXECUTION_SPEC_INPUT = Path(
    r"regime_output\execution_specification\execution_specification.json"
)
DEFAULT_OUTPUT = Path(
    r"regime_output\paper_order_submission\paper_order_submission.json"
)
DEFAULT_WORK_DIR = Path(r"regime_output\paper_order_submission\work")
DEFAULT_ADAPTER_SCRIPT = ROOT / "aura_v05322_alpaca_paper_execution_adapter.py"


def stable_json(obj: Any) -> str:
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Input file not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        payload = json.load(f)
    if not isinstance(payload, dict):
        raise ValueError("Input JSON must contain an object at the top level.")
    return payload


def ready_specs(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Extracts {symbol: execution_specification} for every symbol
    v0.5.3.23 marked EXECUTION_SPEC_READY. Everything else (BLOCKED,
    missing, malformed) is excluded -- never guessed at, never
    submitted."""
    decisions = payload.get("decisions")
    if not isinstance(decisions, dict):
        return {}

    result: dict[str, dict[str, Any]] = {}
    for symbol, item in decisions.items():
        if not isinstance(item, dict):
            continue
        if item.get("status") != "EXECUTION_SPEC_READY":
            continue
        spec = item.get("execution_specification")
        if isinstance(spec, dict):
            result[symbol] = spec
    return result


def symbol_work_key(symbol: str) -> str:
    """Filesystem-safe key for a symbol like 'BTC/USD' -> 'BTC_USD'."""
    return symbol.replace("/", "_")


def submit_one(
    adapter_script: Path,
    spec: dict[str, Any],
    work_dir: Path,
    symbol: str,
    submit_paper: bool,
) -> dict[str, Any]:
    """The only function here that shells out. Invokes v0.5.3.22
    unmodified, one symbol per call. Real Alpaca submission additionally
    requires v0.5.3.22's own AURA_ALPACA_PAPER_ORDERS_ENABLED gate --
    submit_paper alone is not enough, by design."""
    key = symbol_work_key(symbol)
    symbol_dir = work_dir / key
    symbol_dir.mkdir(parents=True, exist_ok=True)
    input_path = symbol_dir / "spec.json"
    output_path = symbol_dir / "result.json"
    input_path.write_text(json.dumps(spec, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    cmd = [
        sys.executable, str(adapter_script),
        "--input", str(input_path),
        "--output", str(output_path),
    ]
    if submit_paper:
        cmd.append("--submit-paper")

    proc = subprocess.run(cmd, capture_output=True, text=True)

    if output_path.exists():
        try:
            return json.loads(output_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            return {
                "adapter_version": None,
                "status": "ADAPTER_OUTPUT_UNREADABLE",
                "symbol": symbol,
                "returncode": proc.returncode,
                "error": f"{type(exc).__name__}: {exc}",
                "stderr": proc.stderr[-2000:],
            }

    return {
        "adapter_version": None,
        "status": "ADAPTER_FAILED",
        "symbol": symbol,
        "returncode": proc.returncode,
        "stderr": proc.stderr[-2000:],
    }


def base_result(execution_spec_path: Path, submit_paper: bool) -> dict[str, Any]:
    return {
        "agent_version": VERSION,
        "engine": "PAPER_ORDER_SUBMISSION",
        "decision_status": "BLOCKED",
        "generated_from_execution_specification": str(execution_spec_path.resolve()),
        "submit_paper_requested": submit_paper,
        "ready_symbol_count": 0,
        "submitted_count": 0,
        "validated_only_count": 0,
        "failed_count": 0,
        "results": {},
        "blocked_reasons": [],
        "state_hash": None,
        "state_id": None,
        "guardrails": {
            "scope": "ENTRIES_ONLY",
            "exit_orders_submitted": False,
            "direction_decided_here": False,
            "risk_decided_here": False,
            "quantity_decided_here": False,
            "adapter_reimplemented": False,
            "adapter_version_required": "AURA v0.5.3.22",
            "submit_paper_forwarded": submit_paper,
            "submission_requires_adapter_env_gate": True,
            "live_execution": False,
            "fail_closed": True,
        },
    }


def finalize(result: dict[str, Any]) -> dict[str, Any]:
    canonical = {
        "agent_version": result["agent_version"],
        "engine": result["engine"],
        "decision_status": result["decision_status"],
        "submit_paper_requested": result["submit_paper_requested"],
        "results": result["results"],
        "blocked_reasons": result["blocked_reasons"],
    }
    result["state_hash"] = sha256_text(stable_json(canonical))
    result["state_id"] = f"POS-{result['state_hash'][:24]}"
    return result


def build_and_submit(
    execution_spec: dict[str, Any],
    execution_spec_path: Path,
    adapter_script: Path,
    work_dir: Path,
    submit_paper: bool,
) -> dict[str, Any]:
    result = base_result(execution_spec_path, submit_paper)

    specs = ready_specs(execution_spec)
    result["ready_symbol_count"] = len(specs)

    if not specs:
        result["decision_status"] = "DECIDED"
        result["blocked_reasons"] = []
        return finalize(result)

    result["decision_status"] = "DECIDED"
    submitted = validated_only = failed = 0

    for symbol in sorted(specs):
        spec = specs[symbol]
        outcome = submit_one(adapter_script, spec, work_dir, symbol, submit_paper)
        result["results"][symbol] = outcome
        status = outcome.get("status")
        if status in {"SUBMITTED", "ALREADY_EXISTS"}:
            submitted += 1
        elif status == "VALIDATED_NO_SUBMISSION":
            validated_only += 1
        else:
            failed += 1

    result["submitted_count"] = submitted
    result["validated_only_count"] = validated_only
    result["failed_count"] = failed

    return finalize(result)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False, allow_nan=False)
        f.write("\n")


def print_report(result: dict[str, Any], output_path: Path) -> None:
    print("=" * 96)
    print(f"{VERSION} — PAPER ORDER SUBMISSION (Phase 5, entries only)")
    print("=" * 96)
    print()
    print(f"SUBMIT-PAPER REQUESTED : {result['submit_paper_requested']}")
    print("ADAPTER ENV GATE       : AURA_ALPACA_PAPER_ORDERS_ENABLED (independent, unchanged)")
    print("EXIT ORDERS            : OUT OF SCOPE (entries only)")
    print("LIVE EXECUTION         : DISABLED")
    print()
    print("SUMMARY")
    print("-" * 96)
    print(f"STATUS                 : {result['decision_status']}")
    print(f"READY SYMBOLS          : {result['ready_symbol_count']}")
    print(f"SUBMITTED              : {result['submitted_count']}")
    print(f"VALIDATED ONLY         : {result['validated_only_count']}")
    print(f"FAILED                 : {result['failed_count']}")
    print(f"STATE ID               : {result['state_id']}")
    print()

    for symbol, outcome in sorted(result["results"].items()):
        print(symbol)
        print(f"  STATUS             : {outcome.get('status')}")
        if outcome.get("broker_order_id"):
            print(f"  BROKER ORDER ID    : {outcome.get('broker_order_id')}")
        if outcome.get("client_order_id"):
            print(f"  CLIENT ORDER ID    : {outcome.get('client_order_id')}")
        if outcome.get("stderr"):
            print(f"  STDERR (tail)      : {outcome['stderr'][-300:]}")
        print()

    if result["blocked_reasons"]:
        print("FAIL-CLOSED REASONS")
        print("-" * 96)
        for reason in result["blocked_reasons"]:
            print(f"  - {reason}")
        print()

    print(f"OUTPUT                 : {output_path.resolve()}")
    print("=" * 96)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execution-spec-input", type=Path, default=DEFAULT_EXECUTION_SPEC_INPUT
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--work-dir", type=Path, default=DEFAULT_WORK_DIR)
    parser.add_argument("--adapter-script", type=Path, default=DEFAULT_ADAPTER_SCRIPT)
    parser.add_argument(
        "--submit-paper",
        action="store_true",
        help=(
            "Forward --submit-paper to v0.5.3.22 for each ready symbol. "
            "v0.5.3.22's own AURA_ALPACA_PAPER_ORDERS_ENABLED=true env var "
            "is STILL required on top of this -- omit either one and every "
            "symbol stays VALIDATED_NO_SUBMISSION."
        ),
    )
    args = parser.parse_args()

    try:
        execution_spec = load_json(args.execution_spec_input)
        result = build_and_submit(
            execution_spec,
            args.execution_spec_input,
            args.adapter_script,
            args.work_dir,
            args.submit_paper,
        )
        write_json(args.output, result)
        print_report(result, args.output)
        return 0

    except FileNotFoundError as exc:
        result = base_result(args.execution_spec_input, args.submit_paper)
        result["blocked_reasons"] = [f"MISSING_INPUT_SOURCE:{exc}"]
        result = finalize(result)
        try:
            write_json(args.output, result)
            print_report(result, args.output)
        except Exception:
            pass
        print(f"FAIL-CLOSED: {exc}", file=sys.stderr)
        return 1

    except Exception as exc:
        result = base_result(args.execution_spec_input, args.submit_paper)
        result["blocked_reasons"] = [f"UNEXPECTED_ENGINE_ERROR:{type(exc).__name__}"]
        result = finalize(result)
        try:
            write_json(args.output, result)
            print_report(result, args.output)
        except Exception:
            pass
        print(f"FAIL-CLOSED ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
