#!/usr/bin/env python3
"""
AURA v0.5.3.65 -- Track B: continuous, unattended, REAL-order-submission
loop around `.363`'s existing manual-trigger CLI
(`aura_v05363_stage1b_manual_trigger_cli.py`).

WHAT THIS MODULE IS
------------------------------------------------------------------------
Martin (2026-09-29): "I want it to actually trade on its own from here so
let get started." This is that: the first module in the repo that can
submit real (paper) orders REPEATEDLY, UNATTENDED, over time, without a
person re-running a CLI command each time. Before this module, `.363` was
a one-shot manual trigger -- run it, it submits (at most) one capped
batch of orders, then exits. This module is `.363`'s continuous-loop
equivalent, exactly the way `.357` is `.356`'s continuous-loop equivalent
-- same shape, same reuse discipline, but pointed at the module that can
actually reach the broker.

Given that, this is also the most safety-sensitive module in this
project so far. Every mechanism below traces to an explicit Martin
decision (AskUserQuestion, 2026-09-29) -- nothing here is guessed.

WHAT THIS MODULE REUSES, AND WHAT IS GENUINELY NEW
------------------------------------------------------------------------
- Every piece of trading logic (evidence, sizing, decision, `.44`
  enforcement, `.38`/`.36` supervised submission) comes from `.363`'s
  existing `run_manual_trigger_stage1b_cycle()`, called unmodified --
  this module does not reimplement or duplicate any of it.
- The market-hours gate (`is_market_open`) and the per-cycle-result
  file-writer (`write_cycle_result`) are `.357`'s own, already-tested
  functions, imported and called directly -- not copy-pasted.
- The "skip symbols already held" requirement Martin asked for needs NO
  new code here: `.355`'s `run_stage1b_paper_cycle` (which `.363` already
  calls) fetches a REAL Alpaca positions snapshot every cycle and marks
  any already-held symbol `already_held=True`; `.53`'s `run_cycle`
  immediately records those as stage `SKIPPED_ALREADY_HELD` -- BEFORE any
  new evidence/decision/enforcement path runs for that symbol (see
  `aura_v05353_full_paper_orchestration.py`, ~line 385, and its module
  docstring's "Scope: entries only" section). This was verified by
  reading that code before writing this module, specifically so this
  module would not duplicate a safety mechanism that already exists.
- Genuinely new in this module: (1) the continuous loop itself; (2) a
  file-based kill switch, checked first, every cycle; (3) an explicit
  conservative `.344.PortfolioLimits`, threaded through `.363`'s newly-
  exposed `limits` parameter (see `.363`'s 2026-09-29 extension); (4) a
  hard daily ceiling on REAL submitted orders, persisted so it survives a
  process restart -- nothing like this existed anywhere in the repo
  before.

Five safety mechanisms (Martin, AskUserQuestion, 2026-09-29)
------------------------------------------------------------------------
1. CONSERVATIVE PORTFOLIO LIMITS -- `DEFAULT_LIMITS` below
   (`max_daily_loss_pct_by_venue={"ALPACA": 0.02}`,
   `max_drawdown_pct_by_venue={"ALPACA": 0.05}`,
   `max_asset_concentration_ratio=0.10`, `max_net_exposure_ratio=0.50`)
   -- Martin's own confirmed numeric values, passed as `.363`'s new
   `limits` parameter on every cycle. `.44`'s other dimensions
   (`max_portfolio_heat_ratio`, leverage, correlated groups) are left
   unconfigured (`LIMIT_NOT_CONFIGURED`, i.e. not blocking) exactly as
   everywhere else in this repo -- Martin was asked about and confirmed
   only these four numbers; this module does not invent additional ones.
   Overridable via CLI flags for a future adjustment, defaulting to
   Martin's approved values (same "approved default, overridable"
   pattern `.357` already uses for `--interval-seconds`).
2. FILE-BASED KILL SWITCH -- `check_kill_switch()` / `--kill-switch-file`
   (default: `<this file's directory>/STOP_365_LIVE_TRADER`). Checked
   FIRST, every iteration, before the market-hours check, before touching
   any client. If the file exists, the loop logs it and STOPS entirely
   (returns; does not `sleep` and retry) -- Martin creates this file
   (e.g. `New-Item STOP_365_LIVE_TRADER` from the repo directory in
   PowerShell) to halt the loop at any time; deleting the file and
   restarting the process resumes it. This module never deletes the file
   itself -- its continued presence after a stop is deliberate, so a
   restart does not silently resume without Martin noticing.
3. SKIP ALREADY-HELD SYMBOLS -- see "WHAT THIS MODULE REUSES" above.
   Free, already-tested, no new code.
4. MODERATE PACE -- `DEFAULT_INTERVAL_SECONDS = 300.0` (5 minutes, same
   value/constant `.357` already uses) and `DEFAULT_MAX_NEW_ORDERS_PER_
   CYCLE = 1`. Both overridable via CLI flags.
5. HARD DAILY CEILING ON REAL ORDERS -- `DEFAULT_MAX_ORDERS_PER_DAY = 5`.
   Genuinely new: nothing in `.33`-`.364` counts submitted orders across
   cycles/days. Implemented as a small append-only JSONL log (one line
   per cycle that actually ran, recording that cycle's real
   `submitted_count`), mirroring `.364`'s own append-only-log pattern and
   convention exactly rather than inventing a new persistence mechanism.
   "Day" is the UTC calendar date of each cycle's `as_of` timestamp --
   the same day-boundary convention `.343`'s `_venue_day_key` (used by
   `.44`'s daily_loss check) already uses, not a new one invented here.
   Before running a cycle, this module sums today's already-logged
   `submitted_count`; if the sum is already >= the ceiling, the cycle is
   skipped entirely (no evidence fetch, no broker call at all this
   cycle) and logged as `DAILY_ORDER_CAP_REACHED`. This log is kept
   local to this module (not a new numbered `.366` file) since nothing
   else in the repo needs "orders submitted today" -- unlike `.364`'s
   equity history, which multiple callers could plausibly want.

Sixth mechanism -- EARNINGS BLACKOUT (Extension, 2026-09-29, Martin: "Lets
go for Earnings blackout")
------------------------------------------------------------------------
Optional, via `--earnings-state-dir` (mirrors `--news-state-dir` exactly:
omit it and this mechanism is entirely inert, reproducing this module's
pre-existing behavior). When supplied, `.67` fetches/caches a Financial
Modeling Prep earnings calendar (free tier; Martin's confirmed provider,
2026-09-29 AskUserQuestion) and `.68`'s day-of blackout check is threaded
through `.363`'s newly-exposed `earnings_state_dir`/
`earnings_calendar_api_key` parameters into `.55`'s existing enforcement
choke point -- blocking a NEW entry (never an exit) for any symbol
reporting earnings that trading day. Fail-closed, Martin's explicit
choice (AskUserQuestion, 2026-09-29): if the calendar can't be fetched and
no fresh cache exists, `.68` blocks NEW entries for EVERY symbol that
cycle, not just the ones actually reporting -- see `.68`'s own module
docstring for why this is the safe default rather than silently trading
through an FMP outage. Requires `FMP_API_KEY` in the environment (see
`.env.example`).

Market-hours gate
------------------------------------------------------------------------
Reuses `.357.is_market_open()` verbatim (a read-only `get_clock()` call)
-- same behavior/semantics as `.357`'s own loop: skip the cycle (not the
whole run) while the market is closed, keep looping.

Confirmation
------------------------------------------------------------------------
`--i-confirm-this-runs-unattended-live-paper-trading` is a REQUIRED CLI
flag (argparse `required=True` on a `store_true`, same discipline as
`.363`'s own `--i-confirm-this-submits-real-paper-orders`) -- there is no
way to pass it as false; omitting it refuses to start before any client
is built. This is a SEPARATE, more explicit confirmation than `.363`'s
own, because this process is more dangerous than a single manual
invocation: once started, it keeps running (and can keep submitting
orders, subject to the five mechanisms above) until Martin stops it via
the kill switch or Ctrl+C. Internally, `confirmed=True` is then passed to
every call to `.363.run_manual_trigger_stage1b_cycle()` this process
makes -- this module's own CLI-level confirmation stands in for the
per-call confirmation `.363` itself would otherwise separately require.

Scope
------------------------------------------------------------------------
Equity/ETF only, Alpaca only, PAPER account only (inherited unchanged
from `.363`/`.355` -- this module constructs no client of its own kind
and does not alter `paper=True`). Track A/MEXC is not imported,
referenced, or touched anywhere in this module. `--scan-pinned-universe`
/ `--requests-config` (exactly one required) reuses `.356`'s already-
tested `build_symbol_requests_from_pinned_universe`, same as `.357`/
`.363` -- no new symbol-list logic is written here.

Output
------------------------------------------------------------------------
Each executed cycle's full result (from `.363.run_manual_trigger_stage1b_
cycle()`, same shape Martin already inspected from a real `.363` run) is
written via `.357.write_cycle_result()` into its own `--output-dir`
(default `stage1b_live_trader_output/`, DELIBERATELY separate from
`.357`'s own `stage3_scheduled_output/` -- this module's output can
contain real submissions and must never be confused with `.357`'s
permanently-preview-only output by a dashboard or a person skimming the
directory listing).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

VERSION = "AURA v0.5.3.65"
ENGINE = "STAGE3B_SCHEDULED_LIVE_TRADER"

ROOT = Path(__file__).resolve().parent

# -- Moderate pace (Martin, AskUserQuestion, 2026-09-29). Same value/
#    constant .357 already uses for its own default interval. --
DEFAULT_INTERVAL_SECONDS = 300.0
DEFAULT_MAX_NEW_ORDERS_PER_CYCLE = 1

# -- Hard daily ceiling on REAL submitted orders (Martin, AskUserQuestion,
#    2026-09-29). Genuinely new concept -- see module docstring, mechanism 5. --
DEFAULT_MAX_ORDERS_PER_DAY = 5
DEFAULT_DAILY_ORDER_LOG_PATH = Path("regime_output/live_trader_order_log/stage1b_daily_order_log.jsonl")

# -- File-based kill switch (Martin, AskUserQuestion, 2026-09-29). --
DEFAULT_KILL_SWITCH_PATH = ROOT / "STOP_365_LIVE_TRADER"

DEFAULT_OUTPUT_DIR = ROOT / "stage1b_live_trader_output"

DEFAULT_STRATEGY_ID = "STAGE1B_SCHEDULED_LIVE_TRADER"
DEFAULT_STRATEGY_VERSION = "v1-scheduled-live-trader"


class ScheduledLiveTraderError(Exception):
    """Programmer-error / missing-required-input / fail-closed-safety
    violations only -- same discipline as `.357`'s `ScheduledRunnerError`
    and `.363`'s `Stage1BManualTriggerCliError`."""


def _load_module(module_name: str, filename: str):
    try:
        return __import__(module_name)
    except ImportError:
        import importlib.util

        spec = importlib.util.spec_from_file_location(module_name, ROOT / filename)
        mod = importlib.util.module_from_spec(spec)
        # Fix, 2026-10-01 (Python 3.14 compatibility): register the module in
        # sys.modules BEFORE exec_module -- see .355's own _load_module for
        # the full explanation (frozen+slots dataclasses need this for
        # ClassVar detection under Python 3.14's dataclasses internals).
        sys.modules[module_name] = mod
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return mod


def load_manual_trigger_module():
    """`.363` -- the ONLY place this module gets its actual trading-cycle
    logic from (which itself reuses `.356`/`.355`/`.53`/`.44`/`.38`/`.36`
    unmodified). Never duplicated, never reimplemented here."""
    return _load_module("aura_v05363_stage1b_manual_trigger_cli", "aura_v05363_stage1b_manual_trigger_cli.py")


def load_scheduled_runner_module():
    """`.357` -- source of `is_market_open()` and `write_cycle_result()`,
    both reused verbatim (see module docstring)."""
    return _load_module("aura_v05357_stage3_scheduled_runner", "aura_v05357_stage3_scheduled_runner.py")


def load_enforcement_module():
    """`.344` -- source of `PortfolioLimits`, used to build
    `DEFAULT_LIMITS` below. Not modified anywhere in this file."""
    return _load_module("aura_v05344_portfolio_exposure_enforcement", "aura_v05344_portfolio_exposure_enforcement.py")


def load_cross_awareness_guard_module():
    """`.371` -- DELTAX cross-awareness pre/post-cycle guard. Opt-in: a
    complete no-op unless `shared_intent_path` is supplied (see module
    docstring addendum, Extension 2026-10-02)."""
    return _load_module("aura_v05371_deltax_aware_cycle_guard", "aura_v05371_deltax_aware_cycle_guard.py")


def load_shared_intent_module():
    """`.370` -- the shared JSONL execution-intent log (read/write/lock
    logic), the identical byte-for-byte copy DELTAX's own repo also
    carries, both pointed at the same path on disk via
    `AURA_DELTAX_SHARED_INTENT_PATH` / `shared_intent_path`."""
    return _load_module("aura_v05370_shared_execution_intent", "aura_v05370_shared_execution_intent.py")


def load_observability_module():
    """`.343` -- source of `fetch_alpaca_portfolio`, used by `.371` to
    snapshot AURA's own ALPACA notional before/after a cycle. Not
    modified anywhere in this file."""
    return _load_module("aura_v05343_portfolio_exposure_observability", "aura_v05343_portfolio_exposure_observability.py")


def _now_iso(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()


def _day_key(iso_ts: str) -> str:
    """UTC calendar date -- same day-boundary convention as `.343`'s own
    `_venue_day_key` (used by `.44`'s daily_loss check), not a new one
    invented here."""
    return iso_ts[:10]


def build_default_limits(enforcement_module: Any) -> Any:
    """Martin's confirmed conservative risk limits (AskUserQuestion,
    2026-09-29): 2% max daily loss, 5% max drawdown (both ALPACA-venue),
    10% max single-asset concentration, 50% max net exposure. Every other
    `.344.PortfolioLimits` dimension is left at its default (`None`/empty
    -> `LIMIT_NOT_CONFIGURED`, not blocking) -- Martin was asked about and
    confirmed only these four numbers; this function does not add more."""
    return enforcement_module.PortfolioLimits(
        max_daily_loss_pct_by_venue={"ALPACA": 0.02},
        max_drawdown_pct_by_venue={"ALPACA": 0.05},
        max_asset_concentration_ratio=0.10,
        max_net_exposure_ratio=0.50,
    )


# ============================================================================
# 1. File-based kill switch -- checked first, every cycle, before anything
#    else (see module docstring, mechanism 2).
# ============================================================================


def check_kill_switch(kill_switch_path: Path) -> bool:
    """Read-only existence check. True means STOP. Never deletes or
    modifies the file -- resuming after a stop is a deliberate, separate
    action (delete the file, restart the process), never automatic."""
    return kill_switch_path.exists()


# ============================================================================
# 2. Daily order-count log -- genuinely new persistence (see module
#    docstring, mechanism 5). Append-only JSONL, one line per cycle that
#    actually ran, mirroring `.364`'s own append-only-log convention.
# ============================================================================


def append_daily_order_log_entry(log_path: Path, *, submitted_count: int, as_of: datetime) -> None:
    """Appends one record for a cycle that actually ran (whether or not it
    submitted anything -- mirrors `.364`'s "log every cycle" principle, so
    the log is a genuine, complete record of every cycle this process
    executed, not just the ones that submitted). Raises
    `ScheduledLiveTraderError` (never a bare OSError) on any failure --
    fail-closed: a caller must not treat a failed write as a recorded
    cycle."""
    as_of_iso = as_of.astimezone(timezone.utc).isoformat()
    entry = {"as_of": as_of_iso, "day_key": _day_key(as_of_iso), "submitted_count": int(submitted_count)}
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, sort_keys=True) + "\n")
    except OSError as exc:
        raise ScheduledLiveTraderError(f"failed to append daily order log entry to {log_path}: {exc}") from exc


def read_todays_submitted_count(log_path: Path, *, today_key: str) -> int:
    """Sums `submitted_count` across every logged entry whose `day_key`
    matches `today_key`. Returns 0 if the log does not exist yet -- an
    empty/unstarted log is not an error, mirroring `.364`'s identical
    choice for its own equity history log. Raises
    `ScheduledLiveTraderError` on a corrupt line rather than silently
    skipping it (fail-closed on corruption, matching `.364`'s discipline)."""
    if not log_path.exists():
        return 0
    total = 0
    with log_path.open("r", encoding="utf-8") as f:
        for line_no, raw_line in enumerate(f, start=1):
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                obj = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                raise ScheduledLiveTraderError(f"corrupt daily order log entry at {log_path}:{line_no}: {exc}") from exc
            if obj.get("day_key") == today_key:
                total += int(obj.get("submitted_count", 0))
    return total


# ============================================================================
# 3. One cycle -- thin wrapper around `.363.run_manual_trigger_stage1b_
#    cycle()`. This is the ONLY place this module calls into the actual
#    trading-cycle logic, and it is always this function.
# ============================================================================


def run_one_live_cycle(
    *,
    manual_trigger_module: Any,
    symbol_requests: tuple[Any, ...],
    bars_client: Any,
    alpaca_client: Any,
    max_new_orders_per_cycle: int,
    lookback_bars: int,
    universe_version: str,
    max_snapshot_age_seconds: float,
    fill_poll_timeout_seconds: float,
    fill_poll_interval_seconds: float,
    limits: Any,
    news_client: Any | None = None,
    news_state_dir: Path | None = None,
    news_fetch_limit: int = 50,
    strategy_id: str = DEFAULT_STRATEGY_ID,
    strategy_version: str = DEFAULT_STRATEGY_VERSION,
    equity_history_log_path: Path | None = None,
    now: datetime | None = None,
    symbol_source: str = "requests_config",
    earnings_calendar_api_key: str | None = None,
    earnings_state_dir: Path | None = None,
    full_universe_scan_status: dict[str, Any] | None = None,
    decision_journal_path: Path | None = None,
) -> dict[str, Any]:
    result = manual_trigger_module.run_manual_trigger_stage1b_cycle(
        symbol_requests,
        confirmed=True,  # this process's OWN CLI-level confirmation stands in -- see module docstring, "Confirmation"
        bars_client=bars_client,
        alpaca_client=alpaca_client,
        max_new_orders_per_cycle=max_new_orders_per_cycle,
        lookback_bars=lookback_bars,
        universe_version=universe_version,
        max_snapshot_age_seconds=max_snapshot_age_seconds,
        fill_poll_timeout_seconds=fill_poll_timeout_seconds,
        fill_poll_interval_seconds=fill_poll_interval_seconds,
        news_client=news_client,
        news_state_dir=news_state_dir,
        news_fetch_limit=news_fetch_limit,
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        equity_history_log_path=equity_history_log_path,
        now=now,
        symbol_source=symbol_source,
        limits=limits,
        earnings_calendar_api_key=earnings_calendar_api_key,
        earnings_state_dir=earnings_state_dir,
        full_universe_scan_status=full_universe_scan_status,
        decision_journal_path=decision_journal_path,
    )
    result = dict(result)
    result["live_trader_engine"] = ENGINE
    result["live_trader_version"] = VERSION
    return result


# ============================================================================
# The loop
# ============================================================================


def run_scheduled_live_loop(
    *,
    symbol_requests: tuple[Any, ...],
    bars_client: Any,
    alpaca_client: Any,
    manual_trigger_module: Any,
    scheduled_runner_module: Any,
    max_new_orders_per_cycle: int,
    lookback_bars: int,
    universe_version: str,
    max_snapshot_age_seconds: float,
    fill_poll_timeout_seconds: float,
    fill_poll_interval_seconds: float,
    limits: Any,
    output_dir: Path,
    interval_seconds: float,
    market_hours_only: bool,
    kill_switch_path: Path,
    max_orders_per_day: int,
    daily_order_log_path: Path,
    news_client: Any | None = None,
    news_state_dir: Path | None = None,
    news_fetch_limit: int = 50,
    strategy_id: str = DEFAULT_STRATEGY_ID,
    strategy_version: str = DEFAULT_STRATEGY_VERSION,
    equity_history_log_path: Path | None = None,
    max_iterations: int | None = None,
    symbol_source: str = "requests_config",
    earnings_calendar_api_key: str | None = None,
    earnings_state_dir: Path | None = None,
    full_universe_scan_status: dict[str, Any] | None = None,
    decision_journal_path: Path | None = None,
    shared_intent_path: Path | None = None,
    shared_intent_max_age_seconds: float = 600.0,
    combined_alpaca_notional_ratio_limit: float | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    log_fn: Callable[[str], None] = print,
) -> int:
    """Runs cycles until `max_iterations` loop passes have happened (None
    = forever, the real CLI usage), sleeping `interval_seconds` between
    passes -- EXCEPT when the kill switch is found present, which stops
    the loop immediately (no further sleep, no further iterations).
    Returns the number of cycles that actually reached
    `run_one_live_cycle` (kill-switch stops, market-closed skips, and
    daily-cap skips don't count). `sleep_fn`/`now_fn`/`log_fn` are
    injectable so tests never sleep for real or depend on the real wall
    clock or filesystem clock."""
    executed = 0
    iteration = 0
    while max_iterations is None or iteration < max_iterations:
        iteration += 1
        now = now_fn()

        # -- Mechanism 2: file-based kill switch, checked FIRST, every
        #    iteration, before anything else (no client touched, no sleep
        #    afterward -- the loop simply stops). --
        if check_kill_switch(kill_switch_path):
            log_fn(f"{_now_iso(now)} KILL_SWITCH_ENGAGED ({kill_switch_path}) -- stopping loop.")
            return executed

        if market_hours_only:
            try:
                market_open = scheduled_runner_module.is_market_open(alpaca_client)
            except Exception as exc:  # noqa: BLE001 -- real broker call; report, keep looping
                log_fn(f"{_now_iso(now)} MARKET_CLOCK_CHECK_FAILED: {type(exc).__name__}: {exc} -- skipping this cycle")
                sleep_fn(interval_seconds)
                continue
            if not market_open:
                log_fn(f"{_now_iso(now)} MARKET_CLOSED -- skipping cycle")
                sleep_fn(interval_seconds)
                continue

        # -- Mechanism 5: hard daily ceiling on REAL submitted orders. --
        today_key = _day_key(_now_iso(now))
        submitted_today = read_todays_submitted_count(daily_order_log_path, today_key=today_key)
        if submitted_today >= max_orders_per_day:
            log_fn(
                f"{_now_iso(now)} DAILY_ORDER_CAP_REACHED ({submitted_today}/{max_orders_per_day} for {today_key}) "
                "-- skipping cycle."
            )
            sleep_fn(interval_seconds)
            continue

        # -- Extension, 2026-10-02: DELTAX cross-awareness pre-cycle check.
        #    Complete no-op (proceed=True, empty evidence) unless
        #    `shared_intent_path` is supplied -- see `.371`'s own module
        #    docstring. Runs entirely OUTSIDE `.344`'s own enforcement
        #    path, as an extra, independent, skip-the-whole-cycle layer,
        #    the same way the kill switch and daily order cap above
        #    already skip a cycle without touching `.344` at all. --
        guard = load_cross_awareness_guard_module()
        shared_intent_mod = load_shared_intent_module()
        observability_mod = load_observability_module()
        proceed, ca_reason, ca_evidence = guard.pre_cycle_check(
            observability_module=observability_mod,
            shared_intent_module=shared_intent_mod,
            alpaca_client=alpaca_client,
            shared_intent_path=shared_intent_path,
            max_age_seconds=shared_intent_max_age_seconds,
            combined_alpaca_notional_ratio_limit=combined_alpaca_notional_ratio_limit,
            now=now,
        )
        if not proceed:
            log_fn(f"{_now_iso(now)} CROSS_AWARENESS_SKIP: {ca_reason} | {ca_evidence}")
            sleep_fn(interval_seconds)
            continue
        before_snapshot = guard.snapshot_alpaca_notional(observability_mod, alpaca_client)

        result = run_one_live_cycle(
            manual_trigger_module=manual_trigger_module,
            symbol_requests=symbol_requests,
            bars_client=bars_client,
            alpaca_client=alpaca_client,
            max_new_orders_per_cycle=max_new_orders_per_cycle,
            lookback_bars=lookback_bars,
            universe_version=universe_version,
            max_snapshot_age_seconds=max_snapshot_age_seconds,
            fill_poll_timeout_seconds=fill_poll_timeout_seconds,
            fill_poll_interval_seconds=fill_poll_interval_seconds,
            limits=limits,
            news_client=news_client,
            news_state_dir=news_state_dir,
            news_fetch_limit=news_fetch_limit,
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            equity_history_log_path=equity_history_log_path,
            now=now,
            symbol_source=symbol_source,
            earnings_calendar_api_key=earnings_calendar_api_key,
            earnings_state_dir=earnings_state_dir,
            full_universe_scan_status=full_universe_scan_status,
            decision_journal_path=decision_journal_path,
        )

        # -- Extension, 2026-10-02: DELTAX cross-awareness post-cycle
        #    record. No-op unless `shared_intent_path` is supplied. --
        guard.post_cycle_record(
            observability_module=observability_mod,
            shared_intent_module=shared_intent_mod,
            alpaca_client=alpaca_client,
            shared_intent_path=shared_intent_path,
            before_snapshot=before_snapshot,
            now=now,
        )

        cycle_path = scheduled_runner_module.write_cycle_result(result, output_dir=output_dir, now=now)
        executed += 1

        submitted_this_cycle = (result.get("stage1_report") or {}).get("submitted_count", 0)
        append_daily_order_log_entry(daily_order_log_path, submitted_count=submitted_this_cycle, as_of=now)

        outcomes = (result.get("stage1_report") or {}).get("outcomes") or []
        outcome_summary = ", ".join(
            f"{o.get('symbol')}={o.get('decision_outcome')}" for o in outcomes
        ) or "no symbols usable this cycle"
        log_fn(
            f"{_now_iso(now)} CYCLE_OK -> {cycle_path.name} | {outcome_summary} | "
            f"submitted_count={submitted_this_cycle} | submitted_today={submitted_today + submitted_this_cycle}/{max_orders_per_day}"
        )

        sleep_fn(interval_seconds)

    return executed


# ============================================================================
# CLI
# ============================================================================


def main(argv: list[str] | None = None) -> int:  # pragma: no cover -- live wiring
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--requests-config", required=False, default=None, type=Path,
                         help="JSON file: list of {symbol, asset_class, quantity}. Same format as .356/.363. "
                              "Exactly one of --requests-config, --scan-pinned-universe, or "
                              "--scan-full-universe is required.")
    parser.add_argument("--scan-pinned-universe", action="store_true",
                         help="Scan every symbol in .51's existing pinned universe (39 symbols as of the "
                              "2026-09-29 ETF curation pass) every cycle -- "
                              "quantity always auto-sized. Exactly one of --requests-config, "
                              "--scan-pinned-universe, or --scan-full-universe is required.")
    parser.add_argument("--scan-full-universe", action="store_true",
                         help="Extension -- 2026-09-30, full universe expansion (Martin, AskUserQuestion): scan "
                              "EVERY tradable US-equity symbol Alpaca lists (via .369, tradable=True only -- no "
                              "price/volume/exchange filter, Martin's explicit confirmed choice), resolved once "
                              "at startup (the .369 cache itself refreshes daily; this process does not "
                              "re-scan the asset list every 5-minute cycle). Bars are fetched batched "
                              "(.351.fetch_recent_bars_batch) for every symbol source, not just this one. "
                              "Exactly one of --requests-config, --scan-pinned-universe, or "
                              "--scan-full-universe is required.")
    parser.add_argument("--full-universe-state-dir", type=Path, default=None,
                         help="Directory for .369's persisted tradable-asset-list cache. Required when "
                              "--scan-full-universe is supplied; ignored otherwise.")
    parser.add_argument("--max-new-orders-per-cycle", type=int, default=DEFAULT_MAX_NEW_ORDERS_PER_CYCLE,
                         help=f"Default: {DEFAULT_MAX_NEW_ORDERS_PER_CYCLE} (Martin's confirmed 'Moderate' pace, "
                              "2026-09-29 AskUserQuestion).")
    parser.add_argument("--max-snapshot-age-seconds", required=True, type=float)
    parser.add_argument("--fill-poll-timeout-seconds", required=True, type=float)
    parser.add_argument("--fill-poll-interval-seconds", required=True, type=float)
    parser.add_argument("--lookback-bars", type=int, default=None,
                         help="Defaults to FROZEN_TECHNICAL_PARAMS.min_bars_required if omitted.")
    parser.add_argument("--universe-version", type=str, default=None,
                         help="Defaults to aura_v054_signal_source.UNIVERSE_VERSION if omitted.")
    parser.add_argument("--news-state-dir", type=Path, default=None,
                         help="Directory for .46's persisted news ledger. Omit to skip news/live-evidence.")
    parser.add_argument("--news-fetch-limit", type=int, default=50)
    parser.add_argument("--earnings-state-dir", type=Path, default=None,
                         help="Directory for .67's persisted earnings-calendar cache. Omit to skip the earnings "
                              "blackout gate entirely (no new-entry block on earnings day for any symbol). "
                              "Requires FMP_API_KEY in the environment (see .env.example) -- a free key from "
                              "https://site.financialmodelingprep.com/.")
    parser.add_argument("--strategy-id", type=str, default=DEFAULT_STRATEGY_ID)
    parser.add_argument("--strategy-version", type=str, default=DEFAULT_STRATEGY_VERSION)
    parser.add_argument("--equity-history-log-path", type=Path, default=None,
                         help="Defaults to .364's own DEFAULT_EQUITY_HISTORY_LOG_PATH if omitted.")
    parser.add_argument("--decision-journal-path", type=Path, default=None,
                         help="Defaults to .361's own DEFAULT_JOURNAL_PATH if omitted. Every symbol every cycle "
                              "-- ABSTAIN, a .44/.38 BLOCK, or an actual submission -- is appended here "
                              "unconditionally (Extension, 2026-10-01).")

    # -- Mechanism 1: conservative portfolio limits, Martin's confirmed
    #    defaults, overridable. --
    parser.add_argument("--max-daily-loss-pct", type=float, default=0.02,
                         help="Default: 0.02 (2%%, Martin's confirmed value, 2026-09-29 AskUserQuestion). "
                              "Applied to the ALPACA venue via .344.PortfolioLimits.max_daily_loss_pct_by_venue.")
    parser.add_argument("--max-drawdown-pct", type=float, default=0.05,
                         help="Default: 0.05 (5%%, Martin's confirmed value, 2026-09-29 AskUserQuestion). "
                              "Applied to the ALPACA venue via .344.PortfolioLimits.max_drawdown_pct_by_venue.")
    parser.add_argument("--max-asset-concentration-ratio", type=float, default=0.10,
                         help="Default: 0.10 (10%%, Martin's confirmed value, 2026-09-29 AskUserQuestion).")
    parser.add_argument("--max-net-exposure-ratio", type=float, default=0.50,
                         help="Default: 0.50 (50%%, Martin's confirmed value, 2026-09-29 AskUserQuestion).")
    parser.add_argument("--max-leverage-ratio-alpaca", type=float, default=2.0,
                         help="Default: 2.0 (200%% of equity, Martin's confirmed value, 2026-10-02 "
                              "AskUserQuestion). Applied to the ALPACA venue via "
                              ".344.PortfolioLimits.max_leverage_ratio_by_venue -- a gross-notional-vs-equity "
                              "ceiling that already covers DELTAX's option positions too, since .343 reads the "
                              "whole account with no filtering by symbol or asset class.")

    # -- Extension, 2026-10-02: DELTAX cross-awareness (see CROSS_AWARENESS_SETUP.md). Omit
    #    --shared-intent-path (and the AURA_DELTAX_SHARED_INTENT_PATH env var) to leave this
    #    entirely inert -- the default below reproduces this module's pre-existing behavior. --
    parser.add_argument("--shared-intent-path", type=Path, default=None,
                         help="Path to the shared DELTAX/AURA execution-intent JSONL log (see "
                              "CROSS_AWARENESS_SETUP.md). Defaults to the AURA_DELTAX_SHARED_INTENT_PATH "
                              "environment variable if this flag is omitted. Omit both to leave cross-awareness "
                              "entirely inert.")
    parser.add_argument("--shared-intent-max-age-seconds", type=float, default=600.0,
                         help="Default: 600.0 (10 minutes). How far back .371 looks in the shared intent log for "
                              "DELTAX's recent pending risk.")
    parser.add_argument("--combined-alpaca-notional-ratio-limit", type=float, default=2.0,
                         help="Default: 2.0 (same as --max-leverage-ratio-alpaca, Martin's confirmed value, "
                              "2026-10-02 AskUserQuestion). The new cross-awareness pre-check (.371): AURA's own "
                              "ALPACA notional PLUS DELTAX's recently-submitted-but-not-yet-filled risk, combined, "
                              "as a ratio of equity -- skips the cycle entirely (no new orders attempted) if "
                              "exceeded. Only takes effect when --shared-intent-path (or "
                              "AURA_DELTAX_SHARED_INTENT_PATH) is set; otherwise this value is never read.")

    # -- Mechanism 2: file-based kill switch. --
    parser.add_argument("--kill-switch-file", type=Path, default=DEFAULT_KILL_SWITCH_PATH,
                         help=f"Default: {DEFAULT_KILL_SWITCH_PATH}. Create this file to stop the loop before its "
                              "next cycle; delete it and restart the process to resume.")

    # -- Mechanism 5: hard daily ceiling on real submitted orders. --
    parser.add_argument("--max-orders-per-day", type=int, default=DEFAULT_MAX_ORDERS_PER_DAY,
                         help=f"Default: {DEFAULT_MAX_ORDERS_PER_DAY} (Martin's confirmed value, 2026-09-29 "
                              "AskUserQuestion). Counts real submitted orders only, UTC calendar day.")
    parser.add_argument("--daily-order-log-path", type=Path, default=DEFAULT_DAILY_ORDER_LOG_PATH,
                         help=f"Default: {DEFAULT_DAILY_ORDER_LOG_PATH}.")

    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR,
                         help=f"Default: {DEFAULT_OUTPUT_DIR} (deliberately separate from .357's preview-only "
                              "stage3_scheduled_output/ -- see module docstring, 'Output').")
    parser.add_argument("--interval-seconds", type=float, default=DEFAULT_INTERVAL_SECONDS,
                         help=f"Default: {DEFAULT_INTERVAL_SECONDS} (5 minutes, Martin's confirmed 'Moderate' "
                              "pace, 2026-09-29 AskUserQuestion).")
    parser.add_argument("--run-outside-market-hours", action="store_true",
                         help="Disable the market-hours gate (runs every interval regardless of the clock).")
    parser.add_argument("--max-iterations", type=int, default=None,
                         help="Stop after this many loop passes. Omit to run forever (Ctrl+C, or the kill switch, "
                              "to stop).")
    parser.add_argument("--i-confirm-this-runs-unattended-live-paper-trading", dest="confirmed", action="store_true",
                         required=True,
                         help="REQUIRED. Once started, this process can submit real orders to your Alpaca PAPER "
                              "account repeatedly and unattended until stopped (kill-switch file or Ctrl+C). There "
                              "is no way to pass this flag as false -- omit it to refuse to start at all.")
    args = parser.parse_args(argv)

    universe_mode_flags = [bool(args.requests_config), bool(args.scan_pinned_universe), bool(args.scan_full_universe)]
    if sum(universe_mode_flags) != 1:
        print(
            "FAIL-CLOSED: EXACTLY_ONE_OF_REQUESTS_CONFIG_OR_SCAN_PINNED_UNIVERSE_OR_SCAN_FULL_UNIVERSE_REQUIRED",
            file=sys.stderr,
        )
        return 1
    if args.scan_full_universe and args.full_universe_state_dir is None:
        print("FAIL-CLOSED: FULL_UNIVERSE_STATE_DIR_REQUIRED_WITH_SCAN_FULL_UNIVERSE", file=sys.stderr)
        return 1

    manual_trigger_module = load_manual_trigger_module()
    scheduled_runner_module = load_scheduled_runner_module()
    enforcement_module = load_enforcement_module()

    try:
        equity_cli = manual_trigger_module.load_equity_cli_module()
        api_key, secret_key = equity_cli.load_equity_paper_credentials()
        technical_module = equity_cli.load_technical_module()
        signal_source_module = equity_cli.load_signal_source_module()

        bars_client = equity_cli.build_bars_client(api_key, secret_key, technical_module)
        alpaca_client = equity_cli.build_trading_client(api_key, secret_key)
        news_client = equity_cli.build_news_client(api_key, secret_key) if args.news_state_dir is not None else None

        earnings_calendar_api_key = None
        if args.earnings_state_dir is not None:
            earnings_module = manual_trigger_module.load_earnings_calendar_module()
            earnings_calendar_api_key = earnings_module.load_fmp_api_key()

        full_universe_scan_status = None
        if args.scan_full_universe:
            full_universe_module = manual_trigger_module.load_full_universe_scan_module()
            full_universe_state = full_universe_module.refresh_full_universe_if_stale(
                alpaca_client=alpaca_client, state_dir=args.full_universe_state_dir, now=datetime.now(timezone.utc),
            )
            full_universe_scan_status = full_universe_state.to_dict()
            symbol_requests = full_universe_module.build_symbol_requests_from_full_universe(
                full_universe_state, equity_cli_module=equity_cli,
            )
            symbol_source = f"scan_full_universe:{full_universe_state.source}:{len(symbol_requests)}_symbols"
        elif args.scan_pinned_universe:
            symbol_requests = equity_cli.build_symbol_requests_from_pinned_universe(technical_module)
            pinned_version = technical_module.load_pinned_universe().version
            symbol_source = f"scan_pinned_universe:{pinned_version}:{len(symbol_requests)}_symbols"
        else:
            symbol_requests = equity_cli.load_symbol_requests(args.requests_config)
            symbol_source = f"requests_config:{args.requests_config}"
        lookback_bars = args.lookback_bars or signal_source_module.FROZEN_TECHNICAL_PARAMS.min_bars_required
        universe_version = args.universe_version or signal_source_module.UNIVERSE_VERSION
    except manual_trigger_module.Stage1BManualTriggerCliError as exc:
        print(f"FAIL-CLOSED: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 -- covers .67's own
        # EarningsCalendarIngestionError (e.g. a missing FMP_API_KEY) --
        # fail-closed at startup, before any client is touched, never a
        # raw traceback. See .363's identical handling for why this is a
        # name check rather than an import (dynamically loaded module).
        if type(exc).__name__ == "EarningsCalendarIngestionError":
            print(f"FAIL-CLOSED: {exc}", file=sys.stderr)
            return 1
        raise

    limits = enforcement_module.PortfolioLimits(
        max_daily_loss_pct_by_venue={"ALPACA": args.max_daily_loss_pct},
        max_drawdown_pct_by_venue={"ALPACA": args.max_drawdown_pct},
        max_asset_concentration_ratio=args.max_asset_concentration_ratio,
        max_net_exposure_ratio=args.max_net_exposure_ratio,
        max_leverage_ratio_by_venue={"ALPACA": args.max_leverage_ratio_alpaca},
    )

    # -- Extension, 2026-10-02: DELTAX cross-awareness. --shared-intent-path wins if supplied;
    #    otherwise fall back to the AURA_DELTAX_SHARED_INTENT_PATH environment variable (the
    #    same variable DELTAX's own execute.py/run.py already read). Neither set -> None,
    #    which keeps .371's guard a complete no-op, exactly as before this change. --
    shared_intent_path = args.shared_intent_path
    if shared_intent_path is None:
        _env_intent_path = os.environ.get("AURA_DELTAX_SHARED_INTENT_PATH", "")
        shared_intent_path = Path(_env_intent_path) if _env_intent_path else None

    print(
        f"{VERSION} starting UNATTENDED LIVE (paper) trading loop -- interval={args.interval_seconds}s, "
        f"market_hours_only={not args.run_outside_market_hours}, max_new_orders_per_cycle={args.max_new_orders_per_cycle}, "
        f"max_orders_per_day={args.max_orders_per_day}, output_dir={args.output_dir}, "
        f"symbol_source={symbol_source}, symbols={[r.symbol for r in symbol_requests]}"
    )
    print(f"KILL SWITCH: create this exact file to stop the loop before its next cycle: {args.kill_switch_file}")
    print(f"limits: {limits.to_dict()}")
    print(
        f"earnings blackout gate: "
        f"{'ENABLED (state_dir=' + str(args.earnings_state_dir) + ')' if args.earnings_state_dir is not None else 'DISABLED (--earnings-state-dir not set)'}"
    )
    print(
        f"DELTAX cross-awareness: "
        f"{'ENABLED (shared_intent_path=' + str(shared_intent_path) + ', combined_alpaca_notional_ratio_limit=' + str(args.combined_alpaca_notional_ratio_limit) + ')' if shared_intent_path is not None else 'DISABLED (no --shared-intent-path and AURA_DELTAX_SHARED_INTENT_PATH not set)'}"
    )
    if full_universe_scan_status is not None:
        print(
            f"full universe scan: {full_universe_scan_status['status']} "
            f"(source={full_universe_scan_status['source']}, symbol_count={full_universe_scan_status['symbol_count']}, "
            f"filter={full_universe_scan_status['filter_applied']})"
        )
    print("THIS MODULE CAN SUBMIT REAL ORDERS TO YOUR ALPACA PAPER ACCOUNT, REPEATEDLY, UNTIL STOPPED.")

    try:
        run_scheduled_live_loop(
            symbol_requests=symbol_requests,
            bars_client=bars_client,
            alpaca_client=alpaca_client,
            manual_trigger_module=manual_trigger_module,
            scheduled_runner_module=scheduled_runner_module,
            max_new_orders_per_cycle=args.max_new_orders_per_cycle,
            lookback_bars=lookback_bars,
            universe_version=universe_version,
            max_snapshot_age_seconds=args.max_snapshot_age_seconds,
            fill_poll_timeout_seconds=args.fill_poll_timeout_seconds,
            fill_poll_interval_seconds=args.fill_poll_interval_seconds,
            limits=limits,
            output_dir=args.output_dir,
            interval_seconds=args.interval_seconds,
            market_hours_only=not args.run_outside_market_hours,
            kill_switch_path=args.kill_switch_file,
            max_orders_per_day=args.max_orders_per_day,
            daily_order_log_path=args.daily_order_log_path,
            news_client=news_client,
            news_state_dir=args.news_state_dir,
            news_fetch_limit=args.news_fetch_limit,
            strategy_id=args.strategy_id,
            strategy_version=args.strategy_version,
            equity_history_log_path=args.equity_history_log_path,
            max_iterations=args.max_iterations,
            symbol_source=symbol_source,
            earnings_calendar_api_key=earnings_calendar_api_key,
            earnings_state_dir=args.earnings_state_dir,
            full_universe_scan_status=full_universe_scan_status,
            decision_journal_path=args.decision_journal_path,
            shared_intent_path=shared_intent_path,
            shared_intent_max_age_seconds=args.shared_intent_max_age_seconds,
            combined_alpaca_notional_ratio_limit=args.combined_alpaca_notional_ratio_limit,
        )
    except KeyboardInterrupt:
        print("\nStopped by Ctrl+C.")
        return 0

    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
