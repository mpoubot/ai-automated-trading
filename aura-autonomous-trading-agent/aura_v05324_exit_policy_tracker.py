#!/usr/bin/env python3
"""
AURA v0.5.3.24 — Exit-Policy Tracker (Phase 3 of the agreed 5-phase plan)

LOCKED ARCHITECTURE
--------------------
Consumes ONLY:
    1. the authenticated v0.5.3.18 Reconciliation output; and
    2. the same already-fetched 1H closed-bars CSV that v0.5.3.12 already
       reads (data/prospective_alpaca/alpaca_1h_closed_bars.csv); and
    3. an explicit, versioned --policy-config JSON file supplied by a
       human, never inferred.

This layer deliberately does NOT:
- fetch market data itself (it reads the same bars file .12 already read
  this cycle -- no independent network call, no new data source);
- calculate indicators, recalculate signals, or recalculate risk;
- size or create a position;
- place, cancel, or modify any order;
- call MEXC, Alpaca, or any exchange;
- fabricate a fill, a position, or a price;
- treat anything short of .18's own RECONCILIATION_STATE ==
  "RECONCILED_EXECUTION" (with position_exists == True) as an open
  position -- PENDING / EXECUTION_NOT_FILLED / NO_ORDER_CONFIRMED /
  RECONCILIATION_CONFLICT / BLOCKED / UNVERIFIED are all "nothing to
  track here yet", never guessed at;
- infer a trailing-stop percent, take-profit percent, max-hold-bars cap,
  or trade direction from a regime label or from this module's own
  judgment. Every one of those must be explicit in --policy-config for a
  given symbol, or that symbol's open position is reported
  POLICY_MISSING and left untouched -- fail closed, never a fabricated
  default. This mirrors v0.5.3.23's own VALIDATED_LONG_ENTRY_REGIME_LABELS
  / VALIDATED_SHORT_ENTRY_REGIME_LABELS pattern (both empty by default,
  populated only by a reviewed human decision) applied to exit policy
  instead of entry direction;
- persist any incremental state (a running stop level, bars-held counter,
  etc.) across cycles. Every run recomputes the full picture from
  scratch -- see "Why no persisted state" below.

Why this exists
----------------
v0.5.3.15 (Position State) and v0.5.3.16 (Paper Execution) both say so in
their own docstrings: neither one tracks OPEN/CLOSED/EXIT once a paper
order intent has been recorded. v0.5.3.18 (Reconciliation) confirms
whether that intent was actually observed-filled, but even a confirmed
fill has no exit management anywhere in v0.5.3.12-.19 -- nothing in the
existing chain ever decides "this open position should now be closed".
This module is that missing decision layer. It never places the closing
order itself (that remains Phase 5's job, wiring v0.5.3.22's isolated
paper adapter); it only computes and reports HOLD or EXIT for each
confirmed-open position, for the next cycle (a human, or a future Phase 5
supervisor step) to act on.

Reused, not reimplemented: the exact trailing-stop / take-profit bar-walk
------------------------------------------------------------------------
simulate_trade() is imported directly (via importlib, matching this
repo's existing convention for cross-file reuse of research scripts) from
aura_exit_policy_backtest.py -- the Phase 2 backtest already validated by
its own test suite. This module never reimplements the stop/target math;
it calls the identical function every cycle with whatever bars have been
fetched so far.

Why no persisted state
-----------------------
Because simulate_trade is a pure function of (bars, entry, policy), and
this module reruns it from entry_ts on every cycle rather than storing a
running stop level anywhere, this module's output can never drift from
what the same math would say given the same inputs -- there is no
mutable tracker state file that could go stale, get corrupted, or
disagree with a fresh recomputation. The cost is a little repeated
computation per cycle (one bar-walk from entry to now); that trade is
made deliberately, in favour of the same "single source of truth, no
mutable authority" discipline used everywhere else in this chain.

Live-tracking adaptation of simulate_trade's TIMEOUT branch
-------------------------------------------------------------
simulate_trade was built for the Phase 2 backtest, where the full
post-entry bar window is already known. Its TIMEOUT branch fires
whenever it runs out of bars to walk -- from inside that function this
is indistinguishable between "the policy's own max_hold_bars limit was
reached" (a real exit) and "only a few bars have been fetched since
entry so far" (not a real exit, just an incomplete live window). This
module tells the two apart itself: it compares the number of bars
actually available since entry against the policy's own max_hold_bars
BEFORE trusting a TIMEOUT verdict. Fewer bars available than the policy
allows downgrades TIMEOUT to HOLD (reason
INSUFFICIENT_BARS_FOR_TIMEOUT_VERDICT). STOP and TARGET verdicts are
trusted immediately in either case, since those are real, already-
observed price levels crossed by real fetched bars, never an artifact of
an incomplete window.

Default inputs:
    regime_output/reconciliation/reconciliation.json   (v0.5.3.18)
    data/prospective_alpaca/alpaca_1h_closed_bars.csv   (same file v0.5.3.12 reads)

--policy-config has no default and no baked-in fallback. A symbol with a
confirmed open position and no matching --policy-config entry is reported
POLICY_MISSING, not silently left out of the report and not given an
assumed policy.

Default output:
    regime_output/exit_policy_tracker/exit_policy_tracker.json
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parent


def _load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# Reuse Phase 2's exact stop/target bar-walk -- see module docstring.
EPB = _load_module("aura_exit_policy_backtest", "aura_exit_policy_backtest.py")
simulate_trade = EPB.simulate_trade

VERSION = "AURA v0.5.3.24"
EXPECTED_RECONCILIATION_VERSION = "AURA v0.5.3.18"
EXPECTED_RECONCILIATION_ENGINE = "RECONCILIATION"

DEFAULT_RECONCILIATION_INPUT = Path(
    r"regime_output\reconciliation\reconciliation.json"
)
DEFAULT_BARS_INPUT = Path(r"data\prospective_alpaca\alpaca_1h_closed_bars.csv")
DEFAULT_OUTPUT = Path(
    r"regime_output\exit_policy_tracker\exit_policy_tracker.json"
)

REQUIRED_BAR_COLUMNS = {"timestamp", "symbol", "open", "high", "low", "close", "volume"}
PRICE_COLUMNS = ("open", "high", "low", "close")
ALLOWED_DIRECTIONS = {"LONG", "SHORT"}


def stable_json(obj: Any) -> str:
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def iso(value: Any) -> str | None:
    if value is None:
        return None
    try:
        ts = pd.Timestamp(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(ts):
        return None
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    return ts.isoformat().replace("+00:00", "Z")


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Input file not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        payload = json.load(f)
    if not isinstance(payload, dict):
        raise ValueError("Input JSON must contain an object at the top level.")
    return payload


def load_bars(path: Path) -> pd.DataFrame:
    """Same loading logic as v0.5.3.12's load_input(), applied to the same
    file, so bar dtypes/timezone handling never diverge between the two
    consumers of this one CSV."""
    if not path.exists():
        raise FileNotFoundError(f"Bars input not found: {path}")

    df = pd.read_csv(path)
    missing = sorted(REQUIRED_BAR_COLUMNS - set(df.columns))
    if missing:
        raise ValueError("Missing required bar columns: " + ", ".join(missing))

    df = df.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    df["symbol"] = df["symbol"].astype(str).str.strip()
    for col in PRICE_COLUMNS + ("volume",):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def load_policy_config(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    payload = load_json(path)
    return payload


def policy_errors_for_symbol(symbol: str, policy: Any) -> list[str]:
    """Structural validity check for one symbol's policy-config entry.
    No value is defaulted or inferred here -- every required field must
    be explicitly present and valid, or the symbol is POLICY_MISSING."""
    if not isinstance(policy, dict):
        return [f"POLICY_MISSING:{symbol}"]

    errors: list[str] = []

    direction = policy.get("direction")
    if direction not in ALLOWED_DIRECTIONS:
        errors.append(f"POLICY_INVALID_DIRECTION:{symbol}")

    trailing_stop_pct = policy.get("trailing_stop_pct")
    if (
        not isinstance(trailing_stop_pct, (int, float))
        or isinstance(trailing_stop_pct, bool)
        or trailing_stop_pct <= 0
    ):
        errors.append(f"POLICY_INVALID_TRAILING_STOP:{symbol}")

    take_profit_pct = policy.get("take_profit_pct")
    if take_profit_pct is not None and (
        not isinstance(take_profit_pct, (int, float))
        or isinstance(take_profit_pct, bool)
        or take_profit_pct <= 0
    ):
        errors.append(f"POLICY_INVALID_TAKE_PROFIT:{symbol}")

    max_hold_bars = policy.get("max_hold_bars")
    if (
        not isinstance(max_hold_bars, int)
        or isinstance(max_hold_bars, bool)
        or max_hold_bars <= 0
    ):
        errors.append(f"POLICY_INVALID_MAX_HOLD_BARS:{symbol}")

    cost_pct = policy.get("cost_pct", 0.0)
    if (
        not isinstance(cost_pct, (int, float))
        or isinstance(cost_pct, bool)
        or cost_pct < 0
    ):
        errors.append(f"POLICY_INVALID_COST_PCT:{symbol}")

    return errors


def canonical_reconciliation_hash(payload: dict[str, Any]) -> str:
    """Mirrors v0.5.3.18's own finalize() canonical dict exactly, so this
    module verifies the SAME state_hash chain .18 published rather than
    trusting an unverified copy."""
    canonical = {
        "agent_version": payload.get("agent_version"),
        "engine": payload.get("engine"),
        "decision_status": payload.get("decision_status"),
        "overall_reconciliation": payload.get("overall_reconciliation"),
        "input_ledger_hash": payload.get("input_ledger_hash"),
        "input_observed_execution_hash": payload.get("input_observed_execution_hash"),
        "upstream_state_id": payload.get("upstream_state_id"),
        "upstream_hash_verified": payload.get("upstream_hash_verified"),
        "frozen_configuration_verified": payload.get("frozen_configuration_verified"),
        "observed_execution_verified": payload.get("observed_execution_verified"),
        "decisions": payload.get("decisions"),
        "blocked_reasons": payload.get("blocked_reasons"),
    }
    return sha256_text(stable_json(canonical))


def verify_reconciliation(payload: dict[str, Any]) -> tuple[bool, list[str]]:
    errors: list[str] = []

    if payload.get("engine") != EXPECTED_RECONCILIATION_ENGINE:
        errors.append("WRONG_UPSTREAM_ENGINE")
    if payload.get("agent_version") != EXPECTED_RECONCILIATION_VERSION:
        errors.append("WRONG_UPSTREAM_VERSION")
    if payload.get("decision_status") != "DECIDED":
        errors.append("UPSTREAM_NOT_DECIDED")

    supplied_hash = payload.get("state_hash")
    if not isinstance(supplied_hash, str) or not supplied_hash:
        errors.append("MISSING_UPSTREAM_STATE_HASH")
    else:
        try:
            calculated = canonical_reconciliation_hash(payload)
        except (TypeError, ValueError):
            errors.append("UPSTREAM_STATE_NOT_HASHABLE")
        else:
            if calculated != supplied_hash:
                errors.append("UPSTREAM_STATE_HASH_MISMATCH")
        if payload.get("state_id") != f"RC-{supplied_hash[:24]}":
            errors.append("UPSTREAM_STATE_ID_MISMATCH")

    guardrails = payload.get("guardrails")
    if not isinstance(guardrails, dict):
        errors.append("MISSING_UPSTREAM_GUARDRAILS")
    else:
        if guardrails.get("reconciliation_only") is not True:
            errors.append("UPSTREAM_RECONCILIATION_ONLY_NOT_ENFORCED")
        if guardrails.get("fail_closed") is not True:
            errors.append("UPSTREAM_FAIL_CLOSED_NOT_ENFORCED")
        if guardrails.get("fabricated_fill") is not False:
            errors.append("UPSTREAM_FABRICATED_FILL_GUARDRAIL_VIOLATION")
        if guardrails.get("fabricated_position") is not False:
            errors.append("UPSTREAM_FABRICATED_POSITION_GUARDRAIL_VIOLATION")

    return len(errors) == 0, sorted(set(errors))


def open_positions_from_reconciliation(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The only symbols this module will ever consider tracking:
    reconciliation_state == RECONCILED_EXECUTION AND position_exists is
    True, with a well-formed position_id/fill_price/fill_timestamp.
    Everything else (PENDING, EXECUTION_NOT_FILLED, NO_ORDER_CONFIRMED,
    RECONCILIATION_CONFLICT, BLOCKED, UNVERIFIED, or a malformed entry)
    is silently excluded -- "nothing confirmed open here", never guessed
    at."""
    decisions = payload.get("decisions")
    if not isinstance(decisions, dict):
        return {}

    open_positions: dict[str, dict[str, Any]] = {}
    for symbol, item in decisions.items():
        if not isinstance(item, dict):
            continue
        if item.get("reconciliation_state") != "RECONCILED_EXECUTION":
            continue
        if item.get("position_exists") is not True:
            continue

        position_id = item.get("position_id")
        fill_price = item.get("fill_price")
        fill_timestamp = item.get("fill_timestamp")

        if not isinstance(position_id, str) or not position_id:
            continue
        if (
            not isinstance(fill_price, (int, float))
            or isinstance(fill_price, bool)
            or fill_price <= 0
        ):
            continue
        if not isinstance(fill_timestamp, str) or not fill_timestamp:
            continue

        open_positions[symbol] = {
            "position_id": position_id,
            "entry_price": float(fill_price),
            "entry_timestamp": fill_timestamp,
        }

    return open_positions


def evaluate_position(
    bars: pd.DataFrame,
    symbol: str,
    entry_ts: pd.Timestamp,
    entry_price: float,
    direction: str,
    trailing_stop_pct: float,
    take_profit_pct: float | None,
    max_hold_bars: int,
    cost_pct: float,
) -> dict[str, Any]:
    """Evaluate ONE confirmed-open position against its explicit policy.
    See the module docstring for why no state is persisted across calls
    and how the live TIMEOUT-vs-HOLD distinction is made."""
    g_full = (
        bars[bars["symbol"] == symbol]
        .dropna(subset=["timestamp"])
        .sort_values("timestamp")
        .reset_index(drop=True)
    )
    if g_full.empty:
        return {
            "tracked_decision": "HOLD",
            "exit_reason": None,
            "reason": "NO_BARS_FOR_SYMBOL",
            "bars_available_since_entry": 0,
            "bars_held": None,
            "exit_ts": None,
            "gross_return_pct": None,
            "net_return_pct": None,
            "mfe_pct": None,
            "mae_pct": None,
        }

    pos = int(g_full["timestamp"].searchsorted(entry_ts, side="right"))
    bars_available = max(0, len(g_full) - pos)

    result = simulate_trade(
        g_full,
        entry_ts,
        entry_price,
        direction,
        trailing_stop_pct,
        take_profit_pct,
        max_hold_bars,
        cost_pct,
    )
    exit_reason = result["exit_reason"]

    if exit_reason == "NO_DATA_AFTER_ENTRY":
        decision, reason, exit_reason = "HOLD", "NO_BARS_AFTER_ENTRY_YET", None
    elif exit_reason == "TIMEOUT" and bars_available < max_hold_bars:
        # Ran out of FETCHED bars, not out of the policy's own hold window
        # -- not a real timeout yet. See module docstring.
        decision, reason, exit_reason = "HOLD", "INSUFFICIENT_BARS_FOR_TIMEOUT_VERDICT", None
    elif exit_reason in {"STOP", "TARGET"}:
        decision, reason = "EXIT", f"{exit_reason}_HIT"
    else:  # TIMEOUT with bars_available >= max_hold_bars: a real policy timeout
        decision, reason = "EXIT", "MAX_HOLD_BARS_REACHED"

    return {
        "tracked_decision": decision,
        "exit_reason": exit_reason,
        "reason": reason,
        "bars_available_since_entry": bars_available,
        "bars_held": result.get("bars_held"),
        "exit_ts": iso(result.get("exit_ts")) if decision == "EXIT" else None,
        "gross_return_pct": result.get("gross_return_pct") if decision == "EXIT" else None,
        "net_return_pct": result.get("net_return_pct") if decision == "EXIT" else None,
        "mfe_pct": result.get("mfe_pct"),
        "mae_pct": result.get("mae_pct"),
    }


def base_result(
    reconciliation_path: Path, bars_path: Path, policy_config_path: Path | None
) -> dict[str, Any]:
    return {
        "agent_version": VERSION,
        "engine": "EXIT_POLICY_TRACKER",
        "decision_status": "BLOCKED",
        "generated_from_reconciliation": str(reconciliation_path.resolve()),
        "generated_from_bars": str(bars_path.resolve()),
        "generated_from_policy_config": (
            str(policy_config_path.resolve()) if policy_config_path else None
        ),
        "input_reconciliation_hash": None,
        "upstream_state_id": None,
        "upstream_hash_verified": False,
        "open_position_count": 0,
        "decisions": {},
        "blocked_reasons": [],
        "state_hash": None,
        "state_id": None,
        "guardrails": {
            "single_source_of_truth": True,
            "source_engine": EXPECTED_RECONCILIATION_ENGINE,
            "market_data_fetch": False,
            "indicator_recalculation": False,
            "signal_recalculation": False,
            "risk_recalculation": False,
            "position_sizing": False,
            "position_creation": False,
            "position_creation_from_observation": False,
            "exchange_state_mutation": False,
            "orders_allowed": False,
            "paper_execution": False,
            "live_execution": False,
            "exit_policy_evaluation_only": True,
            "policy_source": "explicit_config_only",
            "direction_inferred": False,
            "state_persisted_across_cycles": False,
            "fabricated_fill": False,
            "fabricated_position": False,
            "fail_closed": True,
        },
    }


def finalize(result: dict[str, Any]) -> dict[str, Any]:
    canonical = {
        "agent_version": result["agent_version"],
        "engine": result["engine"],
        "decision_status": result["decision_status"],
        "input_reconciliation_hash": result["input_reconciliation_hash"],
        "upstream_state_id": result["upstream_state_id"],
        "upstream_hash_verified": result["upstream_hash_verified"],
        "decisions": result["decisions"],
        "blocked_reasons": result["blocked_reasons"],
    }
    result["state_hash"] = sha256_text(stable_json(canonical))
    result["state_id"] = f"EPT-{result['state_hash'][:24]}"
    return result


def build_tracker(
    reconciliation: dict[str, Any],
    bars: pd.DataFrame,
    policy_config: dict[str, Any],
    reconciliation_path: Path,
    bars_path: Path,
    policy_config_path: Path | None,
) -> dict[str, Any]:
    result = base_result(reconciliation_path, bars_path, policy_config_path)
    result["input_reconciliation_hash"] = reconciliation.get("state_hash")
    result["upstream_state_id"] = reconciliation.get("state_id")

    ok, errors = verify_reconciliation(reconciliation)
    result["upstream_hash_verified"] = ok
    if not ok:
        result["blocked_reasons"] = errors
        return finalize(result)

    open_positions = open_positions_from_reconciliation(reconciliation)
    result["decision_status"] = "DECIDED"
    result["open_position_count"] = len(open_positions)

    for symbol, pos in sorted(open_positions.items()):
        policy = policy_config.get(symbol)
        p_errors = policy_errors_for_symbol(symbol, policy)

        if p_errors:
            result["decisions"][symbol] = {
                "symbol": symbol,
                "position_id": pos["position_id"],
                "entry_price": pos["entry_price"],
                "entry_timestamp": pos["entry_timestamp"],
                "direction": None,
                "policy": None,
                "tracked_decision": "POLICY_MISSING",
                "exit_reason": None,
                "reason": ";".join(p_errors),
                "bars_available_since_entry": None,
                "bars_held": None,
                "exit_ts": None,
                "gross_return_pct": None,
                "net_return_pct": None,
                "mfe_pct": None,
                "mae_pct": None,
            }
            continue

        entry_ts = pd.Timestamp(pos["entry_timestamp"])
        entry_ts = entry_ts.tz_localize("UTC") if entry_ts.tzinfo is None else entry_ts.tz_convert("UTC")

        take_profit_pct = policy.get("take_profit_pct")
        evaluation = evaluate_position(
            bars,
            symbol,
            entry_ts,
            pos["entry_price"],
            policy["direction"],
            float(policy["trailing_stop_pct"]),
            float(take_profit_pct) if take_profit_pct is not None else None,
            int(policy["max_hold_bars"]),
            float(policy.get("cost_pct", 0.0)),
        )

        result["decisions"][symbol] = {
            "symbol": symbol,
            "position_id": pos["position_id"],
            "entry_price": pos["entry_price"],
            "entry_timestamp": pos["entry_timestamp"],
            "direction": policy["direction"],
            "policy": {
                "trailing_stop_pct": policy["trailing_stop_pct"],
                "take_profit_pct": take_profit_pct,
                "max_hold_bars": policy["max_hold_bars"],
                "cost_pct": policy.get("cost_pct", 0.0),
            },
            **evaluation,
        }

    return finalize(result)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False, allow_nan=False)
        f.write("\n")


def print_report(result: dict[str, Any], output_path: Path) -> None:
    print("=" * 96)
    print(f"{VERSION} — EXIT POLICY TRACKER (Phase 3)")
    print("=" * 96)
    print()
    print("MODE                 : RESEARCH / PAPER DECISION ONLY")
    print("ORDERS               : DISABLED")
    print("MEXC / EXCHANGE CALL : DISABLED")
    print("LIVE EXECUTION       : DISABLED")
    print("POSITION CREATION    : DISABLED")
    print("STATE PERSISTED      : DISABLED (recomputed fresh every cycle)")
    print()
    print("TRACKER")
    print("-" * 96)
    print(f"STATUS               : {result['decision_status']}")
    print(f"UPSTREAM STATE ID    : {result['upstream_state_id']}")
    print(f"UPSTREAM HASH VERIFIED: {result['upstream_hash_verified']}")
    print(f"OPEN POSITION COUNT  : {result['open_position_count']}")
    print(f"STATE ID             : {result['state_id']}")
    print()

    for symbol, item in sorted(result["decisions"].items()):
        print(symbol)
        print(f"  POSITION ID        : {item['position_id']}")
        print(f"  ENTRY PRICE        : {item['entry_price']}")
        print(f"  ENTRY TIMESTAMP    : {item['entry_timestamp']}")
        print(f"  DIRECTION          : {item['direction']}")
        print(f"  DECISION           : {item['tracked_decision']}")
        print(f"  EXIT REASON        : {item['exit_reason']}")
        print(f"  REASON             : {item['reason']}")
        if item.get("bars_available_since_entry") is not None:
            print(f"  BARS AVAILABLE     : {item['bars_available_since_entry']}")
        if item["tracked_decision"] == "EXIT":
            print(f"  EXIT TIMESTAMP     : {item['exit_ts']}")
            print(f"  NET RETURN %       : {item['net_return_pct']}")
        print()

    if result["blocked_reasons"]:
        print("FAIL-CLOSED REASONS")
        print("-" * 96)
        for reason in result["blocked_reasons"]:
            print(f"  - {reason}")
        print()

    print(f"OUTPUT               : {output_path.resolve()}")
    print("=" * 96)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reconciliation-input", type=Path, default=DEFAULT_RECONCILIATION_INPUT
    )
    parser.add_argument("--bars-input", type=Path, default=DEFAULT_BARS_INPUT)
    parser.add_argument(
        "--policy-config",
        type=Path,
        default=None,
        help=(
            "JSON file, one entry per symbol: "
            '{"BTC/USD": {"direction": "LONG", "trailing_stop_pct": 2.5, '
            '"take_profit_pct": null, "max_hold_bars": 240, "cost_pct": 0.1}}. '
            "A confirmed open position with no matching entry here is "
            "reported POLICY_MISSING, never given an assumed policy."
        ),
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    try:
        reconciliation = load_json(args.reconciliation_input)
        bars = load_bars(args.bars_input)
        policy_config = load_policy_config(args.policy_config)

        result = build_tracker(
            reconciliation,
            bars,
            policy_config,
            args.reconciliation_input,
            args.bars_input,
            args.policy_config,
        )
        write_json(args.output, result)
        print_report(result, args.output)
        return 0

    except FileNotFoundError as exc:
        result = base_result(args.reconciliation_input, args.bars_input, args.policy_config)
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
        result = base_result(args.reconciliation_input, args.bars_input, args.policy_config)
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
