"""
Tests for aura_v05324_exit_policy_tracker.py (Phase 3: exit-policy
tracker sitting downstream of v0.5.3.18 Reconciliation).

Loaded via importlib, matching the convention used elsewhere in tests/
(e.g. tests/test_aura_v05318_reconciliation.py, tests/test_aura_exit_
policy_backtest.py). Uses synthetic reconciliation payloads and synthetic
OHLC bars throughout -- no network calls, no credentials needed.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "aura_v05324_exit_policy_tracker.py"

spec = importlib.util.spec_from_file_location("aura_v05324_exit_policy_tracker", SCRIPT_PATH)
ept = importlib.util.module_from_spec(spec)
sys.modules["aura_v05324_exit_policy_tracker"] = ept
spec.loader.exec_module(ept)  # type: ignore[union-attr]


def make_bars(rows: list[dict], *, symbol: str = "BTC/USD", start: str = "2026-01-01T00:00:00Z") -> pd.DataFrame:
    """rows: list of {open, high, low, close}, one per hourly bar starting
    at `start`. Adds timestamp/symbol/volume columns, matching the shape
    load_bars() expects from the real CSV."""
    base = pd.Timestamp(start)
    out = []
    for i, r in enumerate(rows):
        out.append({
            "timestamp": base + pd.Timedelta(hours=i),
            "symbol": symbol,
            "open": r["open"], "high": r["high"], "low": r["low"], "close": r["close"],
            "volume": 1.0,
        })
    return pd.DataFrame(out)


def make_reconciliation_payload(
    decisions: dict,
    *,
    decision_status: str = "DECIDED",
    overall_reconciliation: str = "RECONCILED",
) -> dict:
    """Builds a v0.5.3.18-shaped payload and stamps it with a real,
    self-consistent state_hash/state_id using the tracker's own
    canonical_reconciliation_hash(), so verify_reconciliation() succeeds
    by construction unless a test deliberately corrupts a field."""
    payload = {
        "agent_version": ept.EXPECTED_RECONCILIATION_VERSION,
        "engine": ept.EXPECTED_RECONCILIATION_ENGINE,
        "decision_status": decision_status,
        "overall_reconciliation": overall_reconciliation,
        "input_ledger_hash": "test-ledger-hash",
        "input_observed_execution_hash": "test-observed-hash",
        "upstream_state_id": "LE-testfixture0000000000",
        "upstream_hash_verified": True,
        "frozen_configuration_verified": True,
        "observed_execution_verified": True,
        "decisions": decisions,
        "blocked_reasons": [],
        "guardrails": {
            "reconciliation_only": True,
            "fail_closed": True,
            "fabricated_fill": False,
            "fabricated_position": False,
        },
    }
    h = ept.canonical_reconciliation_hash(payload)
    payload["state_hash"] = h
    payload["state_id"] = f"RC-{h[:24]}"
    return payload


def reconciled_decision(symbol: str, *, position_id: str = "PAPER-1", fill_price: float = 100.0, fill_timestamp: str = "2026-01-01T00:00:00Z") -> dict:
    return {
        "symbol": symbol,
        "ledger_event": "PAPER_ORDER_INTENT_RECORDED",
        "observed_order_status": "FILLED",
        "reconciliation_state": "RECONCILED_EXECUTION",
        "execution_reconciled": True,
        "position_exists": True,
        "position_id": position_id,
        "fill_price": fill_price,
        "fill_timestamp": fill_timestamp,
        "reason": "LEDGER_INTENT_MATCHED_OBSERVED_EXECUTION",
    }


def pending_decision(symbol: str) -> dict:
    return {
        "symbol": symbol,
        "ledger_event": "PAPER_ORDER_INTENT_RECORDED",
        "observed_order_status": "PENDING",
        "reconciliation_state": "EXECUTION_PENDING",
        "execution_reconciled": False,
        "position_exists": False,
        "position_id": None,
        "fill_price": None,
        "fill_timestamp": None,
        "reason": "LEDGER_INTENT_AWAITING_OBSERVED_EXECUTION",
    }


def valid_policy(**overrides) -> dict:
    policy = {
        "direction": "LONG",
        "trailing_stop_pct": 2.0,
        "take_profit_pct": None,
        "max_hold_bars": 10,
        "cost_pct": 0.0,
    }
    policy.update(overrides)
    return policy


# --------------------------------------------------------------------------
# verify_reconciliation
# --------------------------------------------------------------------------

def test_verify_reconciliation_accepts_well_formed_payload():
    payload = make_reconciliation_payload({})
    ok, errors = ept.verify_reconciliation(payload)
    assert ok
    assert errors == []


def test_verify_reconciliation_rejects_wrong_engine():
    payload = make_reconciliation_payload({})
    payload["engine"] = "SOMETHING_ELSE"
    ok, errors = ept.verify_reconciliation(payload)
    assert not ok
    assert "WRONG_UPSTREAM_ENGINE" in errors


def test_verify_reconciliation_rejects_tampered_state_hash():
    payload = make_reconciliation_payload({"BTC/USD": reconciled_decision("BTC/USD")})
    # Tamper with a decision AFTER the hash was stamped -- the recomputed
    # hash must no longer match the supplied one.
    payload["decisions"]["BTC/USD"]["fill_price"] = 999999.0
    ok, errors = ept.verify_reconciliation(payload)
    assert not ok
    assert "UPSTREAM_STATE_HASH_MISMATCH" in errors


def test_verify_reconciliation_rejects_not_decided():
    payload = make_reconciliation_payload({}, decision_status="BLOCKED")
    ok, errors = ept.verify_reconciliation(payload)
    assert not ok
    assert "UPSTREAM_NOT_DECIDED" in errors


# --------------------------------------------------------------------------
# open_positions_from_reconciliation
# --------------------------------------------------------------------------

def test_open_positions_includes_only_reconciled_execution():
    payload = make_reconciliation_payload({
        "BTC/USD": reconciled_decision("BTC/USD", position_id="PAPER-1", fill_price=65000.0),
        "ETH/USD": pending_decision("ETH/USD"),
    })
    open_positions = ept.open_positions_from_reconciliation(payload)
    assert set(open_positions.keys()) == {"BTC/USD"}
    assert open_positions["BTC/USD"]["position_id"] == "PAPER-1"
    assert open_positions["BTC/USD"]["entry_price"] == 65000.0


def test_open_positions_excludes_malformed_entry():
    bad = reconciled_decision("BTC/USD")
    bad["position_id"] = None  # position_exists True but no id -- malformed, must not be trusted
    payload = make_reconciliation_payload({"BTC/USD": bad})
    assert ept.open_positions_from_reconciliation(payload) == {}


# --------------------------------------------------------------------------
# policy_errors_for_symbol
# --------------------------------------------------------------------------

def test_policy_errors_for_missing_policy():
    errors = ept.policy_errors_for_symbol("BTC/USD", None)
    assert errors == ["POLICY_MISSING:BTC/USD"]


def test_policy_errors_for_invalid_direction():
    errors = ept.policy_errors_for_symbol("BTC/USD", valid_policy(direction="SIDEWAYS"))
    assert any("POLICY_INVALID_DIRECTION" in e for e in errors)


def test_policy_errors_none_for_valid_policy():
    assert ept.policy_errors_for_symbol("BTC/USD", valid_policy()) == []


# --------------------------------------------------------------------------
# evaluate_position -- the live TIMEOUT-vs-HOLD adaptation is the core new
# behaviour this module adds on top of simulate_trade().
# --------------------------------------------------------------------------

def test_evaluate_position_holds_when_no_bars_after_entry():
    bars = make_bars([{"open": 100, "high": 100, "low": 100, "close": 100}])
    entry_ts = bars.iloc[0]["timestamp"]  # entry ON the only bar -- nothing AFTER it
    result = ept.evaluate_position(
        bars, "BTC/USD", entry_ts, 100.0, "LONG", 2.0, None, 10, 0.0,
    )
    assert result["tracked_decision"] == "HOLD"
    assert result["reason"] == "NO_BARS_AFTER_ENTRY_YET"
    assert result["exit_reason"] is None


def test_evaluate_position_holds_when_bars_available_less_than_max_hold_bars():
    # Flat series, never trips the stop/target. Only 5 bars have arrived
    # since entry, but the policy's max_hold_bars is 10 -- a real
    # simulate_trade() TIMEOUT would fire here, but this module must
    # downgrade it to HOLD because the fetched window is still incomplete.
    bars = make_bars([{"open": 100, "high": 100.1, "low": 99.9, "close": 100} for _ in range(6)])
    entry_ts = bars.iloc[0]["timestamp"]
    result = ept.evaluate_position(
        bars, "BTC/USD", entry_ts, 100.0, "LONG", 50.0, None, 10, 0.0,
    )
    assert result["tracked_decision"] == "HOLD"
    assert result["reason"] == "INSUFFICIENT_BARS_FOR_TIMEOUT_VERDICT"
    assert result["exit_reason"] is None
    assert result["bars_available_since_entry"] == 5


def test_evaluate_position_exits_on_real_timeout_once_enough_bars_arrived():
    # Same flat series, but now 11 bars have arrived since entry -- at
    # least max_hold_bars (10) -- so a TIMEOUT verdict is trustworthy.
    bars = make_bars([{"open": 100, "high": 100.1, "low": 99.9, "close": 100} for _ in range(12)])
    entry_ts = bars.iloc[0]["timestamp"]
    result = ept.evaluate_position(
        bars, "BTC/USD", entry_ts, 100.0, "LONG", 50.0, None, 10, 0.0,
    )
    assert result["tracked_decision"] == "EXIT"
    assert result["exit_reason"] == "TIMEOUT"
    assert result["reason"] == "MAX_HOLD_BARS_REACHED"


def test_evaluate_position_exits_immediately_on_stop_even_with_few_bars():
    # A hard price drop on bar 2 (well before max_hold_bars=10 bars have
    # even arrived) must still be reported as a real EXIT -- STOP/TARGET
    # verdicts are trusted immediately regardless of window completeness.
    rows = [{"open": 100, "high": 100.1, "low": 99.9, "close": 100}]
    rows.append({"open": 100, "high": 100, "low": 90.0, "close": 91.0})  # sharp drop -- trips a 2% trailing stop
    bars = make_bars(rows)
    entry_ts = bars.iloc[0]["timestamp"]
    result = ept.evaluate_position(
        bars, "BTC/USD", entry_ts, 100.0, "LONG", 2.0, None, 10, 0.0,
    )
    assert result["tracked_decision"] == "EXIT"
    assert result["exit_reason"] == "STOP"
    assert result["reason"] == "STOP_HIT"


def test_evaluate_position_holds_when_symbol_absent_from_bars():
    bars = make_bars([{"open": 100, "high": 100, "low": 100, "close": 100}], symbol="ETH/USD")
    entry_ts = bars.iloc[0]["timestamp"]
    result = ept.evaluate_position(
        bars, "BTC/USD", entry_ts, 100.0, "LONG", 2.0, None, 10, 0.0,
    )
    assert result["tracked_decision"] == "HOLD"
    assert result["reason"] == "NO_BARS_FOR_SYMBOL"


# --------------------------------------------------------------------------
# build_tracker -- end-to-end
# --------------------------------------------------------------------------

def test_build_tracker_blocked_when_upstream_not_verified():
    payload = make_reconciliation_payload({}, decision_status="BLOCKED")
    bars = make_bars([{"open": 100, "high": 100, "low": 100, "close": 100}])
    result = ept.build_tracker(payload, bars, {}, Path("recon.json"), Path("bars.csv"), None)
    assert result["decision_status"] == "BLOCKED"
    assert result["decisions"] == {}
    assert result["blocked_reasons"]


def test_build_tracker_reports_policy_missing_for_open_position_without_config():
    payload = make_reconciliation_payload({"BTC/USD": reconciled_decision("BTC/USD", fill_price=100.0)})
    bars = make_bars([{"open": 100, "high": 100, "low": 100, "close": 100}])
    result = ept.build_tracker(payload, bars, {}, Path("recon.json"), Path("bars.csv"), None)
    assert result["decision_status"] == "DECIDED"
    assert result["decisions"]["BTC/USD"]["tracked_decision"] == "POLICY_MISSING"


def test_build_tracker_evaluates_open_position_with_configured_policy():
    payload = make_reconciliation_payload({
        "BTC/USD": reconciled_decision("BTC/USD", fill_price=100.0, fill_timestamp="2026-01-01T00:00:00Z"),
    })
    bars = make_bars([{"open": 100, "high": 100.1, "low": 99.9, "close": 100} for _ in range(3)])
    policy_config = {"BTC/USD": valid_policy(max_hold_bars=10)}
    result = ept.build_tracker(payload, bars, policy_config, Path("recon.json"), Path("bars.csv"), Path("policy.json"))
    decision = result["decisions"]["BTC/USD"]
    assert decision["tracked_decision"] == "HOLD"  # only 2 bars arrived since entry, policy wants 10
    assert decision["direction"] == "LONG"
    assert decision["policy"]["trailing_stop_pct"] == 2.0


def test_build_tracker_ignores_non_open_symbols():
    payload = make_reconciliation_payload({"ETH/USD": pending_decision("ETH/USD")})
    bars = make_bars([{"open": 100, "high": 100, "low": 100, "close": 100}])
    result = ept.build_tracker(payload, bars, {}, Path("recon.json"), Path("bars.csv"), None)
    assert result["open_position_count"] == 0
    assert result["decisions"] == {}


# --------------------------------------------------------------------------
# CLI smoke test
# --------------------------------------------------------------------------

def test_cli_runs_end_to_end(tmp_path: Path):
    recon_payload = make_reconciliation_payload({
        "BTC/USD": reconciled_decision("BTC/USD", fill_price=100.0, fill_timestamp="2026-01-01T00:00:00Z"),
    })
    recon_path = tmp_path / "reconciliation.json"
    recon_path.write_text(json.dumps(recon_payload), encoding="utf-8")

    bars = make_bars([{"open": 100, "high": 100.1, "low": 99.9, "close": 100} for _ in range(3)])
    bars_path = tmp_path / "bars.csv"
    bars.to_csv(bars_path, index=False)

    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps({"BTC/USD": valid_policy(max_hold_bars=10)}), encoding="utf-8")

    output_path = tmp_path / "out.json"

    proc = subprocess.run(
        [
            sys.executable, str(SCRIPT_PATH),
            "--reconciliation-input", str(recon_path),
            "--bars-input", str(bars_path),
            "--policy-config", str(policy_path),
            "--output", str(output_path),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert output_path.exists()
    result = json.loads(output_path.read_text(encoding="utf-8"))
    assert result["decision_status"] == "DECIDED"
    assert result["decisions"]["BTC/USD"]["tracked_decision"] == "HOLD"


def test_cli_fails_closed_on_missing_reconciliation_input(tmp_path: Path):
    bars_path = tmp_path / "bars.csv"
    make_bars([{"open": 100, "high": 100, "low": 100, "close": 100}]).to_csv(bars_path, index=False)
    output_path = tmp_path / "out.json"

    proc = subprocess.run(
        [
            sys.executable, str(SCRIPT_PATH),
            "--reconciliation-input", str(tmp_path / "does_not_exist.json"),
            "--bars-input", str(bars_path),
            "--output", str(output_path),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 1
    result = json.loads(output_path.read_text(encoding="utf-8"))
    assert result["decision_status"] == "BLOCKED"
    assert result["blocked_reasons"]
