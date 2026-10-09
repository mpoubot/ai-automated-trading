#!/usr/bin/env python3
"""
AURA v0.5.3.63 -- Track B Stage 3B: manual-trigger CLI for a CAPPED,
explicitly-confirmed real paper-account submission via `.55`'s EXISTING
`run_stage1b_paper_cycle()`.

WHY THIS IS A SEPARATE FILE, NOT A FLAG ON `.356`
------------------------------------------------------------------------
`aura_v05356_stage3_live_equity_cli.py`'s module docstring states a
structural safety guarantee: "There is no flag, argument, or environment
variable anywhere in this module that can cause an order to be
submitted" -- proven by an AST-level test
(`test_aura_v05356_stage3_live_equity_cli.py`) that greps its own source
for any call to `run_stage1b_paper_cycle` or any `attempt_submission=True`.
Adding a submission path into `.356` would mean either breaking that
guarantee or loosening the test that proves it -- for a file whose entire
value (to Martin, and to any future reader auditing this repo) is being
provably incapable of touching the broker. Martin confirmed (2026-09-25,
AskUserQuestion) keeping `.356` permanently dry-run-only and building the
capped, real-submission path as its own dedicated module instead. THIS
file is now the only place in the repo where a real Alpaca order can be
submitted from the live-evidence pipeline; `.356` remains exactly as
before, unmodified.

WHAT THIS MODULE REUSES, AND WHAT IT DOES NOT DUPLICATE
------------------------------------------------------------------------
Every piece of actual logic here is reused verbatim from already-built,
already-tested modules, via ordinary function calls (not copy-pasted):
credentials/client construction, real `.51`/`.52` evidence fetching,
live sentiment/wave/sector-rotation evidence (`.362`), and ATR risk-based
position sizing all come from `.356`'s own already-tested functions
(`load_equity_paper_credentials`, `build_trading_client`,
`build_bars_client`, `build_news_client`, `load_symbol_requests`,
`fetch_symbol_evidence`, `resolve_quantity_for_symbol`,
`build_reference_price_fn`, `fetch_real_account_equity_usd`, and its
module loaders) -- this module calls them directly rather than
reimplementing any of them. The only genuinely new code here is the
short sequencing that assembles those pieces and then calls `.55`'s
EXISTING `run_stage1b_paper_cycle()` instead of `run_stage1a_dry_run()`.

STRUCTURAL SAFETY GATES -- all three must be true for a broker call
------------------------------------------------------------------------
1. `--i-confirm-this-submits-real-paper-orders` is a REQUIRED CLI flag
   (argparse `required=True` on a `store_true` -- there is no way to
   pass it as "false"; omitting it is the only way to not confirm, and
   omitting it makes argparse itself refuse to run before any client is
   built). This module ALSO re-checks the flag defensively inside
   `run_manual_trigger_stage1b_cycle()` itself (`confirmed=True` is a
   required, no-default keyword-only argument), so a future caller that
   imports this function directly (bypassing argparse) cannot skip the
   confirmation either.
2. `.36`'s `auth_config` (`execution_authorized`/`paper_execution_authorized`)
   and `.38`'s `supervisor_config` kill switch are both closed by
   default in the underlying modules -- this module is the one place
   that explicitly opens them (see `build_live_paper_submission_supervision`
   below), reusing the SAME TTL values (`authorization_ttl_seconds=60`,
   `safety_state_ttl_seconds=60`) `.355`'s own `run_stage1a_dry_run`
   already uses for its (harmless, dry-run) default -- not new numbers
   invented here.
3. `max_new_orders_per_cycle` is a REQUIRED CLI argument (`.53.run_cycle`'s
   own existing cap) -- Martin's stated plan for the very first run is
   `1`, with a 1-2 symbol `--requests-config` file; this module does not
   invent an additional code-level symbol allowlist or a hardcoded order
   ceiling on top of that (per Martin's explicit choice, 2026-09-25,
   AskUserQuestion: control scope via the requests-config file only).

Account equity for sizing
------------------------------------------------------------------------
Unlike `.356` (where `alpaca_client` and the account-equity fetch are
both optional), `alpaca_client` is REQUIRED here -- `.55`'s
`run_stage1b_paper_cycle` itself requires a real client. This module
fetches real account equity once, via the same
`fetch_real_account_equity_usd` `.356` already uses, for ATR-risk
position sizing; `.55`'s own `run_stage1b_paper_cycle` separately builds
its own real portfolio snapshot (a second, independent real read) for
`.44` enforcement -- exactly as it already does for every other caller,
unchanged here.

Daily-loss history (`.364`) -- found while building this module
------------------------------------------------------------------------
`.44`'s daily_loss check BLOCKs unconditionally (`INSUFFICIENT_HISTORY`)
whenever there is no real same-day-prior equity snapshot, and nothing in
this repo persisted one before `.363`/`.364`. Passing an empty
`equity_history` here (as `.356`'s harmless dry-run CLI does) would mean
this module's real submissions BLOCK on daily_loss alone, every time,
regardless of decision or confirmation. This module instead uses
`aura_v05364_equity_history_log.py`'s `record_and_read_equity_history()`:
it appends THIS run's own real, freshly-fetched equity to a small
append-only local log (default `regime_output/equity_history_log/
alpaca_equity_history.jsonl` -- see `.364`'s module docstring), then
reads the full history back (including the just-appended entry) and
passes that as `equity_history`. The first-ever observation in a day is
its own day-start baseline (`daily_loss_pct == 0.0` for that first
cycle -- a real number, not fabricated); every later same-day cycle sees
a genuine, strictly-earlier prior observation, giving real cumulative
same-day-loss tracking across repeated manual-trigger runs (Martin,
2026-09-25, AskUserQuestion).

`strategy_id`/`strategy_version`
------------------------------------------------------------------------
`.355`'s own defaults (`DEFAULT_STRATEGY_ID = "STAGE1_SYNTHETIC_SCENARIO"`,
`DEFAULT_STRATEGY_VERSION = "v1-synthetic-dry-run"`) are explicitly
labeled synthetic/dry-run and would mislabel a real paper submission's
audit trail. This module uses its own, honestly-labeled defaults instead
(`STAGE1B_MANUAL_TRIGGER_LIVE_PAPER` / `v1-manual-trigger`), overridable
via CLI flags.

Scope
------------------------------------------------------------------------
Equity/ETF only, Alpaca only, PAPER account only (`.355.run_stage1b_paper_cycle`
itself only ever talks to whatever `TradingClient` it's given; this
module constructs it with `paper=True`, same as `.356`). Track A/MEXC is
not imported, referenced, or touched anywhere in this module. This
module does not decide when it is appropriate to actually run a live
submission -- that remains a separate, explicit go/no-go Martin makes
each time he chooses to invoke it (see task #139: a completion report
and explicit sign-off precede the first real invocation).

Extension -- 2026-09-29, wide scan against the pinned universe (Martin,
AskUserQuestion)
------------------------------------------------------------------------
`.356`/`.357` gained a `--scan-pinned-universe` mode that builds one
symbol request per symbol in `.51`'s pinned research universe (27
symbols) instead of reading a hand-written `--requests-config` file.
This module now exposes the SAME choice, reusing `.356`'s already-tested
`build_symbol_requests_from_pinned_universe()` verbatim -- no new
symbol-list logic is written here. Exactly one of `--requests-config` /
`--scan-pinned-universe` is required, same fail-closed validation as
`.356`/`.357`.

This does NOT loosen any of the three structural safety gates above:
`--i-confirm-this-submits-real-paper-orders` is still required,
`max_new_orders_per_cycle` still caps how many of the scanned symbols
can actually result in a real order (via `.53`'s existing rank-by-
|final_rank_score| cap-selection, unchanged), and every symbol still
goes through the same evidence/sizing/enforcement pipeline as before.
What changes is only the size of the CANDIDATE pool `.53` ranks from --
Martin's own documented plan for a first run (1-2 hand-picked symbols)
remains fully available via `--requests-config`; `--scan-pinned-universe`
is an explicit, separate choice for when he wants the real-order path to
consider the full pinned universe rather than a curated list. The
resulting `symbol_source` field on the output records which mode
produced a given run, for audit.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

VERSION = "AURA v0.5.3.63"
ENGINE = "STAGE3B_MANUAL_TRIGGER_STAGE1B_CLI"

ROOT = Path(__file__).resolve().parent

DEFAULT_STRATEGY_ID = "STAGE1B_MANUAL_TRIGGER_LIVE_PAPER"
DEFAULT_STRATEGY_VERSION = "v1-manual-trigger"

# Reused verbatim from `.355`'s own `run_stage1a_dry_run` default_supervision
# (see that function's source) -- not new numbers invented here.
AUTH_AUTHORIZATION_TTL_SECONDS = 60
AUTH_SAFETY_STATE_TTL_SECONDS = 60


class Stage1BManualTriggerCliError(Exception):
    """Programmer-error / missing-required-input / fail-closed-safety
    violations only -- mirrors `.356`'s own `Stage3CliError` discipline.
    Never raised for an ordinary handled outcome (a symbol that fails to
    fetch or size, a `.44` BLOCK, an ABSTAIN decision are all recorded in
    the resulting report, not raised)."""


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


def load_equity_cli_module():
    """`.356` -- source of every credential/client/evidence/sizing helper
    this module reuses (see module docstring). `.356` itself is not
    modified anywhere in this file."""
    return _load_module("aura_v05356_stage3_live_equity_cli", "aura_v05356_stage3_live_equity_cli.py")


def load_stage1_runner_module():
    return _load_module("aura_v05355_stage1_paper_trading_runner", "aura_v05355_stage1_paper_trading_runner.py")


def load_equity_history_log_module():
    """`.364` -- the small append-only real-equity log this module uses
    to give `.44`'s daily_loss check a genuine same-day-prior snapshot
    (see module docstring, "Daily-loss history")."""
    return _load_module("aura_v05364_equity_history_log", "aura_v05364_equity_history_log.py")


def load_decision_journal_module():
    """`.361` -- Extension, 2026-10-01 ("lets go for #4"): source of
    `DEFAULT_JOURNAL_PATH`, used here as this module's own default when a
    caller doesn't supply `decision_journal_path`. `.55.run_stage1b_paper_
    cycle()` itself does the actual journaling (via `.361.record_raw_
    decision()`), one row per `AuditRecord`; this module just decides
    which path."""
    return _load_module("aura_v05361_portfolio_enforcement_journal", "aura_v05361_portfolio_enforcement_journal.py")


def load_full_universe_scan_module():
    """`.369` -- Extension, 2026-09-30 ("full universe expansion"): source
    of `refresh_full_universe_if_stale`/`build_symbol_requests_from_full_universe`,
    consumed only when `--scan-full-universe` is supplied."""
    return _load_module("aura_v05369_full_universe_scan", "aura_v05369_full_universe_scan.py")


def load_earnings_calendar_module():
    """`.367` -- Extension, 2026-09-29 (Martin, "Lets go for Earnings
    blackout"). Optional: only loaded/used when the caller supplies both
    `earnings_calendar_api_key` and `earnings_state_dir`."""
    return _load_module("aura_v05367_earnings_calendar_ingestion", "aura_v05367_earnings_calendar_ingestion.py")


def _now_iso(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()


# ============================================================================
# The explicit safety-gate opener. Only ever called after `confirmed` has
# been checked by the caller -- see `run_manual_trigger_stage1b_cycle`.
# ============================================================================


def build_live_paper_submission_supervision() -> dict[str, Any]:
    """The ONLY place in this repo that opens BOTH `.36`'s auth gates and
    `.38`'s supervisor kill switch for a call that can reach a real
    (paper) broker. Reuses `.355`'s own already-established TTL values
    (see module docstring) -- does not invent new ones.

    Deliberately omits `auth_claims_dir`/`supervisor_claims_dir`: leaving
    them out lets `.36`/`.38` fall back to their own real, PRODUCTION
    default directories (`regime_output/common_execution_supervisor/...`),
    not a test-isolated `tmp_path` -- so this run's duplicate-order-
    protection claims persist durably on disk exactly like any other real
    caller's would, and accumulate across manual-trigger runs (correct:
    that persistence IS the duplicate-submission guard)."""
    return {
        "supervisor_config": {"kill_switch": False},
        "auth_config": {
            "kill_switch": False,
            "execution_authorized": True,
            "paper_execution_authorized": True,
            "authorization_ttl_seconds": AUTH_AUTHORIZATION_TTL_SECONDS,
            "safety_state_ttl_seconds": AUTH_SAFETY_STATE_TTL_SECONDS,
        },
    }


# ============================================================================
# Cycle assembly -- reuses `.356`'s evidence/sizing primitives, then calls
# `.55`'s EXISTING `run_stage1b_paper_cycle()`. This is the only place in
# this module `run_stage1b_paper_cycle` is called.
# ============================================================================


def run_manual_trigger_stage1b_cycle(
    symbol_requests: tuple[Any, ...],
    *,
    confirmed: bool,
    bars_client: Any,
    alpaca_client: Any,
    max_new_orders_per_cycle: int,
    lookback_bars: int,
    universe_version: str,
    max_snapshot_age_seconds: float,
    fill_poll_timeout_seconds: float,
    fill_poll_interval_seconds: float,
    news_client: Any | None = None,
    news_state_dir: Path | None = None,
    news_fetch_limit: int = 50,
    strategy_id: str = DEFAULT_STRATEGY_ID,
    strategy_version: str = DEFAULT_STRATEGY_VERSION,
    equity_history_log_path: Path | None = None,
    now: datetime | None = None,
    symbol_source: str = "requests_config",
    limits: Any | None = None,
    earnings_calendar_api_key: str | None = None,
    earnings_state_dir: Path | None = None,
    full_universe_scan_status: dict[str, Any] | None = None,
    decision_journal_path: Path | None = None,
    institutional_config: Any | None = None,
) -> dict[str, Any]:
    """Mirrors `.356.run_live_dry_run_cycle`'s evidence-building and ATR
    sizing exactly (same helper functions, same sequencing), then calls
    `.55.run_stage1b_paper_cycle()` instead of `run_stage1a_dry_run()`.

    `confirmed` is required and keyword-only, with NO default -- a caller
    (CLI or direct import) that does not explicitly pass `confirmed=True`
    gets a fail-closed `Stage1BManualTriggerCliError` before any client
    is touched or any evidence is fetched, let alone before the broker is
    reached.

    Extension -- 2026-09-29 (Martin, AskUserQuestion): `limits` is a new,
    OPTIONAL keyword-only parameter, threaded straight through to `.55`'s
    `run_stage1b_paper_cycle(limits=...)` unchanged. This module's own CLI
    (`main()`, below) still never sets it -- a bare `.363` manual-trigger
    run is unaffected and keeps `.44`'s dimensions unconfigured exactly as
    before (`portfolio_limits_source` reflects whichever was actually
    used). This parameter exists so a caller that DOES want real portfolio-
    exposure limits enforced (first user: `.365`'s continuous live-trading
    loop) can supply a `.344.PortfolioLimits` instance without this module
    reimplementing anything -- `.55` already accepts and correctly applies
    `limits`, this module just was not exposing that existing capability
    until now.

    Extension -- 2026-09-29 (Martin, "Lets go for Earnings blackout"):
    `earnings_calendar_api_key`/`earnings_state_dir` are new, OPTIONAL
    keyword-only parameters, mirroring `news_client`/`news_state_dir`
    exactly. When both are supplied, `.67`'s earnings calendar is
    refreshed-if-stale (scoped to this cycle's usable symbols) and the
    resulting `.67.EarningsCalendarState` is threaded through to `.55`'s
    `run_stage1b_paper_cycle(earnings_calendar_state=...)`, which ANDs
    `.68`'s day-of blackout check with the real `.44` enforcement check
    this function already builds. Leaving either unset (the default)
    reproduces this function's original behavior exactly: no earnings
    check at all.

    Extension -- 2026-09-30 (Martin, "full universe expansion"):
    `full_universe_scan_status` is a new, OPTIONAL keyword-only parameter
    -- purely a pass-through dict (see `.369.FullUniverseScanState.to_dict()`)
    for inclusion in this function's own output, unlike
    `earnings_calendar_api_key`/`earnings_state_dir` above which this
    function itself refreshes. The full-universe scan determines
    `symbol_requests` itself (same role as `--scan-pinned-universe`), so
    it is resolved by the CALLER (this module's own `main()`, or `.365`)
    before this function ever runs, not inside it; this parameter exists
    only so that scan's status (fresh vs. cached vs. unavailable, and the
    resulting symbol count) is visible in this function's output for the
    same auditability every other evidence source already gets here.
    `None` (the default) means no full-universe scan was used this cycle
    -- reproduces this function's original behavior exactly.

    Extension -- 2026-10-01 (Martin, "lets go for #4", AURA_Lablab_
    Hackathon_Official_Winners_Audit_2026-10-01.md ranked finding #4):
    `decision_journal_path` is a new, OPTIONAL keyword-only parameter.
    UNLIKE every other optional parameter above, this one is NOT None-
    means-off -- it defaults to `.361.DEFAULT_JOURNAL_PATH` when omitted,
    the SAME "a real broker-reaching caller journals unconditionally,
    without needing an opt-in flag" convention `equity_history_log_path`/
    `.364` already established one parameter above. Every symbol this
    cycle produces an `AuditRecord` for -- a metadata-fetch failure, an
    ABSTAIN/NOT_SHORTLISTED/NO_TRADE_DECIDED signal, a `.44` BLOCK, a
    `.38` BLOCK, or an actual submission -- is durably journaled via
    `.55.run_stage1b_paper_cycle(journal_path=...)`. To disable journaling
    entirely for a given call, a caller would need to call `.55` directly
    with `journal_path=None` -- this module's own CLI (`main()`, below)
    never does that; a bare `.363` manual-trigger run always journals.

    Extension -- 2026-10-09 (Martin, "PRODUCTION MANDATE -- OPTION 1
    ENFORCEMENT & ENTRY-POINT WIRING"): `institutional_config` is a new,
    OPTIONAL keyword-only parameter, threaded straight through to `.55`'s
    `run_stage1b_paper_cycle(institutional_config=...)` unchanged. `None`
    (the default) reproduces this function's original behavior exactly --
    no institutional Track B/portfolio/Greeks gating at all. This
    module's own CLI (`main()`, below) only ever supplies a non-`None`
    value when `--enable-institutional-gates` is passed, and even then
    builds it with `track_b_borrow_feed`/`track_b_short_interest_feed`
    explicitly `None` (Track B short-protective gates stay INERT -- no
    real vendor feed exists yet, see `.55`'s own
    `_compose_with_institutional_gates`); the real, validated portfolio/
    Greeks/macro-bucket enforcement is what actually activates."""
    if not confirmed:
        raise Stage1BManualTriggerCliError(
            "SUBMISSION_NOT_CONFIRMED:this cycle will not run without explicit confirmation "
            "(CLI: --i-confirm-this-submits-real-paper-orders; direct call: confirmed=True)"
        )

    now_dt = now or datetime.now(timezone.utc)

    equity_cli = load_equity_cli_module()
    stage1_module = load_stage1_runner_module()
    technical_module = equity_cli.load_technical_module()
    short_technical_module = equity_cli.load_short_technical_module()
    signal_source_module = equity_cli.load_signal_source_module()
    observability_module = equity_cli.load_observability_module()
    orchestrator_module = equity_cli.load_orchestrator_module()
    atr_module = equity_cli.load_atr_module()
    exit_engine_module = equity_cli.load_exit_engine_module()
    position_sizing_module = equity_cli.load_position_sizing_module()

    # -- Real .51/.52 evidence. Extension -- 2026-09-30 ("full universe
    #    expansion", batched bars fetches, Martin): a single batched fetch
    #    covering every requested symbol (chunked internally by .351's
    #    fetch_recent_bars_batch), not one HTTP call per symbol -- see
    #    .356's fetch_symbol_evidence_batch. This replaces the previous
    #    one-call-per-symbol loop for EVERY symbol source (pinned-universe,
    #    requests-config, and full-universe alike), not just the new one:
    #    strictly fewer network calls for the same result, so there was no
    #    reason to keep two code paths. Reused for wave/sector-rotation/ATR
    #    exactly as before (see .356's fetch_symbol_evidence_batch). --
    evidence_by_symbol: dict[str, Any] = equity_cli.fetch_symbol_evidence_batch(
        [r.symbol for r in symbol_requests], bars_client=bars_client, technical_module=technical_module,
        short_technical_module=short_technical_module,
        frozen_technical_params=signal_source_module.FROZEN_TECHNICAL_PARAMS,
        frozen_short_technical_params=signal_source_module.FROZEN_SHORT_TECHNICAL_PARAMS,
        lookback_bars=lookback_bars, universe_version=universe_version, now=now_dt,
        orchestrator_module=orchestrator_module, atr_module=atr_module,
    )

    usable_requests = [r for r in symbol_requests if evidence_by_symbol[r.symbol].fetch_error is None]
    fetch_failures = [
        {"symbol": r.symbol, "error": evidence_by_symbol[r.symbol].fetch_error}
        for r in symbol_requests if evidence_by_symbol[r.symbol].fetch_error is not None
    ]

    # -- Live evidence (sentiment + wave + sector-rotation), same as .356.
    news_ingestion_status: dict[str, Any] | None = None
    live_evidence_by_symbol: dict[str, Any] = {}
    if news_client is not None and news_state_dir is not None and usable_requests:
        news_events, news_ingestion_status = orchestrator_module.fetch_universe_news_events(
            news_client, news_state_dir,
            symbols=tuple(r.symbol for r in usable_requests),
            start=now_dt - timedelta(hours=orchestrator_module.SENTIMENT_WAVE_PARAMS["decay_window_hours"]),
            end=now_dt, limit=news_fetch_limit, now=now_dt,
        )
        bars_as_dicts_by_symbol = {
            r.symbol: list(evidence_by_symbol[r.symbol].bars_as_dicts) for r in usable_requests
        }
        live_evidence_by_symbol = orchestrator_module.build_live_evidence_for_universe(
            bars_as_dicts_by_symbol, news_events, now=now_dt,
        )

    # -- Earnings calendar (Extension, 2026-09-29) -- same "only when both
    #    a client/key and a state dir are supplied" gating as the news
    #    block above. Scoped to this cycle's usable symbols; `.67` itself
    #    re-derives the filtered calendar from a universe-agnostic cache,
    #    so this is correct even across universe changes (see `.67`'s
    #    module docstring). --
    earnings_calendar_state = None
    if earnings_calendar_api_key is not None and earnings_state_dir is not None and usable_requests:
        earnings_module = load_earnings_calendar_module()
        earnings_calendar_state = earnings_module.refresh_earnings_calendar_if_stale(
            api_key=earnings_calendar_api_key, state_dir=earnings_state_dir,
            universe_symbols=tuple(r.symbol for r in usable_requests), now=now_dt,
        )

    decide_kwargs = dict(
        orchestrator_module.LIVE_EVIDENCE_DECIDE_KWARGS,
        proposal_module=_load_module("aura_v05349_ai_proposal_pipeline", "aura_v05349_ai_proposal_pipeline.py"),
        llm_client=signal_source_module.NeutralDeterministicLLMClient(),
    )

    # -- Real account equity, once, for ATR-risk sizing (see module
    #    docstring -- .55.run_stage1b_paper_cycle separately builds its
    #    OWN real snapshot for .44 enforcement; that is unchanged here). --
    account_equity_usd = equity_cli.fetch_real_account_equity_usd(alpaca_client, observability_module=observability_module)

    # -- Daily-loss history (see module docstring, "Daily-loss history").
    #    Logged unconditionally whenever a real equity value was fetched,
    #    regardless of what happens afterward (mirrors .361's "log every
    #    cycle" principle) -- never gated on the eventual decision. --
    equity_history_module = load_equity_history_log_module()
    log_path = equity_history_log_path or equity_history_module.DEFAULT_EQUITY_HISTORY_LOG_PATH

    # -- Decision journal (Extension, 2026-10-01, "lets go for #4") --
    #    resolved here (not inside .55) so this function's own output can
    #    report which path was actually used, mirroring equity_history_
    #    log_path's identical resolve-then-report pattern immediately
    #    above. --
    journal_module = load_decision_journal_module()
    journal_path = decision_journal_path or journal_module.DEFAULT_JOURNAL_PATH
    equity_history: list[dict[str, Any]] = []
    if account_equity_usd is not None:
        equity_history = equity_history_module.record_and_read_equity_history(
            log_path, venue="ALPACA", equity=account_equity_usd, as_of=now_dt, now=now_dt,
        )

    reference_price_fn = equity_cli.build_reference_price_fn(evidence_by_symbol)

    resolved_quantity_by_symbol: dict[str, Any] = {}
    sizing_failures: list[dict[str, Any]] = []
    sizeable_requests = []
    for r in usable_requests:
        quantity, sizing_error = equity_cli.resolve_quantity_for_symbol(
            r, evidence_by_symbol[r.symbol], account_equity_usd=account_equity_usd,
            exit_engine_module=exit_engine_module, position_sizing_module=position_sizing_module,
        )
        if sizing_error is not None:
            sizing_failures.append({"symbol": r.symbol, "error": sizing_error})
            continue
        resolved_quantity_by_symbol[r.symbol] = quantity
        sizeable_requests.append(r)

    stage1_symbol_requests = tuple(
        stage1_module.SymbolRequest(
            symbol=r.symbol, asset_class=r.asset_class, quantity=resolved_quantity_by_symbol[r.symbol],
            technical_regime=evidence_by_symbol[r.symbol].technical_regime,
            short_technical_regime=evidence_by_symbol[r.symbol].short_technical_regime,
            sentiment_regime=getattr(live_evidence_by_symbol.get(r.symbol), "sentiment_regime", None),
            wave_result=getattr(live_evidence_by_symbol.get(r.symbol), "wave_result", None),
        )
        for r in sizeable_requests
    )

    # -- The one and only place this module (or, per its own module
    #    docstring, this repo's live-evidence pipeline) can reach the
    #    broker: real submission gates opened explicitly, only because
    #    `confirmed` was checked True above. --
    supervision_kwargs = build_live_paper_submission_supervision()

    report = None
    if stage1_symbol_requests:
        report = stage1_module.run_stage1b_paper_cycle(
            stage1_symbol_requests,
            alpaca_client=alpaca_client,
            decide_kwargs=decide_kwargs,
            max_new_orders_per_cycle=max_new_orders_per_cycle,
            equity_history=equity_history,
            max_snapshot_age_seconds=max_snapshot_age_seconds,
            reference_price_fn=reference_price_fn,
            supervision_kwargs=supervision_kwargs,
            fill_poll_timeout_seconds=fill_poll_timeout_seconds,
            fill_poll_interval_seconds=fill_poll_interval_seconds,
            limits=limits,  # None (default) -> .355 defaults to enforcement_module.PortfolioLimits() = unconfigured,
                            # exactly as before 2026-09-29. A caller (e.g. .365) may now supply real limits here.
            strategy_id=strategy_id, strategy_version=strategy_version,
            now=now_dt,
            earnings_calendar_state=earnings_calendar_state,
            journal_path=journal_path,
            institutional_config=institutional_config,
        )

    return {
        "engine": ENGINE,
        "version": VERSION,
        "observed_at": _now_iso(now_dt),
        "confirmed_real_submission": True,
        "symbol_source": symbol_source,
        "universe_version": universe_version,
        "lookback_bars": lookback_bars,
        "decide_kwargs_source": "aura_v05362_live_evidence_orchestrator.LIVE_EVIDENCE_DECIDE_KWARGS",
        "news_ingestion_status": news_ingestion_status,
        "earnings_calendar_status": earnings_calendar_state.to_dict() if earnings_calendar_state is not None else None,
        "full_universe_scan_status": full_universe_scan_status,
        "sector_rotation_by_symbol": {
            symbol: (ev.sector_rotation_regime.to_dict() if ev.sector_rotation_regime is not None else None)
            for symbol, ev in live_evidence_by_symbol.items()
        },
        "live_evidence_build_errors": {
            symbol: ev.build_error for symbol, ev in live_evidence_by_symbol.items() if ev.build_error is not None
        },
        "account_equity_usd_source": "REAL_ALPACA_GET_ACCOUNT" if account_equity_usd is not None else "UNAVAILABLE_VENUE_NOT_SUCCESS",
        "account_equity_usd": account_equity_usd,
        "symbol_fetch_failures": fetch_failures,
        "position_sizing_note": (
            "quantity is auto-sized via .054_position_sizing (0.5% equity risk) for any symbol whose "
            "requests-config entry omits it; an explicit quantity always overrides. See sizing_failures "
            "for symbols skipped this cycle because they could not be sized."
        ),
        "sizing_failures": sizing_failures,
        "portfolio_limits_source": (
            "enforcement_module.PortfolioLimits() (defaults, left unconfigured -- Martin, 2026-09-25)"
            if limits is None else "caller-supplied .344.PortfolioLimits (see limits_applied below) -- Extension 2026-09-29"
        ),
        "limits_applied": limits.to_dict() if limits is not None else None,
        "institutional_gates_enabled": institutional_config is not None,
        "institutional_gates_note": (
            "institutional_config was supplied -- .44 is composed with the real portfolio/Greeks/"
            "macro-bucket check and, if track_b_borrow_feed/track_b_short_interest_feed were also "
            "non-None, the Track B short-protective checks too (see .55's _compose_with_institutional_gates)."
            if institutional_config is not None else
            "institutional_config was not supplied (--enable-institutional-gates was not passed) -- "
            "no institutional Track B/portfolio/Greeks gating this cycle, unchanged from pre-2026-10-09 behavior."
        ),
        "equity_history_log_path": str(log_path),
        "equity_history_observation_count": len(equity_history),
        "equity_history_note": (
            "This run's own real equity was appended to the log above (when available) before this cycle's "
            "enforcement check ran, then the full same-day history was passed to .44's daily_loss check -- see "
            ".364's module docstring. equity_history_observation_count==0 means no real equity was fetched this "
            "run (see account_equity_usd_source); daily_loss will BLOCK in that case (INSUFFICIENT_HISTORY)."
        ),
        "decision_journal_path": str(journal_path),
        "decision_journal_entries_written": len(report.audit_records) if report is not None else 0,
        "decision_journal_note": (
            "Extension, 2026-10-01: every AuditRecord .55 built this cycle (metadata-fetch failures, ABSTAIN/"
            "NOT_SHORTLISTED/NO_TRADE_DECIDED signals, .44 BLOCKs, .38 BLOCKs, and actual submissions alike) was "
            "appended to the path above via .361.record_raw_decision() -- see .361's module docstring, Extension "
            "2026-10-01. decision_journal_entries_written==0 here means no symbol reached .55 at all this cycle "
            "(every symbol failed evidence fetch or sizing first -- see symbol_fetch_failures/sizing_failures), "
            "not that nothing happened."
        ),
        "stage1_report": equity_cli._stage1_report_to_dict(report, stage1_module) if report is not None else None,
    }


# ============================================================================
# CLI
# ============================================================================


def main(argv: list[str] | None = None) -> int:  # pragma: no cover -- live wiring, exercised via unit tests on its pieces, not this shell
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--requests-config", required=False, default=None, type=Path,
                         help="JSON file: list of {symbol, asset_class, quantity}. No default universe. "
                              "For a first run, Martin's own plan is 1-2 symbols here. Exactly one of "
                              "--requests-config or --scan-pinned-universe is required.")
    parser.add_argument("--scan-pinned-universe", action="store_true",
                         help="Extension -- 2026-09-29, wide scan (Martin, AskUserQuestion): instead of a "
                              "hand-written requests-config file, consider every symbol in .51's existing "
                              "pinned universe (aura_v05351_equity_universe_v1.json, 39 symbols as of the "
                              "2026-09-29 ETF curation pass) as this "
                              "cycle's candidate pool -- quantity always auto-sized. --max-new-orders-per-cycle "
                              "still caps how many of them can actually result in a real order, via .53's "
                              "existing ranking (unchanged). Exactly one of --requests-config, "
                              "--scan-pinned-universe, or --scan-full-universe is required.")
    parser.add_argument("--scan-full-universe", action="store_true",
                         help="Extension -- 2026-09-30, full universe expansion (Martin, AskUserQuestion): "
                              "instead of the pinned 39-symbol universe, consider EVERY tradable US-equity "
                              "symbol Alpaca lists (via .369, tradable=True only -- no price/volume/exchange "
                              "filter, Martin's explicit confirmed choice) as this cycle's candidate pool. "
                              "Bars are fetched batched (.351.fetch_recent_bars_batch), not one call per "
                              "symbol. Exactly one of --requests-config, --scan-pinned-universe, or "
                              "--scan-full-universe is required.")
    parser.add_argument("--full-universe-state-dir", type=Path, default=None,
                         help="Directory for .369's persisted tradable-asset-list cache. Required when "
                              "--scan-full-universe is supplied; ignored otherwise.")
    parser.add_argument("--max-new-orders-per-cycle", required=True, type=int,
                         help="Martin's own plan for the first run is 1. Not restricted further by this CLI "
                              "(see module docstring -- scope is controlled via --requests-config, not a "
                              "code-level cap).")
    parser.add_argument("--max-snapshot-age-seconds", required=True, type=float)
    parser.add_argument("--fill-poll-timeout-seconds", required=True, type=float,
                         help="Required, no default -- .55/.54's own no-invented-default discipline.")
    parser.add_argument("--fill-poll-interval-seconds", required=True, type=float,
                         help="Required, no default -- .55/.54's own no-invented-default discipline.")
    parser.add_argument("--lookback-bars", type=int, default=None,
                         help="Defaults to FROZEN_TECHNICAL_PARAMS.min_bars_required (55) if omitted.")
    parser.add_argument("--universe-version", type=str, default=None,
                         help="Defaults to aura_v054_signal_source.UNIVERSE_VERSION if omitted.")
    parser.add_argument("--news-state-dir", type=Path, default=None,
                         help="Directory for .46's persisted news ledger. Omit to skip news/live-evidence "
                              "(sentiment_regime=None/wave_result=None for every symbol).")
    parser.add_argument("--news-fetch-limit", type=int, default=50)
    parser.add_argument("--earnings-state-dir", type=Path, default=None,
                         help="Directory for .67's persisted earnings-calendar cache. Omit to skip the earnings "
                              "blackout gate entirely (no new-entry block on earnings day for any symbol). "
                              "Requires FMP_API_KEY in the environment (see .env.example) -- a free key from "
                              "https://site.financialmodelingprep.com/.")
    parser.add_argument("--strategy-id", type=str, default=DEFAULT_STRATEGY_ID)
    parser.add_argument("--strategy-version", type=str, default=DEFAULT_STRATEGY_VERSION)
    parser.add_argument("--equity-history-log-path", type=Path, default=None,
                         help="Defaults to .364's own DEFAULT_EQUITY_HISTORY_LOG_PATH "
                              "(regime_output/equity_history_log/alpaca_equity_history.jsonl) if omitted. "
                              "This run's real equity is appended here (see module docstring, 'Daily-loss "
                              "history') so .44's daily_loss check has a genuine same-day-prior snapshot.")
    parser.add_argument("--decision-journal-path", type=Path, default=None,
                         help="Defaults to .361's own DEFAULT_JOURNAL_PATH "
                              "(regime_output/decision_journal/stage1b_decisions.jsonl) if omitted. Every symbol "
                              "this cycle produces an AuditRecord for -- ABSTAIN, a .44/.38 BLOCK, or an actual "
                              "submission -- is appended here unconditionally (Extension, 2026-10-01).")
    parser.add_argument("--i-confirm-this-submits-real-paper-orders", dest="confirmed", action="store_true",
                         required=True,
                         help="REQUIRED. This run can submit a real order to your Alpaca PAPER account. There "
                              "is no way to pass this flag as false -- omit it to refuse to run at all.")
    parser.add_argument("--enable-institutional-gates", action="store_true",
                         help="Extension -- 2026-10-09 (Martin, 'PRODUCTION MANDATE -- OPTION 1 ENFORCEMENT & "
                              "ENTRY-POINT WIRING'). Activates the real, calibrated .344/.344-portfolio Greeks/"
                              "macro-bucket enforcement (SAME_DIRECTION_STACKING_HARD_THRESHOLD=0.20, "
                              "PROPOSED_GREEKS_LIMITS_CONFIG) via .55's InstitutionalGatesConfig. Track B's "
                              "short-protective gates (borrow-fee / squeeze-crowding / gap-tail-risk) stay "
                              "explicitly INERT -- no real borrow-fee or short-interest vendor feed exists in "
                              "this codebase yet (see institutional/track_b/borrow_data_feeds.py's module "
                              "docstring). Fails closed before any client is built or any order is routed to "
                              "the broker if the institutional gate modules cannot be loaded (e.g. AURA_CORE_DIR "
                              "unreadable or missing a required core module file). Omit to reproduce this "
                              "module's pre-2026-10-09 behavior exactly (no institutional gating at all).")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)

    # --- INSTITUTIONAL GATES preflight -- fail closed BEFORE any client is
    # built or any order is routed to the broker (Martin's explicit
    # directive). Only runs at all when --enable-institutional-gates was
    # passed; a bare run is completely unaffected. ---
    institutional_portfolio_module = None
    institutional_macro_buckets_module = None
    institutional_greeks_limits_module = None
    if args.enable_institutional_gates:
        try:
            stage1_module_preflight = load_stage1_runner_module()  # also os.environ.setdefault("AURA_CORE_DIR", ...)
            institutional_portfolio_module = stage1_module_preflight.load_institutional_portfolio_module()
            stage1_module_preflight.load_institutional_track_b_module()  # import-only check; not used while inert
            institutional_macro_buckets_module = _load_module("macro_buckets", "institutional/portfolio/macro_buckets.py")
            institutional_greeks_limits_module = _load_module("greeks_limits", "institutional/portfolio/greeks_limits.py")
        except Exception as exc:  # noqa: BLE001 -- fail-closed at the CLI boundary, never a raw traceback, before any broker contact
            print(
                f"FAIL-CLOSED: INSTITUTIONAL_GATES_PREFLIGHT_FAILED:{type(exc).__name__}:{exc} -- "
                "--enable-institutional-gates was passed but the institutional gate modules could not be "
                "loaded (commonly: AURA_CORE_DIR unreadable, or missing a required core module file). "
                "Aborting before any client was built or any order was routed to the broker.",
                file=sys.stderr,
            )
            return 1
        print(
            "[AURA INSTITUTIONAL CORE] Portfolio & Greeks gates ENABLED (Threshold: 0.20). "
            "Equities Short-Side gates INERT (Missing live vendor feeds)."
        )

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

    try:
        equity_cli = load_equity_cli_module()
        api_key, secret_key = equity_cli.load_equity_paper_credentials()
        technical_module = equity_cli.load_technical_module()
        signal_source_module = equity_cli.load_signal_source_module()

        bars_client = equity_cli.build_bars_client(api_key, secret_key, technical_module)
        alpaca_client = equity_cli.build_trading_client(api_key, secret_key)
        news_client = equity_cli.build_news_client(api_key, secret_key) if args.news_state_dir is not None else None

        earnings_calendar_api_key = None
        if args.earnings_state_dir is not None:
            earnings_module = load_earnings_calendar_module()
            earnings_calendar_api_key = earnings_module.load_fmp_api_key()

        full_universe_scan_status = None
        if args.scan_full_universe:
            full_universe_module = load_full_universe_scan_module()
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

        # --- INSTITUTIONAL GATES config -- only built when the preflight
        # above succeeded (args.enable_institutional_gates True and every
        # institutional module loaded cleanly). track_b_borrow_feed/
        # track_b_short_interest_feed are explicitly None: no real vendor
        # feed exists yet (see .55's _compose_with_institutional_gates,
        # which skips Track B's short-protective checks whenever either is
        # None and prints its own explicit warning -- this is intentional,
        # not an oversight). track_b_bars_provider is still wired to a
        # REAL Alpaca daily-bars provider (not a stub) so the field is
        # genuinely populated and ready for the day a real borrow-fee/
        # short-interest feed is plugged in -- it is simply never invoked
        # while Track B stays inert. --
        institutional_config = None
        if args.enable_institutional_gates:
            bars_provider_module = _load_module("aura_v054_alpaca_bars_provider", "aura_v054_alpaca_bars_provider.py")
            institutional_bars_provider = bars_provider_module.AlpacaEquityBarsProvider(api_key=api_key, secret_key=secret_key)
            institutional_bars_provider.prefetch(tuple(r.symbol for r in symbol_requests))
            institutional_config = stage1_module_preflight.InstitutionalGatesConfig(
                track_b_borrow_feed=None,
                track_b_short_interest_feed=None,
                track_b_bars_provider=institutional_bars_provider.get_daily_bars,
                track_b_config=None,
                macro_config=institutional_macro_buckets_module.PROPOSED_MACRO_BUCKET_CONFIG,
                greeks_config=institutional_greeks_limits_module.PROPOSED_GREEKS_LIMITS_CONFIG,
            )
            _ = institutional_portfolio_module  # loaded during preflight to prove it imports cleanly; .55 loads its own copy internally

        result = run_manual_trigger_stage1b_cycle(
            symbol_requests, confirmed=args.confirmed, bars_client=bars_client, alpaca_client=alpaca_client,
            max_new_orders_per_cycle=args.max_new_orders_per_cycle, lookback_bars=lookback_bars,
            universe_version=universe_version, max_snapshot_age_seconds=args.max_snapshot_age_seconds,
            fill_poll_timeout_seconds=args.fill_poll_timeout_seconds,
            fill_poll_interval_seconds=args.fill_poll_interval_seconds,
            news_client=news_client, news_state_dir=args.news_state_dir, news_fetch_limit=args.news_fetch_limit,
            strategy_id=args.strategy_id, strategy_version=args.strategy_version,
            equity_history_log_path=args.equity_history_log_path,
            symbol_source=symbol_source,
            earnings_calendar_api_key=earnings_calendar_api_key, earnings_state_dir=args.earnings_state_dir,
            full_universe_scan_status=full_universe_scan_status,
            decision_journal_path=args.decision_journal_path,
            institutional_config=institutional_config,
        )
    except Stage1BManualTriggerCliError as exc:
        print(f"FAIL-CLOSED: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 -- covers .67's own
        # EarningsCalendarIngestionError (e.g. a missing FMP_API_KEY),
        # raised before this module's own try block would otherwise catch
        # it via Stage1BManualTriggerCliError -- fail-closed at the CLI
        # boundary, never a raw traceback, matching this module's own
        # discipline for every other startup-time misconfiguration.
        if type(exc).__name__ == "EarningsCalendarIngestionError":
            print(f"FAIL-CLOSED: {exc}", file=sys.stderr)
            return 1
        raise

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, default=str) + "\n", encoding="utf-8")
    submitted_count = (result.get("stage1_report") or {}).get("submitted_count", 0)
    print(f"Wrote {args.output} (submitted_count={submitted_count})")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
