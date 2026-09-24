#!/usr/bin/env python3
"""
AURA v0.5.3.57 -- Track B: scheduled loop runner around the existing
Stage 3A live-wiring CLI (`aura_v05356_stage3_live_equity_cli.py`).

WHAT THIS MODULE IS, AND IS DELIBERATELY NOT
------------------------------------------------------------------------
This is a scheduling/looping wrapper, not new trading logic. Every cycle
calls `.56`'s own `run_live_dry_run_cycle()` unmodified -- the same real
Alpaca data -> real `.51`/`.52` signal -> existing `.53`/`.55`
orchestration -> real `.44` risk enforcement -> preview path already
built and tested in Stage 3A/3B. Nothing here duplicates that logic, and
nothing here can reach order submission: `.56.run_live_dry_run_cycle()`
itself only ever calls `run_stage1a_dry_run()`, never
`run_stage1b_paper_cycle()`, and this module does not import or call
`run_stage1b_paper_cycle` either -- there is no path from this file to a
real order.

This module's only new contribution is: (1) a loop that calls that
existing cycle function on an interval, (2) a market-hours gate (a
read-only `get_clock()` call) so cycles are skipped while the market is
closed rather than run against stale/no data, and (3) writing each
cycle's result to its own timestamped file plus a rolling `latest.json`,
so a dashboard (or a person) has a real history to look at.

Credentials, structural safety
------------------------------------------------------------------------
Reads credentials via `.56.load_equity_paper_credentials()` -- same two
required env vars (`ALPACA_EQUITY_PAPER_API_KEY`/`SECRET_KEY`), same
fail-closed behavior, same "never falls back to the crypto pair" rule.
No credential value is ever logged, printed, or written to any output
file by this module.

Scope
------------------------------------------------------------------------
Equity/ETF only, Alpaca only, preview-only. Track A/MEXC is not
imported, referenced, or touched anywhere in this module.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

VERSION = "AURA v0.5.3.57"
ENGINE = "STAGE3_SCHEDULED_RUNNER"

ROOT = Path(__file__).resolve().parent

DEFAULT_INTERVAL_SECONDS = 300.0
DEFAULT_OUTPUT_DIR = ROOT / "stage3_scheduled_output"


class ScheduledRunnerError(Exception):
    """Programmer-error / missing-required-input / fail-closed-safety
    violations only -- same discipline as `.56`'s `Stage3CliError`."""


def _load_module(module_name: str, filename: str):
    try:
        return __import__(module_name)
    except ImportError:
        import importlib.util

        spec = importlib.util.spec_from_file_location(module_name, ROOT / filename)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return mod


def load_stage3_module():
    """`.56` -- the ONLY place this module gets its actual trading-cycle
    logic from. Never duplicated, never reimplemented."""
    return _load_module("aura_v05356_stage3_live_equity_cli", "aura_v05356_stage3_live_equity_cli.py")


def _now_iso(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()


def _safe_filename_timestamp(now: datetime) -> str:
    # Colons/periods are awkward in Windows filenames -- keep this
    # sortable-as-string while staying filesystem-safe.
    return now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


# ============================================================================
# Market-hours gate -- read-only, the same `TradingClient` `.56` already
# constructs. Never a write/order call.
# ============================================================================


def is_market_open(alpaca_client: Any) -> bool:
    """Read-only `get_clock()` call. Returns True/False for the ordinary
    open/closed states; only a genuine client/auth failure propagates --
    the caller (the loop) decides what to do with that, exactly like
    `.56`'s own fetch functions distinguish "no data" from "programmer
    error"."""
    clock = alpaca_client.get_clock()
    return bool(clock.is_open)


# ============================================================================
# One cycle -- thin wrapper around `.56.run_live_dry_run_cycle()`. This is
# the ONLY place this module calls into the actual trading-cycle logic,
# and it is always this function, never anything from `.55` directly.
# ============================================================================


def run_one_cycle(
    *,
    stage3_module: Any,
    symbol_requests: tuple[Any, ...],
    bars_client: Any,
    alpaca_client: Any,
    max_new_orders_per_cycle: int,
    lookback_bars: int,
    universe_version: str,
    max_snapshot_age_seconds: float,
    skip_account_equity_fetch: bool,
    now: datetime | None = None,
) -> dict[str, Any]:
    result = stage3_module.run_live_dry_run_cycle(
        symbol_requests,
        bars_client=bars_client,
        alpaca_client=alpaca_client,
        max_new_orders_per_cycle=max_new_orders_per_cycle,
        lookback_bars=lookback_bars,
        universe_version=universe_version,
        max_snapshot_age_seconds=max_snapshot_age_seconds,
        skip_account_equity_fetch=skip_account_equity_fetch,
        now=now,
    )
    result = dict(result)
    result["scheduled_runner_engine"] = ENGINE
    result["scheduled_runner_version"] = VERSION
    return result


def write_cycle_result(result: dict[str, Any], *, output_dir: Path, now: datetime) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    cycle_path = output_dir / f"cycle_{_safe_filename_timestamp(now)}.json"
    payload = json.dumps(result, indent=2, default=str) + "\n"
    cycle_path.write_text(payload, encoding="utf-8")
    latest_path = output_dir / "latest.json"
    latest_path.write_text(payload, encoding="utf-8")
    return cycle_path


# ============================================================================
# The loop
# ============================================================================


def run_scheduled_loop(
    *,
    symbol_requests: tuple[Any, ...],
    bars_client: Any,
    alpaca_client: Any,
    stage3_module: Any,
    max_new_orders_per_cycle: int,
    lookback_bars: int,
    universe_version: str,
    max_snapshot_age_seconds: float,
    skip_account_equity_fetch: bool,
    output_dir: Path,
    interval_seconds: float,
    market_hours_only: bool,
    max_iterations: int | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    log_fn: Callable[[str], None] = print,
) -> int:
    """Runs cycles until `max_iterations` loop passes have happened (None
    = forever, the real CLI usage), sleeping `interval_seconds` between
    passes regardless of whether a cycle actually ran or was skipped for
    being outside market hours. Returns the number of cycles actually
    executed (skips don't count). `sleep_fn`/`now_fn`/`log_fn` are
    injectable so tests never sleep for real or depend on the real wall
    clock."""
    executed = 0
    iteration = 0
    while max_iterations is None or iteration < max_iterations:
        iteration += 1
        now = now_fn()

        if market_hours_only:
            try:
                market_open = is_market_open(alpaca_client)
            except Exception as exc:  # noqa: BLE001 -- real broker call; report, keep looping
                log_fn(f"{_now_iso(now)} MARKET_CLOCK_CHECK_FAILED: {type(exc).__name__}: {exc} -- skipping this cycle")
                sleep_fn(interval_seconds)
                continue
            if not market_open:
                log_fn(f"{_now_iso(now)} MARKET_CLOSED -- skipping cycle")
                sleep_fn(interval_seconds)
                continue

        result = run_one_cycle(
            stage3_module=stage3_module,
            symbol_requests=symbol_requests,
            bars_client=bars_client,
            alpaca_client=alpaca_client,
            max_new_orders_per_cycle=max_new_orders_per_cycle,
            lookback_bars=lookback_bars,
            universe_version=universe_version,
            max_snapshot_age_seconds=max_snapshot_age_seconds,
            skip_account_equity_fetch=skip_account_equity_fetch,
            now=now,
        )
        cycle_path = write_cycle_result(result, output_dir=output_dir, now=now)
        executed += 1

        outcomes = (result.get("stage1_report") or {}).get("outcomes") or []
        outcome_summary = ", ".join(
            f"{o.get('symbol')}={o.get('decision_outcome')}" for o in outcomes
        ) or "no symbols usable this cycle"
        submitted = (result.get("stage1_report") or {}).get("submitted_count", 0)
        log_fn(
            f"{_now_iso(now)} CYCLE_OK -> {cycle_path.name} | {outcome_summary} | "
            f"submitted_count={submitted} (always 0)"
        )

        sleep_fn(interval_seconds)

    return executed


# ============================================================================
# CLI
# ============================================================================


def main(argv: list[str] | None = None) -> int:  # pragma: no cover -- live wiring
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--requests-config", required=True, type=Path,
                         help="JSON file: list of {symbol, asset_class, quantity}. Same format as .56.")
    parser.add_argument("--max-new-orders-per-cycle", required=True, type=int)
    parser.add_argument("--max-snapshot-age-seconds", required=True, type=float)
    parser.add_argument("--lookback-bars", type=int, default=None,
                         help="Defaults to FROZEN_TECHNICAL_PARAMS.min_bars_required if omitted, same as .56.")
    parser.add_argument("--universe-version", type=str, default=None,
                         help="Defaults to the already-frozen UNIVERSE_VERSION if omitted, same as .56.")
    parser.add_argument("--skip-account-equity-fetch", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR,
                         help=f"Default: {DEFAULT_OUTPUT_DIR}")
    parser.add_argument("--interval-seconds", type=float, default=DEFAULT_INTERVAL_SECONDS,
                         help=f"Default: {DEFAULT_INTERVAL_SECONDS} (5 minutes).")
    parser.add_argument("--run-outside-market-hours", action="store_true",
                         help="Disable the market-hours gate (runs every interval regardless of the clock).")
    parser.add_argument("--max-iterations", type=int, default=None,
                         help="Stop after this many loop passes (skipped cycles count as passes). "
                              "Omit to run forever (Ctrl+C to stop).")
    args = parser.parse_args(argv)

    stage3_module = load_stage3_module()

    try:
        api_key, secret_key = stage3_module.load_equity_paper_credentials()
        technical_module = stage3_module.load_technical_module()
        signal_source_module = stage3_module.load_signal_source_module()

        bars_client = stage3_module.build_bars_client(api_key, secret_key, technical_module)
        alpaca_client = stage3_module.build_trading_client(api_key, secret_key)

        symbol_requests = stage3_module.load_symbol_requests(args.requests_config)
        lookback_bars = args.lookback_bars or signal_source_module.FROZEN_TECHNICAL_PARAMS.min_bars_required
        universe_version = args.universe_version or signal_source_module.UNIVERSE_VERSION
    except stage3_module.Stage3CliError as exc:
        print(f"FAIL-CLOSED: {exc}", file=sys.stderr)
        return 1

    print(
        f"{VERSION} starting scheduled loop -- interval={args.interval_seconds}s, "
        f"market_hours_only={not args.run_outside_market_hours}, output_dir={args.output_dir}, "
        f"symbols={[r.symbol for r in symbol_requests]}"
    )
    print("PREVIEW ONLY -- run_stage1b_paper_cycle is never called by this module; no order can be submitted.")

    try:
        run_scheduled_loop(
            symbol_requests=symbol_requests,
            bars_client=bars_client,
            alpaca_client=alpaca_client,
            stage3_module=stage3_module,
            max_new_orders_per_cycle=args.max_new_orders_per_cycle,
            lookback_bars=lookback_bars,
            universe_version=universe_version,
            max_snapshot_age_seconds=args.max_snapshot_age_seconds,
            skip_account_equity_fetch=args.skip_account_equity_fetch,
            output_dir=args.output_dir,
            interval_seconds=args.interval_seconds,
            market_hours_only=not args.run_outside_market_hours,
            max_iterations=args.max_iterations,
        )
    except KeyboardInterrupt:
        print("\nStopped by Ctrl+C.")
        return 0

    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
