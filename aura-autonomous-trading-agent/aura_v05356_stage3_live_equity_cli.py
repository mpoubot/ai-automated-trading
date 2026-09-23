#!/usr/bin/env python3
"""
AURA v0.5.3.56 -- Track B Stage 3A: live-wiring CLI (real Alpaca equity/ETF
market data -> `.51`/`.52` real signal -> existing `.53`/`.55` orchestration
-> real `.44` risk enforcement -> `.38` order-construction PREVIEW).

WHAT THIS MODULE IS, AND IS DELIBERATELY NOT
------------------------------------------------------------------------
This is wiring, not new logic -- per the Stage 3 entry audit (see
`AURA_v0.5.3.56_Stage3A_Implementation_Report_*.md`), `.53.run_cycle()` and
`.55`'s Stage 1A/1B runners are already built, already tested, and already
correctly connected to real `.44` enforcement, `.38`/`.36`/`.35`/`.34`, and
`.54`'s fill reconciliation. Nothing here duplicates any of that. This
module's only genuinely new contribution is the layer ABOVE all of it that
was missing: constructing real Alpaca clients from real credentials,
fetching real `.51`/`.52` technical evidence for a caller-supplied symbol
list, translating that evidence into `.53`'s `SymbolCycleInput` shape, and
calling `.55`'s EXISTING `run_stage1a_dry_run()` -- never `run_stage1b_
paper_cycle()`, never `.53.run_cycle(attempt_submission=True, ...)`.

STRUCTURAL SAFETY, NOT A CONVENTION
------------------------------------------------------------------------
There is no flag, argument, or environment variable anywhere in this
module that can cause an order to be submitted. This module never
imports or calls `run_stage1b_paper_cycle`, never constructs a
`.53.run_cycle(attempt_submission=True, ...)` call, and `run_stage1a_dry_
run` itself hardcodes `attempt_submission=False` (not caller-overridable
-- see `.55`'s own source). Running this CLI with any combination of
arguments can retrieve real market data, fetch a real account snapshot
(read-only), and build a complete order-construction PREVIEW, but cannot
reach `.35.submit()`.

Credentials
------------------------------------------------------------------------
Reads exactly two environment variables, resolving the ambiguity flagged
in `.env.example` (Martin's explicit decision, Stage 3 kickoff): a
DEDICATED pair, distinct from the crypto account's `ALPACA_PAPER_API_KEY`/
`SECRET_KEY` pair used elsewhere in this repo --

    ALPACA_EQUITY_PAPER_API_KEY
    ALPACA_EQUITY_PAPER_SECRET_KEY

Both are required; a missing or empty value for either fails closed with
a clear `Stage3CliError` before any client is constructed. No real
credential value is ever logged, written to an output file, or included
in any exception message this module raises.

`.51`/`.52` parameter source -- the "critical parameter rule"
------------------------------------------------------------------------
This module does NOT invent a `TechnicalScoringParams`/`ShortTechnicalScoringParams`/
decision-weight set. It reuses, verbatim, the ALREADY-FROZEN, ALREADY-
APPROVED values in `aura_v054_signal_source.py`
(`FROZEN_TECHNICAL_PARAMS`, `FROZEN_SHORT_TECHNICAL_PARAMS`,
`FROZEN_DECIDE_KWARGS`), which are themselves traced to
`AURA_v0.53_Frozen_Candidate_Freeze_Record_2026-09-15.md` and the
project's own approved test fixtures -- a defensible, documented source,
not a default invented to make this CLI run.

*** FLAGGED, NOT SILENTLY WORKED AROUND: *** `FROZEN_DECIDE_KWARGS` sets
`technical_weight=0.0` and `short_technical_weight=0.0` (already
documented in `aura_v054_signal_source.py`'s own module docstring as
proven, by `tests/test_aura_v054_signal_source.py`, to make `.51`/`.52`
mathematically inert in `.50`'s score). Running this CLI today, even
against real live market data, will therefore make `.50.decide()` return
`ABSTAIN` for every symbol, every cycle -- by construction, not a bug in
this wiring. This CLI still exists to prove the PLUMBING (real data in,
real regimes computed, real risk enforcement exercised, a complete audit
trail produced) -- proving `.51`/`.52` actually drive a trade decision
requires Martin's separate, explicit decision to reweight
`technical_weight`/`short_technical_weight`, which this module does not
make on its own.

Reference price for `.44` enforcement
------------------------------------------------------------------------
`.44`'s notional/exposure math needs a per-symbol reference price.
Rather than inventing a number or making a second live-quote call, this
module reuses the LAST REAL CLOSE from the same bars it already fetched
for `.51`/`.52` this cycle (`fetch_recent_bars` is called once per
symbol; `build_technical_regime_from_bars`/`build_short_technical_regime_
from_bars` -- the network-free cores `fetch_live_technical_regime`/
`fetch_live_short_technical_regime` wrap -- are then called directly on
that same DataFrame, so no symbol is fetched twice). This is a
documented design choice, not a silent assumption.

Account equity for `.44` enforcement
------------------------------------------------------------------------
`run_stage1a_dry_run()` has no parameter for a real, broker-derived
portfolio snapshot -- only `limits=None` (skips `.44` entirely) or a
caller-disclosed `synthetic_account_equity_usd` (a hand-built,
zero-live-position, single-ALPACA-venue snapshot). To genuinely exercise
`.44` against this account's real equity (not skip it, and not fabricate
a number), this module fetches the REAL account equity via a read-only
`alpaca_client.get_account()` call (through `.43`'s own
`fetch_alpaca_portfolio`) and passes that REAL, freshly-fetched figure
through `synthetic_account_equity_usd`. The parameter's name is
misleading for this use case -- flagged here explicitly rather than
glossed over -- but the number itself is real, the fetch is read-only,
and `.55`'s own code treats it identically to any other equity source
once it reaches `.44`. `--skip-account-equity-fetch` opts out (falls
back to `limits=None`, `.44` not exercised at all) for a caller who wants
zero broker contact whatsoever, including account reads.

Scope
------------------------------------------------------------------------
Equity/ETF only, Alpaca only. Track A/MEXC is not imported, referenced,
or touched anywhere in this module.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

VERSION = "AURA v0.5.3.56"
ENGINE = "STAGE3A_LIVE_EQUITY_CLI"

ROOT = Path(__file__).resolve().parent

EQUITY_API_KEY_ENV = "ALPACA_EQUITY_PAPER_API_KEY"
EQUITY_SECRET_KEY_ENV = "ALPACA_EQUITY_PAPER_SECRET_KEY"

REQUIRED_ASSET_CLASSES = frozenset({"STOCK", "ETF"})


class Stage3CliError(Exception):
    """Programmer-error / missing-required-input / fail-closed-safety
    violations only -- mirrors every other milestone's own error-class
    discipline in this repo. Never raised for an ordinary handled outcome
    (a symbol that fails to fetch, an ABSTAIN decision, a `.44` BLOCK are
    all recorded in the resulting report, not raised)."""


# ============================================================================
# Dynamic import of `.34`/`.35`/`.43`/`.44`/`.51`/`.52`/`.53`/`.54`/`.55` --
# same `_load_module` convention every module in this chain already uses.
# None of them is modified; each is used strictly through its existing
# public functions.
# ============================================================================


def _load_module(module_name: str, filename: str):
    try:
        return __import__(module_name)
    except ImportError:
        import importlib.util

        spec = importlib.util.spec_from_file_location(module_name, ROOT / filename)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return mod


def load_technical_module():
    return _load_module("aura_v05351_live_alpaca_equity_signal_source", "aura_v05351_live_alpaca_equity_signal_source.py")


def load_short_technical_module():
    return _load_module("aura_v05352_stock_etf_short_side_signal", "aura_v05352_stock_etf_short_side_signal.py")


def load_signal_source_module():
    """`.54`'s module -- source of the FROZEN_* parameter constants this
    CLI reuses (see module docstring, "critical parameter rule")."""
    return _load_module("aura_v054_signal_source", "aura_v054_signal_source.py")


def load_stage1_runner_module():
    return _load_module("aura_v05355_stage1_paper_trading_runner", "aura_v05355_stage1_paper_trading_runner.py")


def load_cycle_module():
    return _load_module("aura_v05353_full_paper_orchestration", "aura_v05353_full_paper_orchestration.py")


def load_observability_module():
    return _load_module("aura_v05343_portfolio_exposure_observability", "aura_v05343_portfolio_exposure_observability.py")


def load_enforcement_module():
    return _load_module("aura_v05344_portfolio_exposure_enforcement", "aura_v05344_portfolio_exposure_enforcement.py")


def _now_iso(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()


# ============================================================================
# Credentials and client construction. No credential value is ever
# returned in a way that gets logged/printed/written by this module --
# callers hold the strings only long enough to construct a client.
# ============================================================================


def load_equity_paper_credentials() -> tuple[str, str]:
    """Reads ONLY `ALPACA_EQUITY_PAPER_API_KEY`/`ALPACA_EQUITY_PAPER_SECRET_KEY`
    -- never falls back to the crypto account's `ALPACA_PAPER_API_KEY`/
    `SECRET_KEY` pair, per Martin's explicit Stage 3 decision. Fails
    closed (raises) if either is missing or empty; never proceeds with a
    partial credential pair."""
    api_key = os.getenv(EQUITY_API_KEY_ENV)
    secret_key = os.getenv(EQUITY_SECRET_KEY_ENV)
    missing = [name for name, val in ((EQUITY_API_KEY_ENV, api_key), (EQUITY_SECRET_KEY_ENV, secret_key)) if not val]
    if missing:
        raise Stage3CliError(
            "MISSING_ALPACA_EQUITY_PAPER_CREDENTIALS:"
            f"{missing} not set (or empty) in the environment -- set both "
            f"{EQUITY_API_KEY_ENV} and {EQUITY_SECRET_KEY_ENV} (see .env.example). "
            "Refusing to proceed with a partial or missing credential pair; "
            "this module never falls back to the crypto account's ALPACA_PAPER_* pair."
        )
    return api_key, secret_key  # type: ignore[return-value]


def build_trading_client(api_key: str, secret_key: str) -> Any:
    """Real `alpaca-py TradingClient(paper=True)` -- read-only calls only
    are ever made against it from this module (`get_account`,
    `get_all_positions` via `.43`'s `fetch_alpaca_portfolio`). Never used
    to submit an order anywhere in this file."""
    from alpaca.trading.client import TradingClient

    return TradingClient(api_key, secret_key, paper=True)


def build_bars_client(api_key: str, secret_key: str, technical_module: Any) -> Any:
    """`.51`'s own `AlpacaHistoricalBarsClient` wrapper -- read-only
    market-data calls only."""
    return technical_module.AlpacaHistoricalBarsClient(api_key, secret_key)


# ============================================================================
# Symbol request input -- explicit, caller-supplied, no invented universe.
# ============================================================================


@dataclass(frozen=True, slots=True)
class LiveSymbolRequest:
    symbol: str
    asset_class: str  # "STOCK" or "ETF"
    quantity: Any

    def __post_init__(self) -> None:
        if self.asset_class not in REQUIRED_ASSET_CLASSES:
            raise Stage3CliError(f"INVALID_ASSET_CLASS:{self.symbol}:{self.asset_class}")


def load_symbol_requests(path: Path) -> tuple[LiveSymbolRequest, ...]:
    """Reads a required, caller-supplied JSON file: a list of
    `{"symbol": "...", "asset_class": "STOCK"|"ETF", "quantity": <number>}`
    objects -- mirroring the MEXC Track A precedent
    (`scripts/run_execution_spec_for_latest_cycle.py`'s `--sizing-config`):
    no default symbol universe or quantity is ever invented here; an
    absent or empty file fails closed."""
    if not path.exists():
        raise Stage3CliError(f"SYMBOL_REQUESTS_FILE_NOT_FOUND:{path}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list) or not raw:
        raise Stage3CliError(f"SYMBOL_REQUESTS_FILE_EMPTY_OR_INVALID:{path}")
    requests = []
    for i, entry in enumerate(raw):
        for field in ("symbol", "asset_class", "quantity"):
            if field not in entry:
                raise Stage3CliError(f"SYMBOL_REQUESTS_ENTRY_MISSING_FIELD:index={i}:field={field}")
        requests.append(LiveSymbolRequest(symbol=entry["symbol"], asset_class=entry["asset_class"], quantity=entry["quantity"]))
    symbols_seen = [r.symbol for r in requests]
    if len(set(symbols_seen)) != len(symbols_seen):
        raise Stage3CliError("DUPLICATE_SYMBOL_IN_SYMBOL_REQUESTS_FILE")
    return tuple(requests)


# ============================================================================
# Real evidence fetch -- one real bars call per symbol, reused for both
# `.51` and `.52` (never fetched twice), plus the last real close kept
# for the reference-price function (see module docstring).
# ============================================================================


@dataclass(frozen=True, slots=True)
class SymbolEvidence:
    symbol: str
    technical_regime: Any  # `.51` TechnicalRegime
    short_technical_regime: Any  # `.52` ShortTechnicalRegime
    last_close: float | None
    fetch_error: str | None = None


def fetch_symbol_evidence(
    symbol: str,
    *,
    bars_client: Any,
    technical_module: Any,
    short_technical_module: Any,
    frozen_technical_params: Any,
    frozen_short_technical_params: Any,
    lookback_bars: int,
    universe_version: str,
    now: datetime,
) -> SymbolEvidence:
    """Never raises for an ordinary fetch failure (network hiccup, symbol
    with no data, etc.) -- returns a `SymbolEvidence` with `fetch_error`
    set instead, exactly mirroring `.55`'s own `fetch_real_alpaca_asset`
    "never raises, caller checks status" discipline. A programmer error
    (invalid params) still raises."""
    try:
        bars_df = technical_module.fetch_recent_bars(symbol, client=bars_client, lookback_bars=lookback_bars, end=now)
    except technical_module.LiveSignalSourceError as exc:
        return SymbolEvidence(symbol=symbol, technical_regime=None, short_technical_regime=None, last_close=None, fetch_error=f"{type(exc).__name__}: {exc}")
    except Exception as exc:  # noqa: BLE001 -- real network/broker call, classified and reported, never swallowed
        return SymbolEvidence(symbol=symbol, technical_regime=None, short_technical_regime=None, last_close=None, fetch_error=f"{type(exc).__name__}: {exc}")

    technical_regime = technical_module.build_technical_regime_from_bars(
        symbol, bars_df, params=frozen_technical_params, universe_version=universe_version, now=now,
    )
    short_regime = short_technical_module.build_short_technical_regime_from_bars(
        symbol, bars_df, params=frozen_short_technical_params, universe_version=universe_version, now=now,
    )
    last_close = float(bars_df["close"].iloc[-1]) if len(bars_df) else None
    return SymbolEvidence(symbol=symbol, technical_regime=technical_regime, short_technical_regime=short_regime, last_close=last_close)


def build_reference_price_fn(evidence_by_symbol: dict[str, SymbolEvidence]) -> Callable[[str], float]:
    """See module docstring, "Reference price for `.44` enforcement" --
    the last REAL close already fetched this cycle, never a second live
    call and never an invented number."""

    def _reference_price_fn(symbol: str) -> float:
        evidence = evidence_by_symbol.get(symbol)
        if evidence is None or evidence.last_close is None:
            raise Stage3CliError(f"NO_REFERENCE_PRICE_AVAILABLE:{symbol}")
        return evidence.last_close

    return _reference_price_fn


# ============================================================================
# Real (read-only) account equity fetch -- see module docstring, "Account
# equity for `.44` enforcement".
# ============================================================================


def fetch_real_account_equity_usd(alpaca_client: Any, *, observability_module: Any) -> float | None:
    """Read-only. Returns None (not 0.0 -- a real, deliberate "unknown",
    never fabricated) if `.43`'s own portfolio fetch reports the ALPACA
    venue as anything other than SUCCESS."""
    snapshot = observability_module.build_portfolio_snapshot(alpaca_client=alpaca_client)
    alpaca_status = snapshot.venue_fetch_status.get("ALPACA")
    if alpaca_status is not None and alpaca_status.status == "SUCCESS":
        return alpaca_status.equity
    return None


# ============================================================================
# Cycle assembly -- translates real evidence into `.55`'s `SymbolRequest`
# shape and calls the EXISTING `run_stage1a_dry_run()`. This is the only
# place `.55` is called from this module, and it is always this function,
# never `run_stage1b_paper_cycle`.
# ============================================================================


def run_live_dry_run_cycle(
    symbol_requests: tuple[LiveSymbolRequest, ...],
    *,
    bars_client: Any,
    alpaca_client: Any | None,
    max_new_orders_per_cycle: int,
    lookback_bars: int,
    universe_version: str,
    max_snapshot_age_seconds: float,
    skip_account_equity_fetch: bool,
    now: datetime | None = None,
) -> dict[str, Any]:
    now_dt = now or datetime.now(timezone.utc)

    technical_module = load_technical_module()
    short_technical_module = load_short_technical_module()
    signal_source_module = load_signal_source_module()
    stage1_module = load_stage1_runner_module()
    observability_module = load_observability_module()
    enforcement_module = load_enforcement_module()

    evidence_by_symbol: dict[str, SymbolEvidence] = {}
    for r in symbol_requests:
        evidence_by_symbol[r.symbol] = fetch_symbol_evidence(
            r.symbol, bars_client=bars_client, technical_module=technical_module,
            short_technical_module=short_technical_module,
            frozen_technical_params=signal_source_module.FROZEN_TECHNICAL_PARAMS,
            frozen_short_technical_params=signal_source_module.FROZEN_SHORT_TECHNICAL_PARAMS,
            lookback_bars=lookback_bars, universe_version=universe_version, now=now_dt,
        )

    usable_requests = [r for r in symbol_requests if evidence_by_symbol[r.symbol].fetch_error is None]
    fetch_failures = [
        {"symbol": r.symbol, "error": evidence_by_symbol[r.symbol].fetch_error}
        for r in symbol_requests if evidence_by_symbol[r.symbol].fetch_error is not None
    ]

    decide_kwargs = dict(
        signal_source_module.FROZEN_DECIDE_KWARGS,
        proposal_module=_load_module("aura_v05349_ai_proposal_pipeline", "aura_v05349_ai_proposal_pipeline.py"),
        llm_client=signal_source_module.NeutralDeterministicLLMClient(),
    )

    account_equity_usd: float | None = None
    if not skip_account_equity_fetch and alpaca_client is not None:
        account_equity_usd = fetch_real_account_equity_usd(alpaca_client, observability_module=observability_module)

    limits = enforcement_module.PortfolioLimits()
    reference_price_fn = build_reference_price_fn(evidence_by_symbol)

    stage1_symbol_requests = tuple(
        stage1_module.SymbolRequest(
            symbol=r.symbol, asset_class=r.asset_class, quantity=r.quantity,
            technical_regime=evidence_by_symbol[r.symbol].technical_regime,
            short_technical_regime=evidence_by_symbol[r.symbol].short_technical_regime,
        )
        for r in usable_requests
    )

    report = None
    if stage1_symbol_requests:
        report = stage1_module.run_stage1a_dry_run(
            stage1_symbol_requests,
            decide_kwargs=decide_kwargs,
            max_new_orders_per_cycle=max_new_orders_per_cycle,
            reference_price_fn=reference_price_fn,
            limits=limits,
            max_snapshot_age_seconds=max_snapshot_age_seconds,
            equity_history=[],
            now=now_dt,
            synthetic_account_equity_usd=account_equity_usd,
        )

    return {
        "engine": ENGINE,
        "version": VERSION,
        "observed_at": _now_iso(now_dt),
        "universe_version": universe_version,
        "lookback_bars": lookback_bars,
        "technical_scoring_params_source": "aura_v054_signal_source.FROZEN_TECHNICAL_PARAMS",
        "short_technical_scoring_params_source": "aura_v054_signal_source.FROZEN_SHORT_TECHNICAL_PARAMS",
        "decide_kwargs_source": "aura_v054_signal_source.FROZEN_DECIDE_KWARGS",
        "technical_weight_is_zero_warning": (
            "FROZEN_DECIDE_KWARGS.technical_weight == 0.0 and short_technical_weight == 0.0 -- "
            "every decision this cycle produces is mathematically forced to ABSTAIN regardless of "
            "the real .51/.52 evidence fetched; see module docstring."
        ),
        "account_equity_usd_source": (
            "SKIPPED_BY_FLAG" if skip_account_equity_fetch else
            ("REAL_ALPACA_GET_ACCOUNT" if account_equity_usd is not None else "UNAVAILABLE_VENUE_NOT_SUCCESS")
        ),
        "account_equity_usd": account_equity_usd,
        "symbol_fetch_failures": fetch_failures,
        "stage1_report": _stage1_report_to_dict(report, stage1_module) if report is not None else None,
    }


def _stage1_report_to_dict(report: Any, stage1_module: Any) -> dict[str, Any]:
    cr = report.cycle_result
    return {
        "stage": report.stage,
        "submitted_count": cr.submitted_count if cr is not None else 0,
        "outcomes": [
            {
                "symbol": o.symbol, "stage": o.stage,
                "decision_outcome": getattr(o.decision, "outcome", None),
                "decision_final_rank_score": getattr(o.decision, "final_rank_score", None),
                "reasons": list(o.reasons),
            }
            for o in (cr.outcomes if cr is not None else ())
        ],
        "audit_records": [
            {
                "symbol": rec.symbol, "cycle_stage": rec.cycle_stage,
                "signal_outcome": rec.signal_outcome, "risk_decision_status": rec.risk_decision_status,
                "risk_decision_blocked_reasons": list(rec.risk_decision_blocked_reasons),
            }
            for rec in report.audit_records
        ],
    }


# ============================================================================
# CLI
# ============================================================================


def main(argv: list[str] | None = None) -> int:  # pragma: no cover -- live wiring, exercised via unit tests on its pieces, not this shell
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--requests-config", required=True, type=Path,
                         help="JSON file: list of {symbol, asset_class, quantity}. No default universe.")
    parser.add_argument("--max-new-orders-per-cycle", required=True, type=int)
    parser.add_argument("--max-snapshot-age-seconds", required=True, type=float,
                         help="Required, no default -- .44's own freshness-check discipline.")
    parser.add_argument("--lookback-bars", type=int, default=None,
                         help="Defaults to FROZEN_TECHNICAL_PARAMS.min_bars_required (55) if omitted -- "
                              "derived from the already-approved frozen params, not invented; overridable.")
    parser.add_argument("--universe-version", type=str, default=None,
                         help="Defaults to aura_v054_signal_source.UNIVERSE_VERSION (the already-frozen value) if omitted.")
    parser.add_argument("--skip-account-equity-fetch", action="store_true",
                         help="Skip even the read-only get_account() call; .44 enforcement then runs with no "
                              "equity source (fail-closed BLOCK on EXPOSURE_NOT_COMPUTABLE), not disabled.")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)

    try:
        api_key, secret_key = load_equity_paper_credentials()
        technical_module = load_technical_module()
        signal_source_module = load_signal_source_module()

        bars_client = build_bars_client(api_key, secret_key, technical_module)
        alpaca_client = None if args.skip_account_equity_fetch else build_trading_client(api_key, secret_key)

        symbol_requests = load_symbol_requests(args.requests_config)
        lookback_bars = args.lookback_bars or signal_source_module.FROZEN_TECHNICAL_PARAMS.min_bars_required
        universe_version = args.universe_version or signal_source_module.UNIVERSE_VERSION

        result = run_live_dry_run_cycle(
            symbol_requests, bars_client=bars_client, alpaca_client=alpaca_client,
            max_new_orders_per_cycle=args.max_new_orders_per_cycle, lookback_bars=lookback_bars,
            universe_version=universe_version, max_snapshot_age_seconds=args.max_snapshot_age_seconds,
            skip_account_equity_fetch=args.skip_account_equity_fetch,
        )
    except Stage3CliError as exc:
        print(f"FAIL-CLOSED: {exc}", file=sys.stderr)
        return 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, default=str) + "\n", encoding="utf-8")
    print(f"Wrote {args.output} (submitted_count="
          f"{(result.get('stage1_report') or {}).get('submitted_count', 0)}, always 0 -- see module docstring)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
