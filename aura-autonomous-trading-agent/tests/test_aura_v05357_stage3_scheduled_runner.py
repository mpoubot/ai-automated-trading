#!/usr/bin/env python3
"""Contract + wiring tests for AURA v0.5.3.57 scheduled loop runner.

Two layers, deliberately kept separate:

1. Unit tests against a FAKE `.56`-shaped module (`FakeStage3Module`
   below) -- `.57`'s own logic (loop control, market-hours gating, file
   writing, CLI fail-closed behavior) is exercised in isolation, without
   re-verifying `.56`'s internals (that belongs to `.56`'s own test
   suite, `tests/test_aura_v05356_stage3_live_equity_cli.py`).
2. One lightweight integration smoke test that loads the REAL `.56`
   module via `.57.load_stage3_module()` and checks it exposes the
   exact callables `.57` depends on -- proving the actual wiring/import
   chain works offline, without running a real cycle or touching Alpaca.

NO LIVE ALPACA ACCESS IS REQUIRED OR ATTEMPTED BY ANY TEST IN THIS FILE.
NO REAL CREDENTIAL VALUE IS USED ANYWHERE IN THIS FILE.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


M = _load("aura_v05357_stage3_scheduled_runner", ROOT / "aura_v05357_stage3_scheduled_runner.py")

NOW = datetime(2026, 9, 24, 14, 30, 0, tzinfo=timezone.utc)


# ============================================================================
# Fakes
# ============================================================================


class FakeStage3CliError(Exception):
    pass


@dataclass
class FakeSymbolRequest:
    symbol: str
    asset_class: str = "STOCK"
    quantity: Any = 1


@dataclass
class FakeClock:
    is_open: bool


class FakeAlpacaClient:
    def __init__(self, *, is_open: bool = True, clock_exception: Exception | None = None):
        self._is_open = is_open
        self._clock_exception = clock_exception
        self.get_clock_calls = 0

    def get_clock(self):
        self.get_clock_calls += 1
        if self._clock_exception is not None:
            raise self._clock_exception
        return FakeClock(self._is_open)

    def set_is_open(self, value: bool) -> None:
        self._is_open = value


@dataclass
class FakeStage3Module:
    """Stands in for `.56` -- exposes exactly the surface `.57` calls,
    nothing more, so a test failure here means `.57` itself is wrong,
    not `.56`."""

    Stage3CliError = FakeStage3CliError

    credentials: tuple[str, str] = ("FAKE_TEST_KEY_ID_12345", "FAKE_TEST_SECRET_VALUE_67890")
    credentials_error: Exception | None = None
    symbol_requests: tuple[FakeSymbolRequest, ...] = field(default_factory=lambda: (FakeSymbolRequest("AAPL"),))
    cycle_results: list[dict] = field(default_factory=list)
    cycle_calls: list[dict] = field(default_factory=list)
    alpaca_client: Any = None

    def __post_init__(self):
        if self.alpaca_client is None:
            self.alpaca_client = FakeAlpacaClient()

    def load_equity_paper_credentials(self):
        if self.credentials_error is not None:
            raise self.credentials_error
        return self.credentials

    def load_technical_module(self):
        return SimpleNamespace()

    def load_signal_source_module(self):
        return SimpleNamespace(
            FROZEN_TECHNICAL_PARAMS=SimpleNamespace(min_bars_required=55),
            UNIVERSE_VERSION="TEST_UNIVERSE_VERSION",
        )

    def build_bars_client(self, api_key, secret_key, technical_module):
        return "FAKE_BARS_CLIENT"

    def build_trading_client(self, api_key, secret_key):
        return self.alpaca_client

    def load_symbol_requests(self, path):
        return self.symbol_requests

    def run_live_dry_run_cycle(self, symbol_requests, **kwargs):
        self.cycle_calls.append(kwargs)
        idx = len(self.cycle_calls) - 1
        if idx < len(self.cycle_results):
            return self.cycle_results[idx]
        return {
            "stage1_report": {
                "submitted_count": 0,
                "outcomes": [{"symbol": r.symbol, "decision_outcome": "ABSTAIN"} for r in symbol_requests],
            }
        }


def make_ticking_clock(start: datetime, step: timedelta = timedelta(seconds=1)):
    state = {"t": start}

    def _now_fn():
        current = state["t"]
        state["t"] = current + step
        return current

    return _now_fn


# ============================================================================
# 1. is_market_open
# ============================================================================


def test_is_market_open_true():
    client = FakeAlpacaClient(is_open=True)
    assert M.is_market_open(client) is True
    assert client.get_clock_calls == 1


def test_is_market_open_false():
    client = FakeAlpacaClient(is_open=False)
    assert M.is_market_open(client) is False


def test_is_market_open_propagates_real_errors():
    client = FakeAlpacaClient(clock_exception=RuntimeError("auth failed"))
    with pytest.raises(RuntimeError):
        M.is_market_open(client)


# ============================================================================
# 2. run_one_cycle
# ============================================================================


def test_run_one_cycle_calls_stage3_module_and_tags_result():
    stage3 = FakeStage3Module()
    result = M.run_one_cycle(
        stage3_module=stage3,
        symbol_requests=stage3.symbol_requests,
        bars_client="FAKE_BARS_CLIENT",
        alpaca_client=stage3.alpaca_client,
        max_new_orders_per_cycle=1,
        lookback_bars=55,
        universe_version="TEST_UNIVERSE_VERSION",
        max_snapshot_age_seconds=300.0,
        skip_account_equity_fetch=False,
        now=NOW,
    )
    assert len(stage3.cycle_calls) == 1
    call = stage3.cycle_calls[0]
    assert call["max_new_orders_per_cycle"] == 1
    assert call["lookback_bars"] == 55
    assert call["universe_version"] == "TEST_UNIVERSE_VERSION"
    assert call["max_snapshot_age_seconds"] == 300.0
    assert call["skip_account_equity_fetch"] is False
    assert call["now"] == NOW
    assert result["scheduled_runner_engine"] == M.ENGINE
    assert result["scheduled_runner_version"] == M.VERSION


# ============================================================================
# 3. write_cycle_result
# ============================================================================


def test_write_cycle_result_writes_unique_file_and_latest(tmp_path):
    result1 = {"stage1_report": {"submitted_count": 0, "outcomes": []}}
    result2 = {"stage1_report": {"submitted_count": 0, "outcomes": [{"symbol": "AAPL"}]}}

    path1 = M.write_cycle_result(result1, output_dir=tmp_path, now=NOW)
    path2 = M.write_cycle_result(result2, output_dir=tmp_path, now=NOW + timedelta(seconds=1))

    assert path1 != path2
    assert path1.exists() and path2.exists()

    latest = tmp_path / "latest.json"
    assert latest.exists()
    assert json.loads(latest.read_text()) == result2  # latest.json reflects the most recent write
    assert json.loads(path1.read_text()) == result1
    assert json.loads(path2.read_text()) == result2


def test_write_cycle_result_creates_output_dir(tmp_path):
    nested = tmp_path / "does" / "not" / "exist" / "yet"
    M.write_cycle_result({"a": 1}, output_dir=nested, now=NOW)
    assert nested.exists()
    assert (nested / "latest.json").exists()


# ============================================================================
# 4. run_scheduled_loop
# ============================================================================


def test_run_scheduled_loop_executes_every_iteration_when_market_hours_gate_off(tmp_path):
    stage3 = FakeStage3Module()
    sleeps = []
    executed = M.run_scheduled_loop(
        symbol_requests=stage3.symbol_requests,
        bars_client="FAKE_BARS_CLIENT",
        alpaca_client=stage3.alpaca_client,
        stage3_module=stage3,
        max_new_orders_per_cycle=1,
        lookback_bars=55,
        universe_version="TEST_UNIVERSE_VERSION",
        max_snapshot_age_seconds=300.0,
        skip_account_equity_fetch=False,
        output_dir=tmp_path,
        interval_seconds=42.0,
        market_hours_only=False,
        max_iterations=3,
        sleep_fn=sleeps.append,
        now_fn=make_ticking_clock(NOW),
        log_fn=lambda msg: None,
    )
    assert executed == 3
    assert len(stage3.cycle_calls) == 3
    assert sleeps == [42.0, 42.0, 42.0]
    cycle_files = sorted(tmp_path.glob("cycle_*.json"))
    assert len(cycle_files) == 3


def test_run_scheduled_loop_skips_when_market_closed(tmp_path):
    client = FakeAlpacaClient(is_open=False)
    stage3 = FakeStage3Module(alpaca_client=client)
    logs = []
    executed = M.run_scheduled_loop(
        symbol_requests=stage3.symbol_requests,
        bars_client="FAKE_BARS_CLIENT",
        alpaca_client=client,
        stage3_module=stage3,
        max_new_orders_per_cycle=1,
        lookback_bars=55,
        universe_version="TEST_UNIVERSE_VERSION",
        max_snapshot_age_seconds=300.0,
        skip_account_equity_fetch=False,
        output_dir=tmp_path,
        interval_seconds=10.0,
        market_hours_only=True,
        max_iterations=2,
        sleep_fn=lambda s: None,
        now_fn=make_ticking_clock(NOW),
        log_fn=logs.append,
    )
    assert executed == 0
    assert stage3.cycle_calls == []
    assert list(tmp_path.glob("cycle_*.json")) == []
    assert all("MARKET_CLOSED" in line for line in logs)


def test_run_scheduled_loop_mixed_open_closed(tmp_path):
    client = FakeAlpacaClient(is_open=True)
    stage3 = FakeStage3Module(alpaca_client=client)

    # Flip the market closed after the first iteration.
    call_count = {"n": 0}
    real_get_clock = client.get_clock

    def flip_after_first():
        call_count["n"] += 1
        if call_count["n"] >= 2:
            client.set_is_open(False)
        return real_get_clock()

    client.get_clock = flip_after_first  # type: ignore[method-assign]

    executed = M.run_scheduled_loop(
        symbol_requests=stage3.symbol_requests,
        bars_client="FAKE_BARS_CLIENT",
        alpaca_client=client,
        stage3_module=stage3,
        max_new_orders_per_cycle=1,
        lookback_bars=55,
        universe_version="TEST_UNIVERSE_VERSION",
        max_snapshot_age_seconds=300.0,
        skip_account_equity_fetch=False,
        output_dir=tmp_path,
        interval_seconds=5.0,
        market_hours_only=True,
        max_iterations=3,
        sleep_fn=lambda s: None,
        now_fn=make_ticking_clock(NOW),
        log_fn=lambda msg: None,
    )
    assert executed == 1  # only the first iteration ran before the market "closed"
    assert len(stage3.cycle_calls) == 1


def test_run_scheduled_loop_clock_check_failure_does_not_crash_and_does_not_run_cycle(tmp_path):
    client = FakeAlpacaClient(clock_exception=RuntimeError("network blip"))
    stage3 = FakeStage3Module(alpaca_client=client)
    logs = []
    executed = M.run_scheduled_loop(
        symbol_requests=stage3.symbol_requests,
        bars_client="FAKE_BARS_CLIENT",
        alpaca_client=client,
        stage3_module=stage3,
        max_new_orders_per_cycle=1,
        lookback_bars=55,
        universe_version="TEST_UNIVERSE_VERSION",
        max_snapshot_age_seconds=300.0,
        skip_account_equity_fetch=False,
        output_dir=tmp_path,
        interval_seconds=5.0,
        market_hours_only=True,
        max_iterations=2,
        sleep_fn=lambda s: None,
        now_fn=make_ticking_clock(NOW),
        log_fn=logs.append,
    )
    assert executed == 0
    assert stage3.cycle_calls == []
    assert any("MARKET_CLOCK_CHECK_FAILED" in line for line in logs)


def test_run_scheduled_loop_symbol_fetch_failure_in_cycle_is_recorded_not_raised(tmp_path):
    """`.56.run_live_dry_run_cycle` never raises for an ordinary fetch
    failure -- it returns a result dict with `symbol_fetch_failures`
    populated instead. `.57` must not add its own extra try/except
    around that call that would change this behavior."""
    stage3 = FakeStage3Module(
        cycle_results=[{
            "stage1_report": None,
            "symbol_fetch_failures": [{"symbol": "AAPL", "error": "LiveSignalSourceError: no data"}],
        }]
    )
    executed = M.run_scheduled_loop(
        symbol_requests=stage3.symbol_requests,
        bars_client="FAKE_BARS_CLIENT",
        alpaca_client=stage3.alpaca_client,
        stage3_module=stage3,
        max_new_orders_per_cycle=1,
        lookback_bars=55,
        universe_version="TEST_UNIVERSE_VERSION",
        max_snapshot_age_seconds=300.0,
        skip_account_equity_fetch=False,
        output_dir=tmp_path,
        interval_seconds=5.0,
        market_hours_only=False,
        max_iterations=1,
        sleep_fn=lambda s: None,
        now_fn=make_ticking_clock(NOW),
        log_fn=lambda msg: None,
    )
    assert executed == 1
    cycle_files = list(tmp_path.glob("cycle_*.json"))
    assert len(cycle_files) == 1
    written = json.loads(cycle_files[0].read_text())
    assert written["symbol_fetch_failures"] == [{"symbol": "AAPL", "error": "LiveSignalSourceError: no data"}]


# ============================================================================
# 5. main() CLI boundary
# ============================================================================


def test_main_fails_closed_on_missing_credentials(tmp_path, monkeypatch, capsys):
    stage3 = FakeStage3Module(credentials_error=FakeStage3CliError("MISSING_ALPACA_EQUITY_PAPER_CREDENTIALS: ..."))
    monkeypatch.setattr(M, "load_stage3_module", lambda: stage3)

    config_path = tmp_path / "requests.json"
    config_path.write_text('[{"symbol": "AAPL", "asset_class": "STOCK", "quantity": 1}]')

    rc = M.main([
        "--requests-config", str(config_path),
        "--max-new-orders-per-cycle", "1",
        "--max-snapshot-age-seconds", "300",
    ])
    assert rc == 1
    captured = capsys.readouterr()
    assert "FAIL-CLOSED" in captured.err


def test_main_stops_cleanly_on_keyboard_interrupt(tmp_path, monkeypatch, capsys):
    stage3 = FakeStage3Module()
    monkeypatch.setattr(M, "load_stage3_module", lambda: stage3)

    def raise_interrupt(**kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(M, "run_scheduled_loop", raise_interrupt)

    config_path = tmp_path / "requests.json"
    config_path.write_text('[{"symbol": "AAPL", "asset_class": "STOCK", "quantity": 1}]')

    rc = M.main([
        "--requests-config", str(config_path),
        "--max-new-orders-per-cycle", "1",
        "--max-snapshot-age-seconds", "300",
    ])
    assert rc == 0
    captured = capsys.readouterr()
    assert "Stopped by Ctrl+C" in captured.out
    assert "PREVIEW ONLY" in captured.out


# ============================================================================
# 6. Structural safety -- never touches Stage 1B / submission
# ============================================================================


def test_module_never_references_stage1b_or_submission_as_code():
    source = (ROOT / "aura_v05357_stage3_scheduled_runner.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    referenced_names = set()
    submission_keywords = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            referenced_names.add(node.id)
        elif isinstance(node, ast.Attribute):
            referenced_names.add(node.attr)
        elif isinstance(node, ast.keyword) and node.arg == "attempt_submission":
            submission_keywords.append(node)

    assert "run_stage1b_paper_cycle" not in referenced_names
    assert not submission_keywords


# ============================================================================
# 7. Lightweight integration smoke test -- real `.56` module loads and
#    exposes what `.57` needs. No network, no credentials, no real cycle.
# ============================================================================


def test_load_stage3_module_returns_real_56_with_expected_surface():
    stage3 = M.load_stage3_module()
    for attr in (
        "Stage3CliError",
        "load_equity_paper_credentials",
        "load_technical_module",
        "load_signal_source_module",
        "build_bars_client",
        "build_trading_client",
        "load_symbol_requests",
        "run_live_dry_run_cycle",
    ):
        assert hasattr(stage3, attr), f"real .56 module is missing expected attribute: {attr}"
