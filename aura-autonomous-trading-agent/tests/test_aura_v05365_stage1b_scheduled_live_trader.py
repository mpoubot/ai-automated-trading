#!/usr/bin/env python3
"""Contract + wiring tests for AURA v0.5.3.65 continuous, unattended
real-order-submission loop.

Two layers, deliberately kept separate, same convention as `.357`'s own
test suite:

1. Unit tests against a FAKE `.363`-shaped module (`FakeManualTriggerModule`
   below) plus the REAL `.357` module (for `is_market_open`/
   `write_cycle_result`, already covered by `.357`'s own test suite) --
   `.365`'s own new logic (kill switch, daily order cap, loop control,
   CLI fail-closed behavior) is exercised in isolation, without
   re-verifying `.363`'s internals (that belongs to
   `tests/test_aura_v05363_stage1b_manual_trigger_cli.py`).
2. One lightweight integration smoke test that loads the REAL `.363`/
   `.357`/`.344` modules via `.365`'s own loader functions and checks
   they expose the exact surface `.365` depends on -- proving the actual
   wiring/import chain works offline, without running a real cycle or
   touching Alpaca.

NO LIVE ALPACA ACCESS IS REQUIRED OR ATTEMPTED BY ANY TEST IN THIS FILE.
NO REAL CREDENTIAL VALUE IS USED ANYWHERE IN THIS FILE.
"""
from __future__ import annotations

import ast
import importlib.util
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load(name: str, path: Path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


STAGE357 = _load("aura_v05357_stage3_scheduled_runner", ROOT / "aura_v05357_stage3_scheduled_runner.py")
ENFORCE = _load("aura_v05344_portfolio_exposure_enforcement", ROOT / "aura_v05344_portfolio_exposure_enforcement.py")
M = _load("aura_v05365_stage1b_scheduled_live_trader", ROOT / "aura_v05365_stage1b_scheduled_live_trader.py")

NOW = datetime(2026, 9, 29, 14, 30, 0, tzinfo=timezone.utc)


# ============================================================================
# Fakes
# ============================================================================


class FakeCliError(Exception):
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
    """Same shape as `.357`'s own test fixture -- only `get_clock()`
    matters here, since real `.357.is_market_open` is used unmodified."""

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
class FakeEquityCliModule:
    """Stands in for `.356`, reached via `FakeManualTriggerModule.
    load_equity_cli_module()` -- exposes exactly the surface `.365`'s
    `main()` calls."""

    credentials: tuple[str, str] = ("FAKE_TEST_KEY_ID_12345", "FAKE_TEST_SECRET_VALUE_67890")
    credentials_error: Exception | None = None
    symbol_requests: tuple[FakeSymbolRequest, ...] = field(default_factory=lambda: (FakeSymbolRequest("AAPL"),))
    alpaca_client: Any = None
    pinned_universe_version: str = "v1"
    pinned_universe_symbol_requests: tuple[FakeSymbolRequest, ...] = field(
        default_factory=lambda: (FakeSymbolRequest("AAPL"), FakeSymbolRequest("SPY", asset_class="ETF"))
    )

    def __post_init__(self):
        if self.alpaca_client is None:
            self.alpaca_client = FakeAlpacaClient()

    def load_equity_paper_credentials(self):
        if self.credentials_error is not None:
            raise self.credentials_error
        return self.credentials

    def load_technical_module(self):
        return SimpleNamespace(load_pinned_universe=lambda: SimpleNamespace(version=self.pinned_universe_version))

    def build_symbol_requests_from_pinned_universe(self, technical_module):
        return self.pinned_universe_symbol_requests

    def load_signal_source_module(self):
        return SimpleNamespace(
            FROZEN_TECHNICAL_PARAMS=SimpleNamespace(min_bars_required=55), UNIVERSE_VERSION="TEST_UNIVERSE_VERSION"
        )

    def build_bars_client(self, api_key, secret_key, technical_module):
        return "FAKE_BARS_CLIENT"

    def build_trading_client(self, api_key, secret_key):
        return self.alpaca_client

    def build_news_client(self, api_key, secret_key):
        return "FAKE_NEWS_CLIENT"

    def load_symbol_requests(self, path):
        return self.symbol_requests


class EarningsCalendarIngestionError(Exception):
    """Named -- not aliased -- to match `.67.EarningsCalendarIngestionError`
    exactly. `.365` checks `type(exc).__name__` rather than importing `.67`
    (dynamically loaded module), so the class's actual `__name__` is what
    matters, not just a module-level alias pointing at it."""


@dataclass
class FakeEarningsCalendarModule:
    """Stands in for `.67`, reached via `FakeManualTriggerModule.
    load_earnings_calendar_module()`."""

    api_key: str = "FAKE_FMP_KEY"
    api_key_error: Exception | None = None

    def load_fmp_api_key(self):
        if self.api_key_error is not None:
            raise self.api_key_error
        return self.api_key


@dataclass
class FakeManualTriggerModule:
    """Stands in for `.363` -- exposes exactly the surface `.365` calls,
    nothing more, so a test failure here means `.365` itself is wrong,
    not `.363`."""

    Stage1BManualTriggerCliError = FakeCliError

    equity_cli: FakeEquityCliModule = field(default_factory=FakeEquityCliModule)
    earnings_calendar_module: FakeEarningsCalendarModule = field(default_factory=FakeEarningsCalendarModule)
    cycle_results: list[dict] = field(default_factory=list)
    cycle_calls: list[dict] = field(default_factory=list)

    def load_equity_cli_module(self):
        return self.equity_cli

    def load_earnings_calendar_module(self):
        return self.earnings_calendar_module

    def run_manual_trigger_stage1b_cycle(self, symbol_requests, **kwargs):
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
# 1. File-based kill switch
# ============================================================================


def test_check_kill_switch_false_when_absent(tmp_path):
    assert M.check_kill_switch(tmp_path / "STOP") is False


def test_check_kill_switch_true_when_present(tmp_path):
    stop = tmp_path / "STOP"
    stop.write_text("stop")
    assert M.check_kill_switch(stop) is True


# ============================================================================
# 2. Daily order-count log
# ============================================================================


def test_read_todays_submitted_count_zero_when_log_missing(tmp_path):
    assert M.read_todays_submitted_count(tmp_path / "missing.jsonl", today_key="2026-09-29") == 0


def test_append_and_read_sums_same_day_entries(tmp_path):
    log = tmp_path / "log.jsonl"
    M.append_daily_order_log_entry(log, submitted_count=1, as_of=datetime(2026, 9, 29, 14, 0, tzinfo=timezone.utc))
    M.append_daily_order_log_entry(log, submitted_count=0, as_of=datetime(2026, 9, 29, 14, 5, tzinfo=timezone.utc))
    M.append_daily_order_log_entry(log, submitted_count=2, as_of=datetime(2026, 9, 29, 14, 10, tzinfo=timezone.utc))
    assert M.read_todays_submitted_count(log, today_key="2026-09-29") == 3


def test_read_todays_submitted_count_ignores_other_days(tmp_path):
    log = tmp_path / "log.jsonl"
    M.append_daily_order_log_entry(log, submitted_count=5, as_of=datetime(2026, 9, 28, 14, 0, tzinfo=timezone.utc))
    M.append_daily_order_log_entry(log, submitted_count=1, as_of=datetime(2026, 9, 29, 14, 0, tzinfo=timezone.utc))
    assert M.read_todays_submitted_count(log, today_key="2026-09-29") == 1


def test_read_todays_submitted_count_raises_on_corrupt_line(tmp_path):
    log = tmp_path / "log.jsonl"
    log.write_text("not json\n")
    with pytest.raises(M.ScheduledLiveTraderError, match="corrupt"):
        M.read_todays_submitted_count(log, today_key="2026-09-29")


def test_append_daily_order_log_entry_creates_parent_dir(tmp_path):
    log = tmp_path / "does" / "not" / "exist" / "log.jsonl"
    M.append_daily_order_log_entry(log, submitted_count=1, as_of=NOW)
    assert log.exists()


# ============================================================================
# 3. build_default_limits -- Martin's confirmed conservative values
#    (2026-09-29, AskUserQuestion).
# ============================================================================


def test_build_default_limits_matches_martins_confirmed_values():
    limits = M.build_default_limits(ENFORCE)
    assert limits.max_daily_loss_pct_by_venue == {"ALPACA": 0.02}
    assert limits.max_drawdown_pct_by_venue == {"ALPACA": 0.05}
    assert limits.max_asset_concentration_ratio == 0.10
    assert limits.max_net_exposure_ratio == 0.50
    # every other dimension left unconfigured -- not a guess, see .344's own PortfolioLimits docstring
    assert limits.max_portfolio_heat_ratio is None
    assert limits.max_leverage_ratio_by_venue == {}
    assert limits.correlated_groups == {}


# ============================================================================
# 4. run_one_live_cycle
# ============================================================================


def test_run_one_live_cycle_calls_manual_trigger_with_confirmed_true_and_tags_result():
    manual = FakeManualTriggerModule()
    limits = object()
    result = M.run_one_live_cycle(
        manual_trigger_module=manual,
        symbol_requests=manual.equity_cli.symbol_requests,
        bars_client="FAKE_BARS_CLIENT",
        alpaca_client=manual.equity_cli.alpaca_client,
        max_new_orders_per_cycle=1,
        lookback_bars=55,
        universe_version="TEST_UNIVERSE_VERSION",
        max_snapshot_age_seconds=300.0,
        fill_poll_timeout_seconds=1.0,
        fill_poll_interval_seconds=1.0,
        limits=limits,
        now=NOW,
    )
    assert len(manual.cycle_calls) == 1
    call = manual.cycle_calls[0]
    assert call["confirmed"] is True
    assert call["limits"] is limits
    assert call["max_new_orders_per_cycle"] == 1
    assert call["now"] == NOW
    assert result["live_trader_engine"] == M.ENGINE
    assert result["live_trader_version"] == M.VERSION


# ============================================================================
# 5. run_scheduled_live_loop
# ============================================================================


def _loop_kwargs(manual, client, tmp_path, **overrides):
    kwargs = dict(
        symbol_requests=manual.equity_cli.symbol_requests,
        bars_client="FAKE_BARS_CLIENT",
        alpaca_client=client,
        manual_trigger_module=manual,
        scheduled_runner_module=STAGE357,
        max_new_orders_per_cycle=1,
        lookback_bars=55,
        universe_version="TEST_UNIVERSE_VERSION",
        max_snapshot_age_seconds=300.0,
        fill_poll_timeout_seconds=1.0,
        fill_poll_interval_seconds=1.0,
        limits=object(),
        output_dir=tmp_path / "out",
        interval_seconds=5.0,
        market_hours_only=False,
        kill_switch_path=tmp_path / "STOP",
        max_orders_per_day=5,
        daily_order_log_path=tmp_path / "orders.jsonl",
        max_iterations=1,
        sleep_fn=lambda s: None,
        now_fn=make_ticking_clock(NOW),
        log_fn=lambda msg: None,
    )
    kwargs.update(overrides)
    return kwargs


def test_run_scheduled_live_loop_executes_every_iteration_when_gates_off(tmp_path):
    manual = FakeManualTriggerModule()
    client = FakeAlpacaClient(is_open=True)
    sleeps = []
    executed = M.run_scheduled_live_loop(
        **_loop_kwargs(manual, client, tmp_path, max_iterations=3, interval_seconds=42.0, sleep_fn=sleeps.append)
    )
    assert executed == 3
    assert len(manual.cycle_calls) == 3
    assert sleeps == [42.0, 42.0, 42.0]


def test_run_scheduled_live_loop_kill_switch_stops_before_any_cycle(tmp_path):
    manual = FakeManualTriggerModule()
    client = FakeAlpacaClient(is_open=True)
    kill_switch = tmp_path / "STOP"
    kill_switch.write_text("stop")
    sleeps = []
    logs = []
    executed = M.run_scheduled_live_loop(
        **_loop_kwargs(
            manual, client, tmp_path,
            kill_switch_path=kill_switch, max_iterations=5, sleep_fn=sleeps.append, log_fn=logs.append,
        )
    )
    assert executed == 0
    assert manual.cycle_calls == []
    assert sleeps == []  # stopped before ever sleeping -- no wasted interval once told to stop
    assert any("KILL_SWITCH_ENGAGED" in line for line in logs)


def test_run_scheduled_live_loop_kill_switch_stops_mid_loop(tmp_path):
    manual = FakeManualTriggerModule()
    client = FakeAlpacaClient(is_open=True)
    kill_switch = tmp_path / "STOP"

    def sleep_and_engage_kill_switch(seconds):
        kill_switch.write_text("stop")

    executed = M.run_scheduled_live_loop(
        **_loop_kwargs(
            manual, client, tmp_path,
            kill_switch_path=kill_switch, max_iterations=5, sleep_fn=sleep_and_engage_kill_switch,
        )
    )
    assert executed == 1  # first cycle ran; kill switch appeared during its sleep, caught at the top of iteration 2
    assert len(manual.cycle_calls) == 1


def test_run_scheduled_live_loop_skips_when_market_closed(tmp_path):
    manual = FakeManualTriggerModule()
    client = FakeAlpacaClient(is_open=False)
    logs = []
    executed = M.run_scheduled_live_loop(
        **_loop_kwargs(manual, client, tmp_path, market_hours_only=True, max_iterations=2, log_fn=logs.append)
    )
    assert executed == 0
    assert manual.cycle_calls == []
    assert all("MARKET_CLOSED" in line for line in logs)


def test_run_scheduled_live_loop_clock_check_failure_does_not_crash_and_does_not_run_cycle(tmp_path):
    manual = FakeManualTriggerModule()
    client = FakeAlpacaClient(clock_exception=RuntimeError("network blip"))
    logs = []
    executed = M.run_scheduled_live_loop(
        **_loop_kwargs(manual, client, tmp_path, market_hours_only=True, max_iterations=2, log_fn=logs.append)
    )
    assert executed == 0
    assert manual.cycle_calls == []
    assert any("MARKET_CLOCK_CHECK_FAILED" in line for line in logs)


def test_run_scheduled_live_loop_skips_when_daily_cap_already_reached(tmp_path):
    manual = FakeManualTriggerModule()
    client = FakeAlpacaClient(is_open=True)
    log_path = tmp_path / "orders.jsonl"
    M.append_daily_order_log_entry(log_path, submitted_count=5, as_of=NOW)
    logs = []
    executed = M.run_scheduled_live_loop(
        **_loop_kwargs(
            manual, client, tmp_path,
            max_orders_per_day=5, daily_order_log_path=log_path, max_iterations=1, log_fn=logs.append,
        )
    )
    assert executed == 0
    assert manual.cycle_calls == []
    assert any("DAILY_ORDER_CAP_REACHED" in line for line in logs)


def test_run_scheduled_live_loop_stops_submitting_after_cap_reached_mid_run(tmp_path):
    manual = FakeManualTriggerModule(cycle_results=[{"stage1_report": {"submitted_count": 5, "outcomes": []}}])
    client = FakeAlpacaClient(is_open=True)
    logs = []
    executed = M.run_scheduled_live_loop(
        **_loop_kwargs(
            manual, client, tmp_path,
            max_new_orders_per_cycle=5, max_orders_per_day=5, max_iterations=2, log_fn=logs.append,
        )
    )
    assert executed == 1  # first cycle ran and submitted all 5; second cycle skipped by the daily cap
    assert len(manual.cycle_calls) == 1
    assert any("DAILY_ORDER_CAP_REACHED" in line for line in logs)


def test_run_scheduled_live_loop_appends_daily_order_log_entry_per_executed_cycle(tmp_path):
    manual = FakeManualTriggerModule(
        cycle_results=[
            {"stage1_report": {"submitted_count": 1, "outcomes": []}},
            {"stage1_report": {"submitted_count": 0, "outcomes": []}},
        ]
    )
    client = FakeAlpacaClient(is_open=True)
    log_path = tmp_path / "orders.jsonl"
    executed = M.run_scheduled_live_loop(
        **_loop_kwargs(manual, client, tmp_path, daily_order_log_path=log_path, max_iterations=2)
    )
    assert executed == 2
    assert M.read_todays_submitted_count(log_path, today_key=M._day_key(NOW.isoformat())) == 1


def test_run_scheduled_live_loop_threads_limits_into_every_cycle_call(tmp_path):
    manual = FakeManualTriggerModule()
    client = FakeAlpacaClient(is_open=True)
    limits = object()
    M.run_scheduled_live_loop(**_loop_kwargs(manual, client, tmp_path, limits=limits, max_iterations=2))
    assert all(call["limits"] is limits for call in manual.cycle_calls)


def test_run_scheduled_live_loop_writes_cycle_files_via_357s_write_cycle_result(tmp_path):
    manual = FakeManualTriggerModule()
    client = FakeAlpacaClient(is_open=True)
    output_dir = tmp_path / "out"
    M.run_scheduled_live_loop(**_loop_kwargs(manual, client, tmp_path, output_dir=output_dir, max_iterations=2))
    cycle_files = sorted(output_dir.glob("cycle_*.json"))
    assert len(cycle_files) == 2
    assert (output_dir / "latest.json").exists()


def test_run_scheduled_live_loop_passes_symbol_source_to_every_cycle(tmp_path):
    manual = FakeManualTriggerModule()
    client = FakeAlpacaClient(is_open=True)
    M.run_scheduled_live_loop(
        **_loop_kwargs(
            manual, client, tmp_path,
            symbol_requests=manual.equity_cli.pinned_universe_symbol_requests,
            symbol_source="scan_pinned_universe:v1:2_symbols", max_iterations=2,
        )
    )
    assert all(call["symbol_source"] == "scan_pinned_universe:v1:2_symbols" for call in manual.cycle_calls)


# ============================================================================
# 6. main() CLI boundary
# ============================================================================


def test_main_without_confirm_flag_exits_before_running(tmp_path):
    config_path = tmp_path / "requests.json"
    config_path.write_text('[{"symbol": "AAPL", "asset_class": "STOCK", "quantity": 1}]')
    with pytest.raises(SystemExit):
        M.main([
            "--requests-config", str(config_path),
            "--max-snapshot-age-seconds", "300",
            "--fill-poll-timeout-seconds", "1",
            "--fill-poll-interval-seconds", "1",
            # deliberately NOT passing --i-confirm-this-runs-unattended-live-paper-trading
        ])


def test_main_requires_exactly_one_of_requests_config_or_scan_pinned_universe_when_neither_given(capsys):
    rc = M.main([
        "--max-snapshot-age-seconds", "300",
        "--fill-poll-timeout-seconds", "1",
        "--fill-poll-interval-seconds", "1",
        "--i-confirm-this-runs-unattended-live-paper-trading",
    ])
    assert rc == 1
    captured = capsys.readouterr()
    assert "EXACTLY_ONE_OF_REQUESTS_CONFIG_OR_SCAN_PINNED_UNIVERSE_OR_SCAN_FULL_UNIVERSE_REQUIRED" in captured.err


def test_main_requires_exactly_one_of_requests_config_or_scan_pinned_universe_when_both_given(tmp_path, capsys):
    config_path = tmp_path / "requests.json"
    config_path.write_text('[{"symbol": "AAPL", "asset_class": "STOCK", "quantity": 1}]')
    rc = M.main([
        "--requests-config", str(config_path),
        "--scan-pinned-universe",
        "--max-snapshot-age-seconds", "300",
        "--fill-poll-timeout-seconds", "1",
        "--fill-poll-interval-seconds", "1",
        "--i-confirm-this-runs-unattended-live-paper-trading",
    ])
    assert rc == 1
    captured = capsys.readouterr()
    assert "EXACTLY_ONE_OF_REQUESTS_CONFIG_OR_SCAN_PINNED_UNIVERSE_OR_SCAN_FULL_UNIVERSE_REQUIRED" in captured.err


def test_main_fails_closed_on_missing_credentials(tmp_path, monkeypatch, capsys):
    manual = FakeManualTriggerModule(
        equity_cli=FakeEquityCliModule(credentials_error=FakeCliError("MISSING_ALPACA_EQUITY_PAPER_CREDENTIALS: ..."))
    )
    monkeypatch.setattr(M, "load_manual_trigger_module", lambda: manual)
    monkeypatch.setattr(M, "load_scheduled_runner_module", lambda: STAGE357)
    monkeypatch.setattr(M, "load_enforcement_module", lambda: ENFORCE)

    config_path = tmp_path / "requests.json"
    config_path.write_text('[{"symbol": "AAPL", "asset_class": "STOCK", "quantity": 1}]')
    rc = M.main([
        "--requests-config", str(config_path),
        "--max-snapshot-age-seconds", "300",
        "--fill-poll-timeout-seconds", "1",
        "--fill-poll-interval-seconds", "1",
        "--i-confirm-this-runs-unattended-live-paper-trading",
    ])
    assert rc == 1
    captured = capsys.readouterr()
    assert "FAIL-CLOSED" in captured.err


def test_main_scan_pinned_universe_threads_defaults_and_limits(monkeypatch, capsys):
    manual = FakeManualTriggerModule()
    monkeypatch.setattr(M, "load_manual_trigger_module", lambda: manual)
    monkeypatch.setattr(M, "load_scheduled_runner_module", lambda: STAGE357)
    monkeypatch.setattr(M, "load_enforcement_module", lambda: ENFORCE)

    captured_kwargs: dict = {}

    def fake_loop(**kwargs):
        captured_kwargs.update(kwargs)
        return 0

    monkeypatch.setattr(M, "run_scheduled_live_loop", fake_loop)

    rc = M.main([
        "--scan-pinned-universe",
        "--max-snapshot-age-seconds", "300",
        "--fill-poll-timeout-seconds", "1",
        "--fill-poll-interval-seconds", "1",
        "--i-confirm-this-runs-unattended-live-paper-trading",
    ])
    assert rc == 0
    assert captured_kwargs["symbol_requests"] == manual.equity_cli.pinned_universe_symbol_requests
    assert captured_kwargs["symbol_source"] == "scan_pinned_universe:v1:2_symbols"
    assert captured_kwargs["max_new_orders_per_cycle"] == M.DEFAULT_MAX_NEW_ORDERS_PER_CYCLE
    assert captured_kwargs["max_orders_per_day"] == M.DEFAULT_MAX_ORDERS_PER_DAY
    assert captured_kwargs["kill_switch_path"] == M.DEFAULT_KILL_SWITCH_PATH
    assert captured_kwargs["interval_seconds"] == M.DEFAULT_INTERVAL_SECONDS

    limits = captured_kwargs["limits"]
    assert limits.max_daily_loss_pct_by_venue == {"ALPACA": 0.02}
    assert limits.max_drawdown_pct_by_venue == {"ALPACA": 0.05}
    assert limits.max_asset_concentration_ratio == 0.10
    assert limits.max_net_exposure_ratio == 0.50

    captured_out = capsys.readouterr().out
    assert "KILL SWITCH" in captured_out
    assert "THIS MODULE CAN SUBMIT REAL ORDERS" in captured_out
    assert str(M.DEFAULT_KILL_SWITCH_PATH) in captured_out


def test_main_risk_limit_flags_override_defaults(monkeypatch):
    manual = FakeManualTriggerModule()
    monkeypatch.setattr(M, "load_manual_trigger_module", lambda: manual)
    monkeypatch.setattr(M, "load_scheduled_runner_module", lambda: STAGE357)
    monkeypatch.setattr(M, "load_enforcement_module", lambda: ENFORCE)

    captured_kwargs: dict = {}

    def fake_loop(**kwargs):
        captured_kwargs.update(kwargs)
        return 0

    monkeypatch.setattr(M, "run_scheduled_live_loop", fake_loop)

    M.main([
        "--scan-pinned-universe",
        "--max-snapshot-age-seconds", "300",
        "--fill-poll-timeout-seconds", "1",
        "--fill-poll-interval-seconds", "1",
        "--max-daily-loss-pct", "0.01",
        "--max-orders-per-day", "2",
        "--i-confirm-this-runs-unattended-live-paper-trading",
    ])
    assert captured_kwargs["limits"].max_daily_loss_pct_by_venue == {"ALPACA": 0.01}
    assert captured_kwargs["max_orders_per_day"] == 2


def test_main_stops_cleanly_on_keyboard_interrupt(tmp_path, monkeypatch, capsys):
    manual = FakeManualTriggerModule()
    monkeypatch.setattr(M, "load_manual_trigger_module", lambda: manual)
    monkeypatch.setattr(M, "load_scheduled_runner_module", lambda: STAGE357)
    monkeypatch.setattr(M, "load_enforcement_module", lambda: ENFORCE)

    def raise_interrupt(**kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(M, "run_scheduled_live_loop", raise_interrupt)

    config_path = tmp_path / "requests.json"
    config_path.write_text('[{"symbol": "AAPL", "asset_class": "STOCK", "quantity": 1}]')
    rc = M.main([
        "--requests-config", str(config_path),
        "--max-snapshot-age-seconds", "300",
        "--fill-poll-timeout-seconds", "1",
        "--fill-poll-interval-seconds", "1",
        "--i-confirm-this-runs-unattended-live-paper-trading",
    ])
    assert rc == 0
    captured = capsys.readouterr()
    assert "Stopped by Ctrl+C" in captured.out


# ============================================================================
# 7. Structural safety -- exactly one call site for the real-submission
#    entry point, mirroring `.357`'s own AST-level structural check.
# ============================================================================


def test_module_calls_run_manual_trigger_stage1b_cycle_exactly_once_as_code():
    source = (ROOT / "aura_v05365_stage1b_scheduled_live_trader.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "run_manual_trigger_stage1b_cycle"
    ]
    assert len(calls) == 1


# ============================================================================
# 8. Lightweight integration smoke test -- real `.363`/`.357`/`.344`
#    modules load and expose what `.365` needs. No network, no
#    credentials, no real cycle.
# ============================================================================


def test_load_manual_trigger_module_returns_real_363_with_expected_surface():
    manual = M.load_manual_trigger_module()
    for attr in ("Stage1BManualTriggerCliError", "load_equity_cli_module", "run_manual_trigger_stage1b_cycle"):
        assert hasattr(manual, attr), f"real .363 module is missing expected attribute: {attr}"


def test_load_scheduled_runner_module_returns_real_357_with_expected_surface():
    stage3_runner = M.load_scheduled_runner_module()
    for attr in ("is_market_open", "write_cycle_result"):
        assert hasattr(stage3_runner, attr), f"real .357 module is missing expected attribute: {attr}"


def test_load_enforcement_module_returns_real_344_with_portfolio_limits():
    enforcement_module = M.load_enforcement_module()
    assert hasattr(enforcement_module, "PortfolioLimits")
    # .365's own build_default_limits against the REAL .344 dataclass, not just the local test copy above
    limits = M.build_default_limits(enforcement_module)
    assert limits.to_dict()["max_daily_loss_pct_by_venue"] == {"ALPACA": 0.02}


# ============================================================================
# 9. Earnings-blackout wiring -- Extension, 2026-09-29 (Martin, "Lets go
#    for Earnings blackout"). Same "thread it through unmodified" pattern
#    already proven for `limits`/`news_state_dir` above.
# ============================================================================


def test_run_one_live_cycle_threads_earnings_kwargs_into_manual_trigger_call():
    manual = FakeManualTriggerModule()
    M.run_one_live_cycle(
        manual_trigger_module=manual,
        symbol_requests=manual.equity_cli.symbol_requests,
        bars_client="FAKE_BARS_CLIENT",
        alpaca_client=manual.equity_cli.alpaca_client,
        max_new_orders_per_cycle=1,
        lookback_bars=55,
        universe_version="TEST_UNIVERSE_VERSION",
        max_snapshot_age_seconds=300.0,
        fill_poll_timeout_seconds=1.0,
        fill_poll_interval_seconds=1.0,
        limits=object(),
        now=NOW,
        earnings_calendar_api_key="FAKE_FMP_KEY",
        earnings_state_dir=Path("/tmp/fake-earnings-state"),
    )
    call = manual.cycle_calls[0]
    assert call["earnings_calendar_api_key"] == "FAKE_FMP_KEY"
    assert call["earnings_state_dir"] == Path("/tmp/fake-earnings-state")


def test_run_one_live_cycle_earnings_kwargs_default_to_none():
    manual = FakeManualTriggerModule()
    M.run_one_live_cycle(
        manual_trigger_module=manual,
        symbol_requests=manual.equity_cli.symbol_requests,
        bars_client="FAKE_BARS_CLIENT",
        alpaca_client=manual.equity_cli.alpaca_client,
        max_new_orders_per_cycle=1,
        lookback_bars=55,
        universe_version="TEST_UNIVERSE_VERSION",
        max_snapshot_age_seconds=300.0,
        fill_poll_timeout_seconds=1.0,
        fill_poll_interval_seconds=1.0,
        limits=object(),
        now=NOW,
    )
    call = manual.cycle_calls[0]
    assert call["earnings_calendar_api_key"] is None
    assert call["earnings_state_dir"] is None


def test_run_scheduled_live_loop_threads_earnings_kwargs_into_every_cycle_call(tmp_path):
    manual = FakeManualTriggerModule()
    client = FakeAlpacaClient(is_open=True)
    M.run_scheduled_live_loop(**_loop_kwargs(
        manual, client, tmp_path, max_iterations=3,
        earnings_calendar_api_key="FAKE_FMP_KEY", earnings_state_dir=tmp_path / "earnings_state",
    ))
    assert len(manual.cycle_calls) == 3
    for call in manual.cycle_calls:
        assert call["earnings_calendar_api_key"] == "FAKE_FMP_KEY"
        assert call["earnings_state_dir"] == tmp_path / "earnings_state"


def test_main_earnings_state_dir_omitted_disables_the_gate_entirely(monkeypatch, tmp_path):
    manual = FakeManualTriggerModule()
    monkeypatch.setattr(M, "load_manual_trigger_module", lambda: manual)
    monkeypatch.setattr(M, "load_scheduled_runner_module", lambda: STAGE357)
    monkeypatch.setattr(M, "load_enforcement_module", lambda: ENFORCE)

    captured_kwargs: dict = {}

    def fake_loop(**kwargs):
        captured_kwargs.update(kwargs)
        return 0

    monkeypatch.setattr(M, "run_scheduled_live_loop", fake_loop)

    config_path = tmp_path / "requests.json"
    config_path.write_text('[{"symbol": "AAPL", "asset_class": "STOCK", "quantity": 1}]')
    rc = M.main([
        "--requests-config", str(config_path),
        "--max-snapshot-age-seconds", "300",
        "--fill-poll-timeout-seconds", "1",
        "--fill-poll-interval-seconds", "1",
        "--max-iterations", "1",
        "--i-confirm-this-runs-unattended-live-paper-trading",
    ])
    assert rc == 0
    assert captured_kwargs["earnings_calendar_api_key"] is None
    assert captured_kwargs["earnings_state_dir"] is None


def test_main_earnings_state_dir_supplied_loads_fmp_key_and_threads_it_through(monkeypatch, tmp_path):
    manual = FakeManualTriggerModule()
    monkeypatch.setattr(M, "load_manual_trigger_module", lambda: manual)
    monkeypatch.setattr(M, "load_scheduled_runner_module", lambda: STAGE357)
    monkeypatch.setattr(M, "load_enforcement_module", lambda: ENFORCE)

    captured_kwargs: dict = {}

    def fake_loop(**kwargs):
        captured_kwargs.update(kwargs)
        return 0

    monkeypatch.setattr(M, "run_scheduled_live_loop", fake_loop)

    config_path = tmp_path / "requests.json"
    config_path.write_text('[{"symbol": "AAPL", "asset_class": "STOCK", "quantity": 1}]')
    earnings_dir = tmp_path / "earnings_state"
    rc = M.main([
        "--requests-config", str(config_path),
        "--max-snapshot-age-seconds", "300",
        "--fill-poll-timeout-seconds", "1",
        "--fill-poll-interval-seconds", "1",
        "--max-iterations", "1",
        "--i-confirm-this-runs-unattended-live-paper-trading",
        "--earnings-state-dir", str(earnings_dir),
    ])
    assert rc == 0
    assert captured_kwargs["earnings_calendar_api_key"] == manual.earnings_calendar_module.api_key
    assert captured_kwargs["earnings_state_dir"] == earnings_dir


def test_main_earnings_state_dir_supplied_without_fmp_key_fails_closed_before_any_cycle(monkeypatch, tmp_path, capsys):
    manual = FakeManualTriggerModule(
        earnings_calendar_module=FakeEarningsCalendarModule(
            api_key_error=EarningsCalendarIngestionError("MISSING_FMP_API_KEY: set FMP_API_KEY ...")
        )
    )
    monkeypatch.setattr(M, "load_manual_trigger_module", lambda: manual)
    monkeypatch.setattr(M, "load_scheduled_runner_module", lambda: STAGE357)
    monkeypatch.setattr(M, "load_enforcement_module", lambda: ENFORCE)

    config_path = tmp_path / "requests.json"
    config_path.write_text('[{"symbol": "AAPL", "asset_class": "STOCK", "quantity": 1}]')
    rc = M.main([
        "--requests-config", str(config_path),
        "--max-snapshot-age-seconds", "300",
        "--fill-poll-timeout-seconds", "1",
        "--fill-poll-interval-seconds", "1",
        "--max-iterations", "1",
        "--i-confirm-this-runs-unattended-live-paper-trading",
        "--earnings-state-dir", str(tmp_path / "earnings_state"),
    ])
    assert rc == 1
    captured = capsys.readouterr()
    assert "FAIL-CLOSED" in captured.err
    assert "MISSING_FMP_API_KEY" in captured.err
    assert manual.cycle_calls == []  # no cycle ever ran -- failed before the loop started


# ============================================================================
# Extension -- 2026-10-01 ("lets go for #4"): optional `decision_journal_
# path` threaded through run_one_live_cycle() -> .363, and through
# run_scheduled_live_loop() -> run_one_live_cycle() for every iteration --
# same shape as the earnings-kwargs threading tests just above.
# ============================================================================


def test_run_one_live_cycle_threads_decision_journal_path_into_manual_trigger_call():
    manual = FakeManualTriggerModule()
    M.run_one_live_cycle(
        manual_trigger_module=manual,
        symbol_requests=manual.equity_cli.symbol_requests,
        bars_client="FAKE_BARS_CLIENT",
        alpaca_client=manual.equity_cli.alpaca_client,
        max_new_orders_per_cycle=1,
        lookback_bars=55,
        universe_version="TEST_UNIVERSE_VERSION",
        max_snapshot_age_seconds=300.0,
        fill_poll_timeout_seconds=1.0,
        fill_poll_interval_seconds=1.0,
        limits=object(),
        now=NOW,
        decision_journal_path=Path("/tmp/fake-decision-journal.jsonl"),
    )
    call = manual.cycle_calls[0]
    assert call["decision_journal_path"] == Path("/tmp/fake-decision-journal.jsonl")


def test_run_one_live_cycle_decision_journal_path_defaults_to_none():
    manual = FakeManualTriggerModule()
    M.run_one_live_cycle(
        manual_trigger_module=manual,
        symbol_requests=manual.equity_cli.symbol_requests,
        bars_client="FAKE_BARS_CLIENT",
        alpaca_client=manual.equity_cli.alpaca_client,
        max_new_orders_per_cycle=1,
        lookback_bars=55,
        universe_version="TEST_UNIVERSE_VERSION",
        max_snapshot_age_seconds=300.0,
        fill_poll_timeout_seconds=1.0,
        fill_poll_interval_seconds=1.0,
        limits=object(),
        now=NOW,
    )
    call = manual.cycle_calls[0]
    assert call["decision_journal_path"] is None


def test_run_scheduled_live_loop_threads_decision_journal_path_into_every_cycle_call(tmp_path):
    manual = FakeManualTriggerModule()
    client = FakeAlpacaClient(is_open=True)
    journal_path = tmp_path / "decision_journal.jsonl"
    M.run_scheduled_live_loop(**_loop_kwargs(
        manual, client, tmp_path, max_iterations=3, decision_journal_path=journal_path,
    ))
    assert len(manual.cycle_calls) == 3
    for call in manual.cycle_calls:
        assert call["decision_journal_path"] == journal_path


def test_main_decision_journal_path_argument_is_threaded_through(monkeypatch, tmp_path):
    manual = FakeManualTriggerModule()
    monkeypatch.setattr(M, "load_manual_trigger_module", lambda: manual)
    monkeypatch.setattr(M, "load_scheduled_runner_module", lambda: STAGE357)
    monkeypatch.setattr(M, "load_enforcement_module", lambda: ENFORCE)

    captured_kwargs: dict = {}

    def fake_loop(**kwargs):
        captured_kwargs.update(kwargs)
        return 0

    monkeypatch.setattr(M, "run_scheduled_live_loop", fake_loop)

    config_path = tmp_path / "requests.json"
    config_path.write_text('[{"symbol": "AAPL", "asset_class": "STOCK", "quantity": 1}]')
    journal_path = tmp_path / "decision_journal.jsonl"
    rc = M.main([
        "--requests-config", str(config_path),
        "--max-snapshot-age-seconds", "300",
        "--fill-poll-timeout-seconds", "1",
        "--fill-poll-interval-seconds", "1",
        "--max-iterations", "1",
        "--i-confirm-this-runs-unattended-live-paper-trading",
        "--decision-journal-path", str(journal_path),
    ])
    assert rc == 0
    assert captured_kwargs["decision_journal_path"] == journal_path


def test_main_decision_journal_path_omitted_defaults_to_none(monkeypatch, tmp_path):
    manual = FakeManualTriggerModule()
    monkeypatch.setattr(M, "load_manual_trigger_module", lambda: manual)
    monkeypatch.setattr(M, "load_scheduled_runner_module", lambda: STAGE357)
    monkeypatch.setattr(M, "load_enforcement_module", lambda: ENFORCE)

    captured_kwargs: dict = {}

    def fake_loop(**kwargs):
        captured_kwargs.update(kwargs)
        return 0

    monkeypatch.setattr(M, "run_scheduled_live_loop", fake_loop)

    config_path = tmp_path / "requests.json"
    config_path.write_text('[{"symbol": "AAPL", "asset_class": "STOCK", "quantity": 1}]')
    rc = M.main([
        "--requests-config", str(config_path),
        "--max-snapshot-age-seconds", "300",
        "--fill-poll-timeout-seconds", "1",
        "--fill-poll-interval-seconds", "1",
        "--max-iterations", "1",
        "--i-confirm-this-runs-unattended-live-paper-trading",
    ])
    assert rc == 0
    assert captured_kwargs["decision_journal_path"] is None
