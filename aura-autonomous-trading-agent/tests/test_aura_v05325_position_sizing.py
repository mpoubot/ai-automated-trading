"""
Tests for aura_v05325_position_sizing.py (Phase 4: equity-based position
sizing feeding v0.5.3.23's --sizing-config).

compute_sizing()/build_sizing()/sizing_config_from_result() are pure
functions of already-fetched account state + prices, so they're tested
directly with synthetic data -- no live Alpaca credentials or network
access needed. Only two CLI-level fail-closed smoke tests touch
subprocess, and both are argument/credential validation paths that never
reach a network call.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "aura_v05325_position_sizing.py"

spec = importlib.util.spec_from_file_location("aura_v05325_position_sizing", SCRIPT_PATH)
ps = importlib.util.module_from_spec(spec)
sys.modules["aura_v05325_position_sizing"] = ps
spec.loader.exec_module(ps)  # type: ignore[union-attr]


def make_bars(rows: list[dict]) -> pd.DataFrame:
    """rows: list of {symbol, hours_ago, close} (hours_ago used to control
    ordering so 'latest' is unambiguous)."""
    base = pd.Timestamp("2026-01-01T00:00:00Z")
    out = []
    for r in rows:
        out.append({
            "timestamp": base + pd.Timedelta(hours=r["hours_ago"]),
            "symbol": r["symbol"],
            "open": r["close"], "high": r["close"], "low": r["close"], "close": r["close"],
            "volume": 1.0,
        })
    return pd.DataFrame(out)


# --------------------------------------------------------------------------
# latest_close_by_symbol
# --------------------------------------------------------------------------

def test_latest_close_picks_most_recent_bar_per_symbol():
    bars = make_bars([
        {"symbol": "BTC/USD", "hours_ago": 0, "close": 100.0},
        {"symbol": "BTC/USD", "hours_ago": 1, "close": 105.0},  # latest
        {"symbol": "ETH/USD", "hours_ago": 0, "close": 10.0},
    ])
    result = ps.latest_close_by_symbol(bars)
    assert result["BTC/USD"] == 105.0
    assert result["ETH/USD"] == 10.0


def test_latest_close_ignores_non_positive_close():
    bars = make_bars([{"symbol": "BTC/USD", "hours_ago": 0, "close": -5.0}])
    result = ps.latest_close_by_symbol(bars)
    assert "BTC/USD" not in result


# --------------------------------------------------------------------------
# compute_sizing
# --------------------------------------------------------------------------

def test_compute_sizing_sizes_at_fraction_of_equity():
    decisions = ps.compute_sizing(
        equity=100000.0,
        open_symbols=set(),
        open_position_count=0,
        latest_close={"BTC/USD": 50000.0},
        symbols=["BTC/USD"],
        fraction_of_equity=0.10,
        max_concurrent_positions=5,
    )
    item = decisions["BTC/USD"]
    assert item["eligible"] is True
    assert item["quantity"] == pytest.approx(0.2)  # 10% of 100000 = 10000; /50000 = 0.2
    assert item["dollar_size"] == 10000.0


def test_compute_sizing_excludes_already_open_symbol():
    decisions = ps.compute_sizing(
        equity=100000.0,
        open_symbols={"BTC/USD"},
        open_position_count=1,
        latest_close={"BTC/USD": 50000.0},
        symbols=["BTC/USD"],
    )
    assert decisions["BTC/USD"]["eligible"] is False
    assert decisions["BTC/USD"]["reason"] == "ALREADY_OPEN_POSITION_FOR_SYMBOL"


def test_compute_sizing_excludes_all_when_at_max_concurrent_positions():
    # 5 positions already open (none of them BTC/USD) -- BTC/USD must still
    # be excluded because the CAP, not the specific symbol, is the reason.
    decisions = ps.compute_sizing(
        equity=100000.0,
        open_symbols={"A/USD", "B/USD", "C/USD", "D/USD", "E/USD"},
        open_position_count=5,
        latest_close={"BTC/USD": 50000.0},
        symbols=["BTC/USD"],
        max_concurrent_positions=5,
    )
    assert decisions["BTC/USD"]["eligible"] is False
    assert decisions["BTC/USD"]["reason"] == "MAX_CONCURRENT_POSITIONS_REACHED"


def test_compute_sizing_below_cap_still_sizes():
    decisions = ps.compute_sizing(
        equity=100000.0,
        open_symbols={"A/USD"},
        open_position_count=1,
        latest_close={"BTC/USD": 50000.0},
        symbols=["BTC/USD"],
        max_concurrent_positions=5,
    )
    assert decisions["BTC/USD"]["eligible"] is True


def test_compute_sizing_excludes_invalid_equity():
    for bad_equity in (0, -100.0, None, "not-a-number", True):
        decisions = ps.compute_sizing(
            equity=bad_equity,
            open_symbols=set(),
            open_position_count=0,
            latest_close={"BTC/USD": 50000.0},
            symbols=["BTC/USD"],
        )
        assert decisions["BTC/USD"]["eligible"] is False
        assert decisions["BTC/USD"]["reason"] == "INVALID_EQUITY"


def test_compute_sizing_excludes_symbol_with_no_bars():
    decisions = ps.compute_sizing(
        equity=100000.0,
        open_symbols=set(),
        open_position_count=0,
        latest_close={},
        symbols=["BTC/USD"],
    )
    assert decisions["BTC/USD"]["eligible"] is False
    assert decisions["BTC/USD"]["reason"] == "NO_BARS_FOR_SYMBOL"


def test_compute_sizing_respects_custom_fraction():
    decisions = ps.compute_sizing(
        equity=100000.0,
        open_symbols=set(),
        open_position_count=0,
        latest_close={"BTC/USD": 50000.0},
        symbols=["BTC/USD"],
        fraction_of_equity=0.05,
    )
    assert decisions["BTC/USD"]["quantity"] == pytest.approx(0.1)  # 5% of 100000 / 50000


# --------------------------------------------------------------------------
# build_sizing / sizing_config_from_result
# --------------------------------------------------------------------------

def test_build_sizing_end_to_end_decided():
    account_state = {"equity": 100000.0, "open_symbols": [], "open_position_count": 0}
    result = ps.build_sizing(
        account_state,
        {"BTC/USD": 50000.0, "ETH/USD": 4000.0},
        ["BTC/USD", "ETH/USD"],
        Path("bars.csv"),
        0.10,
        5,
    )
    assert result["decision_status"] == "DECIDED"
    assert result["decisions"]["BTC/USD"]["eligible"] is True
    assert result["decisions"]["ETH/USD"]["eligible"] is True
    assert result["blocked_reasons"] == []
    assert result["state_hash"] and result["state_id"].startswith("PS-")


def test_build_sizing_blocked_on_invalid_open_position_count():
    account_state = {"equity": 100000.0, "open_symbols": [], "open_position_count": None}
    result = ps.build_sizing(
        account_state, {"BTC/USD": 50000.0}, ["BTC/USD"], Path("bars.csv"), 0.10, 5,
    )
    assert result["decision_status"] == "BLOCKED"
    assert "INVALID_OPEN_POSITION_COUNT" in result["blocked_reasons"]
    assert result["decisions"] == {}


def test_sizing_config_from_result_only_includes_eligible_entries():
    account_state = {"equity": 100000.0, "open_symbols": ["ETH/USD"], "open_position_count": 1}
    result = ps.build_sizing(
        account_state,
        {"BTC/USD": 50000.0, "ETH/USD": 4000.0},
        ["BTC/USD", "ETH/USD"],
        Path("bars.csv"),
        0.10,
        5,
    )
    sizing_config = ps.sizing_config_from_result(result)
    assert sizing_config == {"BTC/USD": pytest.approx(0.2)}
    assert "ETH/USD" not in sizing_config  # already open -- excluded


def test_build_sizing_is_deterministic_hash():
    account_state = {"equity": 100000.0, "open_symbols": [], "open_position_count": 0}
    r1 = ps.build_sizing(account_state, {"BTC/USD": 50000.0}, ["BTC/USD"], Path("bars.csv"), 0.10, 5)
    r2 = ps.build_sizing(account_state, {"BTC/USD": 50000.0}, ["BTC/USD"], Path("bars.csv"), 0.10, 5)
    assert r1["state_hash"] == r2["state_hash"]


# --------------------------------------------------------------------------
# CLI fail-closed smoke tests -- both paths are validated BEFORE any
# network call, so no live credentials are needed to exercise them.
# --------------------------------------------------------------------------

def test_cli_fails_closed_on_invalid_fraction_of_equity(tmp_path: Path):
    proc = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--fraction-of-equity", "1.5"],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert proc.returncode == 2
    assert "fraction-of-equity" in proc.stderr


def test_cli_fails_closed_on_missing_credentials(tmp_path: Path):
    env = {k: v for k, v in os.environ.items() if "ALPACA" not in k}
    proc = subprocess.run(
        [sys.executable, str(SCRIPT_PATH)],
        cwd=ROOT, capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 2
    assert "credentials" in proc.stderr.lower()
